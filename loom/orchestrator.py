"""
The project orchestrator: takes an idea through the six phases of loom/phases.py in order,
running each phase's agent, with an approval checkpoint after each one where the founder
approves, edits or rejects its document before the next phase starts.

ProjectState is the state machine. It's saved in the project's shared memory, the SQLite
database in .loom/memory/project.db (see loom/memory.py), so a project survives
restarting loom, and tracks which phase the project is in. Each phase is:

  pending ──start──▶ running ──finish──▶ review ──approve──▶ approved
     ▲                  │                   │
     └──────stop────────┘                   │
     └──────────────reject (feedback)───────┘

Only the current phase (the first one not approved) can change, except with back, which
sends the project back to an earlier phase and resets the phases after it to pending
(their documents stay on disk for the agents to revise). The project is complete when
every phase is approved.

Each run of a phase's agent is logged in the phase's run_log: its time, cost, tokens and
commits, and how it ended. The checkpoints' outcomes, and the decisions the phase agents
record, go in the same database. Each agent's task message lists them, and the agents can
search them and the earlier documents (indexed in a vector store) with the recall tool.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from loom import tools as agent_tools
from loom.coders import phase_prompts
from loom.editor import pipe_editor
from loom.memory import (
    CONTEXT_KINDS,
    DB_FILE,
    FOUNDER,
    ProjectDB,
    ProjectMemory,
    ProjectMemoryError,
    chroma_installed,
)
from loom.phases import (
    PHASES,
    PHASES_BY_KEY,
    get_phase,
    next_phase,
    read_test_command,
    read_verdict,
)
from loom.repo import ANY_GIT_ERROR
from loom.tools import count_changes, describe_changes, plural
from loom.utils import format_tokens

STATE_FILE = DB_FILE
# Where projects were saved before the shared memory; loom moves them into it
LEGACY_STATE_FILE = ".loom/project.json"
FORMAT_VERSION = 1

STATUSES = ("pending", "running", "review", "approved")

# event: (statuses it can happen in, the status it leads to)
TRANSITIONS = {
    "start": (("pending", "running"), "running"),
    "finish": (("running",), "review"),
    "approve": (("review",), "approved"),
    "reject": (("review",), "pending"),
    "stop": (("running",), "pending"),
}

# How often a failing test report goes back to the Building agent without the user
MAX_FIX_ROUNDS = 3
# Longer documents aren't put in the agents' messages; they read them with read_file
MAX_INLINE_CHARS = 40_000
# The most loom reads of a phase document or the project file
MAX_DOC_BYTES = 1_000_000
# How many of the latest decisions an agent's task message lists
MAX_DECISIONS = 40
# How many passages of earlier documents the orchestrator looks up for an agent
MAX_RECALLED = 3

# Who the template's decisions and its checks' results come from
TEMPLATE_SOURCE = "template"

# The defaults of the options that change how projects run
DEFAULT_SETTINGS = dict(build_retries=3, build_budget=None, build_workers=3, allow_deploy=False)
# A work plan may have this many packages per builder
PACKAGES_PER_WORKER = 3
MAX_WORKERS = 16

# What each entry of a phase's run_log adds up, for its metrics
METRIC_FIELDS = ("seconds", "cost", "tokens_sent", "tokens_received", "commits")

# How long the project's test command may run, and how much of its output loom keeps: the
# end, where test runners sum up the failures
TEST_TIMEOUT = agent_tools.MAX_BASH_TIMEOUT
TEST_OUTPUT_CHARS = 6000
# Lines of the test output shown to the user
TEST_PREVIEW_LINES = 8


def now():
    return datetime.now().isoformat(timespec="seconds")


def format_cost(cost):
    """A cost in dollars, with enough digits to show a small one."""
    if not cost:
        return "$0.00"
    if cost >= 0.01:
        return f"${cost:.2f}"
    return f"${cost:.4f}"


def format_duration(seconds):
    """A duration like 45s, 3m 05s or 1h 02m."""
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def describe_metrics(metrics):
    """A phase's metrics, or the project's, like "2 runs, 3m 05s, $0.04"."""
    return (
        f"{plural(metrics['runs'], 'run')}, {format_duration(metrics['seconds'])},"
        f" {format_cost(metrics['cost'])}"
    )


def keep_end(text, limit):
    """The end of text, at most about limit characters of it, from the start of a line."""
    if len(text) <= limit:
        return text
    end = text[-limit:]
    newline = end.find("\n")
    if 0 <= newline < limit // 10:
        end = end[newline + 1 :]
    return f"[... {len(text) - len(end):,} earlier characters cut ...]\n{end}"


def describe_checks(phase, results):
    """The outcome of a phase's template checks, as a decision for the agents to come."""
    parts = []
    for result in results:
        if result["passed"]:
            parts.append(f"`{result['command']}` passed")
        else:
            status = result["output"].split("\n", 1)[0].rstrip(".").lower()
            parts.append(f"`{result['command']}` failed ({status})")
    return f"The template's checks after {phase.title}: " + "; ".join(parts)


def usage(coder):
    """What a coder has spent so far: its cost and tokens."""
    return dict(
        cost=coder.total_cost,
        tokens_sent=coder.total_tokens_sent,
        tokens_received=coder.total_tokens_received,
    )


def in_project(root, path):
    """Whether path is a regular file inside root after following symlinks: a repo could
    link a phase document to ~/.aws/credentials, or to /dev/zero."""
    root = Path(root).resolve()
    real = Path(path).resolve()
    return (real == root or root in real.parents) and real.is_file()


def read_project_file(root, path, limit=MAX_DOC_BYTES):
    """The text of a file in the project, at most limit bytes of it, or None when it's
    missing or isn't a regular file inside root. Line endings are \n, as on Windows the
    agents write \r\n."""
    if not in_project(root, path):
        return None
    with open(Path(path).resolve(), "rb") as f:
        text = f.read(limit).decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


class TransitionError(Exception):
    pass


class ProjectState:
    def __init__(self, db, data):
        # The ProjectDB it's saved in
        self.db = db
        self.data = data

    @property
    def path(self):
        return self.db.path

    @classmethod
    def new(cls, root, idea, db=None, template=None, tdd=False):
        """A new project for idea, from template (a Template) if given, test-driven if
        tdd."""
        data = dict(
            version=FORMAT_VERSION,
            idea=idea.strip(),
            created=now(),
            phases={phase.key: dict(status="pending", runs=0) for phase in PHASES},
            fix_rounds=0,
            history=[],
        )
        if template:
            data["template"] = dict(
                name=template.name, source=template.source, hash=template.hash()
            )
        if tdd or (template and template.tdd):
            data["tdd"] = True
        state = cls(db or ProjectDB(root), data)
        state.log(None, "new", idea.strip().split("\n", 1)[0])
        return state

    @classmethod
    def load(cls, root, db=None):
        """The project in root, or None if it has none."""
        db = db or ProjectDB(root)
        try:
            data = db.load_state()
        except ProjectMemoryError as err:
            raise TransitionError(f"Unable to read the project: {err}")
        if data is None:
            return cls.load_legacy(root, db)
        if data.get("version") != FORMAT_VERSION:
            raise TransitionError(f"{db.path} has a project this version of loom can't read")
        for phase in PHASES:
            data["phases"].setdefault(phase.key, dict(status="pending", runs=0))
        return cls(db, data)

    @classmethod
    def load_legacy(cls, root, db):
        """Move a project from .loom/project.json, where loom used to keep it, into the
        database. None if there's none."""
        path = Path(root) / LEGACY_STATE_FILE
        if not path.exists() and not path.is_symlink():
            return None
        try:
            text = read_project_file(root, path)
            if text is None:
                raise ValueError("it isn't a file in the project")
            data = json.loads(text)
        except (OSError, ValueError) as err:
            raise TransitionError(f"Unable to read {path}: {err}")
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            raise TransitionError(f"{path} isn't a loom project file this version can read")
        for phase in PHASES:
            data["phases"].setdefault(phase.key, dict(status="pending", runs=0))
        state = cls(db, data)
        state.save()
        path.rename(path.with_name(path.name + ".migrated"))
        return state

    def save(self):
        try:
            self.db.save_state(self.data, {phase.key: phase.number for phase in PHASES})
        except ProjectMemoryError as err:
            raise TransitionError(f"Unable to save the project: {err}")

    @property
    def idea(self):
        return self.data["idea"]

    @property
    def history(self):
        return self.data["history"]

    @property
    def tdd(self):
        """Whether Building is test-driven."""
        return bool(self.data.get("tdd"))

    @property
    def template_info(self):
        """The project's template, {name, source, hash}, or None."""
        return self.data.get("template")

    def phase_data(self, key):
        return self.data["phases"][key]

    def status(self, key):
        return self.phase_data(key)["status"]

    @property
    def current(self):
        """The phase the project is in: the first one not approved, or None when the
        project is complete."""
        for phase in PHASES:
            if self.status(phase.key) != "approved":
                return phase
        return None

    @property
    def complete(self):
        return self.current is None

    def log(self, key, event, note=""):
        entry = dict(time=now(), event=event)
        if key:
            entry["phase"] = key
        if note:
            entry["note"] = note
        self.history.append(entry)

    def transition(self, key, event, note=""):
        sources, target = TRANSITIONS[event]
        current = self.current
        if not current or current.key != key:
            where = f"in {current.title}" if current else "complete"
            raise TransitionError(
                f"Can't {event} {PHASES_BY_KEY[key].title}: the project is {where}."
            )
        status = self.status(key)
        if status not in sources:
            raise TransitionError(f"Can't {event} {PHASES_BY_KEY[key].title} while it is {status}.")
        self.phase_data(key)["status"] = target
        self.log(key, event, note)
        return self.phase_data(key)

    def start(self, key):
        data = self.transition(key, "start")
        data["runs"] = data.get("runs", 0) + 1
        data["started"] = now()

    def finish(self, key, verdict=None):
        data = self.transition(key, "finish", verdict or "")
        data["verdict"] = verdict
        data["finished"] = now()

    def approve(self, key):
        data = self.transition(key, "approve")
        data["approved"] = now()
        for name in ("feedback", "attach", "stale", "redo"):
            data.pop(name, None)

    def reject(self, key, feedback=""):
        data = self.transition(key, "reject", feedback)
        data["feedback"] = feedback
        data["redo"] = True

    def stop(self, key, reason=""):
        self.transition(key, "stop", reason)

    def run_log(self, key):
        """A record of each run of the phase's agent: when, how long, what it cost, the
        commits it made and how it ended. Projects from before loom kept it have none."""
        return self.phase_data(key).get("run_log") or []

    def metrics(self, key):
        """The totals of a phase's runs: runs, seconds, cost, tokens sent and received,
        and commits."""
        runs = self.run_log(key)
        total = dict(runs=len(runs))
        for field in METRIC_FIELDS:
            total[field] = sum(run.get(field) or 0 for run in runs)
        total["cost"] = round(total["cost"], 6)
        return total

    def fix_rounds(self, key):
        """How often a failing test report sent the project back to this phase."""
        return sum(
            1
            for entry in self.history
            if entry.get("event") == "back"
            and entry.get("phase") == key
            and entry.get("note") == phase_prompts.fix_test_failures
        )

    def totals(self):
        """The metrics of the whole project, every phase's added up."""
        total = dict(runs=0, **{field: 0 for field in METRIC_FIELDS})
        for phase in PHASES:
            for field, value in self.metrics(phase.key).items():
                total[field] += value
        total["cost"] = round(total["cost"], 6)
        return total

    def back(self, key, feedback="", attach=None):
        """Go back to phase key to redo it, with feedback and the documents of the phases in
        attach, like a test report for the Building agent. The phases after it go back to
        pending."""
        target = PHASES_BY_KEY[key]
        current = self.current
        if current and current.number < target.number:
            raise TransitionError(
                f"Can't go back to {target.title}: the project is still in {current.title}."
            )
        for phase in PHASES[target.number - 1 :]:
            data = self.phase_data(phase.key)
            if phase is not target and data["status"] != "pending":
                data["stale"] = True
            data["status"] = "pending"
            data.pop("verdict", None)
            data.pop("feedback", None)
            data.pop("attach", None)
            if phase is not target:
                # Parallel Building starts over, from a scaffold of the new design
                data.pop("packages", None)
                data.pop("parallel", None)
            spec = data.get("spec")
            if phase is not target and spec and spec.get("status") != "pending":
                # Test-driven Building's acceptance tests are written again, from the
                # earlier phases' new documents
                spec.update(status="pending", stale=True)
                for name in ("locked", "commit", "first_run"):
                    spec.pop(name, None)
        data = self.phase_data(key)
        data["feedback"] = feedback
        data["redo"] = True
        data.pop("stale", None)
        if attach:
            data["attach"] = list(attach)
        self.log(key, "back", feedback)


class Orchestrator:
    """Runs the phases' agents in order for the project in coder's root."""

    def __init__(self, coder, state=None):
        self.coder = coder
        self.io = coder.io
        self.root = Path(coder.root)
        self.memory = ProjectMemory(self.root, self.io)
        self.state = state if state is not None else ProjectState.load(self.root, self.memory.db)
        # Phases whose document links outside the project, which loom has warned about
        self.outside_warned = set()
        # The project's template, once loaded (False when it has none or it's gone)
        self.loaded_template = None
        # Settings from loom's options, like --build-retries
        self.settings = dict(DEFAULT_SETTINGS, **(getattr(coder, "project_settings", None) or {}))
        # For what parallel builders change at once, like the coder's costs
        self.lock = threading.RLock()

    # Starting and running

    def new_project(self, idea, template=None, tdd=False):
        """Start a project for idea, from template (a Template) if given, test-driven if
        tdd."""
        try:
            # A new idea starts with an empty memory
            self.memory.clear()
        except ProjectMemoryError as err:
            raise TransitionError(f"Unable to start the project: {err}")
        self.state = ProjectState.new(self.root, idea, self.memory.db, template, tdd)
        self.state.save()
        self.loaded_template = template or False
        self.memory.index_text("idea", "idea", None, "idea", self.state.idea, "The project idea")
        if template:
            for text in template.decisions:
                self.decide(None, "decision", text, source=TEMPLATE_SOURCE)

    # The project's template (loom/project_templates.py)

    @property
    def template(self):
        """The project's Template, or None. Says so once if it's gone or has changed since
        the project started."""
        if self.loaded_template is None:
            self.loaded_template = self.load_template() or False
        return self.loaded_template or None

    def load_template(self):
        from loom.project_templates import TemplateError, load_template

        info = self.state.template_info if self.state else None
        if not info:
            return None
        try:
            template = load_template(self.root, info["name"])
        except TemplateError as err:
            self.io.tool_warning(f"{err} The project carries on without its template.")
            return None
        digest = template.hash()
        if digest != info.get("hash"):
            self.io.tool_warning(
                f"The {template.name} template has changed since the project started; loom"
                " uses it as it is now."
            )
            info["hash"] = digest
            self.save_quietly()
        return template

    def run(self):
        """Run the phases from the current one, stopping at each one's checkpoint for the
        founder, until the project is complete, the founder stops or a phase fails. Returns
        whether the project is complete."""
        if not self.can_run():
            return False
        try:
            while not self.state.complete:
                phase = self.state.current
                if self.state.status(phase.key) != "review" and not self.run_phase(phase):
                    return False
                if not self.review(phase):
                    return False
        except KeyboardInterrupt:
            phase = self.state.current
            if phase and self.state.status(phase.key) == "running":
                self.state.stop(phase.key, "interrupted")
                self.state.save()
            self.io.tool_warning("Stopped. Continue with /project run.")
            return False

        self.io.rule()
        self.io.tool_output("Project complete: all six phases are approved.", bold=True)
        self.show_documents()
        self.io.tool_output(
            "Ship it with /project ship (Fly.io), and --pr or --release for GitHub; write it up"
            " with /project report."
        )
        return True

    def can_run(self):
        if not self.state:
            self.io.tool_error("There is no project. Start one with /project new IDEA.")
            return False
        if self.coder.permissions.mode == "plan":
            self.io.tool_error(
                "Phase agents write documents and code, which plan mode refuses. Switch with"
                " /permissions ask (or accept-edits) first."
            )
            return False
        if not self.coder.main_model.info.get("supports_function_calling"):
            self.io.tool_warning(
                f"{self.coder.main_model.name} may not support tool calling, which phase agents"
                " need."
            )
        return True

    def run_phase(self, phase):
        """Run phase's agent until it has written its document. Returns whether it did."""
        state = self.state
        state.start(phase.key)
        state.save()

        self.io.rule()
        self.io.tool_output(
            (
                f"Phase {phase.number}/{len(PHASES)}: {phase.title}. The {phase.agent} is writing"
                f" {phase.document}."
            ),
            bold=True,
        )
        if phase.key == "building":
            self.apply_skeleton()
            from loom.parallel import ParallelBuilding

            parallel = ParallelBuilding(self)
            if parallel.ready():
                return parallel.run(phase)
            if state.tdd:
                from loom.tdd import TestDrivenBuilding

                building = TestDrivenBuilding(self)
                if building.ready():
                    return building.run(phase)

        agent = self.make_agent(phase)
        meter = self.start_run(agent)
        self.guarded(phase, agent, meter, lambda: self.do_task(agent, phase))
        return self.end_run(phase, agent, meter)

    def do_task(self, agent, phase, message=None):
        """Have agent do phase's task (or message), then write its document if it didn't."""
        agent.run(with_message=message or self.task_message(phase), preproc=False)
        self.nudge_for_document(agent, phase)

    def nudge_for_document(self, agent, phase):
        """Remind agent once to write phase's document, if it finished without it."""
        if agent.failed or self.stopped(agent) or self.document_text(phase, agent.root):
            return
        agent.io.tool_warning(f"The {phase.agent} didn't write {phase.document}.")
        agent.run(
            with_message=phase_prompts.missing_document.format(document=phase.document),
            preproc=False,
        )

    def guarded(self, phase, agent, meter, work, extra=None):
        """Run work(), which runs agent. Whatever happens, add what agent spent to the
        coder's totals, and if work raises, log the run in phase's run_log (with extra) and
        save it before the error goes on up."""
        # How the run ended, if the agent raised
        raised = "failed"
        try:
            work()
            raised = None
        except KeyboardInterrupt:
            raised = "stopped"
            raise
        finally:
            self.collect(agent, meter)
            if raised:
                self.log_run(phase, agent, meter, raised, extra=extra)
                self.save_quietly()

    def end_run(self, phase, agent, meter, extra=None):
        """After agent's run of phase: stop the phase if the agent failed, was stopped or
        didn't write its document, or finish it for review and run the template's checks.
        Either way the run is logged, with extra. Returns whether the phase finished."""
        state = self.state
        reason = None
        outcome = "failed"
        if agent.failed:
            reason = agent.failed
        elif self.stopped(agent):
            reason = "stopped by the user"
            outcome = "stopped"
        elif not self.document_text(phase):
            reason = f"the agent didn't write {phase.document}"
        if reason:
            self.log_run(phase, agent, meter, outcome, extra=extra)
            state.stop(phase.key, reason)
            state.save()
            self.io.tool_error(f"{phase.title} stopped: {reason}. Continue with /project run.")
            return False

        verdict = read_verdict(phase, self.document_text(phase))
        self.log_run(phase, agent, meter, "done", verdict, extra=extra)
        state.finish(phase.key, verdict)
        state.save()
        self.remember_document(phase)
        self.run_checks(phase)
        return True

    def stopped(self, agent):
        return agent.stop_requested or agent.interrupted

    def make_agent(self, phase, locked=None):
        """A new agent for phase. locked are paths of tests it may not change."""
        from loom.coders import Coder
        from loom.coders.phase_coder import PhaseCoder
        from loom.sessions import Session

        # A fresh conversation: everything the agent needs is in its task message, and in
        # the project memory
        return Coder.create(
            from_coder=self.coder,
            coder_class=PhaseCoder,
            edit_format="agent",
            summarize_from_coder=False,
            phase=phase,
            shared_memory=self.memory,
            template=self.template,
            locked=locked,
            fnames=[],
            read_only_fnames=[],
            done_messages=[],
            cur_messages=[],
            session=Session(),
        )

    def collect(self, agent, meter):
        """Add what the agent spent since meter started, and its commits, to the coder that
        started the project. Parallel builders do it at once, so one at a time."""
        with self.lock:
            for field, value in self.spent(agent, meter).items():
                name = "total_cost" if field == "cost" else f"total_{field}"
                setattr(self.coder, name, getattr(self.coder, name) + value)
            self.coder.loom_commit_hashes.update(agent.loom_commit_hashes)

    # Metrics of each run

    def start_run(self, agent):
        """Start measuring a run of agent: the time, what it has spent and the commit HEAD
        is at, where it works."""
        return dict(
            started=now(), clock=time.monotonic(), usage=usage(agent), base=self.head(agent)
        )

    def spent(self, agent, meter):
        """What agent has spent since meter started."""
        before = meter["usage"]
        return {field: value - before[field] for field, value in usage(agent).items()}

    def log_run(self, phase, agent, meter, outcome, verdict=None, extra=None):
        """Add the run that meter measured to the phase's run_log. outcome is how it ended:
        done, stopped or failed. extra adds fields to the entry."""
        data = self.state.phase_data(phase.key)
        head = self.head(agent)
        spent = self.spent(agent, meter)
        entry = dict(
            run=data.get("runs", 0),
            started=meter["started"],
            finished=now(),
            seconds=round(time.monotonic() - meter["clock"], 1),
            cost=round(spent["cost"], 6),
            tokens_sent=spent["tokens_sent"],
            tokens_received=spent["tokens_received"],
            base=meter["base"],
            head=head,
            commits=self.count_commits(meter["base"], head, agent),
            verdict=verdict,
            outcome=outcome,
        )
        entry.update(extra or {})
        data.setdefault("run_log", []).append(entry)
        return entry

    def repo_of(self, agent=None):
        """The GitRepo agent works in (a parallel builder's is its worktree's), or the
        project's."""
        repo = getattr(agent, "repo", None)
        return repo if hasattr(repo, "get_head_commit_sha") else self.coder.repo

    def head(self, agent=None):
        """The commit HEAD is at where agent works, or in the project, or None outside git
        or before the first commit."""
        repo = self.repo_of(agent)
        return repo.get_head_commit_sha() if repo else None

    def count_commits(self, base, head, agent=None):
        """How many commits there are from base to head."""
        if not head or base == head:
            return 0
        try:
            spec = f"{base}..{head}" if base else head
            return int(self.repo_of(agent).repo.git.rev_list("--count", spec))
        except (ValueError,) + ANY_GIT_ERROR:
            return 0

    def save_quietly(self):
        """Save the state while something else has gone wrong, which matters more."""
        try:
            self.state.save()
        except TransitionError as err:
            self.io.tool_warning(str(err))

    def document_path(self, phase):
        return self.root / phase.document

    def document_text(self, phase, root=None):
        """The phase's document, or "" if there's none in the project (or in root, like a
        parallel builder's worktree)."""
        root = Path(root) if root else self.root
        path = root / phase.document
        try:
            text = read_project_file(root, path)
        except OSError:
            return ""
        if text is None:
            if path.is_symlink() and phase.key not in self.outside_warned:
                self.outside_warned.add(phase.key)
                self.io.tool_warning(f"Ignoring {phase.document}: it links outside the project.")
            return ""
        return text.strip()

    def task_message(self, phase, data=None):
        """The agent's first message: the idea, the documents it works from, the decisions
        so far, related passages from the project memory and its task. data is the phase's
        state (its feedback, say), by default the phase's own."""
        data = self.state.phase_data(phase.key) if data is None else data
        parts = [f"# The project idea\n\n{self.state.idea}"]

        inputs = list(phase.inputs) + [key for key in data.get("attach") or [] if key]
        inline = []
        for key in dict.fromkeys(inputs):
            source = PHASES_BY_KEY[key]
            text = self.document_text(source)
            title = f"# The {source.document_title} ({source.document})"
            if not text:
                continue
            if len(text) > MAX_INLINE_CHARS:
                parts.append(f"{title}\n\nIt's too long to include here; read it with read_file.")
            else:
                parts.append(f"{title}\n\n{text}")
                inline.append(source.document)

        feedback = data.get("feedback")
        decisions = self.decisions_context()
        if decisions:
            parts.append(f"# Decisions so far\n\n{decisions}")
        recalled = self.recalled_context(phase, inline, feedback)
        if recalled:
            parts.append(recalled)

        task = [f"You are the {phase.agent}."]
        if phase.key == "building" and not phase.mode:
            task.append(
                "Build the project described in the PRD, following the architecture document,"
                f" then write the build summary to {phase.document}."
            )
        elif phase.mode in phase_prompts.mode_tasks:
            task.append(phase_prompts.mode_tasks[phase.mode].format(document=phase.document))
        else:
            task.append(f"Write the {phase.document_title} to {phase.document}.")

        if self.document_text(phase):
            if feedback:
                why = "the feedback below asks for changes."
            elif data.get("stale"):
                why = "the earlier phases have changed since, so bring it in line with them."
            elif data.get("redo"):
                why = "the user asked for this phase to be done again."
            else:
                why = "it may be unfinished, so complete it."
            task.append(
                phase_prompts.revise_document.format(
                    document_title=phase.document_title, document=phase.document, why=why
                )
            )
            if phase.key == "building" and not phase.mode:
                task.append("The code from that run is in the project; build on it.")
        if feedback:
            prefix = phase_prompts.feedback_prefix.format(document_title=phase.document_title)
            task.append(f"{prefix}\n\n{feedback}")

        parts.append("# Your task\n\n" + "\n\n".join(task))
        return "\n\n".join(parts)

    # The project's tests

    def test_command(self):
        """The command that runs the project's tests: the template's, or the one the
        architecture document's Test command line gives, or None."""
        if self.template and self.template.test_command:
            return self.template.test_command
        return read_test_command(self.document_text(PHASES_BY_KEY["design"]))

    def run_tests(self, command=None, coder=None, timeout=TEST_TIMEOUT):
        """Run the test command (by default the project's) in coder's root, the way the
        bash tool runs a command: no stdin, a timeout, and the same permission check.

        Returns (passed, output). output is the end of what the command printed, after its
        exit status. passed is None when it didn't run: there's no test command, or the
        user didn't allow it."""
        coder = coder or self.coder
        io = coder.io
        command = command or self.test_command()
        if not command:
            return (
                None,
                (
                    "The project has no test command: the architecture document's Testing"
                    " approach needs a **Test command:** line."
                ),
            )
        action = agent_tools.bash(coder, command, timeout)
        io.tool_call("Run Tests", command)
        outcome, _ = coder.permissions.request(action)
        if outcome != "allow":
            message = f"Not allowed to run the test command, {command}."
            io.tool_result("Not run", error=True)
            io.tool_done(message, error=True)
            return None, message

        passed, result = self.run_shell(io, command, coder.root, timeout, "Tests")
        return passed, result

    def run_shell(self, io, command, cwd, timeout, what):
        """Run command in cwd like the bash tool, and show the end of its output under the
        tool call already shown. Returns (passed, its exit status and the end of its
        output)."""
        code, output = agent_tools.run_command(command, cwd, timeout)
        if code is None:
            status = f"Timed out after {timeout} seconds."
        else:
            status = f"Exit code: {code}"
        output = output.replace("\r\n", "\n").rstrip()
        output = keep_end(output, TEST_OUTPUT_CHARS) or "(no output)"
        passed = code == 0
        self.show_shell_output(io, output, passed, status, what)
        result = f"{status}\n{output}"
        io.tool_done(result, error=not passed)
        return passed, result

    def show_shell_output(self, io, output, passed, status, what):
        lines = [line.rstrip() for line in output.splitlines()]
        styles = [None] * len(lines)
        if len(lines) > TEST_PREVIEW_LINES:
            skipped = len(lines) - TEST_PREVIEW_LINES
            lines = [f"… +{skipped} earlier lines"] + lines[-TEST_PREVIEW_LINES:]
            styles = ["dim"] + [None] * TEST_PREVIEW_LINES
        verb = "pass" if what.endswith("s") else "passes"
        failed = "fail" if what.endswith("s") else "fails"
        lines.append(f"{what} {verb}" if passed else f"{what} {failed} ({status.rstrip('.')})")
        styles.append(None if passed else io.tool_error_color or "red")
        io.tool_result(lines, styles=styles)

    # The work plan, for parallel builders (loom/workplan.py)

    @property
    def workers(self):
        """How many builders may work at once: /project workers, or --build-workers."""
        workers = self.state.data.get("workers") if self.state else None
        return max(1, int(workers or self.settings.get("build_workers") or 1))

    def work_plan(self, text=None):
        """The architecture document's work plan, or None if it has none."""
        from loom.workplan import read_workplan

        if text is None:
            text = self.document_text(PHASES_BY_KEY["design"])
        files = []
        if self.coder.repo:
            try:
                files = self.coder.repo.get_tracked_files()
            except ANY_GIT_ERROR:
                files = []
        return read_workplan(text, self.workers * PACKAGES_PER_WORKER, files)

    def show_work_plan(self, text):
        plan = self.work_plan(text)
        if plan is None:
            return
        if plan.valid:
            how = "at once" if self.workers > 1 and len(plan.packages) > 1 else "one by one"
            self.io.tool_output(f"Work plan: {plan.describe()}. Builders: {self.workers}, {how}.")
            return
        self.io.tool_warning(
            "The work plan has problems, so Building will run as one agent unless you fix them:"
        )
        for problem in plan.problems:
            self.io.tool_warning(f"  - {problem}")

    # The template's skeleton and checks

    def apply_skeleton(self):
        """Copy the template's skeleton into the project when Building first starts, and
        commit it."""
        from loom.project_templates import copy_skeleton

        template = self.template
        info = self.state.template_info
        if not template or not template.skeleton or info.get("skeleton"):
            return
        written, skipped = copy_skeleton(template, self.root)
        info["skeleton"] = now()
        self.state.save()
        self.io.tool_output(
            f"Copied the {template.name} template's skeleton into the project:"
            f" {plural(len(written), 'file')}."
        )
        if skipped:
            self.io.tool_output(
                f"Kept the project's own {', '.join(skipped)} instead of the skeleton's."
            )
        if written:
            self.commit(
                [self.root / rel for rel in written],
                f"Add the {template.name} template's skeleton",
            )

    def run_checks(self, phase):
        """Run the template's checks for phase, once its agent is done. Failed checks warn
        but don't stop the project. Returns the results, [{command, passed, output}]."""
        template = self.template
        commands = template.checks_for(phase.key) if template else []
        if not commands:
            return []
        if not self.checks_allowed(template):
            self.io.tool_warning(
                f"Skipped the {template.name} template's checks: they weren't approved."
            )
            return []

        results = []
        for command in commands:
            self.io.tool_call("Check", command)
            passed, output = self.run_shell(self.io, command, self.root, TEST_TIMEOUT, "Check")
            results.append(dict(command=command, passed=passed, output=output))

        data = self.state.phase_data(phase.key)
        data["checks"] = results
        if data.get("run_log"):
            data["run_log"][-1]["checks"] = [
                dict(command=r["command"], passed=r["passed"]) for r in results
            ]
        self.state.save()

        failed = [r for r in results if not r["passed"]]
        if failed:
            self.io.tool_warning(
                f"{len(failed)} of the {template.name} template's {plural(len(results), 'check')}"
                " failed. They don't stop the project; the agents of the next phases see them."
            )
        # The phase's own title, not a step's, like Building's Integration
        self.decide(
            phase,
            "check",
            describe_checks(PHASES_BY_KEY[phase.key], results),
            source=TEMPLATE_SOURCE,
        )
        return results

    def checks_allowed(self, template):
        """Whether the template's checks may run: loom's own templates' and the user's
        always may; a project's own template's after the user approves them, once."""
        from loom import project_templates

        if template.trusted or project_templates.checks_approved(template, self.root):
            return True
        approved = getattr(self.coder, "template_checks_approved", {})
        if template.name not in approved:
            answer = self.io.permission_ask(
                f"Run the checks of this project's {template.name} template?",
                subject=project_templates.describe_checks(template),
                always="trust them in this project",
                explicit_yes_required=True,
            )
            if answer == "always":
                project_templates.approve_checks(template, self.root, self.io)
            approved[template.name] = answer in ("yes", "always")
            self.coder.template_checks_approved = approved
        return approved[template.name]

    # The project memory

    def decide(self, phase, kind, text, reason="", source=FOUNDER):
        """Record the outcome of a checkpoint, or another decision of the founder's."""
        try:
            return self.memory.record_decision(
                phase.key if phase else None, text, reason, source=source, kind=kind
            )
        except ProjectMemoryError as err:
            raise TransitionError(f"Unable to record the decision: {err}")

    def checkpoint_source(self):
        """Who answered the checkpoint: the founder, or --yes-always for them."""
        return FOUNDER if self.io.yes is None else "loom (--yes-always)"

    def remember_document(self, phase):
        """Index the phase's document in the project memory, if it changed."""
        text = self.document_text(phase)
        if not text:
            return
        try:
            self.memory.index_document(phase, text)
        except ProjectMemoryError as err:
            self.io.tool_warning(f"Unable to add {phase.document} to the project memory: {err}")

    def describe_decision(self, decision):
        phase = PHASES_BY_KEY.get(decision["phase"])
        where = phase.title if phase else "Project"
        who = "the founder" if decision["source"] == FOUNDER else decision["source"]
        line = f"{where}, {who}: {decision['text']}"
        if decision.get("reason"):
            line += f" (Why: {decision['reason']})"
        return line

    def decisions_context(self):
        """The decisions so far, as the agents see them."""
        try:
            decisions = self.memory.decisions(CONTEXT_KINDS)
        except ProjectMemoryError:
            return ""
        lines = [f"- {self.describe_decision(d)}" for d in decisions[-MAX_DECISIONS:]]
        if len(decisions) > MAX_DECISIONS:
            lines.insert(0, f"(The latest {MAX_DECISIONS}; use recall to find older ones.)")
        return "\n".join(lines)

    def recalled_context(self, phase, inline, feedback=None):
        """Passages of earlier phases' documents that aren't in the agent's message but may
        matter for its task, from the project memory."""
        earlier = [p.key for p in PHASES if p.number < phase.number]
        query = " ".join(text for text in (phase.recall, feedback) if text)
        if not earlier or not query:
            return ""
        try:
            hits = self.memory.search(
                query, MAX_RECALLED, phases=earlier, kinds=["document"], exclude_sources=inline
            )
        except ProjectMemoryError:
            return ""
        if not hits:
            return ""
        parts = [
            "# Related passages from the project memory\n\nFrom earlier documents that aren't"
            " included above. Use recall to look up more."
        ]
        for hit in hits:
            source = PHASES_BY_KEY[hit.phase]
            title = f" > {hit.title}" if hit.title else ""
            parts.append(f"## The {source.document_title} ({hit.source}){title}\n\n{hit.text}")
        return "\n\n".join(parts)

    # The approval checkpoint after each phase

    def review(self, phase):
        """The checkpoint after a phase: the founder approves its document, edits it or
        rejects it with feedback for the agent. A failing test report can go back to the
        Building agent instead. Returns whether to carry on: the phase was approved, or it
        (or an earlier one) is to be redone."""
        state = self.state
        while True:
            # Read again each time round: the founder may have edited it
            text = self.document_text(phase)
            verdict = read_verdict(phase, text)
            if state.phase_data(phase.key).get("verdict") != verdict:
                state.phase_data(phase.key)["verdict"] = verdict
                state.save()
            self.remember_document(phase)
            self.show_review(phase, text, verdict)
            choice = self.checkpoint(phase, verdict)
            if choice != "edit":
                break
            self.edit_document(phase)

        if choice == "reject":
            return self.ask_feedback(phase)
        if choice == "send back":
            rounds = state.data.get("fix_rounds", 0) + 1
            state.back("building", phase_prompts.fix_test_failures, attach=[phase.key])
            state.data["fix_rounds"] = rounds
            state.save()
            self.decide(
                phase,
                "sent back",
                f"Sent the failing test report back to the Building agent to fix (round {rounds})",
                source=self.checkpoint_source(),
            )
            return True

        if choice == "approve anyway":
            if phase.key == "idea":
                text = (
                    f"Carried on to {next_phase(phase).title} despite the Idea Check's NO-GO"
                    " verdict"
                )
            else:
                text = "Approved the test report although the tests fail"
            self.decide(phase, "override", text, source=self.checkpoint_source())
        else:
            text = f"Approved the {phase.document_title}"
            if verdict:
                text += f" ({phase.verdict_label.lower()} {verdict})"
            self.decide(phase, "approved", text, source=self.checkpoint_source())
        state.approve(phase.key)
        if phase.key == "testing":
            state.data["fix_rounds"] = 0
        state.save()
        return True

    def show_review(self, phase, text, verdict):
        lines = len(text.splitlines())
        self.io.tool_output()
        summary = f"{phase.title} is ready for review: {phase.document} ({lines} lines)"
        if verdict:
            summary += f", {phase.verdict_label.lower()} {verdict}"
        self.io.tool_output(summary + ".", bold=True)
        if phase.verdicts and not verdict:
            self.io.tool_warning(
                f"The {phase.document_title} has no {phase.verdict_label} line loom can read."
            )
        if phase.key == "design":
            command = read_test_command(text)
            template = self.template
            if template and template.test_command:
                self.io.tool_output(
                    f"Test command: {template.test_command} (from the {template.name} template)"
                )
            elif command:
                self.io.tool_output(f"Test command: {command}")
            else:
                self.io.tool_warning(
                    f"The {phase.document_title} has no Test command line loom can read, so"
                    " loom can't run the project's tests itself."
                )
        if phase.key == "building":
            from loom.tdd import describe_build

            runs = self.state.run_log(phase.key)
            for line, warn in describe_build(runs[-1] if runs else None):
                (self.io.tool_warning if warn else self.io.tool_output)(line)
        if phase.key == "design":
            self.show_work_plan(text)
        checks = self.state.phase_data(phase.key).get("checks")
        if checks:
            self.io.tool_output("The template's checks:")
            for check in checks:
                mark = "✓" if check["passed"] else "✗"
                self.io.tool_output(f"  {mark} {check['command']}")
        try:
            decisions = self.memory.decisions(["decision"], phase.key)
        except ProjectMemoryError:
            decisions = []
        decisions = [d for d in decisions if d["source"] == phase.agent]
        if decisions:
            self.io.tool_output(f"The {phase.agent} recorded these decisions:")
            for decision in decisions[-10:]:
                self.io.tool_output(f"  - {decision['text']}")

    def checkpoint(self, phase, verdict):
        """Ask the founder what to do with the phase's document. Returns "approve",
        "approve anyway", "edit", "reject" or "send back"."""
        following = next_phase(phase)
        info = dict(
            phase=phase.key,
            title=phase.title,
            document=phase.document,
            document_title=phase.document_title,
            verdict=verdict,
            next=following.key if following else None,
            next_title=following.title if following else None,
        )
        if phase.key == "idea" and verdict == "NO-GO":
            self.io.tool_warning("The Idea Check agent says NO-GO.")
            return self.io.choice_ask(
                f"Carry on to {following.title} anyway?",
                ["approve anyway", "edit", "reject"],
                default="reject",
                yes_choice="reject",
                checkpoint=info,
            )

        if phase.key == "testing" and verdict == "FAIL":
            rounds = self.state.data.get("fix_rounds", 0)
            if rounds < MAX_FIX_ROUNDS:
                return self.io.choice_ask(
                    "The tests failed. Send the test report back to the Building agent to fix?",
                    ["send back", "edit", "approve anyway", "reject"],
                    default="send back",
                    no_choice="reject",
                    checkpoint=info,
                )
            self.io.tool_warning(f"The tests still fail after {rounds} rounds of fixes.")
            return self.io.choice_ask(
                f"Approve the failing test report and move on to {following.title} anyway?",
                ["approve anyway", "edit", "send back", "reject"],
                default="reject",
                yes_choice="reject",
                checkpoint=info,
            )

        if following:
            question = f"Approve the {phase.document_title} and move on to {following.title}?"
        else:
            question = f"Approve the {phase.document_title} and finish the project?"
        return self.io.choice_ask(question, ["approve", "edit", "reject"], checkpoint=info)

    def edit_document(self, phase):
        """Open the phase's document in the founder's editor, and save their changes.
        Returns whether it changed."""
        path = self.document_path(phase)
        try:
            before = read_project_file(self.root, path)
        except OSError as err:
            before = None
            self.io.tool_error(f"Unable to read {phase.document}: {err}")
        if before is None:
            self.io.tool_error(f"There is no {phase.document} in the project to edit.")
            return False

        if self.io.edit_document:
            # A UI with its own editor, like the web UI's
            after = self.io.edit_document(before, phase.document)
        else:
            commands = getattr(self.coder, "commands", None)
            after = pipe_editor(before, suffix=".md", editor=getattr(commands, "editor", None))
        if after.strip() == before.strip():
            self.io.tool_output(f"The {phase.document_title} is unchanged.")
            return False
        if not in_project(self.root, path):
            self.io.tool_error(f"{phase.document} isn't a file in the project any more.")
            return False
        path.resolve().write_text(after, encoding="utf-8")

        changes = describe_changes(*count_changes(before, after))
        self.io.tool_output(f"Saved your edit of {phase.document}: {changes}.")
        self.decide(phase, "edited", f"Edited the {phase.document_title} by hand ({changes})")
        self.commit(path, f"Edit {phase.document} at the {phase.title} checkpoint")
        return True

    def commit(self, paths, message):
        """Commit the file at paths, or the files, with message."""
        coder = self.coder
        if not coder.repo or not coder.auto_commits or coder.dry_run:
            return
        paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
        try:
            coder.repo.commit(fnames=[str(path) for path in paths], message=message, coder=coder)
        except Exception as err:
            self.io.tool_warning(f"Unable to commit {', '.join(map(str, paths))}: {err}")

    def ask_feedback(self, phase):
        """The founder rejected the document: redo the phase with their feedback, or stop."""
        stop_hint = (
            "Stopped for review. Use /project approve, /project edit, /project reject FEEDBACK"
            " or /project back PHASE, then /project run."
        )
        if self.io.yes is not None:
            # Not interactive: nobody to ask
            self.io.tool_output(stop_hint)
            return False
        feedback = self.io.prompt_ask(
            f"What should the {phase.agent} change? (leave empty to stop here)"
        ).strip()
        if not feedback:
            self.io.tool_output(stop_hint)
            return False
        self.state.reject(phase.key, feedback)
        self.state.save()
        self.decide(
            phase, "rejected", f"Asked for changes to the {phase.document_title}: {feedback}"
        )
        return True

    # Commands that change the state without running anything

    def waiting_phase(self):
        phase = self.state.current
        if not phase or self.state.status(phase.key) != "review":
            raise TransitionError("No phase is waiting for review.")
        return phase

    def approve(self):
        phase = self.waiting_phase()
        self.remember_document(phase)
        self.state.approve(phase.key)
        if phase.key == "testing":
            self.state.data["fix_rounds"] = 0
        self.state.save()
        self.decide(phase, "approved", f"Approved the {phase.document_title}")
        return phase

    def edit(self):
        """Edit the document waiting for review. Returns the phase."""
        phase = self.waiting_phase()
        if self.edit_document(phase):
            text = self.document_text(phase)
            self.state.phase_data(phase.key)["verdict"] = read_verdict(phase, text)
            self.state.save()
            self.remember_document(phase)
        return phase

    def redo(self, feedback=""):
        """Run the current phase again, with feedback. Returns the phase."""
        phase = self.state.current
        if not phase:
            raise TransitionError("The project is complete; use /project back PHASE to redo one.")
        status = self.state.status(phase.key)
        if status == "review":
            self.state.reject(phase.key, feedback)
        else:
            data = self.state.phase_data(phase.key)
            data["redo"] = True
            if feedback:
                data["feedback"] = feedback
        self.state.save()
        if feedback:
            self.decide(
                phase, "rejected", f"Asked for changes to the {phase.document_title}: {feedback}"
            )
        return phase

    def back(self, name, feedback=""):
        try:
            phase = get_phase(name)
        except KeyError:
            names = ", ".join(phase.key for phase in PHASES)
            raise TransitionError(f"There is no phase {name!r}; use one of: {names}.")
        self.state.back(phase.key, feedback)
        self.state.save()
        self.cleanup_worktrees()
        text = f"Went back to {phase.title} to redo it and the phases after it"
        if feedback:
            text += f": {feedback}"
        self.decide(phase, "sent back", text)
        return phase

    def set_workers(self, text):
        """/project workers N: how many builders may work at once."""
        try:
            workers = int(text)
        except ValueError:
            raise TransitionError("Say how many builders: /project workers N")
        if not 1 <= workers <= MAX_WORKERS:
            raise TransitionError(f"Builders must be from 1 to {MAX_WORKERS}.")
        self.state.data["workers"] = workers
        self.state.save()
        return workers

    def record(self, text):
        """A decision of the founder's, for the agents of the phases to come."""
        if not text.strip():
            raise TransitionError("Say what was decided: /project decide DECISION")
        return self.decide(self.state.current, "decision", text)

    def reset(self):
        """Forget the project: its progress, decisions and memory. The documents and code
        stay."""
        try:
            self.memory.clear()
        except ProjectMemoryError as err:
            raise TransitionError(f"Unable to reset the project: {err}")
        self.state = None
        self.cleanup_worktrees()

    def cleanup_worktrees(self):
        """Remove the parallel builders' worktrees and branches (loom/worktrees.py)."""
        from loom import worktrees

        if not self.coder.repo:
            return
        try:
            removed = worktrees.cleanup_all(self.coder.repo.repo, self.root)
        except (OSError,) + ANY_GIT_ERROR as err:
            self.io.tool_warning(f"Unable to remove the builders' worktrees: {err}")
            return
        if removed:
            self.io.tool_output(f"Removed the builders' {plural(removed, 'worktree')}.")

    # Showing the project

    def show_status(self):
        state = self.state
        idea = state.idea.split("\n", 1)[0]
        if len(idea) > 80:
            idea = idea[:79] + "…"
        self.io.tool_output(f"Project: {idea}", bold=True)
        info = state.template_info
        if info or state.tdd:
            parts = []
            if info:
                parts.append(f"from the {info['name']} template")
            if state.tdd:
                parts.append("test-driven Building")
            text = ", ".join(parts)
            self.io.tool_output(f"  {text[0].upper()}{text[1:]}")
        current = state.current
        marks = dict(pending="○", running="●", review="◆", approved="✓")
        labels = dict(
            pending="pending",
            running="interrupted",
            review="waiting for review",
            approved="approved",
        )
        width = max(len(phase.title) for phase in PHASES)
        rows = []
        for phase in PHASES:
            data = state.phase_data(phase.key)
            status = data["status"]
            label = labels[status]
            if status == "pending" and data.get("stale"):
                label = "to redo"
            if data.get("verdict"):
                label += f", {data['verdict']}"
            pointer = "▶" if phase is current else " "
            line = f"{pointer} {marks[status]} {phase.number}. {phase.title:<{width}}  "
            line += f"{phase.produces:<16}  "
            rows.append((line, label, state.metrics(phase.key)))
        label_width = max(len(label) for _, label, _ in rows)
        for line, label, metrics in rows:
            if metrics["runs"]:
                line += f"{label:<{label_width}}  {describe_metrics(metrics)}"
            else:
                line += label
            self.io.tool_output(line.rstrip())
        from loom.parallel import describe_packages

        packages = describe_packages(state)
        if packages:
            self.io.tool_output(
                f"  Building's work packages ({plural(self.workers, 'builder')} at once):"
            )
            for line in packages:
                self.io.tool_output(line)
        totals = state.totals()
        if totals["runs"]:
            self.io.tool_output()
            self.io.tool_output(
                f"Total: {describe_metrics(totals)}, {format_tokens(totals['tokens_sent'])}"
                f" tokens sent, {format_tokens(totals['tokens_received'])} received,"
                f" {plural(totals['commits'], 'commit')}"
            )
        self.io.tool_output()
        if current:
            status = state.status(current.key)
            if status == "review":
                self.io.tool_output(
                    f"{current.title} is waiting for review: read {current.document}, then"
                    " /project approve, /project edit, /project reject FEEDBACK or /project run."
                )
            else:
                self.io.tool_output(f"Next: {current.title}. Run it with /project run.")
        else:
            self.io.tool_output("The project is complete.")

    def write_report(self, fmt, out=None, summary=False):
        """Write the project's report (see loom/project_report.py) as fmt, at out or at
        loom-project/report.FMT, with an executive summary by the weak model if summary.
        Returns the paths written."""
        from loom import project_report

        report = project_report.ProjectReport.from_orchestrator(self)
        text = report.markdown()
        if summary:
            model = self.coder.main_model.weak_model or self.coder.main_model
            self.io.tool_output(f"{model.name} is writing the executive summary...")
            try:
                text = report.markdown(report.write_summary(model, text))
            except project_report.ReportError as err:
                self.io.tool_warning(f"{err} The report has no summary.")
        path = Path(out).expanduser() if out else project_report.default_path(self.root, fmt)
        if not path.is_absolute():
            path = self.root / path
        return project_report.export(text, fmt, path, warn=self.io.tool_warning)

    def show_documents(self):
        for phase in PHASES:
            if self.document_text(phase):
                self.io.tool_output(f"  {phase.document}  ({phase.produces})")

    def show_decisions(self):
        try:
            decisions = self.memory.decisions()
        except ProjectMemoryError as err:
            raise TransitionError(str(err))
        if not decisions:
            self.io.tool_output("No decisions yet.")
            return
        for decision in decisions:
            self.io.tool_output(
                f"#{decision['id']:<3} {decision['kind']:<9}  {self.describe_decision(decision)}"
            )

    def show_recall(self, query):
        from loom.tools import format_hits

        if not query.strip():
            raise TransitionError("Say what to look for: /project recall QUERY")
        try:
            hits = self.memory.search(query, 5)
        except ProjectMemoryError as err:
            raise TransitionError(str(err))
        if not hits:
            self.io.tool_output("Nothing in the project memory matches that.")
            return
        self.io.tool_output(format_hits(hits))

    def show_memory(self):
        stats = self.memory.stats()
        self.io.tool_output(f"Project memory: {self.memory.root / DB_FILE}", bold=True)
        self.io.tool_output(f"  Search: {self.memory.backend}")
        self.io.tool_output(
            f"  {stats['decisions']} decisions, {stats['chunks']} indexed passages of the"
            " idea, documents and decisions"
        )
        if self.memory.store == "keyword" or chroma_installed() or self.io.yes is not None:
            return
        from loom import utils

        if utils.check_pip_install_extra(
            self.io,
            "chromadb",
            "Searching the project memory by meaning needs ChromaDB, from loom's memory extra.",
            utils.loom_extra("memory"),
        ):
            self.memory = ProjectMemory(self.root, self.io)
            self.io.tool_output(f"Project memory now searches with the {self.memory.backend}.")

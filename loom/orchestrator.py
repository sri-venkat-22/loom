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
import time
from datetime import datetime
from pathlib import Path

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
from loom.phases import PHASES, PHASES_BY_KEY, get_phase, next_phase, read_verdict
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

# What each entry of a phase's run_log adds up, for its metrics
METRIC_FIELDS = ("seconds", "cost", "tokens_sent", "tokens_received", "commits")


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
    def new(cls, root, idea, db=None):
        data = dict(
            version=FORMAT_VERSION,
            idea=idea.strip(),
            created=now(),
            phases={phase.key: dict(status="pending", runs=0) for phase in PHASES},
            fix_rounds=0,
            history=[],
        )
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

    # Starting and running

    def new_project(self, idea):
        try:
            # A new idea starts with an empty memory
            self.memory.clear()
        except ProjectMemoryError as err:
            raise TransitionError(f"Unable to start the project: {err}")
        self.state = ProjectState.new(self.root, idea, self.memory.db)
        self.state.save()
        self.memory.index_text("idea", "idea", None, "idea", self.state.idea, "The project idea")

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
        agent = self.make_agent(phase)
        meter = self.start_run(agent)
        # How the run ended, if the agent raised
        raised = "failed"
        try:
            agent.run(with_message=self.task_message(phase), preproc=False)
            if not (agent.failed or self.stopped(agent) or self.document_text(phase)):
                self.io.tool_warning(f"The {phase.agent} didn't write {phase.document}.")
                agent.run(
                    with_message=phase_prompts.missing_document.format(document=phase.document),
                    preproc=False,
                )
            raised = None
        except KeyboardInterrupt:
            raised = "stopped"
            raise
        finally:
            self.collect(agent, meter)
            if raised:
                self.log_run(phase, agent, meter, raised)
                self.save_quietly()

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
            self.log_run(phase, agent, meter, outcome)
            state.stop(phase.key, reason)
            state.save()
            self.io.tool_error(f"{phase.title} stopped: {reason}. Continue with /project run.")
            return False

        verdict = read_verdict(phase, self.document_text(phase))
        self.log_run(phase, agent, meter, "done", verdict)
        state.finish(phase.key, verdict)
        state.save()
        self.remember_document(phase)
        return True

    def stopped(self, agent):
        return agent.stop_requested or agent.interrupted

    def make_agent(self, phase):
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
            fnames=[],
            read_only_fnames=[],
            done_messages=[],
            cur_messages=[],
            session=Session(),
        )

    def collect(self, agent, meter):
        """Add what the agent spent since meter started, and its commits, to the coder that
        started the project."""
        for field, value in self.spent(agent, meter).items():
            name = "total_cost" if field == "cost" else f"total_{field}"
            setattr(self.coder, name, getattr(self.coder, name) + value)
        self.coder.loom_commit_hashes.update(agent.loom_commit_hashes)

    # Metrics of each run

    def start_run(self, agent):
        """Start measuring a run of agent: the time, what it has spent and the commit HEAD
        is at."""
        return dict(started=now(), clock=time.monotonic(), usage=usage(agent), base=self.head())

    def spent(self, agent, meter):
        """What agent has spent since meter started."""
        before = meter["usage"]
        return {field: value - before[field] for field, value in usage(agent).items()}

    def log_run(self, phase, agent, meter, outcome, verdict=None):
        """Add the run that meter measured to the phase's run_log. outcome is how it ended:
        done, stopped or failed."""
        data = self.state.phase_data(phase.key)
        head = self.head()
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
            commits=self.count_commits(meter["base"], head),
            verdict=verdict,
            outcome=outcome,
        )
        data.setdefault("run_log", []).append(entry)
        return entry

    def head(self):
        """The commit HEAD is at, or None outside git or before the first commit."""
        repo = self.coder.repo
        return repo.get_head_commit_sha() if repo else None

    def count_commits(self, base, head):
        """How many commits there are from base to head."""
        if not head or base == head:
            return 0
        try:
            spec = f"{base}..{head}" if base else head
            return int(self.coder.repo.repo.git.rev_list("--count", spec))
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

    def document_text(self, phase):
        """The phase's document, or "" if there's none in the project."""
        path = self.document_path(phase)
        try:
            text = read_project_file(self.root, path)
        except OSError:
            return ""
        if text is None:
            if path.is_symlink() and phase.key not in self.outside_warned:
                self.outside_warned.add(phase.key)
                self.io.tool_warning(f"Ignoring {phase.document}: it links outside the project.")
            return ""
        return text.strip()

    def task_message(self, phase):
        """The agent's first message: the idea, the documents it works from, the decisions
        so far, related passages from the project memory and its task."""
        data = self.state.phase_data(phase.key)
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
        if phase.key == "building":
            task.append(
                "Build the project described in the PRD, following the architecture document,"
                f" then write the build summary to {phase.document}."
            )
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
            if phase.key == "building":
                task.append("The code from that run is in the project; build on it.")
        if feedback:
            prefix = phase_prompts.feedback_prefix.format(document_title=phase.document_title)
            task.append(f"{prefix}\n\n{feedback}")

        parts.append("# Your task\n\n" + "\n\n".join(task))
        return "\n\n".join(parts)

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

    def commit(self, path, message):
        coder = self.coder
        if not coder.repo or not coder.auto_commits or coder.dry_run:
            return
        try:
            coder.repo.commit(fnames=[str(path)], message=message, coder=coder)
        except Exception as err:
            self.io.tool_warning(f"Unable to commit {path}: {err}")

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
        text = f"Went back to {phase.title} to redo it and the phases after it"
        if feedback:
            text += f": {feedback}"
        self.decide(phase, "sent back", text)
        return phase

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

    # Showing the project

    def show_status(self):
        state = self.state
        idea = state.idea.split("\n", 1)[0]
        if len(idea) > 80:
            idea = idea[:79] + "…"
        self.io.tool_output(f"Project: {idea}", bold=True)
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

"""
The project orchestrator: takes an idea through the six phases of loom/phases.py in order,
running each phase's agent and asking the user to approve its document before the next
phase starts.

ProjectState is the state machine. It's saved in .loom/project.json, so a project
survives restarting loom, and tracks which phase the project is in. Each phase is:

  pending ──start──▶ running ──finish──▶ review ──approve──▶ approved
     ▲                  │                   │
     └──────stop────────┘                   │
     └──────────────reject (feedback)───────┘

Only the current phase (the first one not approved) can change, except with back, which
sends the project back to an earlier phase and resets the phases after it to pending
(their documents stay on disk for the agents to revise). The project is complete when
every phase is approved.
"""

import json
from datetime import datetime
from pathlib import Path

from loom.coders import phase_prompts
from loom.phases import PHASES, PHASES_BY_KEY, get_phase, next_phase, read_verdict

STATE_FILE = ".loom/project.json"
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


def now():
    return datetime.now().isoformat(timespec="seconds")


def in_project(root, path):
    """Whether path is a regular file inside root after following symlinks: a repo could
    link a phase document to ~/.aws/credentials, or to /dev/zero."""
    root = Path(root).resolve()
    real = Path(path).resolve()
    return (real == root or root in real.parents) and real.is_file()


def read_project_file(root, path, limit=MAX_DOC_BYTES):
    """The text of a file in the project, at most limit bytes of it, or None when it's
    missing or isn't a regular file inside root."""
    if not in_project(root, path):
        return None
    with open(Path(path).resolve(), "rb") as f:
        return f.read(limit).decode("utf-8", errors="replace")


class TransitionError(Exception):
    pass


class ProjectState:
    def __init__(self, path, data, root=None):
        self.path = Path(path)
        self.data = data
        # The project root, which the file has to stay in
        self.root = root

    @classmethod
    def new(cls, root, idea):
        data = dict(
            version=FORMAT_VERSION,
            idea=idea.strip(),
            created=now(),
            phases={phase.key: dict(status="pending", runs=0) for phase in PHASES},
            fix_rounds=0,
            history=[],
        )
        state = cls(Path(root) / STATE_FILE, data, root)
        state.log(None, "new", idea.strip().split("\n", 1)[0])
        return state

    @classmethod
    def load(cls, root):
        """The project in root, or None if it has none."""
        path = Path(root) / STATE_FILE
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
        return cls(path, data, root)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.root is not None and (self.path.exists() or self.path.is_symlink()):
            # Don't write through a symlink to a file outside the project
            if not in_project(self.root, self.path):
                raise TransitionError(f"{self.path} isn't a file in the project")
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")

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
        self.state = state if state is not None else ProjectState.load(self.root)
        # Phases whose document links outside the project, which loom has warned about
        self.outside_warned = set()

    # Starting and running

    def new_project(self, idea):
        self.state = ProjectState.new(self.root, idea)
        self.state.save()

    def run(self):
        """Run the phases from the current one, asking the user to approve each document,
        until the project is complete, the user stops or a phase fails. Returns whether
        the project is complete."""
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
        try:
            agent.run(with_message=self.task_message(phase), preproc=False)
            if not self.stopped(agent) and not self.document_text(phase):
                self.io.tool_warning(f"The {phase.agent} didn't write {phase.document}.")
                agent.run(
                    with_message=phase_prompts.missing_document.format(document=phase.document),
                    preproc=False,
                )
        finally:
            self.collect(agent)

        reason = None
        if agent.failed:
            reason = agent.failed
        elif self.stopped(agent):
            reason = "stopped by the user"
        elif not self.document_text(phase):
            reason = f"the agent didn't write {phase.document}"
        if reason:
            state.stop(phase.key, reason)
            state.save()
            self.io.tool_error(f"{phase.title} stopped: {reason}. Continue with /project run.")
            return False

        state.finish(phase.key, read_verdict(phase, self.document_text(phase)))
        state.save()
        return True

    def stopped(self, agent):
        return agent.stop_requested or agent.interrupted

    def make_agent(self, phase):
        from loom.coders import Coder
        from loom.coders.phase_coder import PhaseCoder
        from loom.sessions import Session

        # A fresh conversation: everything the agent needs is in its task message
        return Coder.create(
            from_coder=self.coder,
            coder_class=PhaseCoder,
            edit_format="agent",
            summarize_from_coder=False,
            phase=phase,
            fnames=[],
            read_only_fnames=[],
            done_messages=[],
            cur_messages=[],
            session=Session(),
        )

    def collect(self, agent):
        """Add the agent's costs and commits to the coder that started the project."""
        self.coder.total_cost = agent.total_cost
        self.coder.total_tokens_sent = agent.total_tokens_sent
        self.coder.total_tokens_received = agent.total_tokens_received
        self.coder.loom_commit_hashes = agent.loom_commit_hashes

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
        """The agent's first message: the idea, the documents it works from and its task."""
        data = self.state.phase_data(phase.key)
        parts = [f"# The project idea\n\n{self.state.idea}"]

        inputs = list(phase.inputs) + [key for key in data.get("attach") or [] if key]
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

        task = [f"You are the {phase.agent}."]
        if phase.key == "building":
            task.append(
                "Build the project described in the PRD, following the architecture document,"
                f" then write the build summary to {phase.document}."
            )
        else:
            task.append(f"Write the {phase.document_title} to {phase.document}.")

        feedback = data.get("feedback")
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

    # Reviewing a phase's document

    def review(self, phase):
        """Ask the user to approve the phase's document. Returns whether to carry on: the
        phase was approved, or it (or an earlier one) is to be redone."""
        state = self.state
        verdict = state.phase_data(phase.key).get("verdict")
        lines = len(self.document_text(phase).splitlines())
        self.io.tool_output()
        summary = f"{phase.title} is ready for review: {phase.document} ({lines} lines)"
        if verdict:
            summary += f", {phase.verdict_label.lower()} {verdict}"
        self.io.tool_output(summary + ".", bold=True)
        if phase.verdicts and not verdict:
            self.io.tool_warning(
                f"The {phase.document_title} has no {phase.verdict_label} line loom can read."
            )

        if phase.key == "idea" and verdict == "NO-GO":
            if not self.io.confirm_ask(
                "The Idea Check agent says NO-GO. Carry on to Planning anyway?",
                default="n",
                explicit_yes_required=True,
            ):
                return self.ask_feedback(phase)

        if phase.key == "testing" and verdict == "FAIL":
            rounds = state.data.get("fix_rounds", 0)
            if rounds < MAX_FIX_ROUNDS:
                if self.io.confirm_ask(
                    "The tests failed. Send the test report back to the Building agent to fix?"
                ):
                    state.back("building", phase_prompts.fix_test_failures, attach=[phase.key])
                    state.data["fix_rounds"] = rounds + 1
                    state.save()
                    return True
            else:
                self.io.tool_warning(f"The tests still fail after {rounds} rounds of fixes.")
            if not self.io.confirm_ask(
                "Approve the failing test report and move on to Launch anyway?",
                default="n",
                explicit_yes_required=True,
            ):
                return self.ask_feedback(phase)
        else:
            following = next_phase(phase)
            if following:
                question = f"Approve the {phase.document_title} and move on to {following.title}?"
            else:
                question = f"Approve the {phase.document_title} and finish the project?"
            if not self.io.confirm_ask(question):
                return self.ask_feedback(phase)

        state.approve(phase.key)
        if phase.key == "testing":
            state.data["fix_rounds"] = 0
        state.save()
        return True

    def ask_feedback(self, phase):
        """The user didn't approve: redo the phase with their feedback, or stop."""
        stop_hint = (
            "Stopped for review. Use /project approve, /project redo FEEDBACK or /project back"
            " PHASE, then /project run."
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
        return True

    # Commands that change the state without running anything

    def approve(self):
        phase = self.state.current
        if not phase or self.state.status(phase.key) != "review":
            raise TransitionError("No phase is waiting for review.")
        self.state.approve(phase.key)
        self.state.save()
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
        return phase

    def back(self, name, feedback=""):
        try:
            phase = get_phase(name)
        except KeyError:
            names = ", ".join(phase.key for phase in PHASES)
            raise TransitionError(f"There is no phase {name!r}; use one of: {names}.")
        self.state.back(phase.key, feedback)
        self.state.save()
        return phase

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
        for phase in PHASES:
            data = state.phase_data(phase.key)
            status = data["status"]
            label = labels[status]
            if status == "pending" and data.get("stale"):
                label = "to redo"
            if data.get("verdict"):
                label += f", {data['verdict']}"
            pointer = "▶" if phase is current else " "
            self.io.tool_output(
                f"{pointer} {marks[status]} {phase.number}. {phase.title:<{width}}  "
                f"{phase.produces:<16}  {label}"
            )
        self.io.tool_output()
        if current:
            status = state.status(current.key)
            if status == "review":
                self.io.tool_output(
                    f"{current.title} is waiting for review: read {current.document}, then"
                    " /project approve, /project redo FEEDBACK or /project run."
                )
            else:
                self.io.tool_output(f"Next: {current.title}. Run it with /project run.")
        else:
            self.io.tool_output("The project is complete.")

    def show_documents(self):
        for phase in PHASES:
            if self.document_text(phase):
                self.io.tool_output(f"  {phase.document}  ({phase.produces})")

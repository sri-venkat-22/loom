import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from loom.memory import MEMORY_DIR
from loom.orchestrator import ProjectState
from loom.phases import PHASES
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory
from loom.web.backend.project import ProjectWatcher, project_state, read_project
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO, list_commands
from tests.basic.test_orchestrator import SCRIPT_IDEA, SCRIPT_PLANNING, run_project
from tests.web.test_webio import answer_asks, make_io

KEYS = [phase.key for phase in PHASES]


def wait_for(condition, seconds=5):
    for _ in range(int(seconds / 0.02)):
        if condition():
            return True
        threading.Event().wait(0.02)
    return False


class TestProjectState(unittest.TestCase):
    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()

    def tearDown(self):
        self.memory_store.stop()

    def test_no_project(self):
        with IgnorantTemporaryDirectory() as root:
            self.assertIsNone(read_project(root))
            state = project_state(root)
            self.assertIsNone(state["project"])
            self.assertIsNone(state["phase"])
            self.assertEqual([p["key"] for p in state["phases"]], KEYS)
            self.assertEqual({p["status"] for p in state["phases"]}, {"pending"})
            # Looking never creates the project's database
            self.assertFalse(Path(root, MEMORY_DIR).exists())

    def test_a_project_in_progress(self):
        with IgnorantTemporaryDirectory() as root:
            state = ProjectState.new(root, "An adder\nwith a CLI")
            state.start("idea")
            state.finish("idea", "GO")
            state.approve("idea")
            state.start("planning")
            state.save()

            found = project_state(root)
            self.assertEqual(found["project"], "An adder\nwith a CLI")
            self.assertEqual(found["phase"], "planning")
            statuses = {p["key"]: p["status"] for p in found["phases"]}
            self.assertEqual(statuses["idea"], "approved")
            self.assertEqual(statuses["planning"], "running")
            self.assertEqual(statuses["launch"], "pending")

    def test_the_watcher_follows_the_project(self):
        with IgnorantTemporaryDirectory() as root:
            session = WebSession()
            watcher = ProjectWatcher(session, root).start()
            try:
                state = ProjectState.new(root, "An adder")
                state.save()
                self.assertTrue(wait_for(lambda: session.snapshot.get("phase") == "idea"))

                state.start("idea")
                state.save()
                self.assertTrue(
                    wait_for(lambda: session.snapshot["phases"][0]["status"] == "running")
                )
            finally:
                watcher.stop()


class TestPhaseCommand(unittest.TestCase):
    def test_phase_is_listed(self):
        io, _ = make_io()
        commands = [c["cmd"] for c in list_commands(io_commands(io))]
        self.assertIn("/phase", commands)
        self.assertIn("/project", commands)
        self.assertEqual(commands, sorted(commands))

    def test_phase_alone_shows_the_status(self):
        io, _ = make_io()
        self.assertEqual(io.web_command("/phase"), "/project status")
        self.assertEqual(io.web_command("/add calc.py"), "/add calc.py")

    def test_going_back_asks_first(self):
        io, session = make_io()
        asks, thread = answer_asks(session, "y")
        self.assertEqual(io.web_command("/phase Design"), "/project back design")
        thread.join(5)
        self.assertEqual(asks[0]["kind"], "confirm")
        self.assertIn("Go back to Design?", asks[0]["question"])
        self.assertEqual(asks[0]["default"], "n")

        asks, thread = answer_asks(session, "n")
        self.assertEqual(io.web_command("/phase 2"), "")
        thread.join(5)

    def test_unknown_phase(self):
        io, session = make_io()
        self.assertEqual(io.web_command("/phase scaffold"), "")
        self.assertEqual(session.history[-1]["level"], "error")
        self.assertIn("idea, planning, design", session.history[-1]["text"])


def io_commands(io):
    from loom.commands import Commands

    return Commands(io, None)


class TestCheckpointsInTheBrowser(unittest.TestCase):
    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()

    def tearDown(self):
        self.memory_store.stop()

    def test_approve_then_request_changes(self):
        with GitTemporaryDirectory():
            from tests.basic.test_agent import make_repo

            make_repo()
            session = WebSession(interrupt=lambda: None)
            session.started = True
            io = WebIO(pretty=False, session=session)
            # Approve the idea report; ask for changes to the PRD, then leave the feedback
            # empty, which stops the run at the PRD's review
            asks, thread = answer_asks(session, "approve", "reject", "")
            coder, orchestrator, llm, done = run_project(SCRIPT_IDEA, SCRIPT_PLANNING, io=io)
            thread.join(5)

            self.assertFalse(done)
            state = orchestrator.state
            self.assertEqual(state.status("idea"), "approved")
            self.assertEqual(state.status("planning"), "review")
            breadcrumb = project_state(".")
            self.assertEqual(breadcrumb["phase"], "planning")

        first, second, feedback = asks
        self.assertEqual(first["kind"], "checkpoint")
        self.assertEqual(
            first["checkpoint"],
            dict(
                phase="idea",
                title="Idea Check",
                document="loom-project/1-idea-report.md",
                document_title="idea report",
                verdict="GO",
                next="planning",
                next_title="Planning",
            ),
        )
        self.assertEqual([c["value"] for c in first["choices"]], ["approve", "edit", "reject"])
        self.assertEqual(second["checkpoint"]["phase"], "planning")
        self.assertEqual(second["checkpoint"]["next"], "design")
        self.assertEqual(feedback["kind"], "prompt")
        self.assertIn("What should the", feedback["question"])

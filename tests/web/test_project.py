import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from loom.memory import MEMORY_DIR
from loom.orchestrator import ProjectState
from loom.phases import PHASES
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory
from loom.web.backend.project import (
    ProjectWatcher,
    project_state,
    read_project,
    timeline,
)
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


class TestDashboard(unittest.TestCase):
    """The project dashboard's timeline and run diffs."""

    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()

    def tearDown(self):
        self.memory_store.stop()

    def test_no_project(self):
        with IgnorantTemporaryDirectory() as root:
            found = timeline(root)
            self.assertFalse(found["available"])
            self.assertIsNone(found["deployment"])
            self.assertEqual(found["phases"], [])
            self.assertFalse(Path(root, MEMORY_DIR).exists())

    def test_timeline_of_a_project(self):
        with GitTemporaryDirectory():
            from tests.basic.test_agent import make_repo

            make_repo()
            coder, orchestrator, llm, done = run_project(SCRIPT_IDEA, SCRIPT_PLANNING)
            orchestrator.record("Use argparse")

            found = timeline(".")
            self.assertTrue(found["available"])
            self.assertEqual(found["idea"], orchestrator.state.idea)
            self.assertEqual(found["current"], "design")
            self.assertFalse(found["complete"])
            # Design ran out of scripted replies, so its run failed
            self.assertEqual(found["totals"]["runs"], 3)
            self.assertEqual([p["key"] for p in found["phases"]], KEYS)

            idea = found["phases"][0]
            self.assertEqual(idea["status"], "approved")
            self.assertEqual(idea["verdict"], "GO")
            self.assertEqual(idea["document"], "loom-project/1-idea-report.md")
            self.assertEqual(idea["metrics"]["runs"], 1)
            [run] = idea["run_log"]
            self.assertEqual((run["run"], run["outcome"], run["commits"]), (1, "done", 1))
            self.assertEqual([d["kind"] for d in idea["decisions"]], ["approved"])
            self.assertEqual([h["event"] for h in idea["history"]], ["start", "finish", "approve"])

            design = found["phases"][2]
            self.assertTrue(design["current"])
            self.assertEqual(design["status"], "pending")
            self.assertEqual([r["outcome"] for r in design["run_log"]], ["failed"])
            # The founder's decision is the current phase's
            self.assertEqual([d["text"] for d in design["decisions"]], ["Use argparse"])

    def test_the_watcher_pushes_the_timeline(self):
        with IgnorantTemporaryDirectory() as root:
            session = WebSession()
            watcher = ProjectWatcher(session, root).start()
            try:
                # Sent at the start, even without a project
                self.assertFalse(session.timeline["available"])
                state = ProjectState.new(root, "An adder")
                state.save()
                self.assertTrue(wait_for(lambda: session.timeline["available"]))
                state.start("idea")
                state.save()
                self.assertTrue(
                    wait_for(lambda: session.timeline["phases"][0]["status"] == "running")
                )
            finally:
                watcher.stop()

            # Kept as the latest snapshot, not in the conversation
            self.assertNotIn("timeline", [m["type"] for m in session.history])
            backlog = session.connect(object(), None)
            self.assertEqual([m["type"] for m in backlog[:2]], ["session", "timeline"])
            self.assertEqual(backlog[1]["phases"][0]["status"], "running")

    def test_timeline_and_diff_endpoints(self):
        import git
        from fastapi.testclient import TestClient

        from loom.web.backend.app import create_app
        from tests.basic.test_agent import make_repo
        from tests.basic.test_orchestrator import (
            BAD_ADDER,
            SCRIPT_DESIGN,
            script_building,
        )

        with GitTemporaryDirectory() as root:
            make_repo()
            coder, orchestrator, llm, done = run_project(
                SCRIPT_IDEA, SCRIPT_PLANNING, SCRIPT_DESIGN, script_building(BAD_ADDER)
            )
            io, session = make_io()
            io.root = root
            io.git = git.Repo(root)
            app = create_app(session, static_dir=root, allowed_hosts=["localhost"], io=io)
            client = TestClient(app, base_url="http://localhost")

            found = client.get("/api/project/timeline").json()
            self.assertEqual(found["current"], "testing")
            self.assertEqual(found["phases"][3]["metrics"]["runs"], 1)

            diff = client.get("/api/project/phase/building/diff").json()
            self.assertTrue(diff["available"])
            self.assertEqual(diff["run"], 1)
            self.assertEqual([r["run"] for r in diff["runs"]], [1])
            paths = [f["path"] for f in diff["files"]]
            self.assertEqual(paths, ["adder.py", "loom-project/4-build-summary.md"])
            adder = diff["files"][0]
            self.assertEqual(adder["status"], "added")
            self.assertIn("    return a - b", [line["text"] for line in adder["lines"]])

            self.assertEqual(client.get("/api/project/phase/building/diff?run=1").json(), diff)
            self.assertEqual(client.get("/api/project/phase/building/diff?run=7").status_code, 404)
            self.assertEqual(client.get("/api/project/phase/deploy/diff").status_code, 404)
            # A phase that hasn't run
            launch = client.get("/api/project/phase/launch/diff").json()
            self.assertFalse(launch["available"])
            self.assertEqual(launch["files"], [])

    def test_timeline_shows_the_template_and_its_checks(self):
        from loom.project_templates import load_template

        with IgnorantTemporaryDirectory() as root:
            template = load_template(root, "python-cli")
            state = ProjectState.new(root, "A CLI", template=template, tdd=True)
            state.phase_data("idea")["checks"] = [
                dict(command="ruff check .", passed=False, output="Exit code: 1\nE501")
            ]
            state.save()
            found = timeline(root)
            self.assertEqual(found["template"], dict(name="python-cli", source="built-in"))
            self.assertTrue(found["tdd"])
            self.assertEqual(
                found["phases"][0]["checks"], [dict(command="ruff check .", passed=False)]
            )
            self.assertEqual(found["phases"][1]["checks"], [])

    def test_timeline_shows_test_driven_building(self):
        with IgnorantTemporaryDirectory() as root:
            state = ProjectState.new(root, "A CLI", tdd=True)
            building = state.phase_data("building")
            building["spec"] = dict(
                status="approved", tests=["tests/test_cli.py"], locked={"tests/test_cli.py": "x"}
            )
            attempts = [
                dict(attempt=1, passed=False, seconds=3.0, cost=0.01, restored=[]),
                dict(attempt=2, passed=True, seconds=2.0, cost=0.02, restored=[]),
            ]
            building["run_log"] = [
                dict(run=1, step="acceptance tests", outcome="done", commits=1),
                dict(run=1, step="build", outcome="done", attempts=attempts, result="passed"),
            ]
            state.save()
            found = timeline(root)
            phase = found["phases"][3]
            self.assertEqual(
                phase["spec"],
                dict(
                    status="approved",
                    document="loom-project/4a-acceptance-tests.md",
                    tests=["tests/test_cli.py"],
                    locked=True,
                ),
            )
            self.assertEqual(phase["run_log"][1]["attempts"], attempts)
            self.assertIsNone(found["phases"][2]["spec"])

    def test_timeline_shows_the_work_packages(self):
        with IgnorantTemporaryDirectory() as root:
            state = ProjectState.new(root, "A CLI")
            state.data["workers"] = 2
            state.phase_data("building")["packages"] = dict(
                core=dict(status="merged", title="Core", wave=1, cost=0.25, runs=1),
                cli=dict(status="failed", title="CLI", wave=2, error="it didn't write its notes"),
            )
            state.save()
            found = timeline(root)
            self.assertEqual(found["workers"], 2)
            packages = found["phases"][3]["packages"]
            self.assertEqual([p["id"] for p in packages], ["core", "cli"])
            self.assertEqual(packages[0]["status"], "merged")
            self.assertEqual(packages[1]["error"], "it didn't write its notes")
            self.assertEqual(packages[1]["cost"], 0)
            self.assertEqual(found["phases"][2]["packages"], [])

    def test_timeline_shows_the_deployment(self):
        with IgnorantTemporaryDirectory() as root:
            state = ProjectState.new(root, "A CLI")
            state.data["deployment"] = dict(
                provider="fly",
                title="Fly.io",
                app="adder-app",
                region="iad",
                url="https://adder-app.fly.dev",
                healthy=True,
                smoke="GET /health answered 200",
                time="2026-10-04T10:00:00",
                source="founder",
                status="2 machines: started, started",
            )
            state.save()
            found = timeline(root)["deployment"]
            self.assertEqual(found["url"], "https://adder-app.fly.dev")
            self.assertTrue(found["healthy"])
            self.assertIsNone(found["pr"])
            self.assertNotIn("status", found)

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom.coders import Coder
from loom.coders.phase_coder import PhaseCoder
from loom.commands import Commands
from loom.io import InputOutput
from loom.llm import litellm
from loom.models import Model
from loom.orchestrator import (
    MAX_DOC_BYTES,
    MAX_FIX_ROUNDS,
    STATE_FILE,
    Orchestrator,
    ProjectState,
    TransitionError,
)
from loom.permissions import Permissions
from loom.phases import PHASES, PHASES_BY_KEY, get_phase, read_verdict
from loom.sendchat import sanity_check_messages
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory

from .test_agent import FakeLLM, call, make_repo, reply

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider tests"
IDEA = "A command-line tool that adds two numbers"


def make_coder(io=None, allow=()):
    io = io or InputOutput(yes=True)
    return Coder.create(
        Model("gpt-4o-mini"),
        "agent",
        io=io,
        map_tokens=0,
        permissions=Permissions(io, allow=list(allow)),
    )


def write_doc(key, body):
    return call("write_file", path=PHASES_BY_KEY[key].document, content=body)


def phase_of(request):
    """Which phase agent sent a request, from its system prompt."""
    system = request["messages"][0]["content"]
    for phase in PHASES:
        if f"# Your phase: {phase.number}. {phase.title}" in system:
            return phase.key
    return None


def user_messages(request):
    return [
        msg["content"]
        for msg in request["messages"]
        if msg["role"] == "user" and isinstance(msg["content"], str)
    ]


IDEA_REPORT = "# Idea report: adder\n**Verdict:** GO\nSmall and useful.\n"
PRD = "# PRD: adder\n## Functional requirements\nFR-1: add two integers.\n"
ARCHITECTURE = "# Architecture: adder\n## Project structure\nadder.py, tests/test_adder.py\n"
BUILD_SUMMARY = "# Build summary: adder\nFR-1 Done in adder.py.\n"
TEST_PASS = "# Test report: adder\n**Result:** PASS\nAll tests pass.\n"
TEST_FAIL = "# Test report: adder\n**Result:** FAIL\ntest_add fails: 2 + 3 gave -1.\n"
DEPLOYMENT = "# Deployment: adder\n## Target and why\nA container.\n"
TEST_FILE = "from adder import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

SCRIPT_IDEA = [reply(None, write_doc("idea", IDEA_REPORT)), reply("GO: it's small.")]
SCRIPT_PLANNING = [reply(None, write_doc("planning", PRD)), reply("PRD written.")]
SCRIPT_DESIGN = [reply(None, write_doc("design", ARCHITECTURE)), reply("Design written.")]


def script_building(body):
    return [
        reply(
            "Building it.",
            call("write_file", path="adder.py", content=body),
        ),
        reply(None, write_doc("building", BUILD_SUMMARY)),
        reply("Built adder.py."),
    ]


def script_testing(report):
    return [
        reply(
            None,
            call("write_file", path="tests/test_adder.py", content=TEST_FILE),
            # Not a test file, so refused
            call("write_file", path="adder.py", content="broken"),
        ),
        reply(None, call("bash", command=PYTEST)),
        reply(None, write_doc("testing", report)),
        reply("Report written."),
    ]


SCRIPT_LAUNCH = [
    reply(None, call("write_file", path="Dockerfile", content="FROM python:3.12-slim\n")),
    reply(None, write_doc("launch", DEPLOYMENT)),
    reply("Ready to deploy."),
]

GOOD_ADDER = "def add(a, b):\n    return a + b\n"
BAD_ADDER = "def add(a, b):\n    return a - b\n"


class TestProjectState(unittest.TestCase):
    def test_phases_run_in_order(self):
        with GitTemporaryDirectory() as root:
            state = ProjectState.new(root, IDEA)
            self.assertEqual(state.current.key, "idea")
            self.assertEqual([p.key for p in PHASES][0], "idea")

            # Only the current phase can move
            with self.assertRaises(TransitionError):
                state.start("planning")
            # And only along the state machine's edges
            with self.assertRaises(TransitionError):
                state.approve("idea")
            with self.assertRaises(TransitionError):
                state.finish("idea")

            for phase in PHASES:
                self.assertEqual(state.current, phase)
                state.start(phase.key)
                self.assertEqual(state.status(phase.key), "running")
                state.finish(phase.key)
                self.assertEqual(state.status(phase.key), "review")
                state.approve(phase.key)
                self.assertEqual(state.status(phase.key), "approved")

            self.assertTrue(state.complete)
            self.assertIsNone(state.current)
            events = [entry["event"] for entry in state.history]
            self.assertEqual(events[0], "new")
            self.assertEqual(events[1:4], ["start", "finish", "approve"])

    def test_save_and_load(self):
        with GitTemporaryDirectory() as root:
            self.assertIsNone(ProjectState.load(root))
            state = ProjectState.new(root, IDEA)
            state.start("idea")
            state.finish("idea", "GO")
            state.save()
            self.assertTrue((Path(root) / STATE_FILE).exists())

            loaded = ProjectState.load(root)
            self.assertEqual(loaded.idea, IDEA)
            self.assertEqual(loaded.status("idea"), "review")
            self.assertEqual(loaded.phase_data("idea")["verdict"], "GO")
            self.assertEqual(loaded.phase_data("idea")["runs"], 1)

            (Path(root) / STATE_FILE).write_text("not json")
            with self.assertRaises(TransitionError):
                ProjectState.load(root)

    def test_reject_and_stop_go_back_to_pending(self):
        with GitTemporaryDirectory() as root:
            state = ProjectState.new(root, IDEA)
            state.start("idea")
            state.stop("idea", "interrupted")
            self.assertEqual(state.status("idea"), "pending")
            state.start("idea")
            state.finish("idea")
            state.reject("idea", "Consider web users too")
            self.assertEqual(state.status("idea"), "pending")
            self.assertEqual(state.phase_data("idea")["feedback"], "Consider web users too")
            state.start("idea")
            self.assertEqual(state.phase_data("idea")["runs"], 3)
            state.finish("idea")
            state.approve("idea")
            self.assertNotIn("feedback", state.phase_data("idea"))

    def test_back_resets_the_later_phases(self):
        with GitTemporaryDirectory() as root:
            state = ProjectState.new(root, IDEA)
            for phase in PHASES[:4]:
                state.start(phase.key)
                state.finish(phase.key)
                state.approve(phase.key)
            self.assertEqual(state.current.key, "testing")

            # Can't go forward
            with self.assertRaises(TransitionError):
                state.back("launch")

            state.back("planning", "Add a subtract command", attach=["design"])
            self.assertEqual(state.current.key, "planning")
            self.assertEqual(state.status("idea"), "approved")
            for key in ("planning", "design", "building", "testing", "launch"):
                self.assertEqual(state.status(key), "pending")
            data = state.phase_data("planning")
            self.assertEqual(data["feedback"], "Add a subtract command")
            self.assertEqual(data["attach"], ["design"])
            self.assertTrue(state.phase_data("design")["stale"])
            self.assertTrue(state.phase_data("building")["stale"])
            self.assertNotIn("stale", state.phase_data("testing"))


class TestPhases(unittest.TestCase):
    def test_six_phases(self):
        self.assertEqual(
            [phase.title for phase in PHASES],
            ["Idea Check", "Planning", "Design", "Building", "Testing", "Launch"],
        )
        self.assertEqual([phase.number for phase in PHASES], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [phase.produces for phase in PHASES],
            ["idea report", "PRD", "architecture doc", "code", "test report", "deployment"],
        )
        # Building is the coding agent: every tool, any file
        building = PHASES_BY_KEY["building"]
        self.assertIsNone(building.tools)
        self.assertIsNone(building.writable)
        # The others have limited tools; only Testing and Launch can run commands
        for phase in PHASES:
            if phase is building:
                continue
            self.assertIn("write_file", phase.tools)
            self.assertEqual("bash" in phase.tools, phase.key in ("testing", "launch"))
            # Inputs come from earlier phases
            for key in phase.inputs:
                self.assertLess(PHASES_BY_KEY[key].number, phase.number)

    def test_get_phase(self):
        self.assertEqual(get_phase("3").key, "design")
        self.assertEqual(get_phase("Idea Check").key, "idea")
        self.assertEqual(get_phase("TESTING").key, "testing")
        with self.assertRaises(KeyError):
            get_phase("deploy")

    def test_read_verdict(self):
        idea = PHASES_BY_KEY["idea"]
        testing = PHASES_BY_KEY["testing"]
        self.assertEqual(read_verdict(idea, "# Report\n**Verdict:** GO\n"), "GO")
        self.assertEqual(read_verdict(idea, "Verdict: go with changes."), "GO WITH CHANGES")
        self.assertEqual(read_verdict(idea, "**Verdict: NO-GO**"), "NO-GO")
        self.assertEqual(read_verdict(idea, "## Verdict: No go"), "NO-GO")
        # The template's placeholder, or no verdict line
        self.assertIsNone(read_verdict(idea, "**Verdict:** <GO, GO WITH CHANGES or NO-GO>"))
        self.assertIsNone(read_verdict(idea, "Looks fine."))
        self.assertEqual(read_verdict(testing, "**Result:** FAIL\n2 failed"), "FAIL")
        self.assertEqual(read_verdict(testing, "Result: PASS"), "PASS")
        self.assertIsNone(read_verdict(PHASES_BY_KEY["planning"], "Verdict: GO"))


class TestPhaseCoder(unittest.TestCase):
    def make_agent(self, key, io=None):
        coder = make_coder(io)
        return Orchestrator(coder, ProjectState.new(coder.root, IDEA)).make_agent(
            PHASES_BY_KEY[key]
        )

    def test_limited_tools_and_brief(self):
        with GitTemporaryDirectory():
            make_repo()
            agent = self.make_agent("idea")
            self.assertIsInstance(agent, PhaseCoder)
            names = [tool["function"]["name"] for tool in agent.tools]
            self.assertEqual(
                names,
                ["read_file", "list_dir", "glob", "grep", "edit_file", "write_file", "todo_write"],
            )
            system = agent.format_messages().all_messages()[0]["content"]
            self.assertIn("loom's project pipeline", system)
            self.assertIn("# Your phase: 1. Idea Check", system)
            self.assertIn("- loom-project/1-idea-report.md (your idea report)", system)

            building = self.make_agent("building")
            names = [tool["function"]["name"] for tool in building.tools]
            self.assertIn("bash", names)
            system = building.format_messages().all_messages()[0]["content"]
            # The coding agent's own prompt, with the Building brief
            self.assertIn("Act as an expert software engineer", system)
            self.assertIn("# Your phase: 4. Building", system)

    def test_refuses_other_tools_and_files(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="yes")
            agent = self.make_agent("planning", io)
            llm = FakeLLM(
                reply(
                    None,
                    call("bash", command="ls"),
                    call("write_file", path="calc.py", content="x"),
                    write_doc("planning", PRD),
                ),
                reply("Done."),
            )
            with patch.object(litellm, "completion", llm):
                agent.run(with_message="Write the PRD", preproc=False)

            results = [
                msg["content"] for msg in llm.requests[1]["messages"] if msg["role"] == "tool"
            ]
            self.assertIn("can't use bash", results[0])
            self.assertIn("may only write loom-project/2-prd.md", results[1])
            self.assertIn("Created loom-project/2-prd.md", results[2])
            self.assertIn("return a - b", Path("calc.py").read_text())
            self.assertEqual(Path(PHASES_BY_KEY["planning"].document).read_text(), PRD)
            # Writing its own document needs no question
            io.permission_ask.assert_not_called()

    def test_testing_may_write_test_files_only(self):
        with GitTemporaryDirectory():
            make_repo()
            agent = self.make_agent("testing")
            allowed = ["tests/test_x.py", "test_calc.py", "src/app.test.ts", "pkg/a_test.go"]
            for path in allowed + ["calc.py", "src/app.ts"]:
                action = MagicMock(kind="edit", inside=True, target=path)
                refusal = agent.refuse_action("write_file", action)
                self.assertEqual(refusal is None, path in allowed, path)

            launch = self.make_agent("launch")
            for path, ok in [
                ("Dockerfile", True),
                (".github/workflows/ci.yml", True),
                ("docker-compose.yml", True),
                ("README.md", True),
                # Protected, so never a deployment file
                (".env.example", False),
                ("calc.py", False),
            ]:
                action = MagicMock(kind="edit", inside=True, target=path)
                self.assertEqual(launch.refuse_action("write_file", action) is None, ok, path)


class TestOrchestrator(unittest.TestCase):
    def run_project(self, *scripts, io=None):
        coder = make_coder(io, allow=[f"bash({sys.executable} -m pytest*)"])
        llm = FakeLLM(*[step for script in scripts for step in script])
        orchestrator = Orchestrator(coder)
        orchestrator.new_project(IDEA)
        with patch.object(litellm, "completion", llm):
            done = orchestrator.run()
        return coder, orchestrator, llm, done

    def test_runs_the_six_phases(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder, orchestrator, llm, done = self.run_project(
                SCRIPT_IDEA,
                SCRIPT_PLANNING,
                SCRIPT_DESIGN,
                script_building(GOOD_ADDER),
                script_testing(TEST_PASS),
                SCRIPT_LAUNCH,
            )
            self.assertTrue(done)
            self.assertEqual(llm.replies, [])
            state = ProjectState.load(".")
            self.assertTrue(state.complete)
            self.assertEqual(state.phase_data("idea")["verdict"], "GO")
            self.assertEqual(state.phase_data("testing")["verdict"], "PASS")

            for phase, text in [
                ("idea", IDEA_REPORT),
                ("planning", PRD),
                ("design", ARCHITECTURE),
                ("building", BUILD_SUMMARY),
                ("testing", TEST_PASS),
                ("launch", DEPLOYMENT),
            ]:
                self.assertEqual(Path(PHASES_BY_KEY[phase].document).read_text(), text)
            self.assertEqual(Path("adder.py").read_text(), GOOD_ADDER)
            self.assertEqual(Path("tests/test_adder.py").read_text(), TEST_FILE)
            self.assertTrue(Path("Dockerfile").exists())

            # Each phase's agent sent its requests in order
            phases = [phase_of(request) for request in llm.requests]
            expected = []
            for key, script in [
                ("idea", SCRIPT_IDEA),
                ("planning", SCRIPT_PLANNING),
                ("design", SCRIPT_DESIGN),
                ("building", script_building(GOOD_ADDER)),
                ("testing", script_testing(TEST_PASS)),
                ("launch", SCRIPT_LAUNCH),
            ]:
                expected += [key] * len(script)
            self.assertEqual(phases, expected)

            # The agents got the idea and their inputs, and nothing else
            first = {}
            for request in llm.requests:
                first.setdefault(phase_of(request), user_messages(request)[0])
            self.assertIn(IDEA, first["idea"])
            self.assertIn("Write the idea report to loom-project/1-idea-report.md", first["idea"])
            self.assertIn(IDEA_REPORT.strip(), first["planning"])
            self.assertIn(PRD.strip(), first["design"])
            self.assertNotIn(IDEA_REPORT.strip(), first["building"])
            self.assertIn(PRD.strip(), first["building"])
            self.assertIn(ARCHITECTURE.strip(), first["building"])
            self.assertIn(BUILD_SUMMARY.strip(), first["testing"])
            self.assertIn(TEST_PASS.strip(), first["launch"])

            # The Testing agent couldn't change the code, but ran the tests
            testing = [r for r in llm.requests if phase_of(r) == "testing"]
            results = [m["content"] for m in testing[1]["messages"] if m["role"] == "tool"]
            self.assertIn("may only write", results[1])
            results = [m["content"] for m in testing[2]["messages"] if m["role"] == "tool"]
            self.assertIn("1 passed", results[-1])

            # The phases' work was committed, and the main conversation is untouched
            self.assertFalse(repo.is_dirty(untracked_files=False))
            self.assertGreater(len(list(repo.iter_commits())), 6)
            self.assertEqual(coder.done_messages, [])
            self.assertEqual(coder.cur_messages, [])

    def test_failing_tests_go_back_to_building(self):
        with GitTemporaryDirectory():
            make_repo()
            fix = [
                reply(
                    None,
                    call("write_file", path="adder.py", content=GOOD_ADDER),
                    write_doc("building", BUILD_SUMMARY + "Fixed add.\n"),
                ),
                reply("Fixed."),
            ]
            retest = [
                reply(None, call("bash", command=PYTEST)),
                reply(None, write_doc("testing", TEST_PASS)),
                reply("Passes now."),
            ]
            coder, orchestrator, llm, done = self.run_project(
                SCRIPT_IDEA,
                SCRIPT_PLANNING,
                SCRIPT_DESIGN,
                script_building(BAD_ADDER),
                script_testing(TEST_FAIL),
                fix,
                retest,
                SCRIPT_LAUNCH,
            )
            self.assertTrue(done)
            self.assertEqual(llm.replies, [])
            self.assertEqual(Path("adder.py").read_text(), GOOD_ADDER)

            building = [r for r in llm.requests if phase_of(r) == "building"]
            fix_request = user_messages(building[len(script_building(BAD_ADDER))])[0]
            self.assertIn(TEST_FAIL.strip(), fix_request)
            self.assertIn("says the tests FAIL", fix_request)
            self.assertIn("build on it", fix_request)

            testing = [r for r in llm.requests if phase_of(r) == "testing"]
            retest_request = user_messages(testing[len(script_testing(TEST_FAIL))])[0]
            self.assertIn("the earlier phases have changed", retest_request)

            events = [(e.get("phase"), e["event"]) for e in orchestrator.state.history]
            self.assertIn(("building", "back"), events)
            self.assertEqual(orchestrator.state.data["fix_rounds"], 0)

    def test_fix_rounds_are_limited(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            state = ProjectState.new(coder.root, IDEA)
            for phase in PHASES[:4]:
                state.start(phase.key)
                state.finish(phase.key)
                state.approve(phase.key)
            state.start("testing")
            state.finish("testing", "FAIL")
            state.data["fix_rounds"] = MAX_FIX_ROUNDS
            Path(PHASES_BY_KEY["testing"].document).parent.mkdir()
            Path(PHASES_BY_KEY["testing"].document).write_text(TEST_FAIL)

            orchestrator = Orchestrator(coder, state)
            self.assertFalse(orchestrator.run())
            # With --yes-always, loom doesn't approve a failing report
            self.assertEqual(state.status("testing"), "review")

    def test_no_go_stops_without_the_user(self):
        with GitTemporaryDirectory():
            make_repo()
            report = IDEA_REPORT.replace("GO", "NO-GO")
            coder, orchestrator, llm, done = self.run_project(
                [reply(None, write_doc("idea", report)), reply("NO-GO.")]
            )
            self.assertFalse(done)
            self.assertEqual(orchestrator.state.status("idea"), "review")
            self.assertEqual(orchestrator.state.phase_data("idea")["verdict"], "NO-GO")

    def test_missing_document_gets_one_nudge(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io)
            orchestrator = Orchestrator(coder)
            orchestrator.new_project(IDEA)
            llm = FakeLLM(
                reply("The idea looks good."),
                reply(None, write_doc("idea", IDEA_REPORT)),
                reply("Written."),
            )
            with patch.object(litellm, "completion", llm):
                self.assertTrue(orchestrator.run_phase(PHASES_BY_KEY["idea"]))
            self.assertIn("You finished without writing", user_messages(llm.requests[1])[-1])
            self.assertEqual(orchestrator.state.status("idea"), "review")

            # Twice without it, and the phase stops
            orchestrator.state.reject("idea")
            Path(PHASES_BY_KEY["idea"].document).unlink()
            llm = FakeLLM(reply("Good idea."), reply("Still good."))
            with patch.object(litellm, "completion", llm):
                self.assertFalse(orchestrator.run_phase(PHASES_BY_KEY["idea"]))
            self.assertEqual(orchestrator.state.status("idea"), "pending")

    def test_feedback_redoes_the_phase(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            # No to the first idea report, then stop at the PRD's review
            io.confirm_ask = MagicMock(side_effect=[False, True, False])
            io.prompt_ask = MagicMock(side_effect=["Target students", ""])
            revised = IDEA_REPORT + "For students.\n"
            coder, orchestrator, llm, done = self.run_project(
                SCRIPT_IDEA,
                [reply(None, write_doc("idea", revised)), reply("Revised.")],
                SCRIPT_PLANNING,
                io=io,
            )
            self.assertFalse(done)
            self.assertEqual(Path(PHASES_BY_KEY["idea"].document).read_text(), revised)
            redo = user_messages(llm.requests[2])[0]
            self.assertIn("asked for changes:\n\nTarget students", redo)
            self.assertIn("Read it, then revise it", redo)
            state = orchestrator.state
            self.assertEqual(state.status("idea"), "approved")
            self.assertEqual(state.status("planning"), "review")
            self.assertEqual(state.phase_data("idea")["runs"], 2)

    def test_agent_conversation_is_valid(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            orchestrator = Orchestrator(coder)
            orchestrator.new_project(IDEA)
            agent = orchestrator.make_agent(PHASES_BY_KEY["idea"])
            with patch.object(litellm, "completion", FakeLLM(*SCRIPT_IDEA)):
                agent.run(with_message=orchestrator.task_message(PHASES_BY_KEY["idea"]))
            sanity_check_messages(agent.done_messages + [dict(role="user", content="next")])
            self.assertIsNot(agent.session, coder.session)


@unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
class TestProjectFilesStayInTheProject(unittest.TestCase):
    def test_documents_linked_outside_the_project_are_ignored(self):
        with GitTemporaryDirectory(), IgnorantTemporaryDirectory() as outside:
            make_repo()
            secret = Path(outside) / "credentials"
            secret.write_text("aws_secret_access_key = SECRET\n")
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            orchestrator = Orchestrator(make_coder(io))
            orchestrator.new_project(IDEA)

            prd = get_phase("planning")
            Path(prd.document).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(secret, prd.document)
            self.assertEqual(orchestrator.document_text(prd), "")
            self.assertNotIn("SECRET", orchestrator.task_message(get_phase("design")))
            self.assertNotIn("SECRET", orchestrator.task_message(prd))
            # Warned about once
            warnings = [
                c[0][0] for c in io.tool_warning.call_args_list if "links outside" in c[0][0]
            ]
            self.assertEqual(len(warnings), 1)

            # Not a regular file
            os.unlink(prd.document)
            os.symlink("/dev/zero", prd.document)
            self.assertEqual(orchestrator.document_text(prd), "")

            # A link to another file in the project is fine
            os.unlink(prd.document)
            Path("notes.md").write_text("# PRD\n\nThe plan.\n")
            os.symlink(os.path.abspath("notes.md"), prd.document)
            self.assertIn("The plan.", orchestrator.document_text(prd))

    def test_long_documents_are_capped(self):
        with GitTemporaryDirectory():
            make_repo()
            orchestrator = Orchestrator(make_coder())
            orchestrator.new_project(IDEA)
            prd = get_phase("planning")
            Path(prd.document).parent.mkdir(parents=True, exist_ok=True)
            Path(prd.document).write_text("x" * (MAX_DOC_BYTES * 2))
            self.assertEqual(len(orchestrator.document_text(prd)), MAX_DOC_BYTES)

    def test_project_file_linked_outside_the_project(self):
        with GitTemporaryDirectory() as root, IgnorantTemporaryDirectory() as outside:
            target = Path(outside) / "bashrc"
            target.write_text("export PATH=$PATH\n")
            Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, STATE_FILE)
            with self.assertRaises(TransitionError):
                ProjectState.load(root)

            # Saving doesn't write through the link
            state = ProjectState.new(root, IDEA)
            with self.assertRaises(TransitionError):
                state.save()
            self.assertEqual(target.read_text(), "export PATH=$PATH\n")


class TestProjectCommand(unittest.TestCase):
    def test_status_and_errors(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io)
            commands = Commands(io, coder)
            io.tool_output = MagicMock()
            io.tool_error = MagicMock()

            commands.cmd_project("")
            io.tool_output.assert_called_with(
                "There is no project here. Start one with /project new IDEA."
            )
            commands.cmd_project("new")
            io.tool_error.assert_called_with("Describe the idea: /project new IDEA")
            commands.cmd_project("launch-now")
            self.assertIn("Unknown subcommand", io.tool_error.call_args[0][0])

            # Plan mode can't write documents
            coder.permissions.mode = "plan"
            commands.cmd_project(f"new {IDEA}")
            self.assertIn("plan mode", io.tool_error.call_args[0][0])
            self.assertEqual(ProjectState.load(".").status("idea"), "pending")

            commands.cmd_project("approve")
            io.tool_error.assert_called_with("No phase is waiting for review.")

            commands.cmd_project("status")
            shown = "\n".join(c[0][0] for c in io.tool_output.call_args_list if c[0])
            self.assertIn(f"Project: {IDEA}", shown)
            self.assertIn("▶ ○ 1. Idea Check", shown)
            self.assertIn("6. Launch", shown)

    def test_new_runs_phases_and_back(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io)
            commands = Commands(io, coder)
            llm = FakeLLM(*(SCRIPT_IDEA + [reply("The PRD isn't written.")] * 2))
            with patch.object(litellm, "completion", llm):
                commands.cmd_project(f"new {IDEA}")
            state = ProjectState.load(".")
            self.assertEqual(state.status("idea"), "approved")
            # Planning never wrote its PRD
            self.assertEqual(state.status("planning"), "pending")

            commands.cmd_project("back idea Aim it at students")
            state = ProjectState.load(".")
            self.assertEqual(state.current.key, "idea")
            self.assertEqual(state.phase_data("idea")["feedback"], "Aim it at students")

            commands.cmd_project("reset")
            # --yes-always answers yes; the documents stay
            self.assertIsNone(ProjectState.load("."))

    def test_completions(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            commands = Commands(io, make_coder(io))
            completions = commands.get_completions("/project")
            self.assertIn("new", completions)
            self.assertIn("design", completions)


if __name__ == "__main__":
    unittest.main()

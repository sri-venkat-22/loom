import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom.coders import Coder
from loom.io import InputOutput
from loom.llm import litellm
from loom.models import Model
from loom.orchestrator import Orchestrator, ProjectState
from loom.permissions import Permissions
from loom.phases import PHASES, PHASES_BY_KEY, SPEC
from loom.project_report import ProjectReport
from loom.tdd import SKIP_RE, TestDrivenBuilding, describe_build
from loom.utils import GitTemporaryDirectory

from .test_agent import FakeLLM, call, make_repo, reply
from .test_orchestrator import BUILD_SUMMARY, IDEA, PRD, write_doc

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider tests"
ARCHITECTURE = (
    f"# Architecture: adder\n## Testing approach\npytest.\n**Test command:** `{PYTEST}`\n"
)
ACCEPTANCE_TEST = (
    "from adder import add\n\n\ndef test_adds_two_numbers():\n    assert add(2, 3) == 5\n"
)
PLAN = "# Acceptance tests: adder\n## Requirements coverage\nFR-1: tests/test_adder.py\n"

memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})


def setUpModule():
    memory_store.start()


def tearDownModule():
    memory_store.stop()


def spec_script(test=ACCEPTANCE_TEST):
    return [
        reply(
            None,
            call("write_file", path="tests/test_adder.py", content=test),
            call("write_file", path=SPEC.document, content=PLAN),
        ),
        reply("The acceptance tests are written."),
    ]


def attempt(body, summary=True):
    """One try of the Building agent: write adder.py (and the build summary)."""
    calls = [call("write_file", path="adder.py", content=f"def add(a, b):\n    return {body}\n")]
    if summary:
        calls.append(write_doc("building", BUILD_SUMMARY))
    return [reply(None, *calls), reply(f"Built: {body}.")]


def project(io=None, settings=None, architecture=ARCHITECTURE):
    """A test-driven project whose Design is approved, so Building runs next."""
    io = io or InputOutput(yes=True)
    coder = Coder.create(
        Model("gpt-4o-mini"),
        "agent",
        io=io,
        map_tokens=0,
        permissions=Permissions(io, allow=[f"bash({sys.executable}*)"]),
        project_settings=settings or {},
    )
    orchestrator = Orchestrator(coder)
    orchestrator.new_project(IDEA, tdd=True)
    state = orchestrator.state
    for phase, text in zip(PHASES[:3], ["# Idea report\n**Verdict:** GO\n", PRD, architecture]):
        Path(phase.document).parent.mkdir(exist_ok=True)
        Path(phase.document).write_bytes(text.encode())
        state.start(phase.key)
        state.finish(phase.key)
        state.approve(phase.key)
    state.save()
    return coder, orchestrator


def run_building(orchestrator, *scripts):
    llm = FakeLLM(*[step for script in scripts for step in script])
    with patch.object(litellm, "completion", llm):
        done = orchestrator.run_phase(PHASES_BY_KEY["building"])
    return llm, done


def building_requests(llm):
    """The requests of the build loop's agent, after the spec agent's."""
    return [r for r in llm.requests if "# Your phase: 4. Building" in r["messages"][0]["content"]]


class TestTestDrivenBuilding(unittest.TestCase):
    def test_fails_twice_then_passes(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder, orchestrator = project()
            llm, done = run_building(
                orchestrator,
                spec_script(),
                attempt("a - b"),
                attempt("a * b", summary=False),
                attempt("a + b", summary=False),
            )
            self.assertTrue(done)
            self.assertEqual(llm.replies, [])
            state = orchestrator.state
            self.assertEqual(state.status("building"), "review")
            self.assertEqual(Path("adder.py").read_text(), "def add(a, b):\n    return a + b\n")

            # The acceptance tests were approved (--yes-always) and locked
            spec = state.phase_data("building")["spec"]
            self.assertEqual(spec["status"], "approved")
            self.assertEqual(list(spec["locked"]), ["tests/test_adder.py"])
            self.assertEqual(spec["first_run"]["passed"], False)
            # The commit they're locked at has them
            locked = repo.git.show(f"{spec['commit']}:tests/test_adder.py")
            self.assertEqual(locked, ACCEPTANCE_TEST.rstrip("\n"))

            # Two runs: the spec agent's, then the build loop's three attempts
            spec_run, build_run = state.run_log("building")
            self.assertEqual(spec_run["step"], "acceptance tests")
            self.assertEqual(build_run["step"], "build")
            self.assertEqual([a["passed"] for a in build_run["attempts"]], [False, False, True])
            self.assertEqual(build_run["result"], "passed")
            self.assertEqual(build_run["outcome"], "done")
            self.assertGreater(build_run["attempts"][-1]["cost"], 0)

            # The spec agent may only write tests, in spec mode
            system = llm.requests[0]["messages"][0]["content"]
            self.assertIn("# Your phase: 4. Acceptance tests", system)
            self.assertIn("You are the Testing agent in spec mode", system)
            self.assertIn("- **/tests/**", system)

            # The failures went back into the same conversation
            builds = building_requests(llm)
            first = [m["content"] for m in builds[0]["messages"] if m["role"] == "user"]
            self.assertIn("# Test-driven Building", first[-1])
            self.assertIn("The acceptance tests are locked: tests/test_adder.py", first[-1])
            last = [m["content"] for m in builds[-1]["messages"] if m["role"] == "user"]
            retries = [m for m in last if "and the tests fail (attempt" in m]
            self.assertEqual(len(retries), 2)
            self.assertIn("assert -1 == 5", retries[0])
            self.assertIn("assert 6 == 5", retries[1])
            self.assertIn("# Test-driven Building", last[0])

            # The checkpoint says how it went
            entry = state.run_log("building")[-1]
            [(line, warn)] = describe_build(entry)
            self.assertEqual(
                line, "Test-driven Building: 3 attempts (✗ ✗ ✓); the acceptance tests pass."
            )
            self.assertFalse(warn)

            # And the report
            report = ProjectReport.from_orchestrator(orchestrator).markdown()
            self.assertIn("- **Building:** test-driven", report)
            self.assertIn(
                (
                    "*The acceptance test plan, `loom-project/4a-acceptance-tests.md`. Locked"
                    " tests: `tests/test_adder.py`.*"
                ),
                report,
            )
            self.assertIn("## Acceptance tests: adder", report)
            self.assertIn("| 3 | pass |", report)

    def test_out_of_retries(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            coder, orchestrator = project(io, settings=dict(build_retries=1))
            llm, done = run_building(
                orchestrator, spec_script(), attempt("a - b"), attempt("a * b", summary=False)
            )
            # Building still finishes, for the founder to review; Testing comes next
            self.assertTrue(done)
            entry = orchestrator.state.run_log("building")[-1]
            self.assertEqual([a["passed"] for a in entry["attempts"]], [False, False])
            self.assertEqual(entry["result"], "failed")
            warnings = [c[0][0] for c in io.tool_warning.call_args_list]
            self.assertIn("The acceptance tests still fail after 2 attempts.", warnings)
            [(line, warn)] = describe_build(entry)
            self.assertTrue(warn)
            self.assertIn("the acceptance tests still fail", line)

    def test_out_of_budget(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            coder, orchestrator = project(io, settings=dict(build_budget=0.000001))
            llm, done = run_building(orchestrator, spec_script(), attempt("a - b"))
            self.assertTrue(done)
            entry = orchestrator.state.run_log("building")[-1]
            self.assertEqual(len(entry["attempts"]), 1)
            self.assertEqual(entry["result"], "budget")
            self.assertTrue(
                any("--build-budget" in c[0][0] for c in io.tool_warning.call_args_list)
            )

    def test_locked_tests_cant_be_changed(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            coder, orchestrator = project(io)
            cheat = "open('tests/test_adder.py', 'w').write('def test_nothing():\\n    pass\\n')\n"
            skip = "import pytest\n\n\n@pytest.mark.skip\ndef test_later():\n    pass\n"
            first_try = [
                reply(
                    None,
                    # Refused: it's locked
                    call("write_file", path="tests/test_adder.py", content="def test_x(): pass\n"),
                    call("write_file", path="cheat.py", content=cheat),
                    call("write_file", path="tests/test_extra.py", content=skip),
                    call("write_file", path="adder.py", content="def add(a, b):\n    return 0\n"),
                ),
                # Changed some other way
                reply(None, call("bash", command=f"{sys.executable} cheat.py")),
                reply(None, write_doc("building", BUILD_SUMMARY)),
                reply("Done, the tests pass now."),
            ]
            llm, done = run_building(
                orchestrator, spec_script(), first_try, attempt("a + b", summary=False)
            )
            self.assertTrue(done)
            results = [
                m["content"] for m in building_requests(llm)[1]["messages"] if m["role"] == "tool"
            ]
            self.assertIn("is one of the approved acceptance tests, which are locked", results[0])

            # loom put the test back before running the tests, so they failed
            self.assertEqual(Path("tests/test_adder.py").read_text(), ACCEPTANCE_TEST)
            entry = orchestrator.state.run_log("building")[-1]
            self.assertEqual(entry["attempts"][0]["restored"], ["tests/test_adder.py"])
            self.assertEqual([a["passed"] for a in entry["attempts"]], [False, True])
            retry = [
                m["content"] for m in building_requests(llm)[-1]["messages"] if m["role"] == "user"
            ]
            self.assertTrue(any("loom put back tests/test_adder.py" in m for m in retry))

            # The skipped test is flagged
            self.assertEqual(entry["skips"], ["tests/test_extra.py: @pytest.mark.skip"])
            lines = describe_build(entry)
            self.assertIn(
                ("loom put back locked tests that changed: tests/test_adder.py", True), lines
            )
            self.assertIn(("  tests/test_extra.py: @pytest.mark.skip", True), lines)

    def test_no_test_command_builds_the_usual_way(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            architecture = "# Architecture: adder\n## Testing approach\npytest, somehow.\n"
            coder, orchestrator = project(io, architecture=architecture)
            llm, done = run_building(orchestrator, attempt("a + b"))
            self.assertTrue(done)
            self.assertIn(
                "Test-driven Building needs a test command", io.tool_warning.call_args_list[0][0][0]
            )
            [entry] = orchestrator.state.run_log("building")
            self.assertNotIn("step", entry)
            self.assertNotIn("spec", orchestrator.state.phase_data("building"))

    def test_rejected_tests_are_written_again(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="yes")
            io.choice_ask = MagicMock(side_effect=["reject", "approve"])
            io.prompt_ask = MagicMock(return_value="Test negative numbers too")
            coder, orchestrator = project(io)
            better = ACCEPTANCE_TEST + "\n\ndef test_negative():\n    assert add(-2, 1) == -1\n"
            llm, done = run_building(
                orchestrator, spec_script(), spec_script(better), attempt("a + b")
            )
            self.assertTrue(done)
            question = io.choice_ask.call_args_list[0]
            self.assertEqual(
                question[0][0], "Approve the acceptance tests and lock them for Building?"
            )
            self.assertEqual(question[1]["checkpoint"]["title"], "Acceptance tests")
            self.assertEqual(question[1]["checkpoint"]["document"], SPEC.document)
            redo = [m["content"] for m in llm.requests[2]["messages"] if m["role"] == "user"][0]
            self.assertIn("asked for changes:\n\nTest negative numbers too", redo)
            kinds = [(d["kind"], d["text"]) for d in orchestrator.memory.decisions()]
            self.assertIn(
                (
                    "rejected",
                    "Asked for changes to the acceptance tests: Test negative numbers too",
                ),
                kinds,
            )
            self.assertIn(
                ("approved", "Approved the acceptance tests and locked 1 test file"), kinds
            )

    def test_stopping_at_the_tests_checkpoint_and_going_back(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="yes")
            io.choice_ask = MagicMock(return_value="reject")
            io.prompt_ask = MagicMock(return_value="")
            coder, orchestrator = project(io)
            llm, done = run_building(orchestrator, spec_script())
            self.assertFalse(done)
            state = ProjectState.load(".")
            self.assertEqual(state.status("building"), "pending")
            self.assertEqual(state.phase_data("building")["spec"]["status"], "review")

            # /project run picks up at the checkpoint, without writing them again
            io.choice_ask = MagicMock(return_value="approve")
            orchestrator = Orchestrator(coder)
            llm, done = run_building(orchestrator, attempt("a + b"))
            self.assertTrue(done)
            spec = orchestrator.state.phase_data("building")["spec"]
            self.assertEqual(spec["status"], "approved")

            # Going back before Building has the tests written again
            orchestrator.state.approve("building")
            orchestrator.back("design")
            spec = orchestrator.state.phase_data("building")["spec"]
            self.assertEqual(spec["status"], "pending")
            self.assertTrue(spec["stale"])
            self.assertNotIn("locked", spec)

    def test_skip_markers(self):
        for line in [
            "@pytest.mark.skip(reason='later')",
            "@pytest.mark.xfail",
            "pytest.skip('no')",
            "@unittest.skip('x')",
            "self.skipTest('x')",
            "it.skip('adds', () => {})",
            "test.todo('adds')",
            "xit('adds', () => {})",
            "t.Skip()",
            "#[ignore]",
        ]:
            self.assertTrue(SKIP_RE.search(line), line)
        for line in ["def test_skip_list():", "skipped = 0", "assert not result.skipped"]:
            self.assertFalse(SKIP_RE.search(line), line)

    def test_ready_needs_git(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, orchestrator = project()
            coder.repo = None
            io = orchestrator.io
            io.tool_warning = MagicMock()
            self.assertFalse(TestDrivenBuilding(orchestrator).ready())
            self.assertIn("needs a git repo", io.tool_warning.call_args[0][0])


if __name__ == "__main__":
    unittest.main()

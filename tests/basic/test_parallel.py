import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from litellm.types.utils import Choices, Message, ModelResponse

from loom import tools
from loom.coders import Coder, phase_prompts
from loom.io import InputOutput
from loom.llm import litellm
from loom.models import Model
from loom.orchestrator import Orchestrator, ProjectState
from loom.parallel import SCAFFOLD_DOCUMENT, ParallelBuilding
from loom.permissions import Permissions
from loom.phases import PHASES, PHASES_BY_KEY
from loom.utils import GitTemporaryDirectory
from loom.worktrees import WORKTREES_DIR

from .test_agent import call, full_response, make_repo, reply, stream_response
from .test_orchestrator import BUILD_SUMMARY, IDEA, PRD, write_doc

PYTHON = sys.executable
# -B: no stale bytecode when a test rewrites a module within the same second
PYTEST = f"{PYTHON} -B -m pytest -q -p no:cacheprovider tests"
ARCHITECTURE = f"""# Architecture: shouting adder
## Testing approach
pytest.
**Test command:** `{PYTEST}`
## Work packages
```yaml
- id: adder
  title: Adding
  owns: ["adder.py"]
  tests: ["tests/test_adder.py"]
  acceptance: [FR-1]
- id: shout
  title: Shouting
  owns: ["shout.py"]
  tests: ["tests/test_shout.py"]
  acceptance: [FR-2]
- id: app
  title: The command line
  owns: ["app.py"]
  depends_on: [adder, shout]
  acceptance: [FR-3]
```
"""

ADDER = "def add(a, b):\n    return a + b\n"
SHOUT = "def shout(text):\n    return text.upper() + '!'\n"
APP = (
    "from adder import add\nfrom shout import shout\n\n\ndef main():\n    print(shout(str(add(2,"
    " 3))))\n"
)
ADDER_TEST = "from adder import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
SHOUT_TEST = "from shout import shout\n\n\ndef test_shout():\n    assert shout('hi') == 'HI!'\n"

memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})


def setUpModule():
    memory_store.start()


def tearDownModule():
    memory_store.stop()


def notes(package):
    return call("write_file", path=f"loom-project/packages/{package}.md", content=f"# {package}\n")


SCAFFOLD = [
    reply(None, call("write_file", path=SCAFFOLD_DOCUMENT, content="# Scaffold\nShared: none.\n")),
    reply("The scaffold is in place."),
]
BUILD_ADDER = [
    reply(
        None,
        call("write_file", path="adder.py", content=ADDER),
        call("write_file", path="tests/test_adder.py", content=ADDER_TEST),
        notes("adder"),
    ),
    reply("adder is built."),
]
BUILD_SHOUT = [
    reply(
        None,
        call("write_file", path="shout.py", content=SHOUT),
        # Not its package's
        call("write_file", path="adder.py", content="broken"),
        call("write_file", path="tests/test_shout.py", content=SHOUT_TEST),
        notes("shout"),
    ),
    # No allow rule covers it, so the builder asks, through the main thread
    reply(None, call("bash", command=f"{PYTHON} shout.py")),
    reply("shout is built."),
]
BUILD_APP = [
    reply(None, call("write_file", path="app.py", content=APP), notes("app")),
    reply("app is built."),
]
INTEGRATION = [
    reply(None, call("bash", command=PYTEST)),
    reply(None, write_doc("building", BUILD_SUMMARY)),
    reply("Integrated: the tests pass."),
]


class RoutedLLM:
    """Stands in for litellm.completion while agents run on threads: each agent gets the
    replies scripted for it, picked by its phase's title in its system prompt."""

    def __init__(self, **routes):
        self.routes = {title.replace("_", " "): list(script) for title, script in routes.items()}
        self.requests = {title: [] for title in self.routes}
        self.threads = {title: set() for title in self.routes}
        self.lock = threading.Lock()
        self.turns = 0

    def __call__(self, **kwargs):
        if not kwargs.get("tools"):
            message = Message(content="Build a package")
            return ModelResponse(choices=[Choices(message=message, finish_reason="stop")])
        system = kwargs["messages"][0]["content"]
        with self.lock:
            route = next(
                (title for title in self.routes if f"# Your phase: 4. {title}\n" in system), None
            )
            if route is None:
                raise AssertionError("No script for this agent:\n" + system[-3000:])
            if not self.routes[route]:
                raise AssertionError(f"The {route} agent asked for more replies than scripted")
            self.requests[route].append(kwargs)
            self.threads[route].add(threading.current_thread().name)
            scripted = self.routes[route].pop(0)
            self.turns += 1
            turn = self.turns
        if kwargs["stream"]:
            return stream_response(scripted, turn)
        return full_response(scripted, turn)

    def left(self):
        return {title: len(script) for title, script in self.routes.items() if script}


def project(io=None, workers=2, architecture=ARCHITECTURE, allow=(f"bash({PYTEST}*)",)):
    """A project with Design approved, so Building runs next."""
    if io is None:
        io = InputOutput(yes=None)
        io.permission_ask = MagicMock(return_value="yes")
    coder = Coder.create(
        Model("gpt-4o-mini"),
        "agent",
        io=io,
        map_tokens=0,
        permissions=Permissions(io, mode="accept-edits", allow=list(allow)),
        project_settings=dict(build_workers=workers),
    )
    orchestrator = Orchestrator(coder)
    orchestrator.new_project(IDEA)
    state = orchestrator.state
    for phase, text in zip(PHASES[:3], ["# Idea report\n**Verdict:** GO\n", PRD, architecture]):
        Path(phase.document).parent.mkdir(exist_ok=True)
        Path(phase.document).write_bytes(text.encode())
        state.start(phase.key)
        state.finish(phase.key)
        state.approve(phase.key)
    state.save()
    # The documents are committed, so the checkout is clean
    coder.repo.repo.git.add("loom-project")
    coder.repo.repo.git.commit("-m", "The documents")
    return coder, orchestrator


def build(orchestrator, llm):
    with patch.object(litellm, "completion", llm):
        return orchestrator.run_phase(PHASES_BY_KEY["building"])


def steps(state):
    return [(run.get("step"), run.get("package")) for run in state.run_log("building")]


class TestParallelBuilding(unittest.TestCase):
    def test_two_builders_then_one_then_integration(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder, orchestrator = project()
            io = orchestrator.io
            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=BUILD_ADDER,
                Building_shout=BUILD_SHOUT,
                Building_app=BUILD_APP,
                Integration=INTEGRATION,
            )
            self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            state = orchestrator.state
            self.assertEqual(state.status("building"), "review")

            # Every package was built in its worktree, merged, and its worktree removed
            packages = state.phase_data("building")["packages"]
            self.assertEqual(
                {pid: r["status"] for pid, r in packages.items()},
                dict(adder="merged", shout="merged", app="merged"),
            )
            self.assertEqual([packages[p]["wave"] for p in ("adder", "shout", "app")], [1, 1, 2])
            self.assertEqual(Path("adder.py").read_text(), ADDER)
            self.assertEqual(Path("shout.py").read_text(), SHOUT)
            self.assertEqual(Path("app.py").read_text(), APP)
            self.assertEqual([p.name for p in Path(WORKTREES_DIR).iterdir()], [".gitignore"])
            self.assertEqual([b.name for b in repo.branches if b.name.startswith("loom/")], [])
            messages = [commit.message.strip() for commit in repo.iter_commits()]
            for pid in ("adder", "shout", "app"):
                self.assertIn(f"Merge the {pid} work package", messages)
            self.assertFalse(repo.is_dirty(untracked_files=False))

            # The runs, in order
            self.assertEqual(
                steps(state),
                [
                    ("scaffold", None),
                    ("package", "adder"),
                    ("package", "shout"),
                    ("package", "app"),
                    ("integration", None),
                ],
            )
            self.assertEqual(state.run_log("building")[-1]["outcome"], "done")

            # The builders ran on their own threads; the scaffold and integration didn't
            for title in ("Building adder", "Building shout", "Building app"):
                self.assertTrue(all(t.startswith("loom-builder") for t in llm.threads[title]))
            self.assertEqual(llm.threads["Scaffold"], {"MainThread"})

            # A builder may only write its package's files
            results = [
                m["content"]
                for m in llm.requests["Building shout"][1]["messages"]
                if m["role"] == "tool"
            ]
            self.assertIn("may only write", results[1])
            system = llm.requests["Building shout"][0]["messages"][0]["content"]
            self.assertIn("builder of one work package of the project: shout, Shouting", system)

            # Its question went to the main thread, which asked the user. It was the only
            # one: the builders' files aren't protected though their worktrees are in .loom/
            asked = [c[0][0] for c in io.permission_ask.call_args_list]
            self.assertEqual(asked, ["[shout] Run this command?"])

            # What they spent is the project's
            total = sum(run["cost"] for run in state.run_log("building"))
            # Each run's cost is rounded to 6 places
            self.assertAlmostEqual(coder.total_cost, total, delta=1e-5)

            # The report lists them
            from loom.project_report import ProjectReport

            report = ProjectReport.from_orchestrator(orchestrator).markdown()
            self.assertIn("**Parallel builders' work packages:**", report)
            self.assertIn("| shout | Shouting | 1 | merged |", report)
            self.assertIn("Run 1, package:", report)

            # /project status shows the packages
            io.tool_output = MagicMock()
            orchestrator.show_status()
            shown = [c[0][0] for c in io.tool_output.call_args_list if c[0]]
            self.assertIn("  Building's work packages (2 builders at once):", shown)
            self.assertTrue(any(line.startswith("    shout  merged") for line in shown))

    def test_builders_write_under_yes_always(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, orchestrator = project(InputOutput(yes=True))
            quiet_shout = [
                reply(
                    None,
                    call("write_file", path="shout.py", content=SHOUT),
                    call("write_file", path="tests/test_shout.py", content=SHOUT_TEST),
                    notes("shout"),
                ),
                reply("shout is built."),
            ]
            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=BUILD_ADDER,
                Building_shout=quiet_shout,
                Building_app=BUILD_APP,
                Integration=INTEGRATION,
            )
            self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            self.assertEqual(Path("shout.py").read_text(), SHOUT)

    def test_a_cancel_stops_every_builder_and_run_picks_up(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            # Committed, so the builders' worktrees have it
            Path("sleep.py").write_bytes(b"import time\ntime.sleep(60)\n")
            repo.git.add("sleep.py")
            repo.git.commit("-m", "A slow command")
            coder, orchestrator = project(allow=(f"bash({PYTEST}*)", f"bash({PYTHON} sleep.py)"))
            io = orchestrator.io

            # ^C while the main thread asks the user about shout's command, once adder's
            # slow command runs
            def interrupt(*args, **kwargs):
                for _ in range(200):
                    if tools.RUNNING:
                        break
                    time.sleep(0.05)
                raise KeyboardInterrupt

            io.permission_ask = MagicMock(side_effect=interrupt)
            sleeping = [reply(None, call("bash", command=f"{PYTHON} sleep.py")), reply("Slept.")]
            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=sleeping,
                Building_shout=[reply(None, call("bash", command=f"{PYTHON} shout.py"))],
            )
            started = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                build(orchestrator, llm)
            state = orchestrator.state
            packages = state.phase_data("building")["packages"]
            self.assertEqual(packages["adder"]["status"], "stopped")
            self.assertEqual(packages["shout"]["status"], "stopped")
            # adder's sleep was killed, not waited for: the test took seconds, not a minute
            self.assertLess(time.monotonic() - started, 50)
            # As /project run's handler of the interrupt does
            self.assertEqual(state.status("building"), "running")
            state.stop("building", "interrupted")
            state.save()

            # /project run builds them again, without the scaffold
            io.permission_ask = MagicMock(return_value="yes")
            llm = RoutedLLM(
                Building_adder=BUILD_ADDER,
                Building_shout=BUILD_SHOUT,
                Building_app=BUILD_APP,
                Integration=INTEGRATION,
            )
            orchestrator = Orchestrator(coder)
            self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            self.assertEqual(orchestrator.state.status("building"), "review")

    def test_resume_after_a_crash(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, orchestrator = project()
            # shout's builder never writes its notes, so its package fails, and Building
            # stops with adder built but not merged
            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=BUILD_ADDER,
                Building_shout=[reply("I'll do it later."), reply("Later.")],
            )
            self.assertFalse(build(orchestrator, llm))
            state = ProjectState.load(".")
            packages = state.phase_data("building")["packages"]
            self.assertEqual(packages["adder"]["status"], "built")
            self.assertEqual(packages["shout"]["status"], "failed")
            self.assertIn("didn't write", packages["shout"]["error"])
            self.assertEqual(state.status("building"), "pending")

            # Then loom died while app was being built
            state.phase_data("building")["packages"]["shout"]["status"] = "running"
            state.save()

            llm = RoutedLLM(
                Building_shout=BUILD_SHOUT,
                Building_app=BUILD_APP,
                Integration=INTEGRATION,
            )
            orchestrator = Orchestrator(coder)
            self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            # Neither the scaffold nor adder ran again; adder was merged as it was built
            self.assertNotIn("Scaffold", llm.requests)
            self.assertEqual(Path("adder.py").read_text(), ADDER)
            self.assertEqual(
                [s for s in steps(orchestrator.state) if s[0] != "package" or s[1] != "adder"][-3:],
                [("package", "shout"), ("package", "app"), ("integration", None)],
            )

    def test_failing_tests_rebuild_only_their_packages(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, orchestrator = project()
            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=BUILD_ADDER,
                Building_shout=BUILD_SHOUT,
                Building_app=BUILD_APP,
                Integration=INTEGRATION,
            )
            self.assertTrue(build(orchestrator, llm))
            state = orchestrator.state
            state.approve("building")
            testing = PHASES_BY_KEY["testing"]
            Path(testing.document).write_bytes(
                b"# Test report\n**Result:** FAIL\ntest_shout fails: shout.py:2 drops the '!'.\n"
            )
            state.start("testing")
            state.finish("testing", "FAIL")
            state.back("building", phase_prompts.fix_test_failures, attach=["testing"])
            state.save()

            llm = RoutedLLM(Building_shout=BUILD_SHOUT, Integration=INTEGRATION)
            self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            packages = orchestrator.state.phase_data("building")["packages"]
            self.assertEqual({r["status"] for r in packages.values()}, {"merged"})
            # The builder got the test report
            first = [
                m["content"]
                for m in llm.requests["Building shout"][0]["messages"]
                if m["role"] == "user"
            ][0]
            self.assertIn("says the tests FAIL", first)

    def test_a_conflict_goes_to_the_integrator(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder, orchestrator = project()
            original = ParallelBuilding.merge_wave

            def merge_wave(self, ids):
                if "adder" in ids:
                    # Someone changed adder.py in the project meanwhile
                    Path("adder.py").write_bytes(b"def add(a, b):\n    return sum([a, b])\n")
                    repo.git.add("adder.py")
                    repo.git.commit("-m", "Add adder.py another way")
                return original(self, ids)

            resolved = "def add(a, b):\n    return a + b  # both agree\n"
            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=BUILD_ADDER,
                Building_shout=BUILD_SHOUT,
                Integrator=[
                    reply(None, call("write_file", path="adder.py", content=resolved)),
                    reply("Resolved."),
                ],
                Building_app=BUILD_APP,
                Integration=INTEGRATION,
            )
            with patch.object(ParallelBuilding, "merge_wave", merge_wave):
                self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            self.assertEqual(Path("adder.py").read_text(), resolved)
            task = [
                m["content"]
                for m in llm.requests["Integrator"][0]["messages"]
                if m["role"] == "user"
            ]
            self.assertIn("these files conflict: adder.py", task[0])
            merge = [r for r in orchestrator.state.run_log("building") if r.get("step") == "merge"]
            self.assertEqual(merge[0]["conflicts"], ["adder.py"])
            self.assertEqual(merge[0]["outcome"], "done")
            merged = repo.head.commit
            self.assertFalse(repo.is_dirty(untracked_files=False))
            self.assertIn(
                "Merge the adder work package", "\n".join(c.message for c in repo.iter_commits())
            )
            self.assertTrue(merged)

    def test_an_unresolved_conflict_stops_building(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder, orchestrator = project()
            original = ParallelBuilding.merge_wave

            def merge_wave(self, ids):
                Path("adder.py").write_bytes(b"x = 1\n")
                repo.git.add("adder.py")
                repo.git.commit("-m", "Clash")
                return original(self, ids)

            llm = RoutedLLM(
                Scaffold=SCAFFOLD,
                Building_adder=BUILD_ADDER,
                Building_shout=BUILD_SHOUT,
                Integrator=[reply("I can't decide.")],
            )
            with patch.object(ParallelBuilding, "merge_wave", merge_wave):
                self.assertFalse(build(orchestrator, llm))
            state = orchestrator.state
            self.assertEqual(state.status("building"), "pending")
            self.assertEqual(state.phase_data("building")["packages"]["adder"]["status"], "built")
            # The merge was undone
            self.assertEqual(Path("adder.py").read_bytes().replace(b"\r\n", b"\n"), b"x = 1\n")
            self.assertFalse((Path(repo.git_dir) / "MERGE_HEAD").exists())

    def test_test_driven_builders(self):
        with GitTemporaryDirectory():
            make_repo()
            architecture = ARCHITECTURE.replace(
                "  acceptance: [FR-1]\n",
                (
                    f"  acceptance: [FR-1]\n  test_command: '{PYTHON} -B -m pytest -q -p"
                    " no:cacheprovider tests/test_adder.py'\n"
                ),
            )
            coder, orchestrator = project(architecture=architecture)
            # Approve the acceptance tests
            orchestrator.io.choice_ask = MagicMock(return_value="approve")
            orchestrator.state.data["tdd"] = True
            orchestrator.state.save()
            spec = [
                reply(
                    None,
                    call("write_file", path="tests/test_adder.py", content=ADDER_TEST),
                    call("write_file", path="tests/test_shout.py", content=SHOUT_TEST),
                    call(
                        "write_file",
                        path="loom-project/4a-acceptance-tests.md",
                        content="# Acceptance tests\n",
                    ),
                ),
                reply("Written."),
            ]
            adder_twice = [
                reply(
                    None,
                    call(
                        "write_file", path="adder.py", content="def add(a, b):\n    return a - b\n"
                    ),
                    notes("adder"),
                ),
                reply("Done."),
                reply(None, call("write_file", path="adder.py", content=ADDER)),
                reply("Fixed."),
            ]
            shout = [
                reply(None, call("write_file", path="shout.py", content=SHOUT), notes("shout")),
                reply("Done."),
            ]
            llm = RoutedLLM(
                Acceptance_tests=spec,
                Scaffold=SCAFFOLD,
                Building_adder=adder_twice,
                Building_shout=shout,
                Building_app=BUILD_APP,
                Integration=[reply(None, write_doc("building", BUILD_SUMMARY)), reply("Done.")],
            )
            self.assertTrue(build(orchestrator, llm))
            self.assertEqual(llm.left(), {})
            log = orchestrator.state.run_log("building")
            adder = next(r for r in log if r.get("package") == "adder")
            self.assertEqual([a["passed"] for a in adder["attempts"]], [False, True])
            # shout has no test command of its own: one run
            shout_run = next(r for r in log if r.get("package") == "shout")
            self.assertEqual(shout_run["attempts"], [])
            # The integration ran the whole suite, on the locked tests
            integration = log[-1]
            self.assertEqual(integration["step"], "integration")
            self.assertEqual([a["passed"] for a in integration["attempts"]], [True])
            packages = orchestrator.state.phase_data("building")["packages"]
            self.assertEqual(packages["adder"]["attempts"], 2)

            from loom.project_report import ProjectReport

            report = ProjectReport.from_orchestrator(orchestrator).markdown()
            self.assertIn("**Test-driven Building, run 1, the adder package:**", report)
            self.assertIn("**Test-driven Building, run 1, integration:**", report)
            # The template-free project's checks aren't there; Building's are Building's
            self.assertNotIn("after Integration", report)

    def test_serial_fallback(self):
        for workers, architecture, why in [
            (1, ARCHITECTURE, None),
            (2, ARCHITECTURE.split("## Work packages")[0], None),
            (2, ARCHITECTURE.replace('owns: ["app.py"]', 'owns: ["adder.py"]'), "problems"),
        ]:
            with GitTemporaryDirectory():
                make_repo()
                io = InputOutput(yes=True)
                io.tool_warning = MagicMock()
                coder, orchestrator = project(io, workers=workers, architecture=architecture)
                llm = RoutedLLM(
                    Building=[
                        reply(None, call("write_file", path="adder.py", content=ADDER)),
                        reply(None, write_doc("building", BUILD_SUMMARY)),
                        reply("Built."),
                    ]
                )
                self.assertTrue(build(orchestrator, llm))
                self.assertEqual(steps(orchestrator.state), [(None, None)])
                self.assertNotIn("packages", orchestrator.state.phase_data("building"))
                warnings = " ".join(c[0][0] for c in io.tool_warning.call_args_list)
                if why:
                    self.assertIn("work plan has problems", warnings)

    def test_uncommitted_changes_build_serially(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            coder, orchestrator = project(io)
            Path("calc.py").write_bytes(b"# my change\n")
            self.assertFalse(ParallelBuilding(orchestrator).ready())
            self.assertIn("uncommitted changes", io.tool_warning.call_args[0][0])

    def test_workers_command_and_going_back(self):
        from loom.commands import Commands

        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_output = MagicMock()
            io.tool_error = MagicMock()
            coder, orchestrator = project(io)
            commands = Commands(io, coder)
            commands.cmd_project("workers 4")
            self.assertIn("up to 4 builders at once", io.tool_output.call_args[0][0])
            self.assertEqual(ProjectState.load(".").data["workers"], 4)
            commands.cmd_project("workers 0")
            self.assertIn("from 1 to 16", io.tool_error.call_args[0][0])
            commands.cmd_project("workers lots")
            self.assertIn("/project workers N", io.tool_error.call_args[0][0])

            # Going back before Building starts parallel Building over
            state = ProjectState.load(".")
            state.phase_data("building")["packages"] = dict(adder=dict(status="merged"))
            state.phase_data("building")["parallel"] = dict(scaffold="abc")
            state.save()
            commands.cmd_project("back design")
            state = ProjectState.load(".")
            self.assertNotIn("packages", state.phase_data("building"))
            self.assertNotIn("parallel", state.phase_data("building"))


if __name__ == "__main__":
    unittest.main()

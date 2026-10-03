import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom.commands import Commands, parse_new_project
from loom.hooks import approvals_file
from loom.io import InputOutput
from loom.llm import litellm
from loom.orchestrator import Orchestrator, ProjectState
from loom.phases import PHASES, PHASES_BY_KEY
from loom.project_report import ProjectReport
from loom.project_templates import (
    TemplateError,
    copy_skeleton,
    find_templates,
    load_template,
)
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory

from .test_agent import FakeLLM, make_repo, reply
from .test_orchestrator import (
    IDEA,
    IDEA_REPORT,
    SCRIPT_IDEA,
    SCRIPT_PLANNING,
    make_coder,
    phase_of,
    script_building,
    user_messages,
    write_doc,
)

PYTHON = sys.executable
BUILTINS = {"fastapi-react", "ml-pipeline", "python-cli"}


def write_template(folder, text, skeleton=None):
    """A template in folder, its template.yml text and {path: bytes} skeleton files."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "template.yml").write_bytes(text.encode())
    for path, data in (skeleton or {}).items():
        target = folder / "skeleton" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return folder


# Single-quoted, so the backslashes of a Windows path to Python aren't YAML escapes
CHECKED = f"""
name: checked
description: A template whose checks pass and fail
test_command: '{PYTHON} -m pytest -q'
decisions: ["Write it in Python"]
briefs:
  idea: "Mind the users who live offline."
  testing: "Add property tests."
checks:
  idea: ['{PYTHON} pass.py', '{PYTHON} fail.py']
writable:
  testing: ["docs/**"]
"""


class HomeTestCase(unittest.TestCase):
    """A test with its own home folder, for ~/.loom/templates and the approvals."""

    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()
        self.home = IgnorantTemporaryDirectory()
        self.home_patcher = patch("pathlib.Path.home", return_value=Path(self.home.name))
        self.home_patcher.start()

    def tearDown(self):
        self.home_patcher.stop()
        self.home.cleanup()
        self.memory_store.stop()

    @property
    def user_templates(self):
        return Path(self.home.name) / ".loom" / "templates"


class TestLoading(HomeTestCase):
    def test_the_built_in_templates(self):
        with IgnorantTemporaryDirectory() as root:
            templates = find_templates(root)
            self.assertEqual(set(templates), BUILTINS)
            for name, template in templates.items():
                self.assertEqual(template.source, "built-in")
                self.assertTrue(template.description)
                self.assertTrue(template.test_command)
                self.assertTrue(template.decisions)
                # Every phase from Design on gets a brief, and the code a skeleton
                for key in ("design", "building", "testing", "launch"):
                    self.assertTrue(template.brief(key), f"{name} has no {key} brief")
                self.assertTrue(template.skeleton_files(), name)
                rels = [rel for rel, _ in template.skeleton_files()]
                self.assertIn(".gitignore", rels)
                self.assertNotIn("gitignore", rels)
                self.assertEqual(len(template.hash()), 64)

            fastapi = templates["fastapi-react"]
            self.assertIn("npm --prefix web test", fastapi.test_command)
            self.assertEqual(fastapi.writable_for("launch"), ["fly.toml"])
            self.assertEqual(fastapi.checks_for("launch"), ["docker build -t app ."])

    def test_later_places_override_earlier_ones(self):
        with IgnorantTemporaryDirectory() as root:
            write_template(self.user_templates / "python-cli", "description: Mine\n")
            write_template(self.user_templates / "mine", "description: Only mine\ntdd: true\n")
            self.assertEqual(find_templates(root)["python-cli"].source, "user")
            self.assertEqual(find_templates(root)["python-cli"].description, "Mine")
            self.assertTrue(find_templates(root)["mine"].tdd)

            write_template(
                Path(root, ".loom/templates/cli"), "name: python-cli\ndescription: Ours\n"
            )
            template = load_template(root, "python-cli")
            self.assertEqual((template.source, template.description), ("project", "Ours"))
            self.assertFalse(template.trusted)
            self.assertTrue(load_template(root, "mine").trusted)

            with self.assertRaises(TemplateError) as raised:
                load_template(root, "rails")
            self.assertIn("python-cli", str(raised.exception))

    def test_invalid_templates_are_skipped(self):
        with IgnorantTemporaryDirectory() as root:
            for name, text, problem in [
                ("bad-yaml", "description: [unclosed\n", "Unable to read"),
                ("bad-field", "summary: not a field\n", "unknown fields: summary"),
                ("bad-phase", "briefs: {deploy: x}\n", "unknown phase 'deploy'"),
                ("bad-checks", "checks: {building: 3}\n", "must be a list of strings"),
                ("bad-tdd", "tdd: maybe\n", "tdd must be true or false"),
                ("Bad Name", "description: x\n", "name must be lowercase"),
            ]:
                write_template(self.user_templates / name, text)
                warn = MagicMock()
                templates = find_templates(root, warn)
                self.assertNotIn(name, templates)
                self.assertIn(problem, warn.call_args[0][0])
                (self.user_templates / name / "template.yml").unlink()

    def test_parse_new_project(self):
        self.assertEqual(
            parse_new_project("--template python-cli --tdd A tool"), ("python-cli", True, "A tool")
        )
        self.assertEqual(parse_new_project("--tdd --template=x  An idea"), ("x", True, "An idea"))
        self.assertEqual(parse_new_project("An idea --tdd"), (None, False, "An idea --tdd"))
        self.assertEqual(parse_new_project("--template"), (None, False, "--template"))


class TestSkeleton(HomeTestCase):
    def test_copy_keeps_the_projects_files(self):
        with IgnorantTemporaryDirectory() as root:
            template = write_template(
                self.user_templates / "t",
                "description: t\n",
                {
                    "src/app.py": b"print('hi')\n",
                    "README.md": b"# Skeleton\n",
                    "gitignore": b"__pycache__/\n.loom*\n",
                },
            )
            template = load_template(root, "t")
            Path(root, "README.md").write_bytes(b"# Mine\n")
            Path(root, ".gitignore").write_bytes(b".loom*\n!.loom/")
            written, skipped = copy_skeleton(template, root)
            self.assertEqual(written, [".gitignore", "src/app.py"])
            self.assertEqual(skipped, ["README.md"])
            self.assertEqual(Path(root, "README.md").read_bytes(), b"# Mine\n")
            # The skeleton's ignore lines join the project's
            self.assertEqual(
                Path(root, ".gitignore").read_bytes(), b".loom*\n!.loom/\n__pycache__/\n"
            )
            self.assertEqual(
                copy_skeleton(template, root), ([], [".gitignore", "README.md", "src/app.py"])
            )

    def test_building_starts_from_the_skeleton(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder = make_coder()
            orchestrator = Orchestrator(coder)
            template = load_template(".", "python-cli")
            orchestrator.new_project(IDEA, template)
            state = orchestrator.state
            for phase in PHASES[:3]:
                state.start(phase.key)
                state.finish(phase.key)
                state.approve(phase.key)

            llm = FakeLLM(*script_building("def add(a, b):\n    return a + b\n"))
            with patch.object(litellm, "completion", llm):
                self.assertTrue(orchestrator.run_phase(PHASES_BY_KEY["building"]))

            self.assertTrue(Path("src/app/cli.py").exists())
            self.assertTrue(Path("pyproject.toml").exists())
            messages = [commit.message for commit in repo.iter_commits()]
            self.assertIn("Add the python-cli template's skeleton", "\n".join(messages))
            # It was committed before the agent's work
            self.assertFalse(repo.is_dirty(untracked_files=False))
            self.assertTrue(state.template_info["skeleton"])

            # The Building agent's brief has the template's
            system = llm.requests[0]["messages"][0]["content"]
            self.assertIn("## The project template: python-cli", system)
            self.assertIn("Rename the package app", system)

            # Only once: a second run of Building doesn't copy it again
            Path("src/app/cli.py").unlink()
            state.finish = MagicMock()
            orchestrator.apply_skeleton()
            self.assertFalse(Path("src/app/cli.py").exists())


class TestTemplateProject(HomeTestCase):
    def make_project(self, io=None, source="user"):
        folder = (
            self.user_templates / "checked" if source == "user" else Path(".loom/templates/checked")
        )
        write_template(folder, CHECKED)
        Path("pass.py").write_bytes(b"print('all good')\n")
        Path("fail.py").write_bytes(b"import sys\nprint('3 problems')\nsys.exit(1)\n")
        coder = make_coder(io)
        orchestrator = Orchestrator(coder)
        orchestrator.new_project(IDEA, load_template(".", "checked"))
        return coder, orchestrator

    def test_decisions_briefs_and_test_command(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, orchestrator = self.make_project()
            state = ProjectState.load(".")
            self.assertEqual(state.template_info["name"], "checked")
            self.assertEqual(state.template_info["source"], "user")
            self.assertEqual(len(state.template_info["hash"]), 64)
            self.assertFalse(state.tdd)

            [decision] = orchestrator.memory.decisions()
            self.assertEqual(
                (decision["phase"], decision["source"], decision["text"]),
                (None, "template", "Write it in Python"),
            )
            self.assertEqual(orchestrator.test_command(), f"{PYTHON} -m pytest -q")

            idea = orchestrator.make_agent(PHASES_BY_KEY["idea"])
            system = idea.format_messages().all_messages()[0]["content"]
            self.assertIn("## The project template: checked\n\nMind the users", system)
            self.assertIn(
                "- Project, template: Write it in Python",
                orchestrator.task_message(PHASES_BY_KEY["idea"]),
            )
            design = orchestrator.make_agent(PHASES_BY_KEY["design"])
            system = design.format_messages().all_messages()[0]["content"]
            self.assertIn(f"The template runs the tests with `{PYTHON} -m pytest -q`", system)

            # Its writable files are the phase's too
            testing = orchestrator.make_agent(PHASES_BY_KEY["testing"])
            for path, ok in [("docs/plan.md", True), ("tests/test_x.py", True), ("app.py", False)]:
                action = MagicMock(kind="edit", inside=True, target=path)
                self.assertEqual(testing.refuse_action("write_file", action) is None, ok, path)
            self.assertIn("- docs/**", testing.phase_brief())

    def test_checks_pass_and_fail(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            io.tool_output = MagicMock()
            coder, orchestrator = self.make_project(io)
            # A failing check warns, and the project carries on to Planning
            llm = FakeLLM(*SCRIPT_IDEA, *SCRIPT_PLANNING, reply("No design."), reply("None."))
            with patch.object(litellm, "completion", llm):
                orchestrator.run()
            state = orchestrator.state
            self.assertEqual(state.status("idea"), "approved")
            self.assertEqual(state.status("planning"), "approved")

            checks = state.phase_data("idea")["checks"]
            self.assertEqual([c["passed"] for c in checks], [True, False])
            self.assertIn("3 problems", checks[1]["output"])
            self.assertEqual(
                state.run_log("idea")[-1]["checks"],
                [
                    dict(command=f"{PYTHON} pass.py", passed=True),
                    dict(command=f"{PYTHON} fail.py", passed=False),
                ],
            )
            warnings = [c[0][0] for c in io.tool_warning.call_args_list]
            self.assertTrue(
                any("1 of the checked template's 2 checks failed" in w for w in warnings)
            )

            # Shown at the checkpoint
            shown = [c[0][0] for c in io.tool_output.call_args_list if c[0]]
            self.assertIn(f"  ✗ {PYTHON} fail.py", shown)

            # Saved to the memory, where the next agents see them
            check = [d for d in orchestrator.memory.decisions() if d["kind"] == "check"][0]
            self.assertEqual(check["phase"], "idea")
            self.assertIn(f"`{PYTHON} fail.py` failed (exit code: 1)", check["text"])
            planning = next(user_messages(r)[0] for r in llm.requests if phase_of(r) == "planning")
            self.assertIn("The template's checks after Idea Check", planning)

            # And in the report
            report = ProjectReport.from_orchestrator(orchestrator).markdown()
            self.assertIn("- **Template:** checked (user)", report)
            self.assertIn(
                f"**Template checks:** ✓ `{PYTHON} pass.py`, ✗ `{PYTHON} fail.py`", report
            )
            self.assertIn("**Template checks**", report)

    def test_a_projects_own_template_needs_approval(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            io.tool_warning = MagicMock()
            coder, orchestrator = self.make_project(io, source="project")
            idea = PHASES_BY_KEY["idea"]

            self.assertEqual(orchestrator.run_checks(idea), [])
            question = io.permission_ask.call_args
            self.assertIn("Run the checks of this project's checked template?", question[0][0])
            self.assertIn(f"idea: {PYTHON} fail.py", question[1]["subject"])
            self.assertTrue(question[1]["explicit_yes_required"])
            self.assertIn("weren't approved", io.tool_warning.call_args[0][0])
            # Asked once a session
            orchestrator.run_checks(idea)
            io.permission_ask.assert_called_once()

            # Always: remembered with the project hooks' approvals
            coder.template_checks_approved = {}
            io.permission_ask = MagicMock(return_value="always")
            self.assertEqual(len(orchestrator.run_checks(idea)), 2)
            saved = json.loads(approvals_file().read_text())
            self.assertEqual(len(saved), 1)
            self.assertIn(":checked", list(saved)[0])

            coder.template_checks_approved = {}
            io.permission_ask = MagicMock()
            self.assertEqual(len(orchestrator.run_checks(idea)), 2)
            io.permission_ask.assert_not_called()

            # Changing the checks needs approval again
            text = CHECKED.replace("fail.py", "pass.py")
            write_template(Path(".loom/templates/checked"), text)
            orchestrator.loaded_template = None
            io.permission_ask = MagicMock(return_value="no")
            coder.template_checks_approved = {}
            self.assertEqual(orchestrator.run_checks(idea), [])
            io.permission_ask.assert_called_once()
            warnings = [c[0][0] for c in io.tool_warning.call_args_list]
            self.assertTrue(any("has changed since the project started" in w for w in warnings))

    def test_yes_always_doesnt_approve_a_projects_checks(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            coder, orchestrator = self.make_project(io, source="project")
            self.assertEqual(orchestrator.run_checks(PHASES_BY_KEY["idea"]), [])
            self.assertIn("weren't approved", io.tool_warning.call_args[0][0])


class TestTemplateCommands(HomeTestCase):
    def test_new_with_a_template_and_templates(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_output = MagicMock()
            io.tool_error = MagicMock()
            coder = make_coder(io)
            commands = Commands(io, coder)

            commands.cmd_project("templates")
            shown = [c[0][0] for c in io.tool_output.call_args_list if c[0]]
            listed = [line.split()[0] for line in shown if line.startswith("  ")]
            self.assertEqual(listed, sorted(BUILTINS))
            self.assertTrue(any("built-in" in line for line in shown))

            commands.cmd_project(f"new --template rails {IDEA}")
            self.assertIn("There is no template 'rails'", io.tool_error.call_args[0][0])
            self.assertIsNone(ProjectState.load("."))

            llm = FakeLLM(
                reply(None, write_doc("idea", IDEA_REPORT)),
                reply("GO."),
                reply("No PRD."),
                reply("Still none."),
            )
            with patch.object(litellm, "completion", llm):
                commands.cmd_project(f"new --template python-cli --tdd {IDEA}")
            state = ProjectState.load(".")
            self.assertEqual(state.idea, IDEA)
            self.assertEqual(state.template_info["name"], "python-cli")
            self.assertTrue(state.tdd)
            self.assertEqual(state.status("idea"), "approved")

            io.tool_output.reset_mock()
            commands.cmd_project("status")
            shown = [c[0][0] for c in io.tool_output.call_args_list if c[0]]
            self.assertIn("  From the python-cli template, test-driven Building", shown)


if __name__ == "__main__":
    unittest.main()

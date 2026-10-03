import os
import unittest
from unittest.mock import MagicMock, patch

from loom.io import InputOutput
from loom.orchestrator import Orchestrator
from loom.phases import PHASES_BY_KEY
from loom.utils import GitTemporaryDirectory
from loom.workplan import find_cycle, may_overlap, read_workplan, waves

from .test_agent import make_repo
from .test_orchestrator import ARCHITECTURE, IDEA, make_coder

PLAN = """# Architecture: converter
## Build order
1. The core, then the CLI and the web page.
## Work packages
Three packages.

```yaml
- id: core
  title: Conversion functions
  owns: ["src/app/convert.py"]
  tests: ["tests/test_convert.py"]
  acceptance: [FR-1, FR-2]
  test_command: "python -m pytest -q tests/test_convert.py"
- id: cli
  title: The command line
  owns: ["src/app/cli.py"]
  tests: ["tests/test_cli.py"]
  depends_on: [core]
  acceptance: [FR-3]
- id: web
  title: The web page
  owns: ["web/**"]
  depends_on: [core]
```

## Decisions and trade-offs
None.
"""


def plan_with(yaml_text):
    return read_workplan(f"# Architecture\n## Work packages\n```yaml\n{yaml_text}\n```\n")


class TestWorkPlan(unittest.TestCase):
    def test_a_valid_plan(self):
        plan = read_workplan(PLAN)
        self.assertTrue(plan.valid, plan.problems)
        self.assertEqual(plan.ids, ["core", "cli", "web"])
        core = plan.package("core")
        self.assertEqual(core.writable, ("src/app/convert.py", "tests/test_convert.py"))
        self.assertEqual(core.acceptance, ("FR-1", "FR-2"))
        self.assertEqual(core.test_command, "python -m pytest -q tests/test_convert.py")
        self.assertEqual(plan.package("web").depends_on, ("core",))
        self.assertIsNone(plan.package("web").test_command)
        self.assertEqual(plan.waves, [["core"], ["cli", "web"]])
        self.assertEqual(plan.describe(), "3 packages in 2 waves: core → cli; web")
        self.assertEqual(plan.owner("web/src/App.tsx").id, "web")
        self.assertEqual(plan.owner("tests/test_cli.py").id, "cli")
        self.assertIsNone(plan.owner("pyproject.toml"))

    def test_no_section_or_no_yaml(self):
        self.assertIsNone(read_workplan(ARCHITECTURE))
        self.assertIsNone(read_workplan(""))
        plan = read_workplan("## Work packages\nJust one: everything.\n## Next\n```yaml\n- x\n```")
        self.assertEqual(plan.problems, ["The Work packages section has no YAML block."])
        self.assertFalse(plan.valid)
        plan = plan_with("- id: [unclosed")
        self.assertIn("aren't valid YAML", plan.problems[0])
        self.assertIn("must be a YAML list", plan_with("id: core").problems[0])
        # A mapping with a packages list is fine too
        self.assertTrue(plan_with("packages:\n  - {id: a, owns: [a.py]}").valid)

    def test_bad_packages(self):
        for yaml_text, problem in [
            ("- {id: Core, owns: [a.py]}", "needs an id"),
            ("- {id: core}", "core owns no files"),
            ("- {id: core, owns: [a.py], owner: me}", "unknown fields: owner"),
            ("- {id: core, owns: {a: b}}", "owns must be a list of strings"),
            ("- {id: a, owns: [a.py]}\n- {id: a, owns: [b.py]}", "Two packages are called a"),
            ("- {id: a, owns: [a.py], depends_on: [b]}", "depends on b, which isn't one"),
            ("- {id: a, owns: [a.py], depends_on: [a]}", "depends on itself"),
            ("- {id: a, owns: [a.py], test_command: 3}", "test_command must be text"),
            ("- just a string", "isn't a mapping"),
        ]:
            plan = plan_with(yaml_text)
            self.assertFalse(plan.valid, yaml_text)
            self.assertTrue(any(problem in p for p in plan.problems), (yaml_text, plan.problems))

    def test_cycles(self):
        plan = plan_with(
            "- {id: a, owns: [a.py], depends_on: [c]}\n"
            "- {id: b, owns: [b.py], depends_on: [a]}\n"
            "- {id: c, owns: [c.py], depends_on: [b]}\n"
        )
        self.assertIn("in a cycle: a → c → b → a", plan.problems[0])
        self.assertEqual(plan.waves, [])

    def test_waves(self):
        class P:
            def __init__(self, id, *deps):
                self.id = id
                self.depends_on = deps

        found = waves([P("d", "b", "c"), P("a"), P("b", "a"), P("c", "a"), P("e")])
        self.assertEqual(found, [["a", "e"], ["b", "c"], ["d"]])
        self.assertIsNone(find_cycle([P("a"), P("b", "a")]))

    def test_too_many_packages(self):
        text = "\n".join(f"- {{id: p{n}, owns: [p{n}.py]}}" for n in range(7))
        plan = read_workplan(f"## Work packages\n```\n{text}\n```", max_packages=6)
        self.assertIn("7 packages are more than the 6", plan.problems[0])

    def test_overlapping_globs(self):
        for a, b, overlap in [
            ("src/a.py", "src/a.py", True),
            ("src/a.py", "src/b.py", False),
            ("src/app/convert.py", "src/app/*.py", True),
            ("src/**", "src/api/**", True),
            ("api/**", "web/**", False),
            ("src/api/**", "src/**/*.py", True),
            ("src/**/*.py", "src/**/*.ts", False),
            ("tests/**", "**/*.py", True),
            ("src/app/*.py", "src/**/test_*.py", True),
            ("src/app/cli.py", "src/**/test_*.py", False),
            ("web/{src,public}/**", "web/src/App.tsx", True),
            ("web/*.json", "web/src/**", False),
        ]:
            self.assertEqual(may_overlap(a, b), overlap, (a, b))
            self.assertEqual(may_overlap(b, a), overlap, (b, a))

        plan = plan_with("- {id: a, owns: [src/**]}\n- {id: b, owns: [src/b.py]}")
        self.assertIn("Packages a and b could both own the same files", plan.problems[0])

    def test_existing_files_owned_twice(self):
        text = "- {id: a, owns: ['src/[ab]*.py']}\n- {id: b, owns: ['src/b*']}"
        doc = f"## Work packages\n```yaml\n{text}\n```"
        # The globs alone look apart, but a file of the project matches both
        self.assertTrue(read_workplan(doc).valid)
        plan = read_workplan(doc, files=["src/b.py", "README.md"])
        self.assertIn("Packages a and b both own src/b.py", plan.problems[0])


class TestDesignCheckpoint(unittest.TestCase):
    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()

    def tearDown(self):
        self.memory_store.stop()

    def test_shows_the_plan_or_its_problems(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_output = MagicMock()
            io.tool_warning = MagicMock()
            coder = make_coder(io)
            coder.project_settings = dict(build_workers=2)
            orchestrator = Orchestrator(coder)
            orchestrator.new_project(IDEA)
            design = PHASES_BY_KEY["design"]

            orchestrator.show_review(design, PLAN, None)
            shown = [c[0][0] for c in io.tool_output.call_args_list if c[0]]
            self.assertIn(
                "Work plan: 3 packages in 2 waves: core → cli; web. Builders: 2, at once.", shown
            )

            bad = PLAN.replace('owns: ["web/**"]', 'owns: ["src/**"]')
            orchestrator.show_review(design, bad, None)
            warnings = [c[0][0] for c in io.tool_warning.call_args_list]
            self.assertIn(
                (
                    "The work plan has problems, so Building will run as one agent unless you fix"
                    " them:"
                ),
                warnings,
            )
            self.assertTrue(any("Packages core and web could both own" in w for w in warnings))

            # /project workers sets how many build at once, and so the most packages
            orchestrator.state.data["workers"] = 1
            self.assertEqual(orchestrator.workers, 1)
            self.assertTrue(orchestrator.work_plan(PLAN).valid)
            more = PLAN.replace(
                "```\n\n## Decisions", "- {id: docs, owns: [docs/**]}\n```\n\n## Decisions"
            )
            self.assertIn(
                "4 packages are more than the 3", orchestrator.work_plan(more).problems[0]
            )


if __name__ == "__main__":
    unittest.main()

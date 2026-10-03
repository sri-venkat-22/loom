import os
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom import __version__, project_report
from loom.commands import Commands
from loom.io import InputOutput
from loom.orchestrator import Orchestrator
from loom.project_report import (
    ProjectReport,
    ReportError,
    export,
    html_without_pandoc,
    pandoc_version,
    pdf_engine,
    shift_headings,
)
from loom.utils import GitTemporaryDirectory

from .test_agent import call, make_repo, reply
from .test_orchestrator import (
    BAD_ADDER,
    BUILD_SUMMARY,
    GOOD_ADDER,
    IDEA,
    PYTEST,
    SCRIPT_DESIGN,
    SCRIPT_IDEA,
    SCRIPT_LAUNCH,
    SCRIPT_PLANNING,
    TEST_FAIL,
    TEST_PASS,
    make_coder,
    run_project,
    script_building,
    script_testing,
    write_doc,
)

HAS_PANDOC = pandoc_version() is not None

memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})


def setUpModule():
    memory_store.start()


def tearDownModule():
    memory_store.stop()


FIX = [
    reply(
        None,
        call("write_file", path="adder.py", content=GOOD_ADDER),
        write_doc("building", BUILD_SUMMARY + "Fixed add.\n"),
    ),
    reply("Fixed."),
]
RETEST = [
    reply(None, call("bash", command=PYTEST)),
    reply(None, write_doc("testing", TEST_PASS)),
    reply("Passes now."),
]


def run_fixture_project():
    """A complete project whose tests failed once and went back to Building."""
    coder, orchestrator, llm, done = run_project(
        SCRIPT_IDEA,
        SCRIPT_PLANNING,
        SCRIPT_DESIGN,
        script_building(BAD_ADDER),
        script_testing(TEST_FAIL),
        FIX,
        RETEST,
        SCRIPT_LAUNCH,
    )
    assert done
    orchestrator.record("Ship it as a pip package first")
    return orchestrator


class FakeModel:
    name = "weak-model"

    def __init__(self, summary):
        self.summary = summary
        self.messages = None

    def simple_send_with_retries(self, messages):
        self.messages = messages
        return self.summary


class TestMarkdown(unittest.TestCase):
    def test_the_report_of_a_project(self):
        with GitTemporaryDirectory():
            make_repo()
            orchestrator = run_fixture_project()
            report = ProjectReport.from_orchestrator(orchestrator)
            text = report.markdown()

            # The title block, and the cover
            self.assertTrue(text.startswith(f'---\ntitle: "Project report: {IDEA}"\n'), text[:200])
            self.assertIn(f"> {IDEA}", text)
            self.assertIn("- **Model:** gpt-4o-mini", text)
            self.assertIn(f"- **loom:** {__version__}", text)
            self.assertIn("- **Finished:** ", text)
            self.assertNotIn("Not yet", text)
            self.assertIn("- **Effort:** 8 runs, ", text)

            # The timeline: Building ran twice, after one round of fixes
            self.assertIn(
                "| # | Phase | Status | Verdict | Runs | Time | Cost | Fix rounds |", text
            )
            lines = text.splitlines()
            building = next(line for line in lines if line.startswith("| 4 | Building |"))
            self.assertIn("| approved |  | 2 |", building)
            self.assertTrue(building.endswith("| 1 |"), building)
            testing = next(line for line in lines if line.startswith("| 5 | Testing |"))
            self.assertIn("| approved | PASS | 2 |", testing)

            # Each document, its headings a level down
            self.assertIn("# 2. Planning\n\n*The PRD, `loom-project/2-prd.md`.*", text)
            self.assertIn("\n## PRD: adder\n### Functional requirements\n", text)
            self.assertIn("## Idea report: adder", text)

            # The decisions by phase and kind
            decisions = text[text.index("# Decisions") : text.index("# Changes")]
            self.assertIn("## Testing\n\n**Approvals**", decisions)
            self.assertIn("**Sent back**\n\n- Sent the failing test report back", decisions)
            # The project was complete, so the founder's decision is the project's
            self.assertIn("## Project\n\n**Decisions**\n\n- Ship it as a pip package", decisions)
            self.assertIn("first — the founder, ", decisions)
            self.assertIn("(verdict GO) — loom (--yes-always), ", decisions)

            # What each run changed
            changes = text[text.index("# Changes") : text.index("# Appendix")]
            self.assertIn("## 4. Building", changes)
            self.assertIn("adder.py", changes)
            self.assertIn("Run 2: ", changes)

            # And the history
            history = text[text.index("# Appendix: project history") :]
            self.assertIn("| Building | back | The Testing agent's report", history)
            self.assertIn("| Launch | approve |", history)

    def test_a_project_in_progress(self):
        with GitTemporaryDirectory():
            make_repo()
            orchestrator = Orchestrator(make_coder())
            orchestrator.new_project("An idea with a | pipe\nand a second line")
            text = ProjectReport.from_orchestrator(orchestrator).markdown()
            self.assertIn('title: "Project report: An idea with a | pipe"', text)
            self.assertIn("- **Finished:** Not yet: in Idea Check", text)
            self.assertIn("*No idea report yet.*", text)
            self.assertIn("*No decisions were recorded.*", text)
            self.assertIn("*No commits were recorded.*", text)
            self.assertIn("| 1 | Idea Check | pending |  | 0 |", text)
            self.assertIn("> An idea with a | pipe\n> and a second line", text)
            self.assertIn("| new | An idea with a \\| pipe |", text)

    def test_shift_headings(self):
        text = "# Title\nText\n## Section\n```\n# not a heading\n```\n###### Deep\n#hashtag"
        self.assertEqual(
            shift_headings(text),
            "## Title\nText\n### Section\n```\n# not a heading\n```\n###### Deep\n#hashtag",
        )

    def test_executive_summary(self):
        with GitTemporaryDirectory():
            make_repo()
            orchestrator = run_fixture_project()
            report = ProjectReport.from_orchestrator(orchestrator)
            model = FakeModel("The adder works.\n\n# Big heading\n")
            summary = report.write_summary(model)
            self.assertIn(IDEA, model.messages[0]["content"])
            self.assertEqual(summary, "The adder works.\n\n### Big heading")
            text = report.markdown(summary)
            self.assertIn("# Executive summary\n\nThe adder works.", text)
            self.assertLess(text.index("# Executive summary"), text.index("# Timeline"))

            with self.assertRaises(ReportError):
                report.write_summary(FakeModel(""))


class TestExport(unittest.TestCase):
    MARKDOWN = (
        '---\ntitle: "Project report: adder"\nsubtitle: "Built with loom"\ndate: "4 October'
        ' 2026"\n---\n\n# Overview\n\n| a | b |\n|---|---|\n| 1 | 2 |\n'
    )

    def test_markdown(self):
        with GitTemporaryDirectory():
            [path] = export(self.MARKDOWN, "md", "out/report.md")
            self.assertEqual(path.read_bytes().decode(), self.MARKDOWN)

    def test_html_without_pandoc(self):
        html = html_without_pandoc(self.MARKDOWN)
        self.assertIn("<title>Project report: adder</title>", html)
        self.assertIn('<p class="subtitle">Built with loom</p>', html)
        self.assertIn("<td>1</td>", html)
        self.assertNotIn("---", html)
        with GitTemporaryDirectory():
            with patch.object(project_report, "pandoc_version", return_value=None):
                [path] = export(self.MARKDOWN, "html", "report.html")
            self.assertIn("<td>1</td>", path.read_text(encoding="utf-8"))

    def test_word_and_pdf_without_pandoc(self):
        with GitTemporaryDirectory():
            warn = MagicMock()
            with patch.object(project_report, "pandoc_version", return_value=None):
                written = export(self.MARKDOWN, "docx", "report.docx", warn)
                self.assertEqual([p.name for p in written], ["report.html"])
                self.assertIn("needs pandoc", warn.call_args[0][0])
                written = export(self.MARKDOWN, "pdf", "report.pdf", warn)
                self.assertEqual([p.name for p in written], ["report.html"])

    def test_unknown_format(self):
        with self.assertRaises(ReportError):
            export(self.MARKDOWN, "rtf", "report.rtf")

    @unittest.skipUnless(HAS_PANDOC, "needs pandoc")
    def test_html_and_word(self):
        with GitTemporaryDirectory():
            [html] = export(self.MARKDOWN, "html", "report.html")
            text = html.read_text(encoding="utf-8")
            self.assertIn("Project report: adder", text)
            self.assertIn("<table", text)
            self.assertIn("Overview", text)

            [docx] = export(self.MARKDOWN, "docx", "report.docx")
            with zipfile.ZipFile(docx) as doc:
                body = doc.read("word/document.xml").decode()
                styles = doc.read("word/styles.xml").decode()
            self.assertIn("Project report: adder", body)
            self.assertIn('w:val="Title"', body)
            # loom's reference document styled it
            self.assertIn('w:color w:val="1F3864"', styles)
            self.assertIn("w:pageBreakBefore", styles)

    @unittest.skipUnless(HAS_PANDOC, "needs pandoc")
    def test_pdf_without_an_engine(self):
        with GitTemporaryDirectory():
            warn = MagicMock()
            with patch.object(project_report, "pdf_engine", return_value=None):
                written = export(self.MARKDOWN, "pdf", "report.pdf", warn)
            self.assertEqual([p.name for p in written], ["report.docx", "report.html"])
            self.assertIn("needs a PDF engine", warn.call_args[0][0])

    @unittest.skipUnless(HAS_PANDOC and pdf_engine(), "needs pandoc and a PDF engine")
    def test_pdf(self):
        with GitTemporaryDirectory():
            [pdf] = export(self.MARKDOWN, "pdf", "report.pdf")
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))


class TestReportCommand(unittest.TestCase):
    def setUp(self):
        self.io = InputOutput(yes=True)
        self.io.tool_output = MagicMock()
        self.io.tool_error = MagicMock()
        self.io.tool_warning = MagicMock()

    def shown(self):
        return [c[0][0] for c in self.io.tool_output.call_args_list if c[0]]

    def test_report_command(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder(self.io)
            orchestrator = Orchestrator(coder)
            orchestrator.new_project(IDEA)
            commands = Commands(self.io, coder)

            commands.cmd_project("report")
            self.assertIn("Wrote the project report to loom-project/report.md", self.shown())
            self.assertTrue(
                Path("loom-project/report.md").read_text(encoding="utf-8").startswith("---\n")
            )

            commands.cmd_project('report --out "my reports/r.html"')
            self.assertIn("Wrote the project report to my reports/r.html", self.shown())
            self.assertIn("<html", Path("my reports/r.html").read_text(encoding="utf-8"))

            commands.cmd_project("report md -o notes.md")
            self.assertTrue(Path("notes.md").exists())

            commands.cmd_project("report rtf")
            self.assertIn("Unexpected 'rtf'", self.io.tool_error.call_args[0][0])
            commands.cmd_project("report md --frobnicate")
            self.assertIn("Unexpected '--frobnicate'", self.io.tool_error.call_args[0][0])

    def test_without_pandoc_word_falls_back_to_html(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder(self.io)
            Orchestrator(coder).new_project(IDEA)
            commands = Commands(self.io, coder)
            with patch.object(project_report, "pandoc_version", return_value=None):
                commands.cmd_project("report docx")
            self.assertIn("needs pandoc", self.io.tool_warning.call_args[0][0])
            self.assertIn("Wrote the project report to loom-project/report.html", self.shown())

    def test_summary_by_the_weak_model(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder(self.io)
            orchestrator = Orchestrator(coder)
            orchestrator.new_project(IDEA)
            model = FakeModel("It adds numbers.")
            coder.main_model.weak_model = model
            [path] = orchestrator.write_report("md", summary=True)
            self.assertIn(
                "# Executive summary\n\nIt adds numbers.", path.read_text(encoding="utf-8")
            )
            self.assertIn("weak-model is writing the executive summary...", self.shown())

            # A failed summary still writes the report
            coder.main_model.weak_model = FakeModel(None)
            [path] = orchestrator.write_report("md", "plain.md", summary=True)
            self.assertNotIn("Executive summary", path.read_text(encoding="utf-8"))
            self.assertIn("wrote no summary", self.io.tool_warning.call_args[0][0])


if __name__ == "__main__":
    unittest.main()

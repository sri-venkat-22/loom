import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from loom.commands import Commands
from loom.memory import ProjectMemory
from loom.orchestrator import ProjectState
from loom.phases import PHASES_BY_KEY
from loom.run_cmd import run_cmd
from loom.utils import GitTemporaryDirectory
from loom.web.backend.app import create_app
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import make_repo
from tests.basic.test_orchestrator import (
    IDEA_REPORT,
    SCRIPT_IDEA,
    SCRIPT_PLANNING,
    run_project,
)
from tests.web.test_webio import answer_asks, make_io

PYTHON = f'"{sys.executable}"'


class TestCommandOutput(unittest.TestCase):
    def test_run_cmd_calls_back_a_line_at_a_time(self):
        calls = []
        status, output = run_cmd(
            f"{PYTHON} -c \"print('one'); print('two', end='')\"",
            output=lambda text, start=False, exit_code=None: calls.append((text, start, exit_code)),
        )
        self.assertEqual(status, 0)
        self.assertEqual(output.replace("\r", ""), "one\ntwo")
        self.assertTrue(calls[0][1])
        self.assertIn("print('one')", calls[0][0])
        self.assertEqual(calls[1:], [("one\n", False, None), ("two", False, None), ("", False, 0)])

    def test_run_shows_a_card_and_the_terminal(self):
        with GitTemporaryDirectory():
            io, session = make_io()
            commands = Commands(io, None)
            commands.coder = type("Coder", (), dict(root=".", main_model=None))()
            io.command_output("echo hello", start=True)
            io.command_output("hello\n")
            io.command_output("", exit_code=0)
            io.end_tool()

        start, first, line, end = session.history
        self.assertEqual(
            (start["type"], start["name"], start["detail"]), ("tool_start", "Run", "echo hello")
        )
        self.assertEqual(first, dict(type="terminal", text="$ echo hello\n", start=True))
        self.assertEqual(line, dict(type="terminal", text="hello\n", start=False))
        self.assertEqual(end["status"], "done")
        self.assertEqual(end["output"], "hello\n")

    def test_a_failing_command(self):
        io, session = make_io()
        io.command_output("false", start=True)
        io.command_output("", exit_code=1)
        card = io.tool_id
        # A question about it still goes on its card
        self.assertEqual(io.tool_id, card)
        io.end_tool()
        output = next(m for m in session.history if m["type"] == "tool_output")
        self.assertEqual(output["lines"], ["Exit code: 1"])
        ends = [m for m in session.history if m["type"] == "tool_end"]
        self.assertEqual([end["status"] for end in ends], ["failed"])

    def test_run_command_through_loom(self):
        with GitTemporaryDirectory():
            session = WebSession(interrupt=lambda: None)
            session.started = True
            io = WebIO(pretty=False, session=session)
            from loom.coders import Coder
            from loom.models import Model

            coder = Coder.create(Model("gpt-4o-mini"), None, io=io, map_tokens=0)
            asks, thread = answer_asks(session, "n")
            coder.commands.cmd_run(f"{PYTHON} -c \"print('hi from run')\"")
            thread.join(5)

        card = next(msg for msg in session.history if msg["type"] == "tool_start")
        self.assertEqual(card["name"], "Run")
        # The card ended when the command did, before the question
        end = next(msg for msg in session.history if msg["type"] == "tool_end")
        self.assertLess(session.history.index(end), session.history.index(asks[0]))
        terminal = "".join(m["text"] for m in session.history if m["type"] == "terminal")
        self.assertIn("hi from run", terminal)
        # Adding the output to the chat is asked on the card
        self.assertEqual(asks[0]["tool_id"], card["id"])
        self.assertIn("Add", asks[0]["question"])

    def test_diffs_print_inline(self):
        io, session = make_io()
        io.print(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1,2 @@\n z\n+w\n"
        )
        io.print("plain text")
        diffs = [m for m in session.history if m["type"] == "diff"]
        self.assertEqual([d["file"] for d in diffs], ["a.py", "b.py"])
        self.assertEqual(session.history[-1]["text"], "plain text")


class TestEditingDocuments(unittest.TestCase):
    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()

    def tearDown(self):
        self.memory_store.stop()

    def test_edit_at_a_checkpoint(self):
        edited = IDEA_REPORT + "Founder: aim it at students first.\n"
        with GitTemporaryDirectory():
            repo = make_repo()
            session = WebSession(interrupt=lambda: None)
            session.started = True
            io = WebIO(pretty=False, session=session)
            # Edit the idea report in the browser, approve it, then stop at the PRD
            asks, thread = answer_asks(session, "edit", edited, "approve", "reject", "")
            coder, orchestrator, llm, done = run_project(SCRIPT_IDEA, SCRIPT_PLANNING, io=io)
            thread.join(5)

            self.assertEqual(
                Path(PHASES_BY_KEY["idea"].document).read_text(encoding="utf-8"), edited
            )
            self.assertEqual(orchestrator.state.status("idea"), "approved")
            messages = [commit.message for commit in repo.iter_commits()]
            self.assertTrue(any("at the Idea Check checkpoint" in m for m in messages))

        edit = asks[1]
        self.assertEqual(edit["kind"], "edit")
        self.assertEqual(edit["subject"], "loom-project/1-idea-report.md")
        self.assertEqual(edit["default"], IDEA_REPORT)
        # Asked again after the edit
        self.assertEqual(asks[2]["kind"], "checkpoint")


class TestApi(unittest.TestCase):
    def setUp(self):
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        (self.root / "logo.png").write_bytes(b"\x89PNG\0\0\0")
        (self.root.parent / "secret.txt").write_text("not yours")
        self.io, self.session = make_io()
        self.io.root = str(self.root)
        self.io.files = dict(files=["calc.py", "logo.png"], chat=["calc.py"], read_only=[])
        app = create_app(
            self.session, static_dir=self.dir.name, allowed_hosts=["localhost"], io=self.io
        )
        self.client = TestClient(app, base_url="http://localhost")

    def tearDown(self):
        self.memory_store.stop()
        (self.root.parent / "secret.txt").unlink()
        self.dir.cleanup()

    def test_files(self):
        found = self.client.get("/api/files").json()
        self.assertEqual(found["files"], ["calc.py", "logo.png"])
        self.assertEqual(found["chat"], ["calc.py"])

    def test_file(self):
        response = self.client.get("/api/file", params=dict(path="calc.py"))
        self.assertEqual(response.json()["text"], "def add(a, b):\n    return a + b\n")

    def test_only_the_projects_files(self):
        for path in ("../secret.txt", "nope.py", str(self.root.parent / "secret.txt")):
            response = self.client.get("/api/file", params=dict(path=path))
            self.assertEqual(response.status_code, 404, path)

    def test_project_documents_before_the_file_list_has_them(self):
        # A phase writes its document during a run, and loom's list of files catches up
        # only when it next waits for input
        (self.root / "loom-project").mkdir()
        (self.root / "loom-project" / "1-idea-report.md").write_bytes(b"# Idea report\n")
        response = self.client.get("/api/file", params=dict(path="loom-project/1-idea-report.md"))
        self.assertEqual(response.json()["text"], "# Idea report\n")
        response = self.client.get("/api/file", params=dict(path="loom-project/notes.md"))
        self.assertEqual(response.status_code, 404)

    def test_binary_files(self):
        response = self.client.get("/api/file", params=dict(path="logo.png"))
        self.assertEqual(response.status_code, 415)

    def test_memory_without_a_project(self):
        found = self.client.get("/api/memory", params=dict(q="adder")).json()
        self.assertFalse(found["available"])
        # Looking doesn't create the project's memory
        self.assertFalse((self.root / ".loom").exists())

    def test_memory(self):
        state = ProjectState.new(self.root, "A CLI adder")
        state.save()
        memory = ProjectMemory(self.root)
        memory.index_text("idea", "idea", None, "idea", "A CLI that adds two integers", "Idea")
        memory.record_decision("planning", "Use argparse for the arguments")

        found = self.client.get("/api/memory", params=dict(q="integers")).json()
        self.assertTrue(found["available"])
        self.assertEqual(found["hits"][0]["text"], "A CLI that adds two integers")
        self.assertIn("keyword", found["store"])

        found = self.client.get("/api/memory").json()
        self.assertEqual(found["decisions"][0]["text"], "Use argparse for the arguments")

    def test_project_report(self):
        response = self.client.get("/api/project/report")
        self.assertEqual(response.status_code, 404)
        # Asking doesn't create the project's memory
        self.assertFalse((self.root / ".loom").exists())

        state = ProjectState.new(self.root, "A CLI adder")
        state.save()
        ProjectMemory(self.root).record_decision("planning", "Use argparse")
        self.session.update(model="gpt-4o-mini", weak_model="gpt-4o-mini")

        response = self.client.get("/api/project/report", params=dict(format="md"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertIn('filename="report.md"', response.headers["content-disposition"])
        self.assertIn('title: "Project report: A CLI adder"', response.text)
        self.assertIn("- Use argparse", response.text)
        self.assertIn("- **Model:** gpt-4o-mini", response.text)

        response = self.client.get("/api/project/report", params=dict(format="rtf"))
        self.assertEqual(response.status_code, 400)

        from loom import project_report

        with patch.object(project_report, "pandoc_version", return_value=None):
            # Word without pandoc comes as HTML, and a PDF can't be made
            response = self.client.get("/api/project/report", params=dict(format="docx"))
            self.assertEqual(response.status_code, 200)
            self.assertIn('filename="report.html"', response.headers["content-disposition"])
            self.assertIn("<html", response.text)
            response = self.client.get("/api/project/report", params=dict(format="pdf"))
            self.assertEqual(response.status_code, 501)
            self.assertIn("needs pandoc", response.json()["detail"])

    def test_models(self):
        aliases = self.client.get("/api/models").json()["aliases"]
        self.assertIn("sonnet", [a["alias"] for a in aliases])

    def test_not_ready(self):
        app = create_app(WebSession(), allowed_hosts=["localhost"])
        client = TestClient(app, base_url="http://localhost")
        self.assertEqual(client.get("/api/files").status_code, 503)

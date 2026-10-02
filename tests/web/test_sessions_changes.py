import json
import unittest
from pathlib import Path

import git
from fastapi.testclient import TestClient

from loom import prompts
from loom.coders import Coder
from loom.models import Model
from loom.sessions import Session
from loom.utils import GitTemporaryDirectory
from loom.web.backend.app import create_app
from loom.web.backend.changes import changes
from loom.web.backend.session import WebSession
from loom.web.backend.transcript import transcript
from loom.web.backend.webio import WebIO, parse_diff


def counter():
    ids = iter(range(1, 1000))
    return lambda prefix: f"{prefix}{next(ids)}"


def tool_call(call_id, name, **args):
    return dict(id=call_id, type="function", function=dict(name=name, arguments=json.dumps(args)))


MESSAGES = [
    dict(role="user", content=prompts.summary_prefix + "We built an adder."),
    dict(role="assistant", content="Ok."),
    dict(role="user", content="fix add"),
    dict(
        role="assistant",
        content="Let me look.",
        tool_calls=[
            tool_call("c1", "read_file", path="calc.py"),
            tool_call("c2", "bash", command="pytest -q\nexit 1"),
            tool_call("c3", "bash", command="pytest"),
            tool_call("c4", "bash", command="true"),
        ],
    ),
    dict(role="tool", tool_call_id="c1", content="1  def add(a, b):"),
    dict(role="tool", tool_call_id="c2", content="The user denied this action. Stop."),
    dict(role="tool", tool_call_id="c3", content="Exit code: 1\n1 failed"),
    dict(role="tool", tool_call_id="c4", content="Exit code: 0\n"),
    dict(role="assistant", content="**Fixed** it."),
]


class TestTranscript(unittest.TestCase):
    def test_a_saved_conversation_as_messages(self):
        shown = list(transcript(MESSAGES, counter()))
        types = [type for type, _ in shown]
        self.assertEqual(
            types,
            [
                "system",
                "user",
                "assistant_delta",
                "assistant_end",
                "tool_start",
                "tool_end",
                "tool_start",
                "tool_end",
                "tool_start",
                "tool_end",
                "tool_start",
                "tool_end",
                "assistant_delta",
                "assistant_end",
            ],
        )
        payloads = [payload for _, payload in shown]
        self.assertIn("summarized", payloads[0]["text"])
        self.assertEqual(payloads[1]["text"], "fix add")
        self.assertEqual(payloads[4]["name"], "Read")
        self.assertEqual(payloads[4]["detail"], "calc.py")
        self.assertEqual(json.loads(payloads[4]["args"]), dict(path="calc.py"))
        self.assertEqual(payloads[5]["status"], "done")
        self.assertEqual(payloads[6]["detail"], "pytest -q …")
        self.assertEqual(payloads[7]["status"], "failed")
        self.assertEqual(payloads[9]["status"], "failed")
        self.assertEqual(payloads[11]["status"], "done")
        self.assertEqual(payloads[12]["text"], "**Fixed** it.")


class TestSwitchingConversations(unittest.TestCase):
    def test_clear_and_resume_switch_the_chat(self):
        with GitTemporaryDirectory():
            session = WebSession(interrupt=lambda: None)
            session.started = True
            io = WebIO(pretty=False, session=session)
            sessions_dir = Path(".loom.sessions").resolve()
            coder = Coder.create(
                Model("gpt-4o-mini"), None, io=io, map_tokens=0, session=Session(sessions_dir)
            )
            coder.done_messages = list(MESSAGES)
            coder.save_session()
            first = coder.session.id

            io.follow_conversation(coder)
            self.assertEqual(io.conversation_id, first)
            self.assertFalse([m for m in session.history if m["type"] == "conversation"])

            # /clear starts a new conversation: the chat empties
            coder.commands.cmd_clear("")
            io.follow_conversation(coder)
            self.assertNotEqual(io.conversation_id, first)
            self.assertEqual(session.history[0]["type"], "conversation")
            self.assertEqual(len(session.history), 1)

            # /resume shows the saved one again
            coder.commands.cmd_resume(first)
            io.follow_conversation(coder)
            self.assertEqual(io.conversation_id, first)
            self.assertEqual(
                session.history[0], dict(type="conversation", id=first, title=coder.session.title)
            )
            texts = [m.get("text") for m in session.history if m["type"] == "user"]
            self.assertEqual(texts, ["fix add"])

            # The sessions sidebar lists both, the current one marked
            io.root = str(Path(".").resolve())
            app = create_app(session, allowed_hosts=["localhost"], io=io)
            client = TestClient(app, base_url="http://localhost")
            found = client.get("/api/sessions").json()
            self.assertTrue(found["saved"])
            current = [s for s in found["sessions"] if s["current"]]
            self.assertEqual([s["id"] for s in current], [first])
            self.assertEqual(current[0]["title"], coder.session.title)

    def test_a_new_conversation_is_listed_before_it_is_saved(self):
        with GitTemporaryDirectory():
            io = WebIO(pretty=False, session=WebSession())
            io.root = "."
            io.conversation_id = "20261003-000000-abcd"
            app = create_app(io.web, allowed_hosts=["localhost"], io=io)
            found = TestClient(app, base_url="http://localhost").get("/api/sessions").json()
            self.assertFalse(found["saved"])
            self.assertEqual(found["sessions"][0]["id"], "20261003-000000-abcd")
            self.assertTrue(found["sessions"][0]["current"])


def commit_all(repo, message):
    repo.git.add("-A")
    repo.git.commit("-m", message)


class TestChanges(unittest.TestCase):
    def test_changes_since_a_commit(self):
        with GitTemporaryDirectory() as root:
            repo = git.Repo()
            Path("calc.py").write_text("".join(f"line {n}\n" for n in range(1, 41)))
            Path("old.py").write_text("x = 1\n")
            Path("gone.py").write_text("y = 2\n")
            Path(".gitignore").write_text("*.log\n")
            commit_all(repo, "initial")
            base = repo.head.commit.hexsha

            # A committed edit, like loom's auto-commits, and an uncommitted one
            text = Path("calc.py").read_text().replace("line 30\n", "line thirty\n")
            Path("calc.py").write_text(text)
            Path("old.py").rename("new.py")
            Path("gone.py").unlink()
            commit_all(repo, "loom: edits")
            Path("calc.py").write_text(text.replace("line 2\n", "line two\n"))
            Path("fresh.py").write_text("print('hi')\n")
            Path("debug.log").write_text("ignored\n")

            found = {change["path"]: change for change in changes(repo, Path(root), base)}

        self.assertEqual(set(found), {"calc.py", "new.py", "gone.py", "fresh.py"})
        calc = found["calc.py"]
        self.assertEqual((calc["status"], calc["added"], calc["removed"]), ("modified", 2, 2))
        gaps = [line["text"] for line in calc["lines"] if line["kind"] == "gap"]
        self.assertEqual(gaps, ["21 unmodified lines"])
        self.assertEqual(found["new.py"]["status"], "renamed")
        self.assertEqual(found["new.py"]["old_path"], "old.py")
        self.assertEqual(found["gone.py"]["status"], "deleted")
        self.assertEqual(found["fresh.py"]["status"], "added")
        self.assertEqual(found["fresh.py"]["lines"][0]["text"], "print('hi')")

    def test_api(self):
        with GitTemporaryDirectory() as root:
            repo = git.Repo()
            repo.git.checkout("-b", "main")
            Path("a.py").write_text("a = 1\n")
            commit_all(repo, "initial")
            repo.git.checkout("-b", "feature")
            Path("a.py").write_text("a = 2\n")
            commit_all(repo, "on the branch")
            started = repo.head.commit.hexsha
            Path("a.py").write_text("a = 3\n")

            io = WebIO(pretty=False, session=WebSession())
            io.root = root
            io.git = repo
            io.base_commit = started
            app = create_app(io.web, allowed_hosts=["localhost"], io=io)
            client = TestClient(app, base_url="http://localhost")

            session = client.get("/api/changes").json()
            branch = client.get("/api/changes", params=dict(base="branch")).json()

        self.assertEqual(session["label"], "since loom started")
        removed = [line["text"] for line in session["files"][0]["lines"] if line["kind"] == "del"]
        self.assertEqual(removed, ["a = 2"])
        self.assertEqual(branch["label"], "since main")
        removed = [line["text"] for line in branch["files"][0]["lines"] if line["kind"] == "del"]
        self.assertEqual(removed, ["a = 1"])

    def test_no_repo(self):
        io = WebIO(pretty=False, session=WebSession())
        io.root = "."
        app = create_app(io.web, allowed_hosts=["localhost"], io=io)
        found = TestClient(app, base_url="http://localhost").get("/api/changes").json()
        self.assertFalse(found["available"])


class TestGaps(unittest.TestCase):
    def test_gaps_count_the_lines_they_hide(self):
        _, lines = parse_diff(
            "--- a/x\n+++ b/x\n@@ -5,2 +5,2 @@\n a\n-b\n+c\n@@ -20,1 +20,1 @@\n-d\n+e\n"
        )
        gaps = [line["text"] for line in lines if line["kind"] == "gap"]
        self.assertEqual(gaps, ["4 unmodified lines", "13 unmodified lines"])

import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import git

from loom.coders import Coder
from loom.llm import litellm
from loom.models import Model
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import FakeLLM, call, reply

EDIT = [
    reply(None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
    reply("Fixed it."),
]


def run_request(answer, stream):
    """Have the agent edit calc.py in a WebIO session, answering its permission question
    from the browser's side with answer. Returns the session's messages."""
    repo = git.Repo.init()
    Path("calc.py").write_text("def add(a, b):\n    return a - b\n")
    repo.git.add(".")
    repo.git.commit("-m", "initial")

    session = WebSession(interrupt=lambda: None)
    io = WebIO(pretty=stream, session=session)
    coder = Coder.create(
        Model("gpt-4o-mini"),
        "agent",
        io=io,
        map_tokens=0,
        stream=stream,
        permissions=Permissions(io),
    )
    session.started = True
    # Like main() does for the terminal's default of compact edits
    io.agent_diffs = False

    def browser():
        while True:
            asks = [msg for msg in session.history if msg["type"] == "ask"]
            if asks:
                session.handle(dict(type="answer", ask_id=asks[0]["ask_id"], value=answer))
                return
            threading.Event().wait(0.01)

    thread = threading.Thread(target=browser, daemon=True)
    thread.start()
    with patch.object(litellm, "completion", FakeLLM(*EDIT)):
        coder.run(with_message="fix add")
    thread.join(5)
    return session.history


class TestEditsFromTheBrowser(unittest.TestCase):
    def check_accept(self, stream):
        with GitTemporaryDirectory():
            history = run_request("yes", stream)
            self.assertEqual(Path("calc.py").read_text(), "def add(a, b):\n    return a + b\n")

        card = next(msg for msg in history if msg["type"] == "tool_start")
        self.assertEqual(card["name"], "Update")
        diff = next(msg for msg in history if msg["type"] == "diff")
        ask = next(msg for msg in history if msg["type"] == "ask")
        self.assertEqual(diff["tool_id"], card["id"])
        self.assertEqual(ask["tool_id"], card["id"])
        self.assertEqual(ask["kind"], "permission")
        changed = [(line["kind"], line["text"]) for line in diff["lines"] if line["kind"] != "ctx"]
        self.assertEqual(changed, [("del", "    return a - b"), ("add", "    return a + b")])

        end = next(msg for msg in history if msg["type"] == "tool_end")
        self.assertEqual(end["status"], "done")
        self.assertIn("Edited calc.py", end["output"])
        # The edit's diff isn't shown a second time after it's made
        self.assertEqual(len([msg for msg in history if msg["type"] == "diff"]), 1)

    def test_accept(self):
        self.check_accept(stream=False)

    def test_accept_streaming(self):
        self.check_accept(stream=True)

    def test_reject(self):
        with GitTemporaryDirectory():
            history = run_request("no", stream=False)
            self.assertEqual(Path("calc.py").read_text(), "def add(a, b):\n    return a - b\n")

        end = next(msg for msg in history if msg["type"] == "tool_end")
        self.assertEqual(end["status"], "failed")
        output = next(msg for msg in history if msg["type"] == "tool_output")
        self.assertEqual(output["lines"], ["Denied"])

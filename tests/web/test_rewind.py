import unittest
from pathlib import Path
from unittest.mock import patch

import git
from fastapi.testclient import TestClient

from loom.coders import Coder
from loom.commands import Commands
from loom.llm import litellm
from loom.models import Model
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory
from loom.web.backend.app import create_app
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import FakeLLM
from tests.basic.test_rewind import ADD_SUBTRACT, FIX_ADD


def web_coder():
    """An agent in a WebIO session, in a repo with calc.py, allowed to run Python."""
    repo = git.Repo.init()
    Path("calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
    repo.git.add(".")
    repo.git.commit("-m", "initial")
    session = WebSession(interrupt=lambda: None)
    io = WebIO(pretty=False, session=session, yes=True)
    coder = Coder.create(
        Model("gpt-4o-mini"),
        "agent",
        io=io,
        map_tokens=0,
        stream=False,
        permissions=Permissions(io, allow=["bash"]),
    )
    coder.commands = Commands(io, coder)
    session.started = True
    io.root = str(Path.cwd())
    return coder, io, session


def request(coder, session, message, script):
    """Run message as a browser turn."""
    session.start_turn(message)
    with patch.object(litellm, "completion", FakeLLM(*script)):
        coder.run(with_message=message)
    turn = session.turn_id
    session.end_turn()
    return turn


class TestCheckpointsEvent(unittest.TestCase):
    def test_each_request_names_its_turn(self):
        with GitTemporaryDirectory():
            coder, io, session = web_coder()
            first = request(coder, session, "fix add", FIX_ADD)
            second = request(coder, session, "add subtract", ADD_SUBTRACT)

            event = session.checkpoints
            self.assertEqual(event["type"], "checkpoints")
            self.assertEqual(event["conversation"], coder.session.id)
            self.assertTrue(event["git"])
            items = event["items"]
            self.assertEqual([i["prompt"] for i in items], ["add subtract", "fix add"])
            self.assertEqual([i["number"] for i in items], [1, 2])
            self.assertEqual([i["turn_id"] for i in items], [second, first])
            self.assertEqual({i["kind"] for i in items}, {"request"})
            self.assertTrue(all(i["conversation"] for i in items))
            # Kept as a snapshot, which a browser that connects later gets first
            self.assertNotIn(event, session.history)
            client = object()
            self.assertIn(event, session.connect(client, loop=None)[:3])
            session.disconnect(client)

    def test_the_rewind_dialogs_summary(self):
        with GitTemporaryDirectory():
            coder, io, session = web_coder()
            request(coder, session, "fix add", FIX_ADD)
            request(coder, session, "add subtract", ADD_SUBTRACT)
            client = TestClient(
                create_app(session, allowed_hosts=["localhost"], io=io), base_url="http://localhost"
            )
            first = coder.session.checkpoints[0]
            found = client.get(f"/api/checkpoints/{first['id']}").json()
            self.assertEqual(found["prompt"], "fix add")
            self.assertTrue(found["conversation"])
            self.assertEqual(
                found["changes"],
                dict(changed=["calc.py"], created=["gen.txt", "sub.py"], deleted=[]),
            )
            self.assertIsNone(found["busy"])
            # The dialog's look doesn't touch the files or loom's own index
            self.assertTrue(Path("sub.py").exists())
            self.assertEqual(client.get("/api/checkpoints/nope").status_code, 404)

            Path(git.Repo().git_dir, "MERGE_HEAD").write_bytes(b"0" * 40 + b"\n")
            found = client.get(f"/api/checkpoints/{first['id']}").json()
            self.assertIn("middle of a merge", found["busy"])

    def test_rewinding_from_the_browser_shows_the_shorter_conversation(self):
        with GitTemporaryDirectory():
            coder, io, session = web_coder()
            request(coder, session, "fix add", FIX_ADD)
            request(coder, session, "add subtract", ADD_SUBTRACT)
            newest = coder.session.checkpoints[-1]["id"]

            # What the rewind dialog sends
            session.start_turn(f"/rewind {newest} both --yes")
            coder.run(with_message=f"/rewind {newest} both --yes")
            session.end_turn()

            self.assertFalse(Path("sub.py").exists())
            self.assertFalse(Path("gen.txt").exists())
            types = [msg["type"] for msg in session.history]
            # The chat started over as the rewound conversation: only the first request
            start = types.index("conversation")
            users = [m["text"] for m in session.history[start:] if m["type"] == "user"]
            self.assertEqual(users, ["fix add"])
            texts = [m.get("text", "") for m in session.history[start:] if m["type"] == "system"]
            self.assertTrue(any("Rewound the conversation" in text for text in texts))
            items = session.checkpoints["items"]
            self.assertEqual([i["kind"] for i in items], ["rewind", "request"])
            self.assertFalse(items[0]["conversation"])


if __name__ == "__main__":
    unittest.main()

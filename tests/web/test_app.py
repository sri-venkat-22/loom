import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices
from starlette.websockets import WebSocketDisconnect

from loom.coders import Coder
from loom.llm import litellm
from loom.models import Model
from loom.utils import GitTemporaryDirectory
from loom.web.backend.app import create_app
from loom.web.backend.protocol import CLIENT_EVENTS, SERVER_EVENTS
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO

WS_URL = "ws://localhost/ws"
FRONTEND = Path(__file__).resolve().parents[2] / "loom" / "web" / "frontend"


def make_client(session=None, static_dir=None):
    session = session or WebSession(interrupt=lambda: None)
    static_dir = static_dir or tempfile.mkdtemp()
    app = create_app(session, static_dir=static_dir, allowed_hosts=["localhost"])
    return TestClient(app, base_url="http://localhost"), session


def streamed_reply(*pieces):
    """Stands in for litellm.completion, streaming a reply in pieces."""

    def completion(**kwargs):
        assert kwargs["stream"]

        def chunks():
            for piece in pieces:
                yield ModelResponseStream(choices=[StreamingChoices(delta=Delta(content=piece))])
            yield ModelResponseStream(
                choices=[StreamingChoices(delta=Delta(), finish_reason="stop")]
            )

        return chunks()

    return completion


def receive_until(socket, type):
    messages = []
    while True:
        message = socket.receive_json()
        messages.append(message)
        if message["type"] == type:
            return messages


def reply_text(messages):
    text = ""
    for message in messages:
        if message["type"] == "assistant_delta":
            text = message["text"] if message["replace"] else text + message["text"]
    return text


class TestServer(unittest.TestCase):
    def test_health(self):
        client, _ = make_client()
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_other_host_names_are_refused(self):
        client, _ = make_client()
        response = client.get("/api/health", headers=dict(host="attacker.example"))
        self.assertEqual(response.status_code, 400)

    def test_websites_cant_open_the_websocket(self):
        client, _ = make_client()
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect(WS_URL, headers=dict(origin="https://attacker.example")):
                pass

    def test_local_pages_can(self):
        client, _ = make_client()
        # The Vite dev server's page is on another port of this computer
        with client.websocket_connect(WS_URL, headers=dict(origin="http://localhost:5173")) as ws:
            self.assertEqual(ws.receive_json()["type"], "session")

    def test_frontend_not_built(self):
        client, _ = make_client()
        response = client.get("/")
        self.assertIn("npm run build", response.text)

    def test_serves_the_built_frontend(self):
        with tempfile.TemporaryDirectory() as static_dir:
            Path(static_dir, "index.html").write_text("<title>loom</title>")
            client, _ = make_client(static_dir=static_dir)
            self.assertEqual(client.get("/").text, "<title>loom</title>")
            self.assertTrue(client.get("/api/health").json()["ok"])

    def test_reconnecting_replays_the_conversation(self):
        client, session = make_client()
        session.emit("system", level="info", text="Added calc.py to the chat")
        with client.websocket_connect(WS_URL) as ws:
            self.assertEqual(ws.receive_json()["type"], "session")
            self.assertEqual(ws.receive_json()["text"], "Added calc.py to the chat")


class TestStreaming(unittest.TestCase):
    def test_reply_streams_to_the_browser(self):
        with GitTemporaryDirectory():
            session = WebSession(interrupt=lambda: None)
            io = WebIO(pretty=True, session=session)
            coder = Coder.create(Model("gpt-4o-mini"), "ask", io=io, stream=True, map_tokens=0)
            session.started = True
            client, _ = make_client(session)

            completion = streamed_reply("Hello", " from", " loom.")
            with patch.object(litellm, "completion", completion):
                thread = threading.Thread(target=coder.run, daemon=True)
                thread.start()
                try:
                    with client.websocket_connect(WS_URL) as ws:
                        ws.send_json(dict(type="input", text="say hello"))
                        messages = receive_until(ws, "turn_end")
                finally:
                    session.close()
                    thread.join(5)
            self.assertFalse(thread.is_alive())

        types = [msg["type"] for msg in messages]
        start = types.index("user")
        self.assertEqual(messages[start]["text"], "say hello")
        self.assertEqual(types[start + 1], "turn_start")
        self.assertIn("assistant_delta", types)
        self.assertLess(types.index("assistant_delta"), types.index("assistant_end"))
        self.assertEqual(reply_text(messages), "Hello from loom.")
        self.assertEqual(messages[-1]["status"], "done")

        # The snapshot sent when loom was ready again describes the session
        snapshot = [msg for msg in messages if msg["type"] == "session"][-1]
        self.assertEqual(snapshot["model"], "gpt-4o-mini")
        self.assertFalse(snapshot["busy"])
        self.assertIn("/add", [command["cmd"] for command in snapshot["commands"]])
        self.assertGreater(snapshot["tokens"]["sent"], 0)


class TestProtocol(unittest.TestCase):
    def test_frontend_knows_every_message_type(self):
        source = (FRONTEND / "src" / "lib" / "protocol.ts").read_text()
        quoted = set(re.findall(r'"([a-z_]+)"', source))
        for type in SERVER_EVENTS + CLIENT_EVENTS:
            self.assertIn(type, quoted, f"protocol.ts doesn't mention {type}")

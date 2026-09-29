import contextlib
import io as stdio
import json
import os
import socketserver
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom import tools
from loom.io import InputOutput
from loom.llm import litellm
from loom.mcp import (
    McpError,
    McpManager,
    McpServer,
    expand_env,
    format_result,
    load_config_file,
    tool_name,
    tool_parameters,
)
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory

from .test_agent import FakeLLM, call, make_coder, make_repo, reply, tool_results

SERVER_SCRIPT = str(Path(__file__).parent.parent / "fixtures" / "mcp_server.py")
TEST_SERVER = dict(command=sys.executable, args=[SERVER_SCRIPT])


class HomeDirMixin:
    """Point ~ at a temporary directory, for ~/.loom/mcp.json and the approvals file."""

    def setUp(self):
        self.original_env = os.environ.copy()
        self.home = IgnorantTemporaryDirectory()
        os.environ["HOME"] = self.home.name
        self.home_patch = patch("pathlib.Path.home", return_value=Path(self.home.name))
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        os.environ.clear()
        os.environ.update(self.original_env)
        self.home.cleanup()


def connected_server(**config):
    server = McpServer("test", dict(TEST_SERVER, **config), "test.json")
    server.connect()
    return server


class TestConfig(HomeDirMixin, unittest.TestCase):
    def test_load_config_file(self):
        with GitTemporaryDirectory():
            Path("a.json").write_text(
                json.dumps(
                    dict(
                        mcpServers=dict(
                            one=dict(command="x", args=["--y"]),
                            two=dict(type="http", url="http://localhost/mcp"),
                            off=dict(command="z", disabled=True),
                        )
                    )
                )
            )
            self.assertEqual(sorted(load_config_file("a.json")), ["one", "two"])

            # VS Code's format
            Path("b.json").write_text(json.dumps(dict(servers=dict(one=dict(command="x")))))
            self.assertEqual(list(load_config_file("b.json")), ["one"])

            for bad in [
                "{",
                "[]",
                json.dumps(dict(other={})),
                json.dumps(dict(mcpServers=dict(one=dict(args=[])))),
                json.dumps(dict(mcpServers={"bad name": dict(command="x")})),
            ]:
                Path("c.json").write_text(bad)
                with self.assertRaises(McpError):
                    load_config_file("c.json")

    def test_expand_env(self):
        env = dict(TOKEN="t0k")
        self.assertEqual(
            expand_env(dict(a=["${TOKEN}", "x${MISSING:-def}y"], b=1), env),
            dict(a=["t0k", "xdefy"], b=1),
        )
        with self.assertRaises(McpError):
            expand_env("${MISSING}", env)

    def test_tool_names_and_parameters(self):
        self.assertEqual(tool_name("git hub", "create.issue"), "mcp__git_hub__create_issue")
        long_name = tool_name("server", "x" * 100)
        self.assertEqual(len(long_name), 64)
        self.assertNotEqual(long_name, tool_name("server", "x" * 99))
        self.assertEqual(
            tool_parameters(dict(inputSchema={"$schema": "s", "type": "object"})),
            dict(type="object", properties={}),
        )
        self.assertEqual(tool_parameters({}), dict(type="object", properties={}))

    def test_format_result(self):
        result = dict(
            content=[
                dict(type="text", text="hello"),
                dict(type="image", data="...", mimeType="image/png"),
                dict(type="resource", resource=dict(uri="file:///a", text="body")),
                dict(type="resource_link", uri="file:///b", name="b"),
            ]
        )
        text, is_error = format_result(result)
        self.assertEqual(
            text,
            (
                "hello\n[image (image/png) not shown]\n[resource file:///a]\nbody\n"
                "[resource link: file:///b b]"
            ),
        )
        self.assertFalse(is_error)
        self.assertEqual(
            format_result(dict(structuredContent=dict(x=1))), ('{\n  "x": 1\n}', False)
        )
        self.assertEqual(format_result(dict(content=[], isError=True)), ("(no output)", True))

    def test_config_sources_and_approval(self):
        with GitTemporaryDirectory() as root:
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            user_config = Path(self.home.name) / ".loom" / "mcp.json"
            user_config.parent.mkdir()
            user_config.write_text(json.dumps(dict(mcpServers=dict(mine=TEST_SERVER))))
            Path(".mcp.json").write_text(json.dumps(dict(mcpServers=dict(proj=TEST_SERVER))))

            manager = McpManager.from_config(io, root)
            self.assertEqual(sorted(manager.servers), ["mine", "proj"])
            try:
                manager.start()
                # The user's own server starts; the project's asks first
                io.permission_ask.assert_called_once()
                self.assertIn("proj", io.permission_ask.call_args[0][0])
                self.assertTrue(io.permission_ask.call_args[1]["explicit_yes_required"])
                self.assertEqual(manager.servers["mine"].status, "connected")
                self.assertEqual(manager.servers["proj"].status, "not approved")
                self.assertEqual(len(manager.connected()), 1)

                # "always" remembers it
                io.permission_ask.return_value = "always"
                manager.reconnect("proj")
                self.assertEqual(manager.servers["proj"].status, "connected")
            finally:
                manager.close()

            io.permission_ask.reset_mock()
            again = McpManager.from_config(io, root)
            self.assertTrue(again.is_approved(again.servers["proj"]))

            # Until the config changes
            changed = dict(TEST_SERVER, args=[SERVER_SCRIPT, "--changed"])
            Path(".mcp.json").write_text(json.dumps(dict(mcpServers=dict(proj=changed))))
            changed_manager = McpManager.from_config(io, root)
            self.assertFalse(changed_manager.is_approved(changed_manager.servers["proj"]))

    def test_bad_config_files(self):
        with GitTemporaryDirectory() as root:
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            Path(".mcp.json").write_text("{")
            manager = McpManager.from_config(io, root)
            self.assertEqual(manager.servers, {})
            io.tool_warning.assert_called_once()

            with self.assertRaises(McpError):
                McpManager.from_config(io, root, ["missing.json"])

            # --mcp-config files override the defaults
            Path(".mcp.json").write_text(json.dumps(dict(mcpServers=dict(a=dict(command="x")))))
            Path("mine.json").write_text(json.dumps(dict(mcpServers=dict(a=dict(command="y")))))
            manager = McpManager.from_config(io, root, ["mine.json"])
            self.assertEqual(manager.servers["a"].config["command"], "y")
            self.assertFalse(manager.servers["a"].is_project_server)


class TestStdioServer(unittest.TestCase):
    def test_tools_and_calls(self):
        server = connected_server()
        try:
            self.assertEqual(server.status, "connected", server.error)
            self.assertEqual(server.server_info["name"], "test-server")
            self.assertEqual(server.instructions, "Use add for arithmetic.")
            # Both pages of tools
            names = [tool["name"] for tool in server.tools]
            self.assertEqual(names, ["add", "echo", "fail", "slow", "grow", "crash"])

            self.assertEqual(format_result(server.call_tool("add", dict(a=2, b=3)))[0], "5")
            # The server pings loom in the middle of this call
            self.assertEqual(format_result(server.call_tool("echo", dict(text="hi")))[0], "hi")
            self.assertEqual(format_result(server.call_tool("fail", {})), ("it broke", True))
            with self.assertRaisesRegex(McpError, "unknown tool nope"):
                server.call_tool("nope", {})

            # The server says its tools changed
            server.call_tool("grow", {})
            for _ in range(50):
                if server.tools_changed:
                    break
                time.sleep(0.05)
            server.refresh_tools()
            self.assertIn("late", [tool["name"] for tool in server.tools])

            # It exits: the call fails with what it said, and so do later calls
            with self.assertRaisesRegex(McpError, "crashing on purpose"):
                server.call_tool("crash", {})
            with self.assertRaises(McpError):
                server.call_tool("add", dict(a=1, b=1))
            self.assertEqual(server.status, "failed")
        finally:
            server.close()

    def test_timeouts_and_closing(self):
        server = connected_server(timeout=0.5)
        try:
            started = time.time()
            with self.assertRaisesRegex(McpError, "timed out"):
                server.call_tool("slow", dict(seconds=30))
            self.assertLess(time.time() - started, 5)
        finally:
            proc = server.connection.proc
            server.close()
        self.assertIsNotNone(proc.poll())

    def test_connection_failures(self):
        server = McpServer("bad", dict(command="no-such-command-for-loom"), "x.json")
        server.connect()
        self.assertEqual(server.status, "failed")
        self.assertIn("no-such-command-for-loom", server.error)

        script = "import sys; print('bad token', file=sys.stderr); sys.exit(2)"
        server = McpServer("dies", dict(command=sys.executable, args=["-c", script]), "x.json")
        server.connect()
        self.assertEqual(server.status, "failed")
        self.assertIn("bad token", server.error)

        server = McpServer("env", dict(command="${NO_SUCH_VARIABLE_FOR_LOOM}"), "x.json")
        server.connect()
        self.assertIn("NO_SUCH_VARIABLE_FOR_LOOM", server.error)


class McpHttpHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        state = self.server.state
        if self.headers.get("Authorization") != "Bearer s3cret":
            self.send_response(401)
            self.end_headers()
            return
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state["seen"].append((msg, dict(self.headers)))
        if "method" not in msg or "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return

        method = msg["method"]
        if method != "initialize" and self.headers.get("Mcp-Session-Id") != "session-1":
            self.send_response(400)
            self.end_headers()
            return
        if method == "initialize":
            result = dict(
                protocolVersion="2025-06-18",
                capabilities=dict(tools={}),
                serverInfo=dict(name="http-server"),
            )
            self.send_json(msg["id"], result, session="session-1")
        elif method == "tools/list":
            add = dict(name="add", inputSchema=dict(type="object"))
            self.send_json(msg["id"], dict(tools=[add]))
        elif method == "tools/call":
            # Streamed: a notification, a request for the client, then the result
            args = msg["params"]["arguments"]
            events = [
                dict(jsonrpc="2.0", method="notifications/message", params=dict(data="hi")),
                dict(jsonrpc="2.0", id="srv-1", method="ping"),
                dict(
                    jsonrpc="2.0",
                    id=msg["id"],
                    result=dict(content=[dict(type="text", text=str(args["a"] + args["b"]))]),
                ),
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(f"event: message\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()

    def do_DELETE(self):
        self.server.state["deleted"] = self.headers.get("Mcp-Session-Id")
        self.send_response(200)
        self.end_headers()

    def send_json(self, msg_id, result, session=None):
        body = json.dumps(dict(jsonrpc="2.0", id=msg_id, result=result)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)


class McpHttpServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer looks up the host's name, which can take many seconds
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class TestHttpServer(unittest.TestCase):
    def setUp(self):
        self.httpd = McpHttpServer(("127.0.0.1", 0), McpHttpHandler)
        self.httpd.state = dict(seen=[], deleted=None)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/mcp"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_streamable_http(self):
        config = dict(type="http", url=self.url, headers=dict(Authorization="Bearer ${TOKEN}"))
        server = McpServer("web", config, "x.json")
        with patch.dict(os.environ, TOKEN="s3cret"):
            server.connect()
        try:
            self.assertEqual(server.status, "connected", server.error)
            self.assertEqual([tool["name"] for tool in server.tools], ["add"])
            result = server.call_tool("add", dict(a=20, b=22))
            self.assertEqual(format_result(result)[0], "42")

            seen = self.httpd.state["seen"]
            # loom answered the server's ping
            self.assertIn(dict(jsonrpc="2.0", id="srv-1", result={}), [msg for msg, _ in seen])
            # After initializing, requests carry the session and protocol version
            _msg, headers = seen[-1]
            self.assertEqual(headers["Mcp-Session-Id"], "session-1")
            self.assertEqual(headers["MCP-Protocol-Version"], "2025-06-18")
            self.assertIn("text/event-stream", headers["Accept"])
        finally:
            server.close()
        self.assertEqual(self.httpd.state["deleted"], "session-1")

    def test_unauthorized(self):
        server = McpServer("web", dict(url=self.url), "x.json")
        server.connect()
        self.assertEqual(server.status, "failed")
        self.assertIn("Authorization header", server.error)

    def test_old_sse_transport(self):
        server = McpServer("web", dict(type="sse", url=self.url), "x.json")
        server.connect()
        self.assertIn("streamable HTTP", server.error)


class TestAgentWithMcp(HomeDirMixin, unittest.TestCase):
    def make_manager(self, io):
        manager = McpManager(io, [McpServer("test", TEST_SERVER, "test.json")])
        manager.start()
        self.addCleanup(manager.close)
        return manager

    def test_the_agent_uses_mcp_tools(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            coder = make_coder(io, Permissions(io, allow=["mcp(test)"]), mcp=self.make_manager(io))
            llm = FakeLLM(
                reply(
                    None,
                    call("mcp__test__add", a=2, b=3),
                    call("mcp__test__fail"),
                    call("mcp__test__nope"),
                ),
                reply("2 + 3 = 5"),
            )
            out = stdio.StringIO()
            with patch.object(litellm, "completion", llm), contextlib.redirect_stdout(out):
                coder.run(with_message="add 2 and 3")

            offered = {t["function"]["name"]: t["function"] for t in llm.requests[0]["tools"]}
            self.assertIn("read_file", offered)
            self.assertIn("mcp__test__add", offered)
            self.assertEqual(
                offered["mcp__test__add"]["description"], "[test MCP server] Add two numbers."
            )
            self.assertNotIn("$schema", offered["mcp__test__add"]["parameters"])
            system = llm.requests[0]["messages"][0]["content"]
            self.assertIn("Use add for arithmetic.", system)

            results = tool_results(llm.requests[1]["messages"])
            self.assertEqual(results["call_1_0"], "5")
            self.assertEqual(results["call_1_1"], "Error: it broke")
            self.assertIn("no tool named 'mcp__test__nope'", results["call_1_2"])

            self.assertIn("● test - add (MCP)(a: 2, b: 3)", out.getvalue())
            self.assertIn("  ⎿  5", out.getvalue())
            self.assertIn("MCP: test (6 tools)", "\n".join(coder.get_announcements()))

    def test_mcp_permissions(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            manager = self.make_manager(io)

            def decide(mode, rules, name, **args):
                permissions = Permissions(io, mode=mode, allow=rules)
                coder = make_coder(io, permissions, mcp=manager)
                return permissions.decide(tools.prepare(coder, name, args))

            self.assertEqual(decide("ask", [], "mcp__test__add", a=1, b=2), "ask")
            self.assertEqual(decide("accept-edits", [], "mcp__test__echo"), "ask")
            self.assertEqual(decide("ask", ["mcp(test)"], "mcp__test__echo"), "allow")
            self.assertEqual(decide("ask", ["mcp(test__e*)"], "mcp__test__echo"), "allow")
            self.assertEqual(decide("ask", ["mcp(test__e*)"], "mcp__test__fail"), "ask")
            self.assertEqual(decide("ask", ["mcp(other)"], "mcp__test__echo"), "ask")
            self.assertEqual(decide("ask", ["mcp"], "mcp__test__echo"), "allow")
            # Plan mode allows the tools their server says are read-only
            self.assertEqual(decide("plan", [], "mcp__test__add", a=1, b=2), "allow")
            self.assertEqual(decide("plan", ["mcp"], "mcp__test__echo"), "deny")

            # --yes-always doesn't approve them, and "always" saves the exact tool
            permissions = Permissions(io, settings_file=".loom.permissions.json")
            coder = make_coder(io, permissions, mcp=manager)
            action = tools.prepare(coder, "mcp__test__echo", dict(text="x"))
            self.assertEqual(permissions.request(action)[0], "user-deny")
            io.yes = None
            io.permission_ask = MagicMock(return_value="always")
            self.assertEqual(permissions.request(action)[0], "allow")
            self.assertIn("mcp(test__echo)", Path(".loom.permissions.json").read_text())
            self.assertIn("Use the test MCP tool echo?", io.permission_ask.call_args[0][0])

    def test_main_and_mcp_command(self):
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        from loom.main import main

        with GitTemporaryDirectory():
            Path("servers.json").write_text(json.dumps(dict(mcpServers=dict(test=TEST_SERVER))))
            args = ["--model", "gpt-4o-mini", "--no-git", "--yes-always", "--exit"]
            with patch.dict(os.environ, OPENAI_API_KEY="deadbeef", LOOM_CHECK_UPDATE="false"):
                coder = main(
                    args + ["--mcp-config", "servers.json"],
                    input=DummyInput(),
                    output=DummyOutput(),
                    return_coder=True,
                )
                self.addCleanup(coder.mcp.close)
                self.assertEqual(coder.mcp.servers["test"].status, "connected")

                out = stdio.StringIO()
                with contextlib.redirect_stdout(out):
                    coder.commands.cmd_mcp("")
                    coder.commands.cmd_mcp("tools")
                self.assertIn("test: connected, 6 tools", out.getvalue())
                self.assertIn("mcp__test__add  Add two numbers.", out.getvalue())

                self.assertIsNone(
                    main(
                        args + ["--no-mcp"],
                        input=DummyInput(),
                        output=DummyOutput(),
                        return_coder=True,
                    ).mcp
                )
                self.assertEqual(
                    main(
                        args + ["--mcp-config", "missing.json"],
                        input=DummyInput(),
                        output=DummyOutput(),
                        return_coder=True,
                    ),
                    1,
                )

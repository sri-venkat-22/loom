import contextlib
import io as stdio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from loom import stitch, tools
from loom.io import InputOutput
from loom.llm import litellm
from loom.mcp import McpManager, McpServer, builtin_servers
from loom.orchestrator import Orchestrator, ProjectState
from loom.permissions import Permissions
from loom.phases import PHASES_BY_KEY
from loom.utils import GitTemporaryDirectory

from .test_agent import FakeLLM, call, make_coder, make_repo, reply, tool_results
from .test_mcp import HomeDirMixin

STITCH_SERVER = dict(
    command=sys.executable,
    args=[str(Path(__file__).parent.parent / "fixtures" / "stitch_server.py")],
)
HTML = '<!DOCTYPE html>\n<html><head><script src="https://cdn.tailwindcss.com"></script></head>\n<body class="bg-slate-950">Home</body></html>\n'  # noqa: E501


def fake_download(url, transport=None):
    assert url == "https://files.example/s-home.html", url
    return HTML


class TestConfig(HomeDirMixin, unittest.TestCase):
    def test_builtin_server_needs_the_api_key(self):
        self.assertEqual(builtin_servers({}), [])
        self.assertEqual(builtin_servers({"STITCH_API_KEY": " "}), [])
        [(name, config, source)] = builtin_servers({"STITCH_API_KEY": "key"})
        self.assertEqual(name, "stitch")
        self.assertEqual(config["url"], "https://stitch.googleapis.com/mcp")
        self.assertEqual(config["headers"], {"X-Goog-Api-Key": "${STITCH_API_KEY}"})
        self.assertIn("STITCH_API_KEY", source)

    def test_from_config_adds_stitch_unless_configured_or_disabled(self):
        io = InputOutput(yes=True)
        with GitTemporaryDirectory() as root:
            os.environ.pop("STITCH_API_KEY", None)
            self.assertNotIn("stitch", McpManager.from_config(io, root).servers)

            os.environ["STITCH_API_KEY"] = "secret-key"
            manager = McpManager.from_config(io, root)
            server = manager.servers["stitch"]
            self.assertTrue(server.builtin)
            # Loom's own server needs no approval, even with a config file in the project
            self.assertFalse(server.is_project_server)
            self.assertTrue(manager.is_approved(server))
            # The question and /mcp show the variable, never the key
            self.assertIn("header: X-Goog-Api-Key: ${STITCH_API_KEY}", server.describe())
            self.assertNotIn("secret-key", server.describe())
            self.assertIn("built in", server.source_name())

            # Not without the default files
            self.assertNotIn(
                "stitch", McpManager.from_config(io, root, use_default_files=False).servers
            )

            # A config file's own "stitch" server wins
            user = Path(self.home.name) / ".loom" / "mcp.json"
            user.parent.mkdir(exist_ok=True)
            user.write_text(json.dumps(dict(mcpServers=dict(stitch=STITCH_SERVER))))
            server = McpManager.from_config(io, root).servers["stitch"]
            self.assertFalse(server.builtin)
            self.assertEqual(server.config, STITCH_SERVER)

            # And "disabled": true turns it off, with no command or url needed
            user.write_text(json.dumps(dict(mcpServers=dict(stitch=dict(disabled=True)))))
            self.assertNotIn("stitch", McpManager.from_config(io, root).servers)

    def test_parse_screen(self):
        self.assertEqual(stitch.parse_screen("projects/12/screens/ab"), ("12", "ab"))
        self.assertEqual(stitch.parse_screen(" ab ", "projects/12"), ("12", "ab"))
        self.assertEqual(stitch.parse_screen("screens/ab", "12"), ("12", "ab"))
        for screen, project in [("ab", None), ("", "12"), ("x/y", "12"), ("projects/12", None)]:
            with self.assertRaises(stitch.StitchError):
                stitch.parse_screen(screen, project)

    def test_payload(self):
        self.assertEqual(stitch.payload(dict(structuredContent=dict(a=1), content=[])), dict(a=1))
        result = dict(content=[dict(type="text", text='{"title": "Home"}')])
        self.assertEqual(stitch.payload(result), dict(title="Home"))
        for result in [
            dict(content=[dict(type="text", text="not json")]),
            dict(content=[dict(type="text", text="[1]")]),
            dict(content=[dict(type="text", text="denied")], isError=True),
        ]:
            with self.assertRaises(stitch.StitchError):
                stitch.payload(result)

    def test_download(self):
        def handler(request):
            if request.url.path == "/ok.html":
                return httpx.Response(200, content=HTML.replace("\n", "\r\n").encode())
            if request.url.path == "/big.html":
                return httpx.Response(200, content=b"x" * (stitch.MAX_HTML_BYTES + 1))
            return httpx.Response(403, content=b"expired")

        transport = httpx.MockTransport(handler)
        self.assertEqual(stitch.download("https://files.example/ok.html", transport), HTML)
        for url, message in [
            ("https://files.example/gone.html", "HTTP 403"),
            ("https://files.example/big.html", "over 5 MB"),
            ("http://files.example/ok.html", "non-https"),
        ]:
            with self.assertRaisesRegex(stitch.StitchError, message):
                stitch.download(url, transport)


class TestAgentWithStitch(HomeDirMixin, unittest.TestCase):
    def make_manager(self, io, name="design"):
        manager = McpManager(io, [McpServer(name, STITCH_SERVER, "test.json")])
        manager.start()
        self.addCleanup(manager.close)
        return manager

    def test_the_agent_designs_and_saves_screens(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            # Any server with Stitch's tools counts, whatever its name
            manager = self.make_manager(io)
            self.assertEqual(manager.stitch().name, "design")
            coder = make_coder(io, Permissions(io, allow=["mcp(design)"]), mcp=manager)
            llm = FakeLLM(
                reply(
                    None,
                    call(
                        "mcp__design__generate_screen_from_text",
                        projectId="p-1",
                        prompt="A landing page",
                        deviceType="DESKTOP",
                    ),
                    call(
                        "save_stitch_screen",
                        screen="projects/p-1/screens/s-home",
                        path="site/index.html",
                    ),
                    call(
                        "save_stitch_screen",
                        screen="projects/p-1/screens/s-wip",
                        path="site/p.html",
                    ),
                    call("save_stitch_screen", screen="s-nope", project_id="p-1", path="x.html"),
                    call("save_stitch_screen", screen="s-home", path="x.html"),
                ),
                reply("Designed and saved the home page."),
            )
            out = stdio.StringIO()
            with (
                patch.object(litellm, "completion", llm),
                patch.object(stitch, "download", fake_download),
                contextlib.redirect_stdout(out),
            ):
                coder.run(with_message="design me a landing page")

            offered = [t["function"]["name"] for t in llm.requests[0]["tools"]]
            self.assertIn("save_stitch_screen", offered)
            self.assertIn("mcp__design__generate_screen_from_text", offered)
            system = llm.requests[0]["messages"][0]["content"]
            self.assertIn("# Designing with Google Stitch", system)
            self.assertIn("The design MCP server is Google Stitch", system)

            results = tool_results(llm.requests[1]["messages"])
            self.assertIn("assets/ds-1", results["call_1_0"])
            self.assertIn("Saved the HTML of the Stitch screen 'Home'", results["call_1_1"])
            self.assertIn("Created site/index.html", results["call_1_1"])
            self.assertIn("https://files.example/s-home.png", results["call_1_1"])
            self.assertEqual(Path("site/index.html").read_text(), HTML)
            self.assertIn("has no HTML for projects/p-1/screens/s-wip yet", results["call_1_2"])
            self.assertFalse(Path("site/p.html").exists())
            self.assertIn("Screen projects/p-1/screens/s-nope not found", results["call_1_3"])
            self.assertIn("screen must be the screen's resource name", results["call_1_4"])
            self.assertIn("● Stitch(site/index.html)", out.getvalue())

    def test_saving_is_an_edit(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            manager = self.make_manager(io)

            def prepare(path, mode="ask", rules=()):
                coder = make_coder(io, Permissions(io, mode=mode, allow=list(rules)), mcp=manager)
                with patch.object(stitch, "download", fake_download):
                    action = tools.prepare(
                        coder,
                        "save_stitch_screen",
                        dict(screen="projects/p-1/screens/s-home", path=path),
                    )
                return coder.permissions.decide(action), action

            decision, action = prepare("site/index.html")
            self.assertEqual(
                (action.kind, action.target, decision), ("edit", "site/index.html", "ask")
            )
            self.assertTrue(action.new_file)
            self.assertIn("+<!DOCTYPE html>", action.preview)
            self.assertEqual(prepare("site/index.html", mode="accept-edits")[0], "allow")
            # Protected files stay protected
            self.assertEqual(prepare(".env", mode="accept-edits")[0], "ask")

    def test_not_offered_without_stitch(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            coder = make_coder(io)
            offered = [t["function"]["name"] for t in coder.tools]
            self.assertNotIn("save_stitch_screen", offered)
            self.assertNotIn("Google Stitch", coder.format_messages().all_messages()[0]["content"])
            with self.assertRaisesRegex(tools.ToolError, "no Google Stitch MCP server"):
                tools.prepare(coder, "save_stitch_screen", dict(screen="s", path="x.html"))


class TestPhasesWithStitch(HomeDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()
        self.addCleanup(self.memory_store.stop)

    def make_agent(self, key, manager, io):
        coder = make_coder(io, mcp=manager)
        state = ProjectState.new(coder.root, "A landing page for a bakery")
        return Orchestrator(coder, state).make_agent(PHASES_BY_KEY[key])

    def test_design_phase_designs_with_stitch(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None, pretty=False)
            io.permission_ask = MagicMock(return_value="yes")
            manager = McpManager(io, [McpServer("stitch", STITCH_SERVER, "test.json")])
            manager.start()
            self.addCleanup(manager.close)

            design = self.make_agent("design", manager, io)
            names = [tool["function"]["name"] for tool in design.tools]
            self.assertIn("mcp__stitch__generate_screen_from_text", names)
            # Design can't write the code, so it doesn't save screens
            self.assertNotIn("save_stitch_screen", names)
            system = design.format_messages().all_messages()[0]["content"]
            self.assertIn("# Designing with Google Stitch", system)
            self.assertIn("## UI design", system)
            self.assertIn("Google Stitch's tools (mcp__stitch__*)", system)
            self.assertIsNone(
                design.refuse_action("mcp__stitch__get_screen", MagicMock(kind="mcp"))
            )

            planning = self.make_agent("planning", manager, io)
            names = [tool["function"]["name"] for tool in planning.tools]
            self.assertFalse([name for name in names if "stitch" in name])
            self.assertIn(
                "can't use", planning.refuse_action("mcp__stitch__get_screen", MagicMock())
            )
            system = planning.format_messages().all_messages()[0]["content"]
            self.assertNotIn("Google Stitch", system)

            # Building is the coding agent: it saves the screens
            building = self.make_agent("building", manager, io)
            names = [tool["function"]["name"] for tool in building.tools]
            self.assertIn("save_stitch_screen", names)
            self.assertIn("mcp__stitch__get_screen", names)

    def test_design_phase_without_stitch(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            design = self.make_agent("design", None, io)
            names = [tool["function"]["name"] for tool in design.tools]
            self.assertFalse([name for name in names if "stitch" in name])
            system = design.format_messages().all_messages()[0]["content"]
            self.assertNotIn("Stitch", system)
            self.assertIn("read_file, list_dir, glob, grep, web_search", system)


class TestGetScreenArguments(unittest.TestCase):
    def test_sends_only_the_arguments_the_schema_lists(self):
        def server(properties):
            server = MagicMock()
            server.tools = [dict(name="get_screen", inputSchema=dict(properties=properties))]
            server.call_tool.return_value = dict(structuredContent=dict(title="Home"))
            return server

        name = "projects/p/screens/s"
        for properties, expected in [
            (dict(name={}), dict(name=name)),
            (dict(projectId={}, screenId={}), dict(projectId="p", screenId="s")),
            ({}, dict(name=name, projectId="p", screenId="s")),
            (dict(other={}), dict(name=name, projectId="p", screenId="s")),
        ]:
            fake = server(properties)
            self.assertEqual(stitch.get_screen(fake, "p", "s"), dict(title="Home"))
            fake.call_tool.assert_called_once_with("get_screen", expected)

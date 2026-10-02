import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import git
from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    ChatCompletionMessageToolCall,
    Choices,
    Delta,
    Function,
    Message,
    ModelResponse,
    ModelResponseStream,
    StreamingChoices,
)

from loom import tools
from loom.coders import Coder
from loom.coders.agent_coder import MAX_OLD_TOOL_RESULT_CHARS
from loom.io import InputOutput
from loom.llm import litellm
from loom.models import Model
from loom.permissions import (
    SETTINGS_FILE,
    Permissions,
    Rule,
    approvals_file,
    canonical_path,
    split_command,
)
from loom.sendchat import sanity_check_messages
from loom.utils import (
    GitTemporaryDirectory,
    IgnorantTemporaryDirectory,
    flatten_tool_messages,
)


def call(name, **args):
    return (name, args)


def reply(content=None, *tool_calls, thinking=None, reasoning_details=None):
    return dict(
        content=content,
        tool_calls=list(tool_calls),
        thinking=thinking,
        reasoning_details=reasoning_details,
    )


def full_response(scripted, turn):
    tool_calls = [
        ChatCompletionMessageToolCall(
            id=f"call_{turn}_{num}",
            type="function",
            function=Function(name=name, arguments=json.dumps(args)),
        )
        for num, (name, args) in enumerate(scripted["tool_calls"])
    ]
    extra = {}
    if scripted["thinking"]:
        block = dict(type="thinking", thinking=scripted["thinking"], signature=f"sig-{turn}")
        extra["thinking_blocks"] = [block]
    if scripted["reasoning_details"]:
        extra["reasoning_details"] = scripted["reasoning_details"]
    message = Message(content=scripted["content"], tool_calls=tool_calls or None, **extra)
    finish = "tool_calls" if tool_calls else "stop"
    return ModelResponse(choices=[Choices(message=message, finish_reason=finish)])


def stream_response(scripted, turn):
    """Stream like the OpenAI API: the content, then each tool call's id and name followed
    by its arguments in two pieces."""
    chunks = []
    if scripted["thinking"]:
        # Anthropic streams the thinking in pieces, then its signature
        text = scripted["thinking"]
        for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
            block = dict(type="thinking", thinking=piece, signature="")
            chunks.append(Delta(reasoning_content=piece, thinking_blocks=[block]))
        block = dict(type="thinking", thinking="", signature=f"sig-{turn}")
        chunks.append(Delta(thinking_blocks=[block]))
    for detail in scripted["reasoning_details"] or []:
        # OpenRouter streams reasoning_details the same way, merged by index
        text = detail["text"]
        chunks.append(Delta(reasoning_details=[dict(detail, text=text[:3], signature=None)]))
        chunks.append(Delta(reasoning_details=[dict(index=detail["index"], text=text[3:])]))
    if scripted["content"]:
        chunks.append(Delta(content=scripted["content"]))
    for num, (name, args) in enumerate(scripted["tool_calls"]):
        arguments = json.dumps(args)
        half = len(arguments) // 2
        first = ChatCompletionDeltaToolCall(
            index=num,
            id=f"call_{turn}_{num}",
            type="function",
            function=Function(name=name, arguments=arguments[:half]),
        )
        rest = ChatCompletionDeltaToolCall(index=num, function=Function(arguments=arguments[half:]))
        chunks.append(Delta(content=None, tool_calls=[first]))
        chunks.append(Delta(content=None, tool_calls=[rest]))

    for delta in chunks:
        yield ModelResponseStream(choices=[StreamingChoices(delta=delta)])
    finish = "tool_calls" if scripted["tool_calls"] else "stop"
    yield ModelResponseStream(choices=[StreamingChoices(delta=Delta(), finish_reason=finish)])


class FakeLLM:
    """Stands in for litellm.completion. Requests that offer tools get the scripted replies in
    order; others (commit messages) get a canned commit message."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, **kwargs):
        if not kwargs.get("tools"):
            message = Message(content="fix: scripted change")
            return ModelResponse(choices=[Choices(message=message, finish_reason="stop")])

        self.requests.append(kwargs)
        if not self.replies:
            raise AssertionError("The agent asked the model for more replies than scripted")
        scripted = self.replies.pop(0)
        turn = len(self.requests)
        if kwargs["stream"]:
            return stream_response(scripted, turn)
        return full_response(scripted, turn)


def make_repo():
    repo = git.Repo()
    Path("calc.py").write_text("def add(a, b):\n    return a - b\n")
    Path("test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    Path(".gitignore").write_text("secret/\n")
    repo.git.add(".")
    repo.git.commit("-m", "initial")
    return repo


def make_coder(io=None, permissions=None, stream=False, **kwargs):
    io = io or InputOutput(yes=True)
    return Coder.create(
        Model("gpt-4o-mini"),
        "agent",
        io=io,
        map_tokens=0,
        stream=stream,
        permissions=permissions or Permissions(io),
        **kwargs,
    )


def tool_results(messages):
    return {msg["tool_call_id"]: msg["content"] for msg in messages if msg["role"] == "tool"}


PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider test_calc.py"

FIX_THE_TEST = [
    reply(
        "Let me find the failing test.",
        call("grep", pattern="def add"),
        call("glob", pattern="**/test_*.py"),
    ),
    reply(None, call("read_file", path="calc.py")),
    reply(None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
    reply(None, call("bash", command=PYTEST)),
    reply("Fixed `add` to use +; the test passes now."),
]


class TestAgentLoop(unittest.TestCase):
    def run_fix_the_test(self, stream):
        with GitTemporaryDirectory():
            repo = make_repo()
            io = InputOutput(yes=True)
            permissions = Permissions(io, allow=[f"bash({sys.executable} -m pytest*)"])
            coder = make_coder(io, permissions, stream=stream)
            llm = FakeLLM(*FIX_THE_TEST)

            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix the failing test")

            self.assertEqual(Path("calc.py").read_text(), "def add(a, b):\n    return a + b\n")
            self.assertEqual(len(llm.requests), 5)
            self.assertEqual(llm.replies, [])

            # Every tool was offered, and both calls of the first reply were answered
            names = [t["function"]["name"] for t in llm.requests[0]["tools"]]
            self.assertEqual(
                names,
                [
                    "read_file",
                    "list_dir",
                    "glob",
                    "grep",
                    "edit_file",
                    "write_file",
                    "bash",
                    "todo_write",
                ],
            )
            results = tool_results(llm.requests[1]["messages"])
            self.assertIn("calc.py:1: def add(a, b):", results["call_1_0"])
            self.assertIn("test_calc.py", results["call_1_1"])

            results = tool_results(llm.requests[4]["messages"])
            self.assertIn("return a - b", results["call_2_0"])
            self.assertIn("Edited calc.py", results["call_3_0"])
            self.assertIn("Exit code: 0", results["call_4_0"])
            self.assertIn("1 passed", results["call_4_0"])

            # The edit was committed, so /undo can revert it
            self.assertEqual(len(list(repo.iter_commits())), 2)
            self.assertFalse(repo.is_dirty())
            self.assertIn(repo.head.commit.hexsha[:7], coder.loom_commit_hashes)

            # The request moved into the history, which stays a valid conversation
            self.assertEqual(coder.cur_messages, [])
            self.assertEqual(coder.done_messages[0]["content"], "fix the failing test")
            self.assertEqual(coder.done_messages[-1]["role"], "assistant")
            sanity_check_messages(coder.done_messages + [dict(role="user", content="next")])
            return coder

    def test_fix_the_failing_test(self):
        self.run_fix_the_test(stream=False)

    def test_fix_the_failing_test_streaming(self):
        self.run_fix_the_test(stream=True)

    def test_user_denial_stops_the_loop(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            coder = make_coder(io)
            llm = FakeLLM(
                reply(
                    None,
                    call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                    call("read_file", path="calc.py"),
                ),
                reply("This reply should never be requested."),
            )

            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix it")

            self.assertEqual(len(llm.requests), 1)
            self.assertIn("a - b", Path("calc.py").read_text())
            io.permission_ask.assert_called_once()

            results = list(tool_results(coder.done_messages).values())
            self.assertIn("denied", results[0])
            self.assertIn("Not run", results[1])

    def test_plan_mode_refuses_changes_and_carries_on(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io, Permissions(io, mode="plan"))
            llm = FakeLLM(
                reply(
                    None,
                    call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                    call("bash", command="rm calc.py"),
                ),
                reply("Plan: change a - b to a + b in calc.py, then run pytest."),
            )

            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix it")

            self.assertEqual(len(llm.requests), 2)
            self.assertIn("a - b", Path("calc.py").read_text())
            system = llm.requests[0]["messages"][0]["content"]
            self.assertIn("plan mode", system)
            for result in tool_results(llm.requests[1]["messages"]).values():
                self.assertIn("plan mode", result)

    def test_tool_errors_go_back_to_the_model(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            llm = FakeLLM(
                reply(
                    None,
                    call("read_file", path="missing.py"),
                    call("edit_file", path="calc.py", old_string="nope", new_string="x"),
                    call("no_such_tool"),
                    call("read_file"),
                ),
                reply("Done."),
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="go")

            results = list(tool_results(llm.requests[1]["messages"]).values())
            self.assertIn("missing.py does not exist", results[0])
            self.assertIn("old_string was not found", results[1])
            self.assertIn("no tool named", results[2])
            self.assertIn("missing required argument: path", results[3])

    def test_dirty_file_is_committed_before_the_agent_edits_it(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            Path("calc.py").write_text("def add(a, b):\n    return a - b  # user edit\n")
            coder = make_coder()
            llm = FakeLLM(
                reply(
                    None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")
                ),
                reply("Done."),
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="go")

            commits = list(repo.iter_commits())
            self.assertEqual(len(commits), 3)
            # The user's change is committed on its own, before the agent's
            self.assertIn("user edit", commits[1].tree["calc.py"].data_stream.read().decode())
            self.assertIn("a - b", commits[1].tree["calc.py"].data_stream.read().decode())
            self.assertIn("a + b", commits[0].tree["calc.py"].data_stream.read().decode())

    def test_old_tool_results_are_shortened_and_history_works_without_tools(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("big.txt").write_text("line\n" * 5000)
            coder = make_coder()
            llm = FakeLLM(reply(None, call("read_file", path="big.txt")), reply("It's big."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="how big is big.txt?")

            # The model saw the whole result while working...
            full = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertGreater(len(full), MAX_OLD_TOOL_RESULT_CHARS * 2)
            # ...but the history keeps a shorter version
            kept = list(tool_results(coder.done_messages).values())[0]
            self.assertLess(len(kept), MAX_OLD_TOOL_RESULT_CHARS + 200)

            # A coder without tools gets the history as plain text
            ask_coder = Coder.create(from_coder=coder, edit_format="ask")
            messages = ask_coder.format_messages().all_messages()
            self.assertFalse(any(msg["role"] == "tool" for msg in messages))
            self.assertFalse(any(msg.get("tool_calls") for msg in messages))
            self.assertTrue(any("Called read_file" in str(msg["content"]) for msg in messages))

    def test_interrupted_tool_call_is_dropped_from_history(self):
        coder = make_coder()
        coder.cur_messages = [
            dict(role="user", content="go"),
            dict(
                role="assistant",
                content="Reading.",
                tool_calls=[
                    dict(id="a", type="function", function=dict(name="read_file", arguments="{")),
                ],
            ),
            dict(role="user", content="^C KeyboardInterrupt"),
            dict(role="assistant", content="I see that you interrupted my previous reply."),
        ]
        tidy = coder.tidy_tool_messages(coder.cur_messages)
        self.assertNotIn("tool_calls", tidy[1])
        self.assertEqual(tidy[1]["content"], "Reading.")


class TestThinking(unittest.TestCase):
    def run_with_thinking(self, stream, **thinking):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder(stream=stream)
            llm = FakeLLM(
                reply(None, call("read_file", path="calc.py"), **thinking),
                reply(None, call("list_dir"), **thinking),
                reply("Done."),
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="look around")

            sent = [m for m in llm.requests[2]["messages"] if m.get("tool_calls")]
            self.assertEqual(len(sent), 2)
            # The thinking isn't sent back as the reply's text as well
            for msg in sent:
                self.assertFalse(msg.get("content"))
            # Only the last tool-calling message keeps its thinking in the history
            history = [m for m in coder.done_messages if m.get("tool_calls")]
            self.assertNotIn("thinking_blocks", history[0])
            self.assertNotIn("reasoning_details", history[0])
            return sent

    def check_thinking_blocks(self, stream):
        sent = self.run_with_thinking(stream, thinking="I should read calc.py first.")
        for turn, msg in enumerate(sent, 1):
            self.assertEqual(
                msg["thinking_blocks"],
                [
                    dict(
                        type="thinking",
                        thinking="I should read calc.py first.",
                        signature=f"sig-{turn}",
                    )
                ],
            )

    def test_thinking_blocks_go_back_with_tool_calls(self):
        self.check_thinking_blocks(stream=False)

    def test_streamed_thinking_blocks_go_back_with_tool_calls(self):
        self.check_thinking_blocks(stream=True)

    def test_openrouter_reasoning_details_go_back_with_tool_calls(self):
        details = [
            dict(
                type="reasoning.text", text="Reading calc.py", index=0, format="anthropic-claude-v1"
            )
        ]
        for stream in (False, True):
            sent = self.run_with_thinking(stream, reasoning_details=details)
            for msg in sent:
                self.assertEqual(msg["reasoning_details"][0]["text"], "Reading calc.py")
                self.assertEqual(msg["reasoning_details"][0]["format"], "anthropic-claude-v1")

    def test_reasoning_cut_off_is_removed_from_the_reply(self):
        from loom.reasoning_tags import REASONING_TAG

        coder = make_coder()
        coder.partial_response_content = f"<{REASONING_TAG}>\n\nI was thinking about"
        coder.remove_reasoning_content()
        self.assertEqual(coder.partial_response_content, "")

        coder.partial_response_content = f"<{REASONING_TAG}>\n\nHmm\n\n</{REASONING_TAG}>\n\nHi"
        coder.remove_reasoning_content()
        self.assertEqual(coder.partial_response_content, "Hi")

    def test_unsigned_thinking_is_not_sent_back(self):
        coder = make_coder()
        coder.partial_response_thinking_blocks = []
        coder.partial_response_reasoning_details = []
        coder.add_thinking_block_delta(dict(type="thinking", thinking="half a thought"))
        self.assertEqual(coder.get_thinking(), {})


class TestPromptCaching(unittest.TestCase):
    def test_each_step_caches_the_conversation_so_far(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = Coder.create(
                Model("claude-sonnet-4-20250514"),
                "agent",
                io=io,
                map_tokens=0,
                stream=False,
                cache_prompts=True,
            )
            self.assertTrue(coder.add_cache_headers)
            llm = FakeLLM(
                reply(None, call("read_file", path="calc.py")),
                reply(None, call("list_dir")),
                reply("Done."),
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="look around")

            for request in llm.requests:
                messages = request["messages"]
                marked = [
                    m
                    for m in messages
                    if isinstance(m["content"], list) and "cache_control" in m["content"][-1]
                ]
                # Anthropic allows 4 cache breakpoints
                self.assertLessEqual(len(marked), 4)
                self.assertIs(marked[-1], messages[-1])

            self.assertEqual(llm.requests[1]["messages"][-1]["role"], "tool")
            # The stored conversation isn't changed by the marker
            for msg in coder.done_messages:
                self.assertFalse(isinstance(msg.get("content"), list))

    def test_usage_report_totals_cache_hits_over_the_steps(self):
        from litellm.types.utils import Usage

        coder = make_coder()
        coder.partial_response_content = ""
        for prompt, cached in [(1133, 0), (1301, 1024), (1417, 1152)]:
            usage = Usage(
                prompt_tokens=prompt,
                completion_tokens=25,
                total_tokens=prompt + 25,
                prompt_tokens_details={"cached_tokens": cached},
            )
            coder.calculate_and_show_tokens_and_cost(
                [], ModelResponse(model="gpt-4o-mini", usage=usage)
            )
        self.assertTrue(
            coder.usage_report.startswith("Tokens: 3.9k sent, 2.2k cache hit, 75 received.")
        )

    def test_no_markers_without_caching(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            llm = FakeLLM(reply(None, call("list_dir")), reply("Done."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="look around")
            for msg in llm.requests[1]["messages"]:
                self.assertFalse(isinstance(msg["content"], list))


class TestToolCallCollection(unittest.TestCase):
    def test_streamed_calls_are_merged_by_index(self):
        coder = make_coder()
        coder.partial_response_tool_calls = []
        pieces = [
            ChatCompletionDeltaToolCall(
                index=0, id="x", function=Function(name="grep", arguments='{"pat')
            ),
            ChatCompletionDeltaToolCall(
                index=1, id="y", function=Function(name="glob", arguments="")
            ),
            ChatCompletionDeltaToolCall(index=0, function=Function(arguments='tern": "a"}')),
            ChatCompletionDeltaToolCall(index=1, function=Function(arguments='{"pattern": "*"}')),
        ]
        for piece in pieces:
            coder.add_tool_call_delta(piece)

        calls = coder.get_tool_calls()
        self.assertEqual([c["id"] for c in calls], ["x", "y"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"pattern": "a"})
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"pattern": "*"})


class TestTools(unittest.TestCase):
    def test_glob_match(self):
        self.assertTrue(tools.glob_match("**/*.py", "a.py"))
        self.assertTrue(tools.glob_match("**/*.py", "x/y/a.py"))
        self.assertFalse(tools.glob_match("*.py", "x/a.py"))
        self.assertTrue(tools.glob_match("src/*.{js,ts}", "src/a.ts"))
        self.assertFalse(tools.glob_match("src/*.{js,ts}", "src/a.py"))
        self.assertTrue(tools.glob_match("tests/**", "tests/a/b.py"))
        self.assertTrue(tools.glob_match("test_[ab].py", "test_a.py"))

    def run_tool(self, coder, name, **args):
        return tools.prepare(coder, name, args).run()

    def test_read_file_numbers_lines_and_pages(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            Path("long.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)))
            out = self.run_tool(coder, "read_file", path="long.txt", offset=3, limit=2)
            self.assertEqual(out.splitlines()[:2], ["     3\tline 3", "     4\tline 4"])
            self.assertIn("Showing lines 3-4 of 10", out)
            with self.assertRaises(tools.ToolError):
                tools.prepare(coder, "read_file", dict(path="nope.txt")).run()

    def test_search_skips_gitignored_files(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("secret").mkdir()
            Path("secret/keys.py").write_text("def add(): pass\n")
            Path("new_module.py").write_text("def add_more(): pass\n")
            coder = make_coder()

            found = self.run_tool(coder, "glob", pattern="**/*.py").splitlines()
            self.assertEqual(found, ["calc.py", "new_module.py", "test_calc.py"])

            out = self.run_tool(coder, "grep", pattern=r"def add", glob="*.py")
            self.assertIn("calc.py:1:", out)
            self.assertIn("new_module.py:1:", out)
            self.assertNotIn("secret", out)

            out = self.run_tool(coder, "grep", pattern="ADD", ignore_case=True, files_only=True)
            self.assertEqual(out.splitlines(), ["calc.py", "new_module.py", "test_calc.py"])

            out = self.run_tool(coder, "list_dir", path=".")
            self.assertEqual(out.splitlines()[0], "secret/")
            self.assertNotIn(".git/", out)

    def test_edit_file_needs_a_unique_match(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            Path("x.txt").write_text("a\na\nb\n")
            with self.assertRaisesRegex(tools.ToolError, "appears 2 times"):
                tools.prepare(
                    coder, "edit_file", dict(path="x.txt", old_string="a", new_string="c")
                )
            action = tools.prepare(
                coder,
                "edit_file",
                dict(path="x.txt", old_string="a", new_string="c", replace_all=True),
            )
            self.assertIn("-a", action.preview)
            self.assertIn("+c", action.preview)
            self.assertIn("replaced 2 occurrences", action.run())
            self.assertEqual(Path("x.txt").read_text(), "c\nc\nb\n")

    def test_write_file_creates_directories(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            action = tools.prepare(coder, "write_file", dict(path="pkg/new.py", content="x = 1\n"))
            self.assertTrue(action.new_file)
            self.assertEqual(action.target, "pkg/new.py")
            self.assertIn("Created pkg/new.py", action.run())
            self.assertEqual(Path("pkg/new.py").read_text(), "x = 1\n")

    def test_paths_outside_the_project(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            action = tools.prepare(coder, "read_file", dict(path="../outside.txt"))
            self.assertFalse(action.inside)
            self.assertTrue(os.path.isabs(action.target))

    def test_bash_reports_exit_code_and_timeout(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            # Python instead of shell builtins, so the commands work in cmd.exe too
            py = f'"{sys.executable}" -c'
            out = self.run_tool(coder, "bash", command=f'{py} "print(1); raise SystemExit(3)"')
            self.assertEqual(out, "Exit code: 3\n1")
            out = self.run_tool(
                coder, "bash", command=f'{py} "import time; time.sleep(5)"', timeout=1
            )
            self.assertIn("Timed out after 1 seconds", out)

    def test_coerce_args(self):
        self.assertEqual(
            tools.coerce_args("read_file", dict(path="a", offset="3", extra=1)),
            dict(path="a", offset=3),
        )
        with self.assertRaises(tools.ToolError):
            tools.coerce_args("read_file", dict(path="a", offset="three"))


def action(kind, target, inside=True):
    return tools.Action(kind, target, inside, "title", run=lambda: "")


class TestMcpPermissionsHardened(unittest.TestCase):
    def setUp(self):
        self.home = IgnorantTemporaryDirectory()
        self.home_patcher = patch("pathlib.Path.home", return_value=Path(self.home.name))
        self.home_patcher.start()

    def tearDown(self):
        self.home_patcher.stop()
        self.home.cleanup()

    def _mcp_action(self, server_name, tool_name_, read_only=False):
        """A fake MCP Action that carries the (server, tool) in extra, like the real
        mcp_tool() builds."""
        from types import SimpleNamespace

        server = SimpleNamespace(name=server_name)
        tool = dict(name=tool_name_)
        return tools.Action(
            "mcp",
            f"{server_name}__{tool_name_}",
            True,
            "t",
            run=lambda: "",
            extra=dict(read_only=read_only, server=server, tool=tool),
        )

    def test_rule_for_github_does_not_cover_a_server_whose_name_starts_with_github(self):
        """mcp(github) matched \'github__issues__get\' through the old glob fallback."""
        perms = Permissions(InputOutput(yes=None), allow=["mcp(github)"])
        self.assertEqual(perms.decide(self._mcp_action("github", "get_issue")), "allow")
        self.assertEqual(perms.decide(self._mcp_action("githubsneaky", "wipe")), "ask")

    def test_an_always_approval_for_one_tool_does_not_cover_a_sibling(self):
        from loom.permissions import exact_rule

        perms = Permissions(InputOutput(yes=None), allow=[])
        # The user said "always" to github\'s get_issue
        perms.add_rule(exact_rule("mcp", "github__get_issue"), "test")
        self.assertEqual(perms.decide(self._mcp_action("github", "get_issue")), "allow")
        # But a sibling tool still asks
        self.assertEqual(perms.decide(self._mcp_action("github", "delete_repo")), "ask")

    def test_plan_mode_ignores_server_declared_readonly(self):
        io = InputOutput(yes=None)
        perms = Permissions(io, mode="plan")
        destructive = self._mcp_action("db", "drop_table", read_only=True)
        # Server lied about readOnlyHint; plan mode no longer trusts it
        self.assertEqual(perms.decide(destructive), "ask")

    def test_plan_mode_allows_tools_from_user_mcp_readonly_file(self):
        from loom.permissions import mcp_readonly_file

        readonly = mcp_readonly_file()
        readonly.parent.mkdir(parents=True, exist_ok=True)
        readonly.write_text(json.dumps(dict(allow=["mcp(github__get_*)"])))
        perms = Permissions(InputOutput(yes=None), mode="plan")
        self.assertEqual(perms.decide(self._mcp_action("github", "get_issue")), "allow")
        self.assertEqual(perms.decide(self._mcp_action("github", "delete_repo")), "ask")

    def test_mcp_server_name_with_double_underscore_is_rejected(self):
        from loom.mcp import McpError, load_config_file

        with GitTemporaryDirectory():
            Path("c.json").write_text(
                json.dumps(dict(mcpServers={"evil__shadow": dict(command="x")}))
            )
            with self.assertRaises(McpError) as cm:
                load_config_file("c.json")
            self.assertIn("__", str(cm.exception))


class TestToolCallFailuresDoNotBrickSession(unittest.TestCase):
    def test_an_unexpected_exception_still_gets_a_matching_tool_reply(self):
        from unittest.mock import MagicMock

        from loom import tools as agent_tools

        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io)
            # A tool that raises something that\'s neither ToolError nor OSError
            boom = MagicMock(side_effect=OverflowError("int too large"))
            call = dict(
                id="call_1",
                type="function",
                function=dict(name="read_file", arguments='{"path": "ok.py"}'),
            )
            with patch.object(agent_tools, "prepare", boom):
                coder.run_tool_calls([call])
            # Pairing is intact: the assistant\'s tool_calls message would be followed by
            # a tool reply for id "call_1", so a /continue won\'t 400
            replies = [m for m in coder.cur_messages if m.get("role") == "tool"]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["tool_call_id"], "call_1")
            self.assertIn("OverflowError", replies[0]["content"])
            # And the agent isn\'t stopped
            self.assertFalse(coder.stop_requested)

    def test_a_tool_whose_run_raises_still_gets_a_reply(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io, Permissions(io, mode="accept-edits", allow=["bash"]))
            # bash timeout=1e400 → float overflow when int() is called on it
            call = dict(
                id="call_r1",
                type="function",
                function=dict(
                    name="bash",
                    arguments='{"command": "echo hi", "timeout": 1e400}',
                ),
            )
            coder.run_tool_calls([call])
            replies = [m for m in coder.cur_messages if m.get("role") == "tool"]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["tool_call_id"], "call_r1")
            self.assertFalse(coder.stop_requested)


class TestToolSafety(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "needs os.setsid")
    def test_a_process_in_a_new_session_cannot_hold_the_timeout(self):
        with IgnorantTemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "pid"
            script = (
                f"import os, time; os.setsid(); open({str(pid_file)!r},"
                " 'w').write(str(os.getpid())); time.sleep(60)"
            )
            command = f'{sys.executable} -c "{script}" & echo done'
            try:
                with patch.object(tools, "KILL_GRACE", 0.5):
                    start = time.time()
                    code, output = tools.run_command(command, tmp, 1)
                    elapsed = time.time() - start
                self.assertIsNone(code)
                self.assertIn("done", output)
                self.assertLess(elapsed, 10)
            finally:
                for _ in range(50):
                    if pid_file.exists() and pid_file.read_text():
                        break
                    time.sleep(0.1)
                if pid_file.exists() and pid_file.read_text():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_commands_do_not_get_api_keys(self):
        secrets = dict(
            OPENAI_API_KEY="sk-1",
            ANTHROPIC_API_KEY="sk-2",
            GITHUB_TOKEN="ghp",
            AWS_SECRET_ACCESS_KEY="aws",
            DB_PASSWORD="pw",
        )
        with IgnorantTemporaryDirectory() as tmp, patch.dict(os.environ, secrets, LOOM_KEEP="1"):
            script = "import os; print(sorted(os.environ))"
            code, output = tools.run_command(f'{sys.executable} -c "{script}"', tmp, 30)
        self.assertEqual(code, 0, output)
        for name in secrets:
            self.assertNotIn(name, output)
        self.assertIn("LOOM_KEEP", output)
        self.assertIn("PATH", output)

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_search_skips_symlinks_out_of_the_project(self):
        with GitTemporaryDirectory(), IgnorantTemporaryDirectory() as outside:
            secret = Path(outside) / "host_key"
            secret.write_text("SECRET-KEY-MATERIAL\n")
            make_repo()
            os.symlink(secret, "notes.txt")
            os.symlink(outside, "linked_dir")
            Path("inside.txt").write_text("SECRET-KEY-MATERIAL is mentioned here\n")
            os.symlink("inside.txt", "alias.txt")
            repo = git.Repo(".")
            repo.git.add("notes.txt", "alias.txt")
            repo.git.commit("-m", "links")
            os.symlink(secret, "untracked_link.txt")
            coder = make_coder()

            found = tools.prepare(coder, "grep", dict(pattern="SECRET")).run()
            self.assertIn("inside.txt", found)
            self.assertIn("alias.txt", found)
            self.assertNotIn("notes.txt", found)
            self.assertNotIn("untracked_link.txt", found)
            listed = tools.prepare(coder, "glob", dict(pattern="**/*")).run()
            self.assertNotIn("notes.txt", listed)
            self.assertNotIn("host_key", listed)

            # Reading through the link asks, as reading outside the project does
            for name, args in [
                ("read_file", dict(path="notes.txt")),
                ("grep", dict(pattern="SECRET", path="notes.txt")),
                ("grep", dict(pattern="SECRET", path="linked_dir")),
            ]:
                action = tools.prepare(coder, name, args)
                self.assertFalse(action.inside, name)
                self.assertEqual(coder.permissions.decide(action), "ask", name)
            self.assertFalse(tools.is_inside(coder, Path("notes.txt").absolute()))
            self.assertTrue(tools.is_inside(coder, Path("alias.txt").absolute()))

            # Once the user allows reading outside, the folder they named can be searched
            action = tools.prepare(coder, "grep", dict(pattern="SECRET", path=outside))
            self.assertIn("host_key", action.run())


class TestPermissions(unittest.TestCase):
    def setUp(self):
        # Approvals of the project's rules are saved in the home directory
        self.home = IgnorantTemporaryDirectory()
        self.home_patcher = patch("pathlib.Path.home", return_value=Path(self.home.name))
        self.home_patcher.start()

    def tearDown(self):
        self.home_patcher.stop()
        self.home.cleanup()

    def test_split_command(self):
        self.assertEqual(split_command("pytest -q && git status"), ["pytest -q", "git status"])
        self.assertEqual(split_command("echo 'a; b' | wc -l"), ["echo 'a; b'", "wc -l"])
        self.assertEqual(split_command("pytest 2>&1"), ["pytest"])
        self.assertEqual(split_command("pytest >/dev/null 2>&1; ls"), ["pytest", "ls"])
        self.assertIsNone(split_command("pytest $(rm -rf x)"))
        self.assertIsNone(split_command('echo "`rm -rf x`"'))
        self.assertIsNone(split_command("pytest > out.txt"))
        self.assertIsNone(split_command("echo 'unbalanced"))
        # >&WORD writes a file called WORD
        self.assertIsNone(split_command("pytest >&1.txt"))
        self.assertIsNone(split_command("pytest >/dev/null.txt"))

    def test_split_command_fails_closed_on_quoting_it_does_not_parse(self):
        # In $'...' a \' doesn't end the string, so this is ls with one argument followed
        # by touch
        self.assertIsNone(split_command("ls $'\\'' ; touch PWNED ; #'"))
        self.assertIsNone(split_command("echo $'a' && ls"))
        self.assertIsNone(split_command('echo $"a" && ls'))
        self.assertIsNone(split_command("cat <<EOF"))
        self.assertIsNone(split_command("cat <<'EOF'"))
        self.assertIsNone(split_command("diff <(ls a) <(ls b)"))
        self.assertIsNone(split_command("tee >(wc -l)"))

        perms = Permissions(InputOutput(yes=None), allow=["bash(ls*)"])
        self.assertEqual(perms.decide(action("bash", "ls $'\\'' ; touch PWNED ; #'")), "ask")
        self.assertEqual(perms.decide(action("bash", "ls -la")), "allow")

    def test_split_command_for_cmd_exe(self):
        def split(command):
            return split_command(command, windows=True)

        self.assertEqual(
            split("pytest tests\\basic && git status"), ["pytest tests\\basic", "git status"]
        )
        self.assertEqual(split("pytest 2>&1 >NUL"), ["pytest"])
        # cmd.exe doesn't treat ' or \\ as quoting, so the & runs a second command
        self.assertIsNone(split("ls 'a & touch PWNED'"))
        self.assertIsNone(split('echo "\\" & touch PWNED"'))
        self.assertIsNone(split("echo ^& touch PWNED"))
        self.assertIsNone(split("echo %PATH%"))
        self.assertIsNone(split("pytest > out.txt"))
        self.assertIsNone(split("pytest >NUL.txt"))

    def test_modes(self):
        io = InputOutput(yes=None)
        perms = Permissions(io)
        self.assertEqual(perms.decide(action("read", "a.py")), "allow")
        self.assertEqual(perms.decide(action("read", "/etc/hosts", inside=False)), "ask")
        self.assertEqual(perms.decide(action("edit", "a.py")), "ask")
        self.assertEqual(perms.decide(action("bash", "ls")), "ask")

        perms.mode = "accept-edits"
        self.assertEqual(perms.decide(action("edit", "a.py")), "allow")
        self.assertEqual(perms.decide(action("edit", "/tmp/a.py", inside=False)), "ask")
        self.assertEqual(perms.decide(action("bash", "ls")), "ask")

        perms.mode = "plan"
        self.assertEqual(perms.decide(action("read", "a.py")), "allow")
        self.assertEqual(perms.decide(action("edit", "a.py")), "deny")
        self.assertEqual(perms.decide(action("bash", "ls")), "deny")

        with self.assertRaises(ValueError):
            Permissions(io, mode="yolo")

    def test_allow_rules(self):
        io = InputOutput(yes=None)
        perms = Permissions(io, allow=["bash(pytest*)", "bash(git status)", "edit(tests/**)"])
        self.assertEqual(perms.decide(action("bash", "pytest -q tests")), "allow")
        self.assertEqual(perms.decide(action("bash", "pytest && git status")), "allow")
        self.assertEqual(perms.decide(action("bash", "pytest; rm -rf ~")), "ask")
        self.assertEqual(perms.decide(action("bash", "pytest $(rm -rf ~)")), "ask")
        self.assertEqual(perms.decide(action("bash", "pytest > /etc/passwd")), "ask")
        self.assertEqual(perms.decide(action("bash", "git status --short")), "ask")
        self.assertEqual(perms.decide(action("edit", "tests/a/test_x.py")), "allow")
        self.assertEqual(perms.decide(action("edit", "src/x.py")), "ask")

        perms = Permissions(io, allow=["bash"])
        self.assertEqual(perms.decide(action("bash", "anything > at_all")), "allow")

        for bad in ["rm(x)", "", "bash(unclosed"]:
            with self.assertRaises(ValueError):
                Rule.parse(bad)

    def test_always_saves_the_exact_command(self):
        with GitTemporaryDirectory():
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="always")
            perms = Permissions(io, settings_file=SETTINGS_FILE)
            command = "pytest -k 'a*' && ls"
            self.assertEqual(perms.request(action("bash", command))[0], "allow")

            saved = json.loads(Path(SETTINGS_FILE).read_text())["allow"]
            self.assertEqual(len(saved), 1)

            # The saved rule covers exactly that command, in a new session too, without
            # asking to approve it: the user chose it
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock()
            perms = Permissions(io, settings_file=SETTINGS_FILE)
            perms.start()
            io.permission_ask.assert_not_called()
            self.assertEqual(perms.decide(action("bash", command)), "allow")
            self.assertEqual(perms.decide(action("bash", "pytest -k 'ab' && ls")), "ask")

    def test_project_rules_need_approval(self):
        with GitTemporaryDirectory():
            # A cloned repo's rules
            rules = ["bash", "edit(**)", "read"]
            Path(SETTINGS_FILE).write_text(json.dumps(dict(allow=rules)))

            def session(answer, yes=None):
                io = InputOutput(yes=yes)
                io.permission_ask = MagicMock(return_value=answer)
                perms = Permissions(io, settings_file=SETTINGS_FILE)
                # Not used before the user says so
                self.assertEqual(perms.decide(action("bash", "curl x")), "ask")
                perms.start()
                return perms, io.permission_ask

            perms, ask = session("no")
            ask.assert_called_once()
            self.assertTrue(ask.call_args[1]["explicit_yes_required"])
            self.assertIn(SETTINGS_FILE, ask.call_args[0][0])
            self.assertEqual(perms.decide(action("bash", "curl x")), "ask")
            self.assertEqual(perms.decide(action("read", "/etc/hosts", inside=False)), "ask")
            # Asked once per session
            perms.start()
            ask.assert_called_once()

            # --yes-always doesn't approve them
            perms, ask = session("no", yes=True)
            self.assertEqual(perms.decide(action("bash", "curl x")), "ask")

            # "yes" is for this session only
            perms, ask = session("yes")
            self.assertEqual(perms.decide(action("bash", "curl x")), "allow")
            self.assertFalse(approvals_file().exists())

            # "always" is remembered
            perms, ask = session("always")
            self.assertEqual(perms.decide(action("bash", "curl x")), "allow")
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock()
            perms = Permissions(io, settings_file=SETTINGS_FILE)
            perms.start()
            io.permission_ask.assert_not_called()
            self.assertEqual(perms.decide(action("bash", "curl x")), "allow")

            # Until a new rule shows up: only that one asks
            Path(SETTINGS_FILE).write_text(json.dumps(dict(allow=rules + ["mcp"])))
            io.permission_ask = MagicMock(return_value="no")
            perms = Permissions(io, settings_file=SETTINGS_FILE)
            self.assertEqual(perms.decide(action("bash", "curl x")), "allow")
            perms.start()
            self.assertEqual(
                io.permission_ask.call_args[1]["subject"], "mcp  [.loom.permissions.json]"
            )
            self.assertEqual(perms.decide(action("mcp", "github__get_issue")), "ask")

    def test_project_config_rules_need_approval(self):
        with GitTemporaryDirectory():
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            perms = Permissions(
                io,
                allow=["bash(pytest*)"],
                project_allow=[("bash", ".loom.conf.yml")],
                settings_file=SETTINGS_FILE,
            )
            perms.start()
            self.assertIn(".loom.conf.yml", io.permission_ask.call_args[0][0])
            self.assertEqual(perms.decide(action("bash", "pytest")), "allow")
            self.assertEqual(perms.decide(action("bash", "curl x")), "ask")

    def test_always_for_edits_accepts_edits_for_the_session(self):
        io = InputOutput(yes=None)
        io.permission_ask = MagicMock(return_value="always")
        perms = Permissions(io)
        self.assertEqual(perms.request(action("edit", "a.py"))[0], "allow")
        self.assertEqual(perms.mode, "accept-edits")

    def test_protected_files_always_need_an_explicit_yes(self):
        io = InputOutput(yes=None)
        perms = Permissions(io, mode="accept-edits", allow=["edit"])
        self.assertEqual(perms.decide(action("edit", "src/a.py")), "allow")
        for path in [
            ".git/hooks/pre-commit",
            ".loom.permissions.json",
            "sub/.loom.conf.yml",
            ".loom/hooks.json",
            ".loom/commands/review.md",
            ".env",
        ]:
            self.assertEqual(perms.decide(action("edit", path)), "ask", path)
            self.assertEqual(perms.decide(action("edit", path), hook_allowed=True), "ask", path)

        io = InputOutput(yes=True)
        perms = Permissions(io, mode="accept-edits")
        self.assertEqual(perms.request(action("edit", ".git/hooks/pre-commit"))[0], "user-deny")

    def test_protected_files_in_other_cases_and_spellings(self):
        # On APFS and NTFS these are the real .git, .loom and .env files
        perms = Permissions(InputOutput(yes=None), mode="accept-edits", allow=["edit"])
        for path in [
            ".GIT/hooks/pre-commit",
            ".Git/config",
            "sub/.gIt",
            ".Loom.conf.yml",
            ".LOOM/hooks.json",
            ".Loom.Permissions.json",
            ".ENV",
            ".Env.local",
            "sub\\.GIT\\hooks\\pre-commit",
            ".git./hooks/pre-commit",
            ".git /hooks/pre-commit",
            ".git::$INDEX_ALLOCATION/hooks/pre-commit",
            ".g\u200cit/hooks/pre-commit",
            "/abs/project/.GIT/hooks/pre-commit",
        ]:
            self.assertEqual(perms.decide(action("edit", path)), "ask", path)
            self.assertEqual(perms.decide(action("edit", path), hook_allowed=True), "ask", path)
        self.assertEqual(perms.decide(action("edit", "src/gitx.py")), "allow")
        self.assertEqual(perms.decide(action("edit", ".github/workflows/ci.yml")), "allow")

        # The real path counts too
        edit = action("edit", "docs/x")
        edit.path = Path("/abs/project/.GIT/hooks/x")
        self.assertEqual(perms.decide(edit), "ask")

        self.assertEqual(canonical_path("A\\.GIT.\\Hooks"), "a/.git/hooks")
        self.assertEqual(canonical_path("dir\u0065\u0301/.Env"), "dir\u00e9/.env")

    def test_write_file_to_a_case_variant_of_git_asks(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            coder = make_coder(io, Permissions(io, mode="accept-edits"))
            write = tools.prepare(
                coder, "write_file", dict(path=".GIT/hooks/pre-commit", content="x")
            )
            self.assertEqual(coder.permissions.decide(write), "ask")
            # Through a symlink as well
            os.symlink(".git", "notgit")
            write = tools.prepare(
                coder, "write_file", dict(path="notgit/hooks/post-commit", content="x")
            )
            self.assertEqual(write.target, ".git/hooks/post-commit")
            self.assertEqual(coder.permissions.decide(write), "ask")

    def test_cycle_mode(self):
        perms = Permissions(InputOutput(yes=None))
        self.assertEqual([perms.cycle_mode() for _ in range(3)], ["accept-edits", "plan", "ask"])

    def test_shift_tab_cycles_the_mode_at_the_prompt(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from loom.commands import Commands

        with GitTemporaryDirectory(), create_pipe_input() as pipe:
            make_repo()
            io = InputOutput(input=pipe, output=DummyOutput(), pretty=False)
            coder = make_coder(io)
            self.assertEqual(coder.get_prompt_label(), "agent")
            coder.commands = Commands(io, coder)

            pipe.send_text("\x1b[Z\x1b[Zfix it\r")  # Shift-Tab twice
            self.assertEqual(coder.get_input(), "fix it")
            self.assertEqual(coder.permissions.mode, "plan")
            self.assertEqual(io.prompt_prefix, "agent plan> ")

            # Other coders have no modes to cycle
            ask = Coder.create(from_coder=coder, edit_format="ask")
            self.assertIsNone(ask.get_mode_cycler())

    def test_yes_always_does_not_approve_commands(self):
        io = InputOutput(yes=True)
        perms = Permissions(io)
        self.assertEqual(perms.request(action("edit", "a.py"))[0], "allow")
        outcome, message = perms.request(action("bash", "ls"))
        self.assertEqual(outcome, "user-deny")
        self.assertIn("--allow", message)

    def test_permission_ask_answers(self):
        io = InputOutput(yes=None, fancy_input=False)
        for typed, expected in [("", "yes"), ("n", "no"), ("a", "always"), ("Always", "always")]:
            with patch("builtins.input", return_value=typed):
                self.assertEqual(io.permission_ask("Run?", always="always allow"), expected)
        # (A)lways is only offered when there's something to always allow
        with patch("builtins.input", side_effect=["a", "y"]):
            self.assertEqual(io.permission_ask("Run?"), "yes")


class TestFlattenToolMessages(unittest.TestCase):
    def test_flatten(self):
        messages = [
            dict(role="user", content="hi"),
            dict(
                role="assistant",
                content=None,
                tool_calls=[
                    dict(id="1", type="function", function=dict(name="glob", arguments="{}"))
                ],
            ),
            dict(role="tool", tool_call_id="1", content="a.py"),
            dict(role="assistant", content="Found a.py."),
        ]
        flat = flatten_tool_messages(messages)
        self.assertEqual([m["role"] for m in flat], ["user", "assistant", "user", "assistant"])
        self.assertIn("Called glob", flat[1]["content"])
        self.assertIn("a.py", flat[2]["content"])
        sanity_check_messages(flat)


class TestProjectMemory(unittest.TestCase):
    def setUp(self):
        self.home = GitTemporaryDirectory()
        home = self.home.__enter__()
        self.home_patcher = patch("pathlib.Path.home", return_value=Path(home))
        self.home_patcher.start()

    def tearDown(self):
        self.home_patcher.stop()
        self.home.__exit__(None, None, None)

    def system_prompt(self, coder):
        return coder.format_messages().all_messages()[0]["content"]

    def test_loom_md_is_in_every_system_prompt(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("LOOM.md").write_text("Always use tabs {not spaces}.\n")
            io = InputOutput(yes=True)

            for edit_format in ["agent", "diff", "ask", "whole"]:
                coder = Coder.create(Model("gpt-4o-mini"), edit_format, io=io, map_tokens=0)
                system = self.system_prompt(coder)
                self.assertIn("# Project memory", system, edit_format)
                self.assertIn("## LOOM.md", system, edit_format)
                self.assertIn("Always use tabs {not spaces}.", system, edit_format)

            self.assertIn("Project memory: LOOM.md", coder.get_announcements())

            # Edits apply to the next message, no restart needed
            Path("LOOM.md").write_text("Use spaces.\n")
            self.assertIn("Use spaces.", self.system_prompt(coder))

            coder = Coder.create(
                Model("gpt-4o-mini"), "agent", io=io, map_tokens=0, project_memory=False
            )
            self.assertNotIn("Project memory", self.system_prompt(coder))

    def test_user_and_subdirectory_memory(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            (Path.home() / ".loom").mkdir()
            (Path.home() / ".loom" / "LOOM.md").write_text("User rule.\n")
            Path("LOOM.md").write_text("Project rule.\n")
            Path("pkg").mkdir()
            Path("pkg/LOOM.md").write_text("Package rule.\n")
            Path("other").mkdir()
            Path("other/LOOM.md").write_text("Unrelated rule.\n")

            os.chdir("pkg")
            try:
                coder = make_coder()
                system = self.system_prompt(coder)
            finally:
                os.chdir(root)

            user = system.index("## ~/.loom/LOOM.md")
            project = system.index("## LOOM.md")
            package = system.index("## pkg/LOOM.md")
            self.assertLess(user, project)
            self.assertLess(project, package)
            self.assertIn("Package rule.", system)
            self.assertNotIn("Unrelated rule.", system)

    def test_no_memory_files(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = make_coder()
            self.assertEqual(coder.get_project_memory(), "")
            self.assertNotIn("Project memory", self.system_prompt(coder))


class NoToolsLLM:
    """A provider that rejects the tools parameter, and answers plain chat requests."""

    def __init__(self, error, answer):
        self.error = error
        self.answer = answer
        self.tool_requests = 0
        self.chat_requests = []

    def __call__(self, **kwargs):
        if kwargs.get("tools"):
            self.tool_requests += 1
            raise self.error
        self.chat_requests.append(kwargs)
        message = Message(content=self.answer)
        return ModelResponse(choices=[Choices(message=message, finish_reason="stop")])


class TestToolCallingFallback(unittest.TestCase):
    def run_without_tools(self, error, mode="ask"):
        """The provider rejects the tools: the agent stops instead of switching to an edit
        format, whose edits don't go through Permissions."""
        with GitTemporaryDirectory():
            make_repo()
            before = Path("calc.py").read_text()
            io = InputOutput(yes=True)
            io.tool_error = MagicMock()
            io.tool_output = MagicMock()
            coder = make_coder(io, Permissions(io, mode=mode))
            llm = NoToolsLLM(error, "calc.py\n```\ndef add(a, b):\n    return a + b\n```\n")

            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add in calc.py")

            # Not retried, and not sent again without tools
            self.assertEqual(llm.tool_requests, 1)
            self.assertEqual(llm.chat_requests, [])
            self.assertEqual(coder.edit_format, "agent")
            self.assertEqual(Path("calc.py").read_text(), before)
            self.assertIn("can't use the agent's tools", io.tool_error.call_args[0][0])
            # The request got no reply, so it isn't kept
            self.assertEqual(coder.cur_messages, [])
            self.assertNotIn("fix add in calc.py", str(coder.done_messages))
            return io.tool_output.call_args[0][0]

    def test_provider_without_tool_calling(self):
        self.run_without_tools(
            litellm.BadRequestError(
                message="registry.ollama.ai/library/gemma:2b does not support tools",
                model="ollama/gemma:2b",
                llm_provider="ollama",
            )
        )

    def test_unsupported_tools_param_is_not_retried(self):
        self.run_without_tools(
            litellm.UnsupportedParamsError(
                message="model does not support parameters: ['tools']", llm_provider="x"
            )
        )

    def test_says_how_to_switch_but_not_in_plan_mode(self):
        def error():
            return litellm.UnsupportedParamsError(
                message="model does not support parameters: ['tools']", llm_provider="x"
            )

        hint = self.run_without_tools(error())
        self.assertIn("/chat-mode", hint)
        self.assertIn("without asking", hint)

        # Plan mode doesn't edit, so it only points at /ask
        hint = self.run_without_tools(error(), mode="plan")
        self.assertIn("/ask", hint)
        self.assertNotIn("/chat-mode", hint)

    def test_other_errors_do_not_fall_back(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_error = MagicMock()
            coder = make_coder(io)
            error = litellm.BadRequestError(message="Invalid model", model="x", llm_provider="x")
            llm = NoToolsLLM(error, "unused")
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="hi")
            self.assertEqual(coder.edit_format, "agent")
            self.assertEqual(llm.chat_requests, [])
            self.assertIn("Invalid model", io.tool_error.call_args[0][0])

    def test_tool_call_written_as_text_gets_one_nudge(self):
        text_call = '<tool_call>{"name": "read_file", "arguments": {"path": "calc.py"}}</tool_call>'
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            coder = make_coder(io)
            llm = FakeLLM(
                reply(text_call),
                reply(None, call("read_file", path="calc.py")),
                reply("add subtracts."),
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="what does add do?")

            self.assertEqual(llm.replies, [])
            nudge = llm.requests[1]["messages"][-1]
            self.assertEqual(nudge["role"], "user")
            self.assertIn("tool-calling API", nudge["content"])
            self.assertIn("as text", io.tool_warning.call_args[0][0])
            sanity_check_messages(coder.done_messages + [dict(role="user", content="next")])

            # Again in the next request: one nudge, then a suggestion to use an edit format
            llm = FakeLLM(reply(text_call), reply(text_call))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="and mul?")
            self.assertEqual(llm.replies, [])
            self.assertIn("--no-agent", io.tool_warning.call_args[0][0])


class TestMainOptions(unittest.TestCase):
    def setUp(self):
        self.original_env = os.environ.copy()
        # Keep the real ~/.loom (MCP servers, config) out of the tests
        self.home = IgnorantTemporaryDirectory()
        os.environ["HOME"] = self.home.name
        os.environ["OPENAI_API_KEY"] = "deadbeef"
        os.environ["LOOM_CHECK_UPDATE"] = "false"
        os.environ["LOOM_ANALYTICS"] = "false"
        self.webbrowser_patcher = patch("loom.io.webbrowser.open")
        self.webbrowser_patcher.start()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
        self.home.cleanup()
        self.webbrowser_patcher.stop()

    def main(self, *args):
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        from loom.main import main

        return main(
            ["--model", "gpt-4o-mini", "--no-git", "--yes-always", "--exit", *args],
            input=DummyInput(),
            output=DummyOutput(),
            return_coder=True,
        )

    def test_agent_is_the_default_for_tool_calling_models(self):
        with GitTemporaryDirectory():
            coder = self.main()
            self.assertEqual(coder.edit_format, "agent")
            self.assertEqual(coder.permissions.mode, "ask")

            self.assertNotEqual(self.main("--no-agent").edit_format, "agent")
            self.assertEqual(self.main("--edit-format", "whole").edit_format, "whole")

    def test_prompt_caching_is_on_for_the_agent(self):
        with GitTemporaryDirectory():
            main_model = "sonnet"
            self.assertTrue(self.main("--model", main_model).add_cache_headers)
            self.assertFalse(
                self.main("--model", main_model, "--no-cache-prompts").add_cache_headers
            )
            self.assertFalse(self.main("--model", main_model, "--no-agent").add_cache_headers)
            self.assertTrue(
                self.main("--model", main_model, "--no-agent", "--cache-prompts").add_cache_headers
            )

    def test_permission_options(self):
        with GitTemporaryDirectory():
            coder = self.main("--permission-mode", "plan", "--allow", "bash(pytest*)")
            self.assertEqual(coder.permissions.mode, "plan")
            self.assertEqual([str(rule) for rule, _ in coder.permissions.rules], ["bash(pytest*)"])

            # The permissions survive switching chat modes
            ask = Coder.create(from_coder=coder, edit_format="ask")
            self.assertIs(ask.permissions, coder.permissions)

            self.assertEqual(self.main("--allow", "sudo(rm)"), 1)

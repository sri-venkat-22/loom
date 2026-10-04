import contextlib
import io as stdio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from litellm.types.utils import Choices, Message, ModelResponse

from loom import prompts
from loom.coders import Coder, agent_coder
from loom.io import InputOutput
from loom.llm import litellm
from loom.permissions import Permissions
from loom.sendchat import sanity_check_messages
from loom.sessions import (
    MAX_SESSIONS,
    SESSIONS_DIR,
    Session,
    SessionError,
    list_sessions,
)
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory

from .test_agent import FakeLLM, call, make_coder, make_repo, reply, tool_results

TODOS = [dict(content="Fix calc.add", status="completed")]

FIX_IT = [
    reply(None, call("todo_write", todos=TODOS), call("read_file", path="calc.py")),
    reply(None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
    reply("Fixed `add`."),
]


class SummarizingLLM(FakeLLM):
    """A FakeLLM whose requests without tools, other than commit messages, get a summary."""

    def __init__(self, *replies, fail_over_chars=None):
        super().__init__(*replies)
        self.summaries = []
        self.summary_prompts = []
        self.fail_over_chars = fail_over_chars
        self.overflows = 0

    def __call__(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        summarizing = system.startswith((prompts.compact_steps, prompts.summarize))
        if not kwargs.get("tools") and summarizing:
            self.summaries.append(kwargs["messages"][1]["content"])
            self.summary_prompts.append(system)
            message = Message(content=f"SUMMARY {len(self.summaries)}")
            return ModelResponse(choices=[Choices(message=message, finish_reason="stop")])

        size = len(json.dumps(kwargs["messages"]))
        if kwargs.get("tools") and self.fail_over_chars and size > self.fail_over_chars:
            self.overflows += 1
            raise litellm.ContextWindowExceededError(
                message="prompt is too long", model="gpt-4o-mini", llm_provider="openai"
            )
        return super().__call__(**kwargs)


def session_coder(io=None, **kwargs):
    io = io or InputOutput(yes=True, pretty=False)
    return make_coder(io, Permissions(io), session=Session(Path(SESSIONS_DIR)), **kwargs)


class TestSessions(unittest.TestCase):
    def test_each_request_is_saved(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = session_coder()
            coder.run(with_message="/add calc.py")
            # Nothing to save yet
            self.assertFalse(Path(SESSIONS_DIR).exists())

            with patch.object(litellm, "completion", FakeLLM(*FIX_IT)):
                coder.run(with_message="fix calc.add")

            path = coder.session.path
            self.assertEqual(path.parent.name, SESSIONS_DIR)
            data = json.loads(path.read_text())
            self.assertEqual(data["title"], "fix calc.add")
            self.assertEqual(data["files"], ["calc.py"])
            self.assertEqual(data["todos"], TODOS)
            self.assertEqual(data["edit_format"], "agent")
            self.assertEqual(data["messages"], coder.done_messages)
            self.assertTrue(any(msg.get("tool_calls") for msg in data["messages"]))

            # The next request updates the same file
            with patch.object(litellm, "completion", FakeLLM(reply("Nothing to do."))):
                coder.run(with_message="anything else?")
            self.assertEqual(list(Path(SESSIONS_DIR).glob("*.json")), [path])
            self.assertEqual(
                json.loads(path.read_text())["messages"][-1]["content"], "Nothing to do."
            )

    def test_clear_starts_a_new_session_and_resume_goes_back(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = session_coder()
            with patch.object(litellm, "completion", FakeLLM(*FIX_IT)):
                coder.run(with_message="fix calc.add")
            first = coder.session.id

            coder.run(with_message="/clear")
            self.assertNotEqual(coder.session.id, first)
            self.assertEqual(coder.todos, [])
            with patch.object(litellm, "completion", FakeLLM(reply("Hi."))):
                coder.run(with_message="hello")

            sessions = list_sessions(SESSIONS_DIR)
            self.assertEqual({s[0] for s in sessions}, {first, coder.session.id})

            # The start of the id is enough, unless both started in the same second with
            # random parts that start the same
            prefix = first if coder.session.id.startswith(first[:18]) else first[:18]
            out = stdio.StringIO()
            with contextlib.redirect_stdout(out):
                coder.run(with_message="/sessions")
                coder.run(with_message="/resume " + prefix)
            self.assertIn(f"{coder.session.id}", out.getvalue())
            self.assertEqual(coder.session.id, first)
            self.assertEqual(coder.done_messages[0]["content"], "fix calc.add")
            self.assertEqual(coder.todos, TODOS)
            # The recap shows the conversation so far
            self.assertIn("> fix calc.add", out.getvalue())
            self.assertIn("● Update(calc.py)", out.getvalue())
            self.assertIn("Fixed `add`.", out.getvalue())

    def test_find_and_prune(self):
        with GitTemporaryDirectory():
            directory = Path(SESSIONS_DIR)
            coder = session_coder()
            coder.done_messages = [dict(role="user", content="hi")]
            ids = []
            for num in range(MAX_SESSIONS + 2):
                session = Session(directory, f"20260101-000000-{num:04x}")
                session.save(coder)
                os.utime(session.path, (num, num))
                ids.append(session.id)
            # The oldest were deleted as new ones were saved
            self.assertEqual(len(list(directory.glob("*.json"))), MAX_SESSIONS)
            self.assertEqual(Session.latest(directory).id, ids[-1])
            self.assertEqual(Session.find(directory, ids[-1]).id, ids[-1])
            with self.assertRaises(SessionError):
                Session.find(directory, "20260101")  # ambiguous
            with self.assertRaises(SessionError):
                Session.find(directory, "nope")

            (directory / "broken.json").write_text("{")
            os.utime(directory / "broken.json", (10**6, 10**6))
            self.assertEqual(Session.latest(directory).id, ids[-1])


class TestContinue(unittest.TestCase):
    def setUp(self):
        self.original_env = os.environ.copy()
        # Keep the real ~/.loom (MCP servers, config) out of the tests
        self.home = IgnorantTemporaryDirectory()
        os.environ["HOME"] = self.home.name
        os.environ["OPENAI_API_KEY"] = "deadbeef"
        os.environ["LOOM_CHECK_UPDATE"] = "false"
        os.environ["LOOM_ANALYTICS"] = "false"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)
        self.home.cleanup()

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

    def test_continue_resumes_the_last_conversation(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = self.main("calc.py")
            with patch.object(litellm, "completion", FakeLLM(*FIX_IT)):
                coder.run(with_message="fix calc.add")
            saved = coder.done_messages
            coder.run(with_message="/drop calc.py")

            # A new session doesn't see the old one...
            fresh = self.main()
            self.assertEqual(fresh.done_messages, [])
            self.assertNotEqual(fresh.session.id, coder.session.id)

            # ...but --continue does, with its files and to-do list
            resumed = self.main("--continue")
            self.assertEqual(resumed.session.id, coder.session.id)
            self.assertEqual(resumed.done_messages, saved)
            self.assertEqual(resumed.get_inchat_relative_files(), ["calc.py"])
            self.assertEqual(resumed.todos, TODOS)
            self.assertIn(
                f"Continuing conversation {coder.session.id}",
                "\n".join(resumed.get_announcements()),
            )

            # The model gets the old conversation, tool calls and all
            llm = FakeLLM(reply("You fixed calc.add."))
            with patch.object(litellm, "completion", llm):
                resumed.run(with_message="what did we do?")
            messages = llm.requests[0]["messages"]
            self.assertTrue(any(msg.get("content") == "fix calc.add" for msg in messages))
            self.assertIn("Edited calc.py", str(tool_results(messages)))
            sanity_check_messages([msg for msg in messages if msg["role"] != "system"])

            self.assertEqual(
                self.main("--resume", coder.session.id).done_messages[-1]["content"],
                "You fixed calc.add.",
            )
            self.assertEqual(self.main("--resume", "nope"), 1)

    def test_continue_with_nothing_saved_starts_fresh(self):
        with GitTemporaryDirectory():
            coder = self.main("--continue")
            self.assertEqual(coder.done_messages, [])
            self.assertEqual(self.main("--no-sessions").session.directory, None)


def big_file_steps(num):
    return [reply(None, call("read_file", path=f"big{n}.txt")) for n in range(num)]


class TestCompaction(unittest.TestCase):
    def make_big_files(self, num):
        for n in range(num):
            Path(f"big{n}.txt").write_text(
                "".join(f"line {i} of big file {n}\n" for i in range(400))
            )

    def check_history(self, messages):
        """Every tool result follows the assistant message that called it."""
        called = set()
        for msg in messages:
            for tool_call in msg.get("tool_calls") or []:
                called.add(tool_call["id"])
            if msg["role"] == "tool":
                self.assertIn(msg["tool_call_id"], called)
                # A result is never shortened twice, and keeps its end
                self.assertLessEqual(msg["content"].count("characters dropped"), 1)
                if msg["content"].startswith("     1\tline 0 of big file"):
                    self.assertIn("line 399 of big file", msg["content"])
        sanity_check_messages([msg for msg in messages if msg["role"] != "system"])

    def test_a_long_request_is_compacted_instead_of_overflowing(self):
        with GitTemporaryDirectory():
            make_repo()
            self.make_big_files(12)
            coder = make_coder()
            window = 20_000
            coder.main_model.info = dict(coder.main_model.info, max_input_tokens=window)
            llm = SummarizingLLM(
                reply(
                    None,
                    call("todo_write", todos=[dict(content="Read them all", status="in_progress")]),
                ),
                *big_file_steps(12),
                reply("I read all twelve files."),
            )
            out = stdio.StringIO()
            with patch.object(litellm, "completion", llm), contextlib.redirect_stdout(out):
                coder.run(with_message="read every big file")

            # Every step ran, and no request came near the context window
            self.assertEqual(llm.replies, [])
            for request in llm.requests:
                self.assertLess(
                    coder.count_tokens(request["messages"]), window * agent_coder.COMPACT_AT
                )

            # Old steps were summarized into the request, recent ones kept as they were
            self.assertTrue(llm.summaries)
            self.assertIn("Compacted the conversation", out.getvalue())
            last = llm.requests[-1]["messages"]
            request = next(msg for msg in last if msg["role"] == "user")
            self.assertTrue(request["content"].startswith("read every big file"))
            self.assertIn("# Progress so far", request["content"])
            self.assertIn("SUMMARY", request["content"])
            self.assertIn("[~] Read them all", request["content"])
            self.assertEqual(request["content"].count("# Progress so far"), 1)
            self.assertIn("line 1 of big file 11", str(tool_results(last)))
            self.assertIn("characters dropped from the middle", str(tool_results(last)))
            self.check_history(last)
            self.check_history(coder.done_messages)

    def test_the_newest_tool_result_is_kept_whole(self):
        with GitTemporaryDirectory():
            make_repo()
            # Each file is about a third of the window, so after compacting the conversation
            # is over the target but under the limit
            for n in range(6):
                lines = [f"line {i} of file {n} " + "word " * 10 for i in range(230)]
                Path(f"big{n}.txt").write_text("\n".join(lines + [f"END OF FILE {n}"]) + "\n")
            coder = make_coder()
            coder.main_model.info = dict(coder.main_model.info, max_input_tokens=12_000)
            llm = SummarizingLLM(*big_file_steps(6), reply("Done."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="read the big files")

            self.assertTrue(llm.summaries)
            for num, request in enumerate(llm.requests[1:]):
                newest = [msg for msg in request["messages"] if msg["role"] == "tool"][-1]
                self.assertIn(f"END OF FILE {num}", newest["content"])

    def test_overflow_errors_are_compacted_and_retried(self):
        with GitTemporaryDirectory():
            make_repo()
            self.make_big_files(4)
            coder = make_coder()
            # The window is unknown, and the model rejects long requests
            coder.main_model.info = dict(coder.main_model.info, max_input_tokens=None)
            llm = SummarizingLLM(*big_file_steps(4), reply("Done."), fail_over_chars=40_000)
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="read the big files")

            self.assertGreater(llm.overflows, 0)
            self.assertEqual(llm.replies, [])
            self.assertEqual(coder.done_messages[-1]["content"], "Done.")
            self.check_history(coder.done_messages)

    def test_a_summary_that_saves_nothing_is_not_used(self):
        coder = make_coder()
        coder.request_text = "go"
        call_msg = dict(
            role="assistant",
            content=None,
            tool_calls=[
                dict(id="a", type="function", function=dict(name="list_dir", arguments="{}"))
            ],
        )
        steps = []
        for num in range(3):
            step = dict(call_msg, tool_calls=[dict(call_msg["tool_calls"][0], id=f"c{num}")])
            steps += [step, dict(role="tool", tool_call_id=f"c{num}", content="short")]
        coder.cur_messages = [dict(role="user", content="go")] + steps
        before = list(coder.cur_messages)
        long_summary = [dict(role="user", content="summary " * 500)]
        with patch.object(coder.summarizer, "summarize_all", return_value=long_summary):
            self.assertEqual(coder.summarize_steps(1), 0)
        self.assertEqual(coder.cur_messages, before)

    def test_no_auto_compact(self):
        coder = make_coder(auto_compact=False)
        coder.main_model.info = dict(coder.main_model.info, max_input_tokens=10)
        coder.cur_messages = [dict(role="user", content="x" * 1000)]
        with patch.object(coder, "compact") as compact:
            coder.compact_if_needed()
        compact.assert_not_called()

    def test_compact_command(self):
        with GitTemporaryDirectory():
            coder = make_coder(InputOutput(yes=True, pretty=False))
            llm = SummarizingLLM()
            out = stdio.StringIO()
            with patch.object(litellm, "completion", llm), contextlib.redirect_stdout(out):
                coder.run(with_message="/compact")
                coder.done_messages = [
                    dict(role="user", content="question " * 200),
                    dict(role="assistant", content="answer " * 200),
                ]
                coder.run(with_message="/compact the answers")
            self.assertIn("There's no chat history to compact.", out.getvalue())
            self.assertIn("Compacted the chat history", out.getvalue())
            self.assertEqual(
                coder.done_messages[0]["content"], prompts.summary_prefix + "SUMMARY 1"
            )
            self.assertEqual(coder.done_messages[1]["role"], "assistant")
            self.assertIn("Focus the summary on: the answers", llm.summary_prompts[-1])

    def test_switching_modes_keeps_the_session(self):
        coder = make_coder()
        ask = Coder.create(from_coder=coder, edit_format="ask")
        self.assertIs(ask.session, coder.session)


class TestSummarizerFailuresKeepHistory(unittest.TestCase):
    def test_summarizer_exception_leaves_done_messages_alone(self):
        """A network error or 401 from the summarizer must not wipe the chat history."""
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io)
            before = [
                dict(role="user", content="first question"),
                dict(role="assistant", content="first answer"),
                dict(role="user", content="second question"),
                dict(role="assistant", content="second answer"),
            ]
            coder.done_messages = list(before)
            coder.summarizer.too_big = lambda messages: True
            coder.summarizer.summarize = lambda messages: (_ for _ in ()).throw(
                ConnectionError("provider unreachable")
            )
            captured = []
            coder.io.tool_warning = lambda msg, *a, **kw: captured.append(msg)

            coder.summarize_start()
            coder.summarize_end()

            self.assertEqual(coder.done_messages, before)
            self.assertTrue(any("Unable to summarize" in m for m in captured))

    def test_a_successful_summary_still_replaces_history(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = make_coder(io)
            coder.done_messages = [
                dict(role="user", content="q1"),
                dict(role="assistant", content="a1"),
            ]
            summary = [dict(role="user", content="summary of the chat so far")]
            coder.summarizer.too_big = lambda messages: True
            coder.summarizer.summarize = lambda messages: summary

            coder.summarize_start()
            coder.summarize_end()
            self.assertEqual(coder.done_messages, summary)


class TestSessionFilesStayInsideTheProject(unittest.TestCase):
    def test_add_session_files_drops_entries_outside_the_project(self):
        from loom.main import add_session_files

        with GitTemporaryDirectory() as root, IgnorantTemporaryDirectory() as outside:
            Path("README.md").write_text("# ok\n")
            Path("src").mkdir()
            Path("src/x.py").write_text("x = 1\n")
            secret = Path(outside) / "id_rsa"
            secret.write_text("PRIVATE KEY\n")
            outside_abs = Path(outside) / "notes.txt"
            outside_abs.write_text("outside\n")

            session = Session(
                data=dict(
                    files=[
                        "README.md",
                        "../" + Path(outside).name + "/id_rsa",
                        "src/x.py",
                        "/etc/passwd",
                    ],
                    read_only_files=[str(outside_abs), "src/x.py", str(secret)],
                )
            )
            io = InputOutput(yes=True)
            captured = []
            io.tool_warning = lambda msg, *a, **kw: captured.append(msg)

            fnames, read_only = add_session_files(session, root, [], [], io=io)

            # Nothing from the attacker set reached the lists (match by resolved path)
            def inside(path):
                resolved = Path(path).resolve()
                return resolved == Path(root).resolve() or Path(root).resolve() in resolved.parents

            for lst in (fnames, read_only):
                for f in lst:
                    self.assertTrue(inside(f), f)
            self.assertNotIn(
                str(secret.resolve()), [str(Path(f).resolve()) for f in fnames + read_only]
            )
            self.assertNotIn(
                str(outside_abs.resolve()), [str(Path(f).resolve()) for f in read_only]
            )

            # Warning mentions what was skipped
            self.assertTrue(captured)
            text = captured[0]
            self.assertIn("id_rsa", text)
            self.assertIn("/etc/passwd", text)

    def test_add_session_files_keeps_working_for_a_normal_session(self):
        from loom.main import add_session_files

        with GitTemporaryDirectory() as root:
            Path("a.py").write_text("x = 1\n")
            Path("b.py").write_text("y = 1\n")
            abs_b = str((Path(root) / "b.py").resolve())
            session = Session(data=dict(files=["a.py"], read_only_files=[abs_b]))
            fnames, read_only = add_session_files(session, root, [], [])
            self.assertEqual(len(fnames), 1)
            self.assertEqual(len(read_only), 1)

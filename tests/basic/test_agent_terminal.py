import contextlib
import io as stdio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom import tools
from loom.esc import EscListener, clean_typed_text
from loom.io import InputOutput, numbered_diff_lines
from loom.llm import litellm
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory

from .test_agent import (
    PYTEST,
    FakeLLM,
    call,
    make_coder,
    make_repo,
    reply,
    tool_results,
)

TODOS_START = [
    dict(content="Find the bug", status="in_progress", active_form="Finding the bug"),
    dict(content="Fix calc.add", status="pending"),
    dict(content="Run the tests", status="pending"),
]
TODOS_END = [
    dict(content="Find the bug", status="completed"),
    dict(content="Fix calc.add", status="completed"),
    dict(content="Run the tests", status="in_progress", active_form="Running the tests"),
]

SCRIPT = [
    reply("I'll plan this.", call("todo_write", todos=TODOS_START)),
    reply(
        None,
        call("grep", pattern="def add"),
        call("glob", pattern="**/*.py"),
        call("read_file", path="calc.py"),
    ),
    reply(
        None,
        call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
        call("todo_write", todos=TODOS_END),
    ),
    reply(None, call("bash", command=PYTEST), call("read_file", path="nope.py")),
    reply("Fixed `add`; the tests pass."),
]


def run_script(mode, script=SCRIPT, agent_diffs=False):
    """Run a scripted request, returning the coder, the fake model and what was printed."""
    io = InputOutput(yes=True, pretty=False)
    io.agent_diffs = agent_diffs
    permissions = Permissions(io, mode=mode, allow=[f"bash({sys.executable} -m pytest*)"])
    coder = make_coder(io, permissions)
    llm = FakeLLM(*script)
    out = stdio.StringIO()
    with patch.object(litellm, "completion", llm), contextlib.redirect_stdout(out):
        coder.run(with_message="fix the failing test")
    return coder, llm, out.getvalue()


class TestCompactDisplay(unittest.TestCase):
    def test_tool_calls_are_shown_compactly(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, llm, out = run_script("accept-edits")

            for expected in [
                '● Grep("def add")',
                "  ⎿  Found 1 match in 1 file",
                "● Glob(**/*.py)",
                "  ⎿  Found 2 files",
                "● Read(calc.py)",
                "  ⎿  Read 2 lines",
                "● Update(calc.py)",
                "  ⎿  Updated calc.py with 1 addition and 1 removal",
                "● Bash(",
                "1 passed",
                "● Read(nope.py)",
                "  ⎿  Error: nope.py does not exist",
            ]:
                self.assertIn(expected, out)

            # Edits show their line counts, not the diff
            self.assertNotIn("return a + b", out)
            # Only the end of a command's output is shown, and no exit code when it passed
            self.assertNotIn("Exit code: 0", out)
            # The model still gets the full results
            results = tool_results(llm.requests[2]["messages"])
            self.assertIn("return a - b", results["call_2_2"])

    def test_each_edit_shows_its_diff_once(self):
        with GitTemporaryDirectory():
            make_repo()
            # With --agent-diffs, in accept-edits mode the diff is shown after the edit
            _coder, _llm, out = run_script("accept-edits", agent_diffs=True)
            self.assertIn("     2 -     return a - b", out)
            self.assertIn("     2 +     return a + b", out)

        with GitTemporaryDirectory():
            make_repo()
            # In ask mode it's shown with the question, and not again afterwards
            _coder, _llm, out = run_script("ask", agent_diffs=True)
            self.assertEqual(out.count("return a + b"), 1)
            self.assertIn("Edit calc.py?", out)
            self.assertIn("  ⎿  Updated calc.py with 1 addition and 1 removal", out)

    def test_edit_questions_show_line_counts_by_default(self):
        with GitTemporaryDirectory():
            make_repo()
            _coder, _llm, out = run_script("ask")
            self.assertIn("calc.py: 1 addition and 1 removal", out)
            self.assertIn("Edit calc.py?", out)
            self.assertNotIn("return a + b", out)

    def test_failed_command_shows_the_exit_code(self):
        with GitTemporaryDirectory():
            make_repo()
            failing = f"{sys.executable} -c \"import sys; print('boom'); sys.exit(3)\""
            io = InputOutput(yes=True, pretty=False)
            coder = make_coder(io, Permissions(io, allow=["bash"]))
            llm = FakeLLM(reply(None, call("bash", command=failing)), reply("It failed."))
            out = stdio.StringIO()
            with patch.object(litellm, "completion", llm), contextlib.redirect_stdout(out):
                coder.run(with_message="run it")
            self.assertIn("  ⎿  boom", out.getvalue())
            self.assertIn("     Exit code: 3", out.getvalue())

    def test_numbered_diff_lines(self):
        before = "".join(f"line {i}\n" for i in range(1, 21))
        after = before.replace("line 2\n", "line two\n").replace("line 18\n", "-- 18\n")
        diff = tools.make_diff(MagicMock(root="."), Path("x").resolve(), before, after)
        lines = numbered_diff_lines(diff)

        self.assertIn((2, "-", "line 2"), lines)
        self.assertIn((2, "+", "line two"), lines)
        self.assertIn((1, " ", "line 1"), lines)
        # A removed line starting with -- isn't mistaken for a file header
        self.assertIn((18, "+", "-- 18"), lines)
        self.assertIn(None, lines)  # the gap between the two hunks

    def test_summaries(self):
        self.assertEqual(
            tools.describe_changes(*tools.count_changes("a\nb\n", "a\nc\nd\n")),
            "2 additions and 1 removal",
        )
        self.assertEqual(tools.plural(1, "entry", "entries"), "1 entry")
        self.assertEqual(tools.display_name("edit_file"), "Update")
        self.assertEqual(tools.display_name("mcp__github__get_issue"), "github - get_issue (MCP)")
        self.assertEqual(tools.plural(3, "entry", "entries"), "3 entries")


class TestEscapeSequences(unittest.TestCase):
    SPOOF = "touch PWNED #\x1b[2K\x1b[1G● Bash(pytest -q)"

    def test_sanitize_for_display(self):
        from loom.display import sanitize_for_display

        self.assertEqual(
            sanitize_for_display(self.SPOOF, show_escapes=True),
            "touch PWNED #\\x1b[2K\\x1b[1G● Bash(pytest -q)",
        )
        self.assertEqual(sanitize_for_display("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(sanitize_for_display("50%\r100%"), "50%\n100%")
        self.assertEqual(sanitize_for_display("a\x1b]0;title\x07b\x9b2Kc"), "abc")
        self.assertEqual(sanitize_for_display("x\x08\x7f\u202ey"), "x\\x08\\x7f\\u202ey")
        self.assertEqual(sanitize_for_display("tab\tand\nnewline"), "tab\tand\nnewline")
        self.assertEqual(sanitize_for_display("over\rwrite", show_escapes=True), "over\\x0dwrite")

    def test_a_command_cannot_disguise_itself_in_the_approval(self):
        with GitTemporaryDirectory():
            make_repo()
            script = [reply("Running the tests.\x1b[8m", call("bash", command=self.SPOOF))]
            coder, llm, out = run_script("ask", script)
            self.assertNotIn("\x1b", out)
            self.assertIn("touch PWNED #\\x1b[2K\\x1b[1G", out)
            self.assertIn("Running the tests.", out)
            self.assertFalse(Path("PWNED").exists())

    def test_results_and_diffs_cannot_control_the_terminal(self):
        io = InputOutput(yes=True, pretty=False)
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out):
            io.tool_result("\x1b[31mred\x1b[0m\x1b[1A\x1b[2K")
            io.diff_output("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\x1b[8m hidden\n")
            io.tool_warning("warn\x1b[2J")
            io.tool_output("note\x1b]52;c;Zm9v\x07")
            io.permission_ask("Run this command?", subject="ls\x1b[2K\nrm x")
        printed = out.getvalue()
        self.assertNotIn("\x1b", printed)
        self.assertIn("red", printed)
        self.assertIn("a = 2\\x1b[8m hidden", printed)
        self.assertIn("ls\\x1b[2K", printed)


class TestTodos(unittest.TestCase):
    def test_the_todo_list_is_shown_and_kept(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, llm, out = run_script("accept-edits")

            self.assertIn("● Update Todos", out)
            self.assertIn("  ⎿  ◼ Find the bug", out)
            self.assertIn("     ☐ Fix calc.add", out)
            self.assertIn("     ☒ Fix calc.add", out)
            self.assertIn("     ◼ Run the tests", out)
            self.assertEqual(coder.todos, TODOS_END)

            result = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertIn("0 of 3 done", result)
            self.assertIn("In progress: Find the bug", result)

            # It works in plan mode, and survives switching chat modes
            coder.permissions.mode = "plan"
            self.assertEqual(
                coder.permissions.decide(tools.prepare(coder, "todo_write", dict(todos=[]))),
                "allow",
            )
            from loom.coders import Coder

            ask = Coder.create(from_coder=coder, edit_format="ask", summarize_from_coder=False)
            self.assertEqual(ask.todos, TODOS_END)

    def test_spinner_shows_the_item_in_progress(self):
        coder = make_coder()
        coder.request_started = time.time() - 5
        coder.todos[:] = TODOS_END
        self.assertEqual(coder.get_spinner_text()(), "Running the tests… (5s)")
        coder.todos[:] = []
        coder.io.esc_listener = MagicMock()
        self.assertEqual(coder.get_spinner_text()(), "Working… (5s · esc to interrupt)")

    def test_parse_todos(self):
        parsed = tools.parse_todos(
            '[{"content": "a", "status": "in progress", "activeForm": "Doing a"}, "b"]'
        )
        self.assertEqual(
            parsed,
            [
                dict(content="a", status="in_progress", active_form="Doing a"),
                dict(content="b", status="pending"),
            ],
        )
        with self.assertRaises(tools.ToolError):
            tools.parse_todos([dict(content="a", status="done-ish")])
        with self.assertRaises(tools.ToolError):
            tools.parse_todos([dict(status="pending")])
        with self.assertRaises(tools.ToolError):
            tools.parse_todos("not json")

    def test_todos_command(self):
        coder = make_coder(InputOutput(yes=True, pretty=False))
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out):
            coder.commands.cmd_todos("")
            coder.todos[:] = TODOS_END
            coder.commands.cmd_todos("")
        self.assertIn("The to-do list is empty.", out.getvalue())
        self.assertIn("● Todos(2 of 3 done)", out.getvalue())


@unittest.skipIf(os.name == "nt", "uses a pseudo-terminal")
class TestEscInterrupts(unittest.TestCase):
    def setUp(self):
        import pty

        self.master, slave = pty.openpty()
        self.stdin = os.fdopen(slave, "r")

    def tearDown(self):
        self.stdin.close()
        os.close(self.master)

    def type_later(self, *chunks):
        def type_keys():
            for chunk in chunks:
                time.sleep(0.2)
                os.write(self.master, chunk)

        threading.Thread(target=type_keys, daemon=True).start()

    def test_esc_interrupts_a_running_command(self):
        listener = EscListener(stdin=self.stdin)
        self.assertTrue(EscListener.supported(self.stdin))
        listener.start()
        # An arrow key is an escape sequence, not Esc
        self.type_later(b"hi\x1b[Athere\x7f\x7f", b"\x1b")
        started = time.time()
        try:
            with self.assertRaises(KeyboardInterrupt):
                tools.run_command("sleep 20", ".", 30)
            self.assertLess(time.time() - started, 10)
            self.assertTrue(listener.consume_escape())
            self.assertFalse(listener.consume_escape())
        finally:
            listener.stop()
        self.assertEqual(listener.take_typed(), "hithe")

    def test_keys_other_than_esc_dont_interrupt(self):
        listener = EscListener(stdin=self.stdin)
        listener.start()
        self.type_later(b"abc\x1bOP")
        try:
            time.sleep(0.6)
        finally:
            listener.stop()
        self.assertFalse(listener.pressed)
        self.assertEqual(listener.take_typed(), "abc")

    def test_clean_typed_text(self):
        self.assertEqual(clean_typed_text("ab\x7fc\r\x1b[1;5Cd\x01"), "ac\nd")


class TestInterruptedRequests(unittest.TestCase):
    def test_esc_stops_the_request_and_never_exits(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True, pretty=False)
            coder = make_coder(io, Permissions(io, allow=["bash"]))
            llm = FakeLLM(
                reply(
                    None,
                    call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                    call("bash", command="sleep 20"),
                    call("read_file", path="calc.py"),
                ),
                reply("This reply should never be requested."),
            )

            # As if Esc was pressed while the command ran
            listener = MagicMock()
            listener.consume_escape.return_value = True

            @contextlib.contextmanager
            def esc_interrupts():
                io.esc_listener = listener
                yield
                io.esc_listener = None

            io.esc_interrupts = esc_interrupts
            with (
                patch.object(litellm, "completion", llm),
                patch.object(tools, "run_command", side_effect=KeyboardInterrupt),
            ):
                coder.run(with_message="fix it")
                # Twice in quick succession: ^C would exit, Esc doesn't
                coder.keyboard_interrupt()
                io.esc_listener = listener
                coder.keyboard_interrupt()

            self.assertEqual(len(llm.requests), 1)
            results = list(tool_results(coder.done_messages).values())
            self.assertIn("Edited calc.py", results[0])
            self.assertIn("Interrupted by the user", results[1])
            self.assertIn("Not run", results[2])
            # The edit made before the interrupt was still committed
            self.assertEqual(coder.cur_messages, [])
            self.assertTrue(coder.loom_commit_hashes)

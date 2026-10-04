import json
import re
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from litellm.types.utils import Choices, Message, ModelResponse

from loom import subagents, tools
from loom.coders import Coder
from loom.coders.subagent_coder import SubAgentCoder
from loom.io import InputOutput
from loom.llm import litellm
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory

from .test_agent import (
    call,
    full_response,
    make_coder,
    make_repo,
    reply,
    stream_response,
)
from .test_mcp import HomeDirMixin

FOOTER_RE = re.compile(r"\n\n\[task: \d+ tool uses? · [\d.]+k? tokens( · \$[\d.]+)? · \d+s\]$")


def agent_of(request):
    """Which agent sent a request: the sub-agent's type, or "parent"."""
    system = request["messages"][0]["content"]
    match = re.search(r"^# Your agent type: (\S+)$", system, re.MULTILINE)
    return match.group(1) if match else "parent"


class AgentsLLM:
    """Stands in for litellm.completion when agents start sub-agents: each agent gets the
    replies scripted for it, picked by the agent type in its system prompt ("parent" for
    the main agent)."""

    def __init__(self, **scripts):
        self.scripts = {name: list(replies) for name, replies in scripts.items()}
        self.requests = {name: [] for name in scripts}
        self.lock = threading.Lock()
        self.turns = 0

    def __call__(self, **kwargs):
        if not kwargs.get("tools"):
            message = Message(content="Fix the adder")
            return ModelResponse(choices=[Choices(message=message, finish_reason="stop")])
        name = agent_of(kwargs)
        with self.lock:
            script = self.scripts.get(name)
            if not script:
                raise AssertionError(f"The {name} agent asked for more replies than scripted")
            self.requests[name].append(kwargs)
            scripted = script.pop(0)
            self.turns += 1
            turn = self.turns
        if kwargs["stream"]:
            return stream_response(scripted, turn)
        return full_response(scripted, turn)

    def left(self):
        return {name: len(script) for name, script in self.scripts.items() if script}


def tool_names(request):
    return [schema["function"]["name"] for schema in request["tools"]]


def results_of(messages):
    return [msg["content"] for msg in messages if msg["role"] == "tool"]


EXPLORE_REPORT = "add() is in calc.py:1 and subtracts instead of adding (calc.py:2)."


def explore_task(prompt="Find where add() is defined and what's wrong with it."):
    return call("task", description="Find the adder", prompt=prompt, agent="explore")


class TestTaskTool(HomeDirMixin, unittest.TestCase):
    def run_parent(self, llm, message="fix the adder", **kwargs):
        coder = make_coder(**kwargs)
        with patch.object(litellm, "completion", llm):
            coder.run(with_message=message)
        return coder

    def test_child_gets_a_fresh_context_and_the_parent_only_its_report(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("LOOM.md").write_text("Always use tabs in this project.\n")
            llm = AgentsLLM(
                parent=[
                    reply("Let me ask an explorer.", explore_task()),
                    reply("It's calc.py:2; I'll fix it next."),
                ],
                explore=[
                    reply(None, call("grep", pattern="def add"), call("read_file", path="calc.py")),
                    reply(EXPLORE_REPORT),
                ],
            )
            coder = make_coder(stream=True)
            coder.done_messages = [
                dict(role="user", content="An earlier secret request"),
                dict(role="assistant", content="An earlier reply"),
            ]
            coder.add_rel_fname("test_calc.py")
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix the adder")
            self.assertEqual(llm.left(), {})

            first = llm.requests["explore"][0]
            messages = first["messages"]
            # The system prompt (with LOOM.md and the agent type) and the task: nothing else
            self.assertEqual([msg["role"] for msg in messages], ["system", "user"])
            self.assertIn("You are a sub-agent", messages[0]["content"])
            self.assertIn("Always use tabs in this project.", messages[0]["content"])
            self.assertIn("You are the explore agent", messages[0]["content"])
            self.assertEqual(
                messages[1]["content"], "Find where add() is defined and what's wrong with it."
            )
            everything = str(first["messages"])
            self.assertNotIn("An earlier secret request", everything)
            self.assertNotIn("assert add(2, 3) == 5", everything)
            self.assertNotIn("fix the adder", everything)
            # Its own grep and read
            results = results_of(llm.requests["explore"][1]["messages"])
            self.assertIn("calc.py:1: def add(a, b):", results[0])
            self.assertIn("return a - b", results[1])

            # The parent gets the report and the footer, not the child's tool results
            result = results_of(llm.requests["parent"][1]["messages"])[0]
            self.assertTrue(result.startswith(EXPLORE_REPORT), result)
            self.assertRegex(result, FOOTER_RE)
            self.assertIn("[task: 2 tool uses · ", result)
            self.assertNotIn("return a - b", result)

            # The task is remembered for /tasks
            task = coder.session.tasks[0]
            self.assertEqual((task.number, task.status, task.report), (1, "done", EXPLORE_REPORT))

    def test_child_tools(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[
                    reply(
                        None,
                        explore_task(),
                        call("task", description="Fix it", prompt="Fix add() in calc.py."),
                    ),
                    reply("Done."),
                ],
                explore=[reply(EXPLORE_REPORT)],
                general=[reply("Nothing to do.")],
            )
            self.run_parent(llm)

            parent_tools = tool_names(llm.requests["parent"][0])
            self.assertIn("task", parent_tools)
            explore_tools = tool_names(llm.requests["explore"][0])
            self.assertEqual(
                explore_tools,
                ["read_file", "list_dir", "glob", "grep", "web_search", "web_fetch"],
            )
            general_tools = tool_names(llm.requests["general"][0])
            self.assertEqual(general_tools, [name for name in parent_tools if name != "task"])

    def test_child_cant_start_tasks(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[
                    reply(None, call("task", description="Nest", prompt="Start another task.")),
                    reply("Done."),
                ],
                general=[
                    reply(None, call("task", description="Inner", prompt="Do it.")),
                    reply("I couldn't start a task."),
                ],
            )
            self.run_parent(llm)
            result = results_of(llm.requests["general"][1]["messages"])[0]
            self.assertIn("a sub-agent can't start tasks of its own", result)
            self.assertEqual(llm.left(), {})

    def test_parent_commits_the_childs_edits(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            llm = AgentsLLM(
                parent=[
                    reply(None, call("task", description="Fix the adder", prompt="Fix add().")),
                    reply("The sub-agent fixed add()."),
                ],
                general=[
                    reply(
                        None,
                        call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                    ),
                    reply("Changed calc.py:2 to a + b."),
                ],
            )
            coder = self.run_parent(llm)

            self.assertIn("a + b", Path("calc.py").read_text())
            self.assertFalse(repo.is_dirty())
            commits = list(repo.iter_commits())
            self.assertEqual(len(commits), 2)
            self.assertIn("calc.py", commits[0].stats.files)
            self.assertIn(commits[0].hexsha[:7], coder.loom_commit_hashes)

    def test_step_limit_forces_a_summary(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, call("glob", pattern="*.py")),
                    reply(None, call("grep", pattern="add")),
                    reply("I found add() in calc.py but didn't check the tests."),
                ],
            )
            coder = self.run_parent(llm, subagent_settings=dict(max_steps=2))
            self.assertEqual(llm.left(), {})

            last = llm.requests["explore"][-1]["messages"][-1]
            self.assertEqual(last, dict(role="user", content=subagents.STOP_NOW))
            result = results_of(llm.requests["parent"][1]["messages"])[0]
            self.assertTrue(result.startswith("I found add() in calc.py"), result)
            self.assertEqual(coder.session.tasks[0].status, "done")

    def test_no_report_returns_its_last_results_marked_incomplete(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, call("read_file", path="calc.py")),
                    # Told to stop, it calls a tool again, which doesn't run
                    reply(None, call("grep", pattern="add")),
                ],
            )
            coder = self.run_parent(llm, subagent_settings=dict(max_steps=1))
            self.assertEqual(llm.left(), {})

            result = results_of(llm.requests["parent"][1]["messages"])[0]
            self.assertIn("Incomplete: the sub-agent reached its limit of 1 steps", result)
            self.assertIn("return a - b", result)
            self.assertIn(subagents.NOT_RUN, str(llm.requests["explore"]) + result)
            self.assertEqual(coder.session.tasks[0].status, "incomplete")

    def test_empty_final_reply_asks_once_for_a_report(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, call("read_file", path="calc.py")),
                    reply(""),
                    reply(EXPLORE_REPORT),
                ],
            )
            self.run_parent(llm)
            asked = llm.requests["explore"][-1]["messages"][-1]
            self.assertEqual(asked, dict(role="user", content=subagents.ASK_FOR_REPORT))
            result = results_of(llm.requests["parent"][1]["messages"])[0]
            self.assertTrue(result.startswith(EXPLORE_REPORT))

    def test_child_compacts_at_its_own_context_cap(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("big.py").write_text("".join(f"value_{n} = {n}  # padding\n" for n in range(2000)))
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, call("read_file", path="big.py")),
                    reply(None, call("read_file", path="calc.py")),
                    reply(EXPLORE_REPORT),
                ],
            )
            with patch.object(subagents, "CONTEXT_TOKENS", 6000):
                coder = self.run_parent(llm)
            self.assertEqual(llm.left(), {})
            # The model's window is far larger, but the sub-agent compacted at its own cap
            self.assertGreater(coder.main_model.info["max_input_tokens"], 100_000)
            big = results_of(llm.requests["explore"][2]["messages"])[0]
            self.assertIn("characters dropped from the middle", big)
            names = [
                e["name"] for e in coder.session.tasks[0].io.transcript if e["kind"] == "tool_call"
            ]
            self.assertIn("Compacted the conversation", names)

    def test_long_report_keeps_its_start_and_end(self):
        with GitTemporaryDirectory():
            make_repo()
            report = "START " + "x" * 20_000 + " END"
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[reply(report)],
            )
            self.run_parent(llm)
            result = results_of(llm.requests["parent"][1]["messages"])[0]
            body = FOOTER_RE.split(result)[0]
            self.assertLess(len(body), subagents.MAX_REPORT_CHARS + 200)
            self.assertTrue(body.startswith("START "))
            self.assertTrue(body.endswith(" END"))
            self.assertIn("characters omitted", body)

    def test_plan_mode_refuses_general_and_allows_explore(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            llm = AgentsLLM(
                parent=[
                    reply(
                        None,
                        call("task", description="Fix it", prompt="Fix add().", agent="general"),
                        explore_task(),
                    ),
                    reply("Plan: change calc.py:2."),
                ],
                explore=[reply(EXPLORE_REPORT)],
            )
            self.run_parent(llm, io=io, permissions=Permissions(io, mode="plan"))
            self.assertEqual(llm.left(), {})

            description = next(
                schema["function"]["description"]
                for schema in llm.requests["parent"][0]["tools"]
                if schema["function"]["name"] == "task"
            )
            self.assertIn("only the read-only types run: explore", description)
            refused, explored = results_of(llm.requests["parent"][1]["messages"])
            self.assertIn("plan mode", refused)
            self.assertIn("the general agent can edit files", refused)
            self.assertTrue(explored.startswith(EXPLORE_REPORT))
            self.assertIn("plan mode", llm.requests["explore"][0]["messages"][0]["content"])

    def test_child_edits_are_refused_in_plan_mode_too(self):
        # A general task can't get round plan mode: it shares the parent's permissions
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            permissions = Permissions(io)
            llm = AgentsLLM(
                parent=[
                    reply(None, call("task", description="Fix it", prompt="Fix add().")),
                    reply("Done."),
                ],
                general=[
                    reply(
                        None,
                        call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                    ),
                    reply("I couldn't edit calc.py."),
                ],
            )
            coder = make_coder(io, permissions)
            original = coder.run_task

            def run_task(task):
                # Plan mode is turned on while the task runs
                permissions.mode = "plan"
                return original(task)

            coder.run_task = run_task
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix it")
            self.assertIn("a - b", Path("calc.py").read_text())
            result = results_of(llm.requests["general"][1]["messages"])[0]
            self.assertIn("plan mode", result)

    def test_costs_and_tokens_go_to_the_parent(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[reply(None, call("glob", pattern="*.py")), reply(EXPLORE_REPORT)],
            )
            io = InputOutput(yes=True)
            io.usage_output = MagicMock()
            coder = self.run_parent(llm, io=io)

            task = coder.session.tasks[0]
            child = task.child
            self.assertGreater(child.total_tokens_sent, 0)
            self.assertEqual(task.tokens, child.total_tokens_sent + child.total_tokens_received)
            # One report for the whole request, which includes the task's tokens
            io.usage_output.assert_called_once()
            report = io.usage_output.call_args[0][0]
            self.assertIn("Including 1 task:", report)
            sent = io.usage_output.call_args[1]["sent"]
            self.assertGreater(sent, child.total_tokens_sent)
            self.assertEqual(coder.total_tokens_sent, sent)
            self.assertEqual(coder.task_usage["tasks"], 0)

    def test_budget_stops_the_child(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, call("glob", pattern="*.py")),
                    reply("Out of budget: add() is in calc.py."),
                ],
            )

            def priced(self, *args, **kwargs):
                self.total_cost += 0.5
                self.message_cost += 0.5

            with patch.object(SubAgentCoder, "calculate_and_show_tokens_and_cost", priced):
                self.run_parent(llm, subagent_settings=dict(budget=0.4))
            self.assertEqual(llm.left(), {})
            self.assertEqual(
                llm.requests["explore"][-1]["messages"][-1]["content"], subagents.STOP_NOW
            )

    def test_denied_action_stops_the_child_and_the_parent(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            llm = AgentsLLM(
                parent=[reply(None, call("task", description="Fix it", prompt="Fix add()."))],
                general=[
                    reply(
                        None,
                        call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                    )
                ],
            )
            coder = self.run_parent(llm, io=io)
            self.assertEqual(llm.left(), {})
            # The question says which sub-agent asks
            question = io.permission_ask.call_args[0][0]
            self.assertEqual(question, "[general: Fix it] Edit calc.py?")
            self.assertTrue(coder.stop_requested)
            result = results_of(coder.done_messages)[0]
            self.assertIn("The user denied one of the sub-agent's actions", result)
            self.assertEqual(coder.session.tasks[0].status, "denied")

    def test_esc_in_a_child_stops_the_parent(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[
                    reply(
                        None,
                        call("task", description="Wait", prompt="Run sleep 30."),
                        call("read_file", path="calc.py"),
                    )
                ],
                general=[reply(None, call("bash", command="sleep 30"))],
            )
            coder = make_coder()

            def interrupted(*args, **kwargs):
                raise KeyboardInterrupt()

            with (
                patch.object(litellm, "completion", llm),
                patch.object(tools, "run_command", interrupted),
            ):
                coder.permissions.mode = "bypass"
                coder.run(with_message="go")

            self.assertTrue(coder.stop_requested)
            task_result, read_result = results_of(coder.done_messages)
            self.assertIn("Interrupted by the user", task_result)
            self.assertIn("Not run", read_result)
            self.assertEqual(coder.session.tasks[0].status, "interrupted")

    def test_task_on_the_weak_model(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[
                    reply(
                        None,
                        call(
                            "task",
                            description="Look",
                            prompt="Look.",
                            agent="explore",
                            model="weak",
                        ),
                        call(
                            "task",
                            description="Look",
                            prompt="Look.",
                            agent="explore",
                            model="gpt-9",
                        ),
                    ),
                    reply("Done."),
                ],
                explore=[reply(EXPLORE_REPORT)],
            )
            coder = self.run_parent(llm)
            weak = coder.main_model.weak_model.name
            self.assertEqual(llm.requests["explore"][0]["model"], weak)
            refused = results_of(llm.requests["parent"][1]["messages"])[1]
            self.assertIn("tasks can't use the model 'gpt-9'", refused)

    def test_no_subagents(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(parent=[reply("Done.")])
            coder = self.run_parent(llm, subagent_settings=dict(enabled=False))
            self.assertNotIn("task", tool_names(llm.requests["parent"][0]))
            system = llm.requests["parent"][0]["messages"][0]["content"]
            self.assertNotIn("# Sub-agents", system)
            self.assertFalse(coder.can_delegate())


if __name__ == "__main__":
    unittest.main()


def capture(io, terminal=False, width=100):
    """Point io's console at a string, as a terminal or not. Returns the string."""
    from io import StringIO

    from rich.console import Console

    out = StringIO()
    io.console = Console(
        file=out, force_terminal=terminal, width=width, color_system=None, highlight=False
    )
    return out


class FakeTask:
    label = "explore: auth flow"
    description = "Explore auth flow"
    status = "running"

    def summary(self):
        return "Done (5 tool uses · 2k tokens · 3s)"


class TestTaskDisplay(HomeDirMixin, unittest.TestCase):
    def board(self, terminal, verbose=False):
        from loom.subagent_io import TaskBoard

        io = InputOutput(pretty=True, yes=True)
        out = capture(io, terminal=terminal)
        board = TaskBoard(io, verbose=verbose)
        return board, board.view(FakeTask()), out

    def test_plain_output_prints_every_line(self):
        board, view, out = self.board(terminal=False)
        self.assertFalse(board.rolling)
        for num in range(5):
            view.tool_call("Read", f"file{num}.py")
        view.note("The linter found problems in file4.py.")
        board.close()
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                "  ⎿  Read(file0.py)",
                "     Read(file1.py)",
                "     Read(file2.py)",
                "     Read(file3.py)",
                "     Read(file4.py)",
                "     The linter found problems in file4.py.",
            ],
        )

    def test_terminal_rolls_the_last_three(self):
        board, view, out = self.board(terminal=True)
        self.assertTrue(board.rolling)
        printed = []
        board.print = lambda text: printed.append(text.plain)
        view.tool_call("Read", "src/auth/session.py")
        self.assertEqual(
            [t.plain for t in board.render().renderables], ["  ⎿  Read(src/auth/session.py)"]
        )
        for num in range(4):
            view.tool_call("Grep", f'"pattern{num}"')
        self.assertEqual(
            [t.plain for t in board.render().renderables],
            [
                '  ⎿  Grep("pattern1")',
                '     Grep("pattern2")',
                '     Grep("pattern3")',
                "     … +2 more tool uses",
            ],
        )
        self.assertEqual(printed, [])
        board.close()
        # What stays on the screen
        self.assertEqual(
            printed,
            [
                '  ⎿  Grep("pattern1")',
                '     Grep("pattern2")',
                '     Grep("pattern3")',
                "     … +2 more tool uses",
            ],
        )
        self.assertIsNone(board.live)

    def test_a_question_leaves_the_lines_on_the_screen(self):
        board, view, out = self.board(terminal=True)
        printed = []
        board.print = lambda text: printed.append(text.plain)
        view.tool_call("Read", "calc.py")
        view.tool_call("Update", "calc.py")
        board.pause()
        self.assertEqual(printed, ["  ⎿  Read(calc.py)", "     Update(calc.py)"])
        view.tool_call("Bash", "pytest")
        self.assertEqual([t.plain for t in board.render().renderables], ["     Bash(pytest)"])
        board.close()
        self.assertEqual(printed[-1], "     Bash(pytest)")

    def test_rendering_never_waits_for_the_board(self):
        # rich's refresh thread calls render() holding Live's own lock, so render() taking
        # the board's lock would deadlock with a thread holding it while refreshing
        board, view, out = self.board(terminal=True)
        view.tool_call("Read", "calc.py")
        holding, release = threading.Event(), threading.Event()

        def hold():
            with board.lock:
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        holding.wait(5)
        rendered = []
        renderer = threading.Thread(target=lambda: rendered.append(board.render()), daemon=True)
        renderer.start()
        renderer.join(2)
        release.set()
        self.assertTrue(rendered, "render() waited for the board's lock")
        board.close()

    def test_live_board_survives_threads_and_questions(self):
        from loom.subagent_io import TaskBoard

        io = InputOutput(pretty=True, yes=True)
        capture(io, terminal=True)

        class Task(FakeTask):
            def __init__(self, label):
                self.label = label
                self.description = label.split(": ")[1]

        with patch.object(TaskBoard, "REFRESH_PER_SECOND", 200):
            board = TaskBoard(io, headers=True)
            views = [board.view(Task(f"explore: Find {name}")) for name in "AB"]
            stop = threading.Event()

            def work(view):
                num = 0
                while not stop.is_set():
                    view.tool_call("Read", f"file{num}.py")
                    view.note("The API provider has rate limited you.", level="warning")
                    num += 1

            threads = [threading.Thread(target=work, args=(v,), daemon=True) for v in views]
            for thread in threads:
                thread.start()

            def questions():
                for _ in range(30):
                    with board.asking(views[0]):
                        pass
                board.close()

            main = threading.Thread(target=questions, daemon=True)
            main.start()
            main.join(20)
            stop.set()
            for thread in threads:
                thread.join(5)
            self.assertFalse(main.is_alive(), "the board deadlocked")
            self.assertIsNone(board.live)

    def test_verbose_prints_every_line_in_a_terminal(self):
        board, view, out = self.board(terminal=True, verbose=True)
        self.assertFalse(board.rolling)
        for num in range(5):
            view.tool_call("Read", f"file{num}.py")
        self.assertIn("Read(file0.py)", out.getvalue())
        self.assertIn("Read(file4.py)", out.getvalue())

    def test_task_display_in_a_run(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True)
            out = capture(io)
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, call("glob", pattern="*.py"), call("grep", pattern="add")),
                    reply(None, call("read_file", path="calc.py")),
                    reply("It said: " + EXPLORE_REPORT),
                ],
            )
            with patch.object(litellm, "completion", llm):
                make_coder(io).run(with_message="go")
            lines = out.getvalue().splitlines()
            start = lines.index("● Task(Find the adder)")
            self.assertEqual(
                lines[start : start + 5],
                [
                    "● Task(Find the adder)",
                    "  ⎿  Glob(*.py)",
                    '     Grep("add")',
                    "     Read(calc.py)",
                    lines[start + 4],
                ],
            )
            self.assertRegex(
                lines[start + 4], r"^  ⎿  Done \(3 tool uses · [\d.]+k tokens · \d+s\)$"
            )
            # The sub-agent's replies aren't shown
            self.assertNotIn("It said", out.getvalue())

    def test_rolling_display_in_a_run(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=True, yes=True)
            out = capture(io, terminal=True)
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[
                    reply(None, *[call("glob", pattern=f"*{num}.py") for num in range(5)]),
                    reply(EXPLORE_REPORT),
                ],
            )
            with patch.object(litellm, "completion", llm):
                make_coder(io).run(with_message="go")
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.getvalue())
            self.assertIn("     … +2 more tool uses\n  ⎿  Done (5 tool uses", text)


class TestTranscripts(HomeDirMixin, unittest.TestCase):
    def run_with_session(self, io=None):
        from loom.sessions import Session

        io = io or InputOutput(pretty=False, yes=True)
        session = Session(Path(".loom.sessions"))
        llm = AgentsLLM(
            parent=[reply(None, explore_task()), reply("Done.")],
            explore=[
                reply("Let me look.", call("read_file", path="calc.py")),
                reply(EXPLORE_REPORT),
            ],
        )
        coder = make_coder(io, session=session)
        with patch.object(litellm, "completion", llm):
            coder.run(with_message="go")
        return coder, session

    def test_transcript_is_saved_with_the_session(self):
        with GitTemporaryDirectory():
            make_repo()
            coder, session = self.run_with_session()
            path = Path(".loom.sessions") / session.id / "tasks" / "1.json"
            self.assertTrue(path.is_file())
            import json

            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["number"], 1)
            self.assertEqual(record["agent"], "explore")
            self.assertEqual(record["status"], "done")
            self.assertEqual(record["report"], EXPLORE_REPORT)
            self.assertEqual(record["tool_uses"], 1)
            self.assertTrue(record["prompt"].startswith("Find where add()"))
            kinds = [event["kind"] for event in record["events"]]
            self.assertEqual(
                kinds, ["user", "assistant", "tool_call", "tool_result", "tool_done", "assistant"]
            )
            self.assertEqual(record["events"][2]["name"], "Read")
            self.assertEqual(record["messages"][0]["content"], record["prompt"])
            # The session file itself is still the parent's conversation
            self.assertTrue((Path(".loom.sessions") / f"{session.id}.json").is_file())

    def test_tasks_command(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True)
            out = capture(io)
            coder, session = self.run_with_session(io)
            out.truncate(0)
            out.seek(0)
            coder.commands.run("/tasks")
            listing = out.getvalue().splitlines()
            self.assertEqual(
                listing[0].split(), ["#", "Status", "Agent", "Tokens", "Time", "Description"]
            )
            self.assertRegex(
                listing[1], r"^  1  done         explore\s+[\d.]+k\s+\d+s  Find the adder$"
            )

            out.truncate(0)
            out.seek(0)
            coder.commands.run("/tasks 1")
            shown = out.getvalue()
            self.assertIn("● Task 1(Find the adder)", shown)
            self.assertIn("explore · done · 1 tool use", shown)
            self.assertIn("> Find where add() is defined", shown)
            self.assertIn("● Read(calc.py)\n  ⎿  Read 2 lines", shown)
            self.assertIn(EXPLORE_REPORT, shown)

            out.truncate(0)
            out.seek(0)
            coder.commands.run("/tasks 7")
            self.assertIn("There's no task 7", out.getvalue())

    def test_resumed_session_lists_its_tasks_and_numbers_on(self):
        from loom.sessions import Session

        with GitTemporaryDirectory():
            make_repo()
            coder, session = self.run_with_session()
            resumed = Session.find(Path(".loom.sessions"), session.id)
            self.assertEqual(list(resumed.load_tasks()), [1])
            self.assertEqual(resumed.load_tasks()[1]["report"], EXPLORE_REPORT)
            self.assertEqual(resumed.next_task_number(), 2)
            resumed.restart()
            self.assertEqual(resumed.load_tasks(), {})

    def test_tasks_are_kept_in_memory_without_sessions(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Done.")],
                explore=[reply(EXPLORE_REPORT)],
            )
            coder = make_coder()
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="go")
            self.assertIsNone(coder.session.directory)
            self.assertEqual(coder.session.load_tasks()[1]["status"], "done")
            self.assertFalse(Path(".loom.sessions").exists())

    def test_deleting_an_old_session_deletes_its_tasks(self):
        from loom import sessions

        with GitTemporaryDirectory():
            make_repo()
            coder, session = self.run_with_session()
            tasks = Path(".loom.sessions") / session.id
            self.assertTrue(tasks.is_dir())
            with patch.object(sessions, "MAX_SESSIONS", 0):
                coder.session.restart()
                coder.done_messages = [dict(role="user", content="new")]
                coder.save_session()
            self.assertFalse(tasks.exists())


REVIEWER = """---
name: reviewer
description: Reviews a change for bugs. Use it after making a change.
tools: Read, Grep, glob, mcp__github
model: weak
---
You are a careful code reviewer.
Report bugs with file:line references.
"""


def write_agent(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestAgentFiles(HomeDirMixin, unittest.TestCase):
    def test_parsing(self):
        with GitTemporaryDirectory():
            path = write_agent(".loom/agents/reviewer.md", REVIEWER)
            agent_type, problems = subagents.load_agent_file(path, "project")
            self.assertEqual(problems, [])
            self.assertEqual(agent_type.name, "reviewer")
            self.assertEqual(
                agent_type.description, "Reviews a change for bugs. Use it after making a change."
            )
            # Claude Code's names become loom's
            self.assertEqual(agent_type.tools, ("read_file", "grep", "glob", "mcp__github"))
            self.assertEqual(agent_type.model, "weak")
            self.assertEqual(
                agent_type.prompt,
                "You are a careful code reviewer.\nReport bugs with file:line references.",
            )
            self.assertFalse(agent_type.read_only)
            self.assertEqual(agent_type.where(Path.cwd()), ".loom/agents/reviewer.md")
            self.assertTrue(agent_type.allows("mcp__github__get_issue"))
            self.assertFalse(agent_type.allows("mcp__gitlab__get_issue"))
            self.assertFalse(agent_type.allows("bash"))

    def test_defaults_and_problems(self):
        with GitTemporaryDirectory():
            path = write_agent(
                "finder.md",
                (
                    "---\ntools: [read_file, grep, Task, frobnicate]\nmodel: inherit\n---\n"
                    "Find things quickly.\nThen stop.\n"
                ),
            )
            agent_type, problems = subagents.load_agent_file(path, "user")
            # The name from the file, the description from the prompt
            self.assertEqual(
                (agent_type.name, agent_type.description), ("finder", "Find things quickly.")
            )
            self.assertIsNone(agent_type.model)
            self.assertEqual(agent_type.tools, ("read_file", "grep"))
            self.assertTrue(agent_type.read_only)
            self.assertEqual(len(problems), 2)
            self.assertIn("Task isn't available to sub-agents", problems[0])
            self.assertIn("unknown tool 'frobnicate'", problems[1])

            no_tools = write_agent("helper.md", "---\nname: helper\n---\nHelp.\n")
            agent_type, _ = subagents.load_agent_file(no_tools, "user")
            self.assertIsNone(agent_type.tools)
            self.assertTrue(agent_type.allows("bash"))
            self.assertFalse(agent_type.allows("task"))

            for text, error in [
                ("---\nname: x\n---\n", "has no prompt"),
                ("---\nname: bad name\n---\nHi.\n", "isn't a valid agent name"),
                ("---\nname: [x\n---\nHi.\n", "invalid front matter"),
            ]:
                path = write_agent("broken.md", text)
                with self.assertRaises(subagents.AgentFileError) as ctx:
                    subagents.load_agent_file(path, "user")
                self.assertIn(error, str(ctx.exception))

    def test_project_overrides_user_and_builtins_cant_be_overridden(self):
        with GitTemporaryDirectory():
            write_agent(Path.home() / ".loom/agents/reviewer.md", REVIEWER.replace("weak", "main"))
            write_agent(Path.home() / ".loom/agents/mine.md", "---\nname: mine\n---\nMine.\n")
            write_agent(".loom/agents/reviewer.md", REVIEWER)
            write_agent(".loom/agents/explore.md", "---\nname: explore\n---\nNot the real one.\n")
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            registry = subagents.AgentTypes(io, Path.cwd())
            types = registry.all()
            self.assertEqual(list(types), ["explore", "general", "plan", "mine", "reviewer"])
            self.assertEqual(types["reviewer"].source, "project")
            self.assertEqual(types["reviewer"].model, "weak")
            self.assertEqual(types["mine"].source, "user")
            self.assertIs(types["explore"], subagents.BUILTIN_TYPES["explore"])
            io.tool_warning.assert_called_once()
            self.assertIn(
                "built-in explore agent can't be overridden", io.tool_warning.call_args[0][0]
            )

            # Warned once, and reloaded when a file changes
            registry.all()
            io.tool_warning.assert_called_once()
            write_agent(".loom/agents/reviewer.md", REVIEWER.replace("weak", "main") + "More.\n")
            self.assertEqual(registry.all()["reviewer"].model, "main")

    def test_project_agents_need_approval(self):
        with GitTemporaryDirectory():
            path = write_agent(".loom/agents/reviewer.md", REVIEWER)
            write_agent(Path.home() / ".loom/agents/mine.md", "---\nname: mine\n---\nMine.\n")
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            registry = subagents.AgentTypes(io, Path.cwd())
            types = registry.all()

            self.assertTrue(registry.approve(types["mine"], io))
            self.assertTrue(registry.approve(types["explore"], io))
            io.permission_ask.assert_not_called()

            self.assertFalse(registry.approve(types["reviewer"], io))
            question = io.permission_ask.call_args[0][0]
            self.assertEqual(
                question, "Use the reviewer agent from this project's .loom/agents/reviewer.md?"
            )
            self.assertIn(
                "tools: read_file, grep, glob, mcp__github",
                io.permission_ask.call_args[1]["subject"],
            )
            self.assertTrue(io.permission_ask.call_args[1]["explicit_yes_required"])

            # Yes is for this session
            io.permission_ask.return_value = "yes"
            self.assertTrue(registry.approve(types["reviewer"], io))
            self.assertTrue(registry.approve(types["reviewer"], io))
            self.assertEqual(io.permission_ask.call_count, 2)
            self.assertFalse(subagents.approvals_file().exists())
            self.assertFalse(subagents.AgentTypes(io, Path.cwd()).is_approved(types["reviewer"]))

            # Always is remembered, until the file changes
            io.permission_ask.return_value = "always"
            fresh = subagents.AgentTypes(io, Path.cwd())
            self.assertTrue(fresh.approve(fresh.all()["reviewer"], io))
            saved = json.loads(subagents.approvals_file().read_text())
            self.assertEqual(saved, {str(path.resolve()): types["reviewer"].hash})
            again = subagents.AgentTypes(io, Path.cwd())
            self.assertTrue(again.is_approved(again.all()["reviewer"]))
            write_agent(path, REVIEWER + "Also check the tests.\n")
            self.assertFalse(again.is_approved(again.all()["reviewer"]))

    def test_new_agent_file(self):
        with GitTemporaryDirectory():
            path = subagents.new_agent_file(Path.cwd(), "reviewer")
            self.assertEqual(path, Path.cwd() / ".loom/agents/reviewer.md")
            agent_type, problems = subagents.load_agent_file(path, "project")
            self.assertEqual(problems, [])
            self.assertEqual(agent_type.name, "reviewer")
            self.assertEqual(agent_type.tools, ("read_file", "list_dir", "glob", "grep"))
            for name, error in [
                ("reviewer", "already exists"),
                ("explore", "built-in"),
                ("no way", "isn't a valid agent name"),
            ]:
                with self.assertRaises(subagents.AgentFileError) as ctx:
                    subagents.new_agent_file(Path.cwd(), name)
                self.assertIn(error, str(ctx.exception))

    def test_agents_command(self):
        with GitTemporaryDirectory():
            make_repo()
            write_agent(".loom/agents/reviewer.md", REVIEWER)
            io = InputOutput(pretty=False, yes=True)
            out = capture(io)
            coder = make_coder(io)
            coder.commands.run("/agents")
            text = out.getvalue()
            self.assertIn("explore (built-in, read-only)\n  Read-only: searches the code", text)
            self.assertIn("plan (built-in, read-only)", text)
            self.assertIn(
                (
                    "reviewer (.loom/agents/reviewer.md, model weak, asks before first use)\n"
                    "  Reviews a change for bugs."
                ),
                text,
            )
            coder.commands.run("/agents new tester")
            self.assertTrue(Path(".loom/agents/tester.md").is_file())
            self.assertIn("Created .loom/agents/tester.md", out.getvalue())


class TestCustomAgentTasks(HomeDirMixin, unittest.TestCase):
    def test_custom_agent_runs_with_its_tools_model_and_prompt(self):
        with GitTemporaryDirectory():
            make_repo()
            write_agent(".loom/agents/reviewer.md", REVIEWER)
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="yes")
            llm = AgentsLLM(
                parent=[
                    reply(
                        None,
                        call(
                            "task", description="Review", prompt="Review calc.py.", agent="reviewer"
                        ),
                    ),
                    reply("Done."),
                ],
                reviewer=[
                    reply(None, call("bash", command="rm -rf /")),
                    reply("calc.py:2 subtracts."),
                ],
            )
            coder = make_coder(io)
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="review it")
            self.assertEqual(llm.left(), {})

            # The task tool lists it
            description = next(
                schema["function"]["description"]
                for schema in llm.requests["parent"][0]["tools"]
                if schema["function"]["name"] == "task"
            )
            self.assertIn("- reviewer: Reviews a change for bugs.", description)
            self.assertIn("- plan: Read-only: investigates", description)

            first = llm.requests["reviewer"][0]
            self.assertEqual(tool_names(first), ["read_file", "glob", "grep"])
            self.assertEqual(first["model"], coder.main_model.weak_model.name)
            self.assertIn("You are a careful code reviewer.", first["messages"][0]["content"])
            refused = results_of(llm.requests["reviewer"][1]["messages"])[0]
            self.assertIn("Refused: the reviewer agent can't use bash", refused)
            # Asked once, before it first ran
            self.assertIn("Use the reviewer agent", io.permission_ask.call_args_list[0][0][0])

    def test_unapproved_project_agent_stops_the_parent(self):
        with GitTemporaryDirectory():
            make_repo()
            write_agent(".loom/agents/reviewer.md", REVIEWER)
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            llm = AgentsLLM(
                parent=[
                    reply(
                        None, call("task", description="Review", prompt="Review.", agent="reviewer")
                    )
                ],
            )
            coder = make_coder(io)
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="review it")
            self.assertTrue(coder.stop_requested)
            self.assertIn(
                "didn't approve the project's reviewer agent", results_of(coder.done_messages)[0]
            )
            self.assertEqual(coder.session.tasks, [])

    def test_plan_mode_allows_read_only_custom_agents(self):
        with GitTemporaryDirectory():
            make_repo()
            write_agent(
                Path.home() / ".loom/agents/finder.md", "---\ntools: Read, Grep\n---\nFind.\n"
            )
            write_agent(
                Path.home() / ".loom/agents/fixer.md", "---\ntools: Read, Edit\n---\nFix.\n"
            )
            write_agent(Path.home() / ".loom/agents/planner.md", "---\nname: planner\n---\nPlan.\n")
            io = InputOutput(yes=True)
            llm = AgentsLLM(
                parent=[
                    reply(
                        None,
                        call("task", description="Find", prompt="Find add.", agent="finder"),
                        call("task", description="Fix", prompt="Fix add.", agent="fixer"),
                        call("task", description="Plan", prompt="Plan.", agent="planner"),
                        call("task", description="Plan", prompt="Plan the fix.", agent="plan"),
                    ),
                    reply("Plan: fix calc.py:2."),
                ],
                finder=[reply("calc.py:2")],
                plan=[reply("1. Change calc.py:2.")],
            )
            coder = make_coder(io, Permissions(io, mode="plan"))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="plan it")
            self.assertEqual(llm.left(), {})
            found, fixer, planner, plan = results_of(llm.requests["parent"][1]["messages"])
            self.assertTrue(found.startswith("calc.py:2"))
            self.assertIn("plan mode", fixer)
            self.assertIn("plan mode", planner)
            self.assertTrue(plan.startswith("1. Change calc.py:2."))
            self.assertIn("read_only types run: explore, plan, finder", description_of(llm))
            self.assertNotIn("exit_plan_mode", tool_names(llm.requests["plan"][0]))


def description_of(llm):
    return next(
        schema["function"]["description"]
        for schema in llm.requests["parent"][0]["tools"]
        if schema["function"]["name"] == "task"
    ).replace("read-only", "read_only")


TASK_HOOK = """
import json, sys
data = json.load(sys.stdin)
with open("hook-log.jsonl", "a") as f:
    f.write(json.dumps(data) + "\\n")
action = sys.argv[1]
if action == "block-task":
    print("No sub-agents in this repo", file=sys.stderr)
    sys.exit(2)
if action == "carry-on":
    log = [json.loads(line) for line in open("hook-log.jsonl")]
    if len([e for e in log if e["hook_event_name"] == "SubagentStop"]) == 1:
        print(json.dumps({"decision": "block", "reason": "Also check the tests."}))
"""


class TestTaskHooks(HomeDirMixin, unittest.TestCase):
    def hooks(self, io, *specs):
        import sys

        from loom.hooks import Hook, Hooks

        Path("hook.py").write_text(TASK_HOOK)
        hooks = [
            Hook(event, matcher, f'"{sys.executable}" hook.py {action}', 60, "test")
            for event, matcher, action in specs
        ]
        return Hooks(io, hooks, root=str(Path.cwd()))

    def log(self):
        return [json.loads(line) for line in Path("hook-log.jsonl").read_text().splitlines()]

    def test_pre_tool_use_hook_blocks_a_task(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            llm = AgentsLLM(parent=[reply(None, explore_task()), reply("Ok.")])
            coder = make_coder(io, hooks=self.hooks(io, ("PreToolUse", "Task", "block-task")))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="go")
            self.assertEqual(llm.left(), {})
            result = results_of(llm.requests["parent"][1]["messages"])[0]
            self.assertIn("No sub-agents in this repo", result)
            self.assertEqual(coder.session.tasks, [])
            event = self.log()[0]
            self.assertEqual(event["tool_name"], "Task")
            self.assertEqual(event["tool_input"]["subagent_type"], "explore")
            self.assertEqual(event["tool_input"]["description"], "Find the adder")

    def test_hooks_see_the_childs_tool_calls_and_subagent_stop(self):
        with GitTemporaryDirectory():
            make_repo()
            from loom.sessions import Session

            io = InputOutput(yes=True)
            hooks = self.hooks(
                io,
                ("PreToolUse", "task|read_file", "log"),
                ("PostToolUse", "Task", "log"),
                ("SubagentStop", "explore", "carry-on"),
                ("SubagentStop", "general", "log"),
            )
            llm = AgentsLLM(
                parent=[reply(None, explore_task()), reply("Ok.")],
                explore=[
                    reply(None, call("read_file", path="calc.py")),
                    reply("First report."),
                    reply(None, call("glob", pattern="test_*.py")),
                    reply("Second report, with the tests."),
                ],
            )
            session = Session(Path(".loom.sessions"))
            coder = make_coder(io, hooks=hooks, session=session)
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="go")
            self.assertEqual(llm.left(), {})

            log = self.log()
            events = [(e["hook_event_name"], e.get("tool_name")) for e in log]
            self.assertEqual(
                events,
                [
                    ("PreToolUse", "Task"),
                    ("PreToolUse", "read_file"),
                    ("SubagentStop", None),
                    ("SubagentStop", None),
                    ("PostToolUse", "Task"),
                ],
            )
            read = log[1]
            self.assertEqual((read["agent_id"], read["agent_type"]), ("1", "explore"))
            self.assertEqual(read["session_id"], session.id)
            first_stop, second_stop = log[2], log[3]
            self.assertEqual(first_stop["report"], "First report.")
            self.assertFalse(first_stop["stop_hook_active"])
            self.assertTrue(second_stop["stop_hook_active"])
            self.assertEqual(first_stop["description"], "Find the adder")
            self.assertTrue(Path(first_stop["transcript_path"]).is_file())
            # The hook's reason went to the sub-agent, which carried on
            self.assertEqual(
                llm.requests["explore"][2]["messages"][-1],
                dict(role="user", content="Also check the tests."),
            )
            result = results_of(llm.requests["parent"][1]["messages"])[0]
            self.assertTrue(result.startswith("Second report, with the tests."))
            self.assertIn("[task: 2 tool uses", result)
            self.assertTrue(log[4]["tool_response"].startswith("Second report"))


class PromptsLLM:
    """Stands in for litellm.completion while sub-agents run at once: each sub-agent gets
    the replies scripted for its task's prompt, and the parent those for "parent". A
    scripted reply can be a function of the request, which may wait for other threads."""

    def __init__(self, **scripts):
        self.scripts = {name: list(replies) for name, replies in scripts.items()}
        self.requests = {name: [] for name in scripts}
        self.threads = {name: set() for name in scripts}
        self.times = {name: [] for name in scripts}
        self.lock = threading.Lock()
        self.turns = 0

    def __call__(self, **kwargs):
        import time

        if not kwargs.get("tools"):
            message = Message(content="Fix the adder")
            return ModelResponse(choices=[Choices(message=message, finish_reason="stop")])
        messages = kwargs["messages"]
        name = messages[1]["content"] if agent_of(kwargs) != "parent" else "parent"
        with self.lock:
            script = self.scripts.get(name)
            if not script:
                raise AssertionError(f"{name!r} asked for more replies than scripted")
            self.requests[name].append(kwargs)
            self.threads[name].add(threading.current_thread().name)
            scripted = script.pop(0)
            self.turns += 1
            turn = self.turns
        start = time.monotonic()
        if callable(scripted):
            scripted = scripted(kwargs)
        self.times[name].append((start, time.monotonic()))
        if kwargs["stream"]:
            return stream_response(scripted, turn)
        return full_response(scripted, turn)

    def left(self):
        return {name: len(script) for name, script in self.scripts.items() if script}


def task_call(prompt, description=None, agent="explore"):
    return call("task", description=description or prompt.rstrip("."), prompt=prompt, agent=agent)


def waits_for(barrier, then):
    """A scripted reply that waits at barrier first: it only passes when every sub-agent
    meant to be running at once is."""

    def scripted(kwargs):
        barrier.wait()
        return then

    return scripted


class TestParallelTasks(HomeDirMixin, unittest.TestCase):
    def run_parent(self, llm, io=None, **kwargs):
        io = io or InputOutput(pretty=False, yes=True)
        coder = make_coder(io, **kwargs)
        with patch.object(litellm, "completion", llm):
            coder.run(with_message="go")
        return coder

    def test_tasks_in_one_reply_run_at_once(self):
        with GitTemporaryDirectory():
            make_repo()
            # Neither can pass the barrier until the other reaches it
            barrier = threading.Barrier(2, timeout=10)
            llm = PromptsLLM(
                parent=[
                    reply(None, task_call("Look at calc."), task_call("Look at the tests.")),
                    reply("Done."),
                ],
                **{
                    "Look at calc.": [
                        waits_for(barrier, reply(None, call("read_file", path="calc.py"))),
                        reply("calc.py:2 subtracts."),
                    ],
                    "Look at the tests.": [
                        waits_for(barrier, reply(None, call("read_file", path="test_calc.py"))),
                        reply("test_calc.py:5 expects 5."),
                    ],
                },
            )
            coder = self.run_parent(llm)
            self.assertEqual(llm.left(), {})
            (a_start, a_end), (b_start, b_end) = (
                llm.times["Look at calc."][0],
                llm.times["Look at the tests."][0],
            )
            # The first replies overlapped
            self.assertLess(max(a_start, b_start), min(a_end, b_end))
            threads = llm.threads["Look at calc."] | llm.threads["Look at the tests."]
            self.assertEqual(len(threads), 2)
            self.assertTrue(all(name.startswith("loom-task") for name in threads))
            tasks = coder.session.tasks
            self.assertEqual([(t.number, t.status) for t in tasks], [(1, "done"), (2, "done")])

    def test_results_come_back_in_call_order(self):
        with GitTemporaryDirectory():
            make_repo()
            second_done = threading.Event()

            def slow(kwargs):
                # The first task finishes after the second
                self.assertTrue(second_done.wait(10))
                return reply("First report.")

            def fast(kwargs):
                second_done.set()
                return reply("Second report.")

            llm = PromptsLLM(
                parent=[
                    reply(
                        None,
                        task_call("First."),
                        call("read_file", path="calc.py"),
                        task_call("Second."),
                    ),
                    reply("Done."),
                ],
                **{"First.": [slow], "Second.": [fast]},
            )
            self.run_parent(llm)
            messages = llm.requests["parent"][1]["messages"]
            tools = [msg for msg in messages if msg["role"] == "tool"]
            self.assertEqual(
                [msg["tool_call_id"] for msg in tools], ["call_1_0", "call_1_1", "call_1_2"]
            )
            self.assertTrue(tools[0]["content"].startswith("First report."))
            self.assertIn("return a - b", tools[1]["content"])
            self.assertTrue(tools[2]["content"].startswith("Second report."))

    def test_a_question_from_a_task_is_answered_on_the_main_thread(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=None)
            asked = []

            def permission_ask(question, **kwargs):
                asked.append((question, threading.current_thread() is threading.main_thread()))
                return "yes"

            io.permission_ask = permission_ask
            barrier = threading.Barrier(2, timeout=10)
            llm = PromptsLLM(
                parent=[
                    reply(
                        None,
                        task_call("Run the tests.", "Test", agent="general"),
                        task_call("Read calc.", "Read"),
                    ),
                    reply("Done."),
                ],
                **{
                    "Run the tests.": [
                        waits_for(barrier, reply(None, call("bash", command="echo tested"))),
                        reply("The tests ran."),
                    ],
                    "Read calc.": [
                        waits_for(barrier, reply(None, call("read_file", path="calc.py"))),
                        reply("Read it."),
                    ],
                },
            )
            coder = self.run_parent(llm, io=io)
            self.assertEqual(llm.left(), {})
            self.assertEqual(asked, [("[general: Test] Run this command?", True)])
            result = results_of(llm.requests["Run the tests."][1]["messages"])[0]
            self.assertIn("tested", result)
            self.assertEqual([t.status for t in coder.session.tasks], ["done", "done"])

    def test_esc_stops_every_task(self):
        import time

        from loom.workers import Asks

        with GitTemporaryDirectory():
            make_repo()
            sleep = f'"{sys.executable}" -c "import time; time.sleep(30)"'
            llm = PromptsLLM(
                parent=[
                    reply(
                        None,
                        task_call("Sleep one.", agent="general"),
                        task_call("Sleep two.", agent="general"),
                        call("read_file", path="calc.py"),
                    )
                ],
                **{
                    "Sleep one.": [reply(None, call("bash", command=sleep))],
                    "Sleep two.": [reply(None, call("bash", command=sleep))],
                },
            )
            original = Asks.serve

            def serve(self, io, timeout=0.1):
                # Esc, once both commands are running
                with tools.RUNNING_LOCK:
                    running = len(tools.RUNNING)
                if running == 2:
                    raise KeyboardInterrupt()
                return original(self, io, timeout)

            io = InputOutput(pretty=False, yes=True)
            coder = make_coder(io)
            coder.permissions.mode = "bypass"
            started = time.monotonic()
            with patch.object(litellm, "completion", llm), patch.object(Asks, "serve", serve):
                coder.run(with_message="go")
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(tools.RUNNING, {})
            self.assertTrue(coder.stop_requested)
            one, two, read = results_of(coder.done_messages)
            self.assertIn("Interrupted by the user", one)
            self.assertIn("Interrupted by the user", two)
            self.assertIn("Not run", read)
            self.assertEqual(
                [t.status for t in coder.session.tasks], ["interrupted", "interrupted"]
            )

    def test_costs_are_summed_exactly(self):
        with GitTemporaryDirectory():
            make_repo()
            barrier = threading.Barrier(2, timeout=10)
            llm = PromptsLLM(
                parent=[reply(None, task_call("A."), task_call("B.")), reply("Done.")],
                **{
                    "A.": [waits_for(barrier, reply(None, call("glob", pattern="*"))), reply("a")],
                    "B.": [waits_for(barrier, reply(None, call("glob", pattern="*"))), reply("b")],
                },
            )

            def priced(self, messages, completion=None):
                # Each request costs 1/8 dollar, 1000 tokens sent and 10 received
                self.total_cost += 0.125
                self.message_cost += 0.125
                self.message_tokens_sent += 1000
                self.message_tokens_received += 10
                self.usage_report = "Tokens"

            io = InputOutput(pretty=False, yes=True)
            io.usage_output = MagicMock()
            with patch.object(Coder, "calculate_and_show_tokens_and_cost", priced):
                coder = self.run_parent(llm, io=io)
            tasks = coder.session.tasks
            self.assertEqual([t.cost for t in tasks], [0.25, 0.25])
            self.assertEqual([t.tokens for t in tasks], [2020, 2020])
            # Two requests of the parent's and two of each sub-agent's
            self.assertEqual(coder.total_cost, 0.75)
            self.assertEqual(coder.total_tokens_sent, 6000)
            self.assertEqual(coder.total_tokens_received, 60)
            io.usage_output.assert_called_once()
            self.assertEqual(io.usage_output.call_args[1]["sent"], 6000)
            self.assertEqual(io.usage_output.call_args[1]["cost"], 0.75)
            self.assertIn(
                "Including 2 tasks: 4.0k sent, 40 received.", io.usage_output.call_args[0][0]
            )

    def test_max_parallel_tasks(self):
        with GitTemporaryDirectory():
            make_repo()
            llm = PromptsLLM(
                parent=[
                    reply(None, task_call("A."), task_call("B."), task_call("C.")),
                    reply("Done."),
                ],
                **{"A.": [reply("a")], "B.": [reply("b")], "C.": [reply("c")]},
            )
            coder = self.run_parent(llm, subagent_settings=dict(max_parallel=1))
            self.assertEqual(llm.left(), {})
            # One at a time, on the main thread
            for name in ("A.", "B.", "C."):
                self.assertEqual(llm.threads[name], {"MainThread"})
            self.assertEqual([t.status for t in coder.session.tasks], ["done"] * 3)

    def test_parallel_display(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True)
            out = capture(io)
            llm = PromptsLLM(
                parent=[reply(None, task_call("Find A."), task_call("Find B.")), reply("Done.")],
                **{
                    "Find A.": [reply(None, call("glob", pattern="*.py")), reply("a")],
                    "Find B.": [reply(None, call("grep", pattern="add")), reply("b")],
                },
            )
            self.run_parent(llm, io=io)
            lines = out.getvalue().splitlines()
            # Labelled lines as they come, then each task's outcome in order
            self.assertIn("     [explore: Find A] Glob(*.py)", lines)
            self.assertIn('     [explore: Find B] Grep("add")', lines)
            start = lines.index("● Task(Find A)")
            self.assertEqual(lines[start + 2], "● Task(Find B)")
            self.assertRegex(lines[start + 1], r"^  ⎿  Done \(1 tool use · ")
            self.assertRegex(lines[start + 3], r"^  ⎿  Done \(1 tool use · ")

    def test_parallel_board_in_a_terminal(self):
        from loom.subagent_io import TaskBoard

        io = InputOutput(pretty=True, yes=True)
        capture(io, terminal=True)
        board = TaskBoard(io, headers=True)

        class Task(FakeTask):
            def __init__(self, label, status):
                self.label = label
                self.description = label.split(": ")[1]
                self.status = status

        first = board.view(Task("explore: Find A", "running"))
        board.view(Task("explore: Find B", "pending"))
        for num in range(3):
            first.tool_call("Read", f"a{num}.py")
        self.assertEqual(
            [t.plain for t in board.render().renderables],
            [
                "● Task(Find A)",
                "  ⎿  Read(a2.py)",
                "     … +2 more tool uses",
                "● Task(Find B)",
                "  ⎿  Waiting to start…",
            ],
        )
        first.task.status = "done"
        self.assertEqual(
            [t.plain for t in board.render().renderables][:2],
            ["● Task(Find A)", "  ⎿  Done (5 tool uses · 2k tokens · 3s)"],
        )
        printed = []
        board.print = lambda text: printed.append(text.plain)
        board.close()
        self.assertEqual(printed[0], "● Task(Find A)")

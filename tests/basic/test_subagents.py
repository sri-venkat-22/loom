import re
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from litellm.types.utils import Choices, Message, ModelResponse

from loom import subagents, tools
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


class TestTaskTool(unittest.TestCase):
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

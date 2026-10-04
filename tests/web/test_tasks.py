"""Sub-agents' tasks in loom --web: their tool calls nested in Task cards, task messages,
questions on a sub-agent's own card, and stopping one task."""

import sys
import threading
import unittest
from unittest.mock import patch

from loom.io import InputOutput
from loom.llm import litellm
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory
from tests.basic.test_agent import call, make_coder, make_repo, reply
from tests.basic.test_mcp import HomeDirMixin
from tests.basic.test_subagents import PromptsLLM, task_call

from .test_webio import answer_asks, make_io


def of_type(session, type):
    return [msg for msg in session.history if msg["type"] == type]


class TestWebTasks(HomeDirMixin, unittest.TestCase):
    def run_coder(self, io, llm, permissions=None):
        coder = make_coder(io, permissions or Permissions(io))
        with patch.object(litellm, "completion", llm):
            coder.run(with_message="go")
        return coder

    def test_one_task_nests_its_cards_in_the_task_card(self):
        with GitTemporaryDirectory():
            make_repo()
            io, session = make_io()
            llm = PromptsLLM(
                parent=[reply(None, task_call("Find add.", "Find the adder")), reply("Done.")],
                **{
                    "Find add.": [
                        reply(None, call("grep", pattern="def add")),
                        reply(None, call("read_file", path="calc.py")),
                        reply("calc.py:2"),
                    ]
                },
            )
            self.run_coder(io, llm)
            starts = of_type(session, "tool_start")
            task_card = starts[0]
            self.assertEqual((task_card["name"], task_card["detail"]), ("Task", "Find the adder"))
            self.assertNotIn("parent_id", task_card)
            children = [msg for msg in starts if msg.get("parent_id") == task_card["id"]]
            self.assertEqual([msg["name"] for msg in children], ["Grep", "Read"])
            self.assertEqual({msg["agent_id"] for msg in children}, {"1"})
            ends = {msg["id"]: msg for msg in of_type(session, "tool_end")}
            for child in children:
                self.assertEqual(ends[child["id"]]["status"], "done")
                self.assertEqual(ends[child["id"]]["parent_id"], task_card["id"])
            outputs = [msg for msg in of_type(session, "tool_output") if msg.get("agent_id")]
            self.assertIn("Read 2 lines", [line for msg in outputs for line in msg["lines"]])
            # The Task card ends with the report the parent got
            self.assertTrue(ends[task_card["id"]]["output"].startswith("calc.py:2"))

            tasks = of_type(session, "task")
            self.assertEqual([msg["status"] for msg in tasks], ["pending", "running", "done"])
            done = tasks[-1]
            self.assertEqual(done["parent_id"], task_card["id"])
            self.assertEqual((done["number"], done["agent"]), (1, "explore"))
            self.assertEqual(done["tool_uses"], 2)
            self.assertRegex(done["summary"], r"^Done \(2 tool uses · ")
            self.assertEqual(done["prompt"], "Find add.")
            # The sub-agent's replies stay out of the chat
            texts = "".join(msg["text"] for msg in of_type(session, "assistant_delta"))
            self.assertNotIn("calc.py:2", texts)

    def test_a_task_asks_on_its_own_card(self):
        with GitTemporaryDirectory():
            make_repo()
            io, session = make_io()
            llm = PromptsLLM(
                parent=[
                    reply(None, task_call("Run echo.", "Echo", agent="general")),
                    reply("Done."),
                ],
                **{
                    "Run echo.": [reply(None, call("bash", command="echo hi")), reply("Ran it.")],
                },
            )
            asks, thread = answer_asks(session, "yes")
            self.run_coder(io, llm)
            thread.join(5)
            ask = asks[0]
            self.assertEqual(ask["question"], "[general: Echo] Run this command?")
            bash = next(msg for msg in of_type(session, "tool_start") if msg["name"] == "Bash")
            self.assertEqual(ask["tool_id"], bash["id"])
            self.assertEqual(bash["agent_id"], "1")

    def test_tasks_at_once_get_their_own_task_cards_in_one_batch(self):
        with GitTemporaryDirectory():
            make_repo()
            io, session = make_io()
            barrier = threading.Barrier(2, timeout=10)

            def waits(then):
                def scripted(kwargs):
                    barrier.wait()
                    return then

                return scripted

            llm = PromptsLLM(
                parent=[
                    reply(
                        None, task_call("A.", "Map A"), task_call("B.", "Map B", agent="general")
                    ),
                    reply("Done."),
                ],
                **{
                    "A.": [waits(reply(None, call("glob", pattern="*.py"))), reply("a")],
                    "B.": [waits(reply(None, call("bash", command="echo b"))), reply("b")],
                },
            )
            asks, thread = answer_asks(session, "yes")
            coder = self.run_coder(io, llm)
            thread.join(5)
            cards = [msg for msg in of_type(session, "tool_start") if msg["name"] == "Task"]
            self.assertEqual([msg["detail"] for msg in cards], ["Map A", "Map B"])
            tasks = {msg["parent_id"]: msg for msg in of_type(session, "task")}
            self.assertEqual(len({msg["batch"] for msg in tasks.values()}), 1)
            self.assertEqual([tasks[card["id"]]["status"] for card in cards], ["done", "done"])
            ends = {msg["id"]: msg for msg in of_type(session, "tool_end")}
            self.assertTrue(ends[cards[0]["id"]]["output"].startswith("a"))
            # B's question went on B's bash card, from the main thread
            bash = next(msg for msg in of_type(session, "tool_start") if msg["name"] == "Bash")
            self.assertEqual(bash["parent_id"], cards[1]["id"])
            self.assertEqual(asks[0]["tool_id"], bash["id"])
            self.assertEqual([t.status for t in coder.session.tasks], ["done", "done"])

    def test_stop_one_task(self):
        with GitTemporaryDirectory():
            make_repo()
            io, session = make_io()
            sleep = f'"{sys.executable}" -c "import time; time.sleep(30)"'
            llm = PromptsLLM(
                parent=[
                    reply(
                        None,
                        task_call("Sleep.", "Sleep", agent="general"),
                        task_call("Read.", "Read calc"),
                    ),
                    reply("Carried on without the sleeper."),
                ],
                **{
                    "Sleep.": [reply(None, call("bash", command=sleep))],
                    "Read.": [reply(None, call("read_file", path="calc.py")), reply("Read it.")],
                },
            )

            def stop_when_sleeping():
                from loom import tools

                for _ in range(1000):
                    with tools.RUNNING_LOCK:
                        running = bool(tools.RUNNING)
                    if running:
                        self.assertTrue(session.handle(dict(type="stop_task", agent_id="1")))
                        return
                    threading.Event().wait(0.01)

            stopper = threading.Thread(target=stop_when_sleeping, daemon=True)
            stopper.start()
            coder = make_coder(io, Permissions(io))
            coder.permissions.mode = "bypass"
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="go")
            stopper.join(5)
            self.assertEqual(llm.left(), {})
            self.assertEqual([t.status for t in coder.session.tasks], ["stopped", "done"])
            self.assertFalse(coder.stop_requested)
            stopped = [msg for msg in coder.done_messages if msg["role"] == "tool"][0]["content"]
            self.assertIn("The user stopped this sub-agent", stopped)
            # Nothing to stop once it's over
            self.assertFalse(session.handle(dict(type="stop_task", agent_id="1")))

    def test_terminal_before_the_browser_connects(self):
        # Until the server listens, tasks show in the terminal like anywhere else
        with GitTemporaryDirectory():
            make_repo()
            io, session = make_io(started=False)
            llm = PromptsLLM(
                parent=[reply(None, task_call("Find add.")), reply("Done.")],
                **{"Find add.": [reply("calc.py:2")]},
            )
            self.run_coder(io, llm)
            self.assertFalse(of_type(session, "task"))
            self.assertIsInstance(InputOutput.task_board(io), object)


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from unittest.mock import patch

from loom.llm import litellm
from loom.models import Model
from scripts import check_agent_models as check

from .test_agent import FakeLLM, call, reply

TEXT_CALL = '<tool_call>{"name": "read_file", "arguments": {"path": "README.md"}}</tool_call>'


def raising(error):
    def completion(**kwargs):
        raise error

    return completion


class TestProbe(unittest.TestCase):
    def probe(self, llm):
        with patch.object(litellm, "completion", llm):
            res = check.probe(Model("gpt-4o-mini"))
        return res, check.verdict(dict(probe=res))

    def test_parallel_tool_calls(self):
        llm = FakeLLM(
            reply(None, call("read_file", path="README.md"), call("read_file", path="setup.py"))
        )
        res, _ = self.probe(llm)
        self.assertTrue(res["ok"])
        self.assertTrue(res["parallel"])
        self.assertEqual(check.describe_probe(res)[:22], "ok, 2 parallel calls, ")

    def test_tool_call_as_text(self):
        res, verdict = self.probe(FakeLLM(reply(TEXT_CALL)))
        self.assertFalse(res["ok"])
        self.assertTrue(res["as_text"])
        self.assertEqual(verdict, "no: use an edit format")
        self.assertEqual(check.describe_probe(res), "wrote the calls as text")

    def test_rejected_tools(self):
        error = litellm.BadRequestError(
            message="this model does not support tools", model="x", llm_provider="x"
        )
        res, verdict = self.probe(raising(error))
        self.assertTrue(res["rejected_tools"])
        self.assertEqual(verdict, "no: use an edit format")

    def test_unreachable_model_is_not_tested(self):
        error = litellm.Timeout(message="Request timed out", model="x", llm_provider="x")
        res, verdict = self.probe(raising(error))
        self.assertFalse(res["rejected_tools"])
        self.assertEqual(verdict, "not tested: the model couldn't be reached")


class TestTask(unittest.TestCase):
    def test_agent_task(self):
        llm = FakeLLM(
            reply(None, call("read_file", path="check_calc.py"), call("read_file", path="calc.py")),
            reply(None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
            reply(None, call("bash", command=f"{sys.executable} check_calc.py")),
            reply("Fixed add; check_calc.py prints OK."),
        )
        with patch.object(litellm, "completion", llm):
            res = check.run_task(Model("gpt-4o-mini"), 60, None, stream=False)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["steps"], 3)
        self.assertEqual(res["tools"], ["read_file", "read_file", "edit_file", "bash"])
        self.assertEqual(check.verdict(dict(probe=dict(ok=True), task=res)), "yes")

    def test_task_that_stops_early(self):
        llm = FakeLLM(
            reply(None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
            reply("Done."),
        )
        with patch.object(litellm, "completion", llm):
            res = check.run_task(Model("gpt-4o-mini"), 60, None, stream=True)

        self.assertTrue(res["fixed"])
        self.assertFalse(res["checked"])
        self.assertFalse(res["ok"])
        self.assertIn("fixed, not checked", check.describe_task(res))
        self.assertEqual(
            check.verdict(dict(probe=dict(ok=True), task=res)),
            "partly: tool calls work, the task failed",
        )


if __name__ == "__main__":
    unittest.main()

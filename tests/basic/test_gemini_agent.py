"""The agent with Gemini 3, whose API is faked at the HTTP level, so litellm's real request
and response translation runs. Gemini 3 rejects a conversation whose function calls come
back without the thought signatures it sent with them."""

import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from loom.coders import Coder
from loom.io import InputOutput
from loom.models import Model
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory

from .test_agent import make_repo

GEMINI_3 = "gemini/gemini-3.1-pro-preview"
# What litellm sends for a call Gemini didn't sign, like the second of parallel calls:
# Google's documented value for skipping the check
UNSIGNED = base64.b64encode(b"skip_thought_signature_validator").decode()


def function_call(name, signature=None, **args):
    part = dict(functionCall=dict(name=name, args=args))
    if signature:
        part["thoughtSignature"] = signature
    return part


class FakeGemini:
    """Answers requests that offer tools with the scripted model turns (lists of parts),
    and others (commit messages) with a canned text."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []

    def __call__(self, url, data=None, json=None, stream=False, **kwargs):
        body = json if json is not None else globals()["json"].loads(data)
        if body.get("tools"):
            self.requests.append(body)
            parts = self.turns.pop(0)
        else:
            parts = [dict(text="fix: scripted change")]

        response = dict(
            candidates=[
                dict(content=dict(role="model", parts=parts), finishReason="STOP", index=0)
            ],
            usageMetadata=dict(promptTokenCount=100, candidatesTokenCount=10, totalTokenCount=110),
            modelVersion="gemini-3.1-pro-preview",
        )
        request = httpx.Request("POST", url)
        if "streamGenerateContent" in url:
            content = f"data: {globals()['json'].dumps(response)}\r\n\r\n".encode()
            return httpx.Response(200, content=content, request=request)
        return httpx.Response(200, json=response, request=request)


def field(part, name):
    """A part's field, which litellm may spell in snake_case, like function_call."""
    snake = "".join("_" + c.lower() if c.isupper() else c for c in name)
    return part.get(name, part.get(snake))


def model_turns(body):
    """The model turns in a request: each one's function calls and signatures."""
    res = []
    for content in body["contents"]:
        if content["role"] != "model":
            continue
        calls = [
            (field(part, "functionCall")["name"], field(part, "thoughtSignature"))
            for part in content["parts"]
            if field(part, "functionCall")
        ]
        res.append(calls)
    return res


def function_responses(body):
    """(name, response) of the function results in a request."""
    return [
        (field(part, "functionResponse")["name"], field(part, "functionResponse")["response"])
        for content in body["contents"]
        for part in content["parts"]
        if field(part, "functionResponse")
    ]


class TestGeminiAgent(unittest.TestCase):
    def setUp(self):
        self.original_env = os.environ.copy()
        os.environ["GEMINI_API_KEY"] = "fake-key"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)

    def run_task(self, stream):
        check = f"{sys.executable} -c \"from calc import add; assert add(2, 3) == 5; print('OK')\""
        gemini = FakeGemini(
            # Parallel calls: Gemini signs only the first
            [
                function_call("read_file", "sig-A", path="calc.py"),
                function_call("grep", pattern="add"),
            ],
            [
                dict(text="The bug is a - b."),
                function_call(
                    "edit_file", "sig-B", path="calc.py", old_string="a - b", new_string="a + b"
                ),
            ],
            [function_call("bash", "sig-C", command=check)],
            [dict(text="Fixed add, and the check passes.")],
        )
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = Coder.create(
                Model(GEMINI_3),
                "agent",
                io=io,
                map_tokens=0,
                stream=stream,
                permissions=Permissions(io, mode="accept-edits", allow=["bash"]),
            )
            with patch(
                "litellm.llms.custom_httpx.http_handler.HTTPHandler.post", side_effect=gemini
            ):
                coder.run(with_message="fix add in calc.py")

            self.assertEqual(gemini.turns, [])
            self.assertIn("a + b", Path("calc.py").read_text())

        requests = gemini.requests
        self.assertEqual(len(requests), 4)
        # The tools went over as Gemini function declarations
        declared = [
            f["name"]
            for t in requests[0]["tools"]
            for f in t.get("functionDeclarations", t.get("function_declarations", []))
        ]
        self.assertIn("edit_file", declared)

        # Each step's calls went back with their signatures, and with their results
        first_step = [("read_file", "sig-A"), ("grep", UNSIGNED)]
        self.assertEqual(model_turns(requests[1]), [first_step])
        results = function_responses(requests[1])
        self.assertEqual([name for name, _ in results], ["read_file", "grep"])
        self.assertIn("return a - b", json.dumps(results[0][1]))
        self.assertEqual(
            model_turns(requests[3]),
            [first_step, [("edit_file", "sig-B")], [("bash", "sig-C")]],
        )
        bash_result = [response for name, response in function_responses(requests[3])][-1]
        self.assertIn("Exit code: 0", json.dumps(bash_result))
        return coder

    def test_tool_calls_keep_their_thought_signatures(self):
        self.run_task(stream=False)

    def test_streamed_tool_calls_keep_their_thought_signatures(self):
        self.run_task(stream=True)

    def test_gemini_3_is_an_agent_model(self):
        self.assertTrue(Model("gemini-3.1").info.get("supports_function_calling"))


if __name__ == "__main__":
    unittest.main()

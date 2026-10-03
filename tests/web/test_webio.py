import json
import threading
import unittest
from unittest.mock import patch

from loom.io import ConfirmGroup
from loom.reasoning_tags import REASONING_END, REASONING_START
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO, WebMarkdownStream, parse_diff, split_reasoning


def make_io(started=True):
    session = WebSession(interrupt=lambda: None)
    session.started = started
    return WebIO(pretty=False, session=session), session


def answer_asks(session, *values):
    """Answer the next asks from the browser's side, in order, as they come in."""
    answered = []
    earlier = len([msg for msg in session.history if msg["type"] == "ask"])

    def answer():
        while len(answered) < len(values):
            asks = [msg for msg in session.history if msg["type"] == "ask"][earlier:]
            if len(asks) > len(answered):
                ask = asks[len(answered)]
                session.handle(
                    dict(type="answer", ask_id=ask["ask_id"], value=values[len(answered)])
                )
                answered.append(ask)
            threading.Event().wait(0.01)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return answered, thread


class TestQuestions(unittest.TestCase):
    def test_before_the_server_starts_questions_use_the_terminal(self):
        io, session = make_io(started=False)
        with patch("builtins.input", return_value="n") as terminal:
            self.assertFalse(io.confirm_ask("Create a git repo?"))
        terminal.assert_called_once()
        self.assertFalse([msg for msg in session.history if msg["type"] == "ask"])

    def test_confirm(self):
        io, session = make_io()
        asks, thread = answer_asks(session, "n")
        self.assertFalse(io.confirm_ask("Add calc.py to the chat?"))
        thread.join(5)

        ask = asks[0]
        self.assertEqual(ask["kind"], "confirm")
        self.assertEqual(ask["question"], "Add calc.py to the chat?")
        self.assertEqual([c["value"] for c in ask["choices"]], ["y", "n"])
        self.assertEqual(ask["default"], "y")

    def test_confirm_group_and_never(self):
        io, session = make_io()
        group = ConfirmGroup(["a.py", "b.py"])
        asks, thread = answer_asks(session, "a")
        self.assertTrue(io.confirm_ask("Add a.py?", group=group))
        # All answers the rest of the group without asking
        self.assertTrue(io.confirm_ask("Add b.py?", group=group))
        thread.join(5)
        self.assertEqual(len(asks), 1)
        self.assertEqual([c["value"] for c in asks[0]["choices"]], ["y", "n", "a", "s", "d"])

        asks, thread = answer_asks(session, "d")
        self.assertFalse(io.confirm_ask("Open the docs?", allow_never=True))
        thread.join(5)
        # Don't ask again is remembered
        self.assertFalse(io.confirm_ask("Open the docs?", allow_never=True))
        self.assertEqual(len([m for m in session.history if m["type"] == "ask"]), 2)

    def test_permission(self):
        io, session = make_io()
        asks, thread = answer_asks(session, "always")
        answer = io.permission_ask("Run pytest?", always="pytest *", bypass="this session")
        thread.join(5)
        self.assertEqual(answer, "always")
        self.assertEqual(asks[0]["kind"], "permission")
        self.assertEqual(
            [c["value"] for c in asks[0]["choices"]], ["yes", "no", "always", "bypass"]
        )

    def test_choice(self):
        io, session = make_io()
        asks, thread = answer_asks(session, "edit")
        answer = io.choice_ask("Approve the PRD?", ["approve", "edit", "reject"])
        thread.join(5)
        self.assertEqual(answer, "edit")
        self.assertEqual(asks[0]["kind"], "choice")
        self.assertEqual(asks[0]["default"], "approve")
        self.assertEqual(asks[0]["choices"][1], dict(value="edit", label="Edit"))

    def test_prompt(self):
        io, session = make_io()
        asks, thread = answer_asks(session, "make it faster")
        self.assertEqual(io.prompt_ask("What should change?"), "make it faster")
        thread.join(5)
        self.assertEqual(asks[0]["kind"], "prompt")
        self.assertEqual(asks[0]["choices"], [])

    def test_yes_always_still_answers_itself(self):
        session = WebSession()
        session.started = True
        io = WebIO(pretty=False, yes=True, session=session)
        self.assertTrue(io.confirm_ask("Add calc.py?"))
        self.assertFalse([msg for msg in session.history if msg["type"] == "ask"])


class TestOutput(unittest.TestCase):
    def test_tool_messages(self):
        io, session = make_io()
        io.tool_output("Added calc.py", "to the chat")
        io.tool_output()
        io.tool_output("only logged", log_only=True)
        io.tool_warning("careful")
        io.tool_error("broken")
        messages = [(msg["level"], msg["text"]) for msg in session.history]
        self.assertEqual(
            messages,
            [("info", "Added calc.py to the chat"), ("warning", "careful"), ("error", "broken")],
        )

    def test_usage_goes_to_the_status_line(self):
        io, session = make_io()
        io.usage_output("Tokens: 3.6k sent, 129 received.", sent=3600, received=129, cost=0.01)
        io.usage_output("Tokens: 1k sent, 1 received.", sent=1000, received=1, cost=0.02)
        self.assertEqual(session.history, [])
        self.assertEqual(session.snapshot["tokens"], dict(sent=4600, received=130))
        self.assertAlmostEqual(session.snapshot["cost"], 0.03)

    def test_assistant_output(self):
        io, session = make_io()
        io.assistant_output("All done.")
        delta, end = session.history
        self.assertEqual(delta["type"], "assistant_delta")
        self.assertEqual(delta["text"], "All done.")
        self.assertEqual(end, dict(type="assistant_end", id=delta["id"]))

    def test_markdown_stream_sends_what_changed(self):
        session = WebSession()
        stream = WebMarkdownStream(session)
        stream.update("Than")
        stream.update("Thanks")
        stream.update("Thanks")
        stream.update("Hi there")
        stream.update("Hi there!", final=True)
        stream.update("ignored after the end", final=True)

        sent = [(m["type"], m.get("text"), m.get("replace")) for m in session.history]
        self.assertEqual(
            sent,
            [
                ("assistant_delta", "Than", False),
                ("assistant_delta", "ks", False),
                ("assistant_delta", "Hi there", True),
                ("assistant_delta", "!", False),
                ("assistant_end", None, None),
            ],
        )

    def test_thinking_is_sent_apart_from_the_answer(self):
        session = WebSession()
        stream = WebMarkdownStream(session)
        thinking = f"\n{REASONING_START}\n\n"
        stream.update(thinking + "Let me")
        stream.update(thinking + "Let me read it.")
        answered = thinking + f"Let me read it.\n\n{REASONING_END}\n\n"
        stream.update(answered + "It adds")
        stream.update(answered + "It adds two numbers.", final=True)

        sent = [(m.get("reasoning"), m.get("text")) for m in session.history[:-1]]
        self.assertEqual(
            sent,
            [("Let me", ""), (" read it.", ""), ("", "It adds"), ("", " two numbers.")],
        )
        self.assertEqual(split_reasoning("plain answer"), ("", "plain answer"))


DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,5 @@
 def add(a, b):
-    return a - b
+    return a + b
+
+
@@ -9,1 +12,2 @@
 x = 1
+y = 2
... (3 more diff lines)"""


class TestPermissionMode(unittest.TestCase):
    def test_set_mode_switches_the_coders_permissions(self):
        io, session = make_io()

        class Permissions:
            mode = "ask"

        class Coder:
            permissions = Permissions()

        coder = Coder()
        self.assertTrue(io.set_mode(coder, "accept-edits"))
        self.assertEqual(coder.permissions.mode, "accept-edits")
        self.assertEqual(session.snapshot["permission_mode"], "accept-edits")
        self.assertFalse(io.set_mode(coder, "anything"))
        self.assertEqual(coder.permissions.mode, "accept-edits")
        # Coders without permissions, like the ones that edit by diff, have no modes
        self.assertFalse(io.set_mode(object(), "plan"))

    def test_no_branch_outside_a_repo(self):
        io, session = make_io()
        self.assertIsNone(io.current_branch())
        self.assertIsNone(session.snapshot["branch"])


class TestToolCards(unittest.TestCase):
    def test_a_tool_call_is_a_card(self):
        io, session = make_io()
        io.tool_call("Read", "calc.py", args=dict(path="calc.py"))
        io.tool_result("Read 2 lines")
        io.tool_done("1  def add(a, b):\n2      return a + b")

        start, output, end = session.history
        self.assertEqual(start["type"], "tool_start")
        self.assertEqual((start["name"], start["detail"]), ("Read", "calc.py"))
        self.assertEqual(json.loads(start["args"]), dict(path="calc.py"))
        self.assertEqual(output["id"], start["id"])
        self.assertEqual(output["lines"], ["Read 2 lines"])
        self.assertFalse(output["error"])
        self.assertEqual(end["status"], "done")
        self.assertIn("return a + b", end["output"])
        self.assertIsNone(io.tool_id)

    def test_errors_fail_the_card(self):
        io, session = make_io()
        io.tool_call("Bash", "pytest")
        io.tool_result(["1 failed", "Exit code: 1"], styles=[None, "red"])
        io.tool_done("Exit code: 1\n1 failed")
        self.assertEqual(session.history[1]["styles"], [None, "error"])
        self.assertEqual(session.history[-1]["status"], "done")

        io.tool_call("Update", "calc.py")
        io.tool_result("Denied", error=True)
        io.tool_done("The user denied this edit.")
        self.assertEqual(session.history[-1]["status"], "failed")

        io.tool_call("Bash", "sleep 100")
        io.tool_done("Interrupted by the user.", error=True)
        self.assertEqual(session.history[-1]["status"], "failed")

    def test_cards_close_when_something_else_starts(self):
        io, session = make_io()
        io.tool_call("Todos", "1 of 2 done")
        io.tool_call("Compacted the conversation")
        io.get_assistant_mdstream()
        ends = [msg for msg in session.history if msg["type"] == "tool_end"]
        self.assertEqual(len(ends), 2)
        self.assertEqual([end["output"] for end in ends], ["", ""])

    def test_results_without_a_card(self):
        io, session = make_io()
        io.tool_result("Read 2 lines")
        self.assertEqual(session.history[0]["type"], "system")

    def test_parse_diff(self):
        file, lines = parse_diff(DIFF)
        self.assertEqual(file, "calc.py")
        self.assertEqual(
            [(line["kind"], line["old"], line["new"]) for line in lines],
            [
                ("ctx", 1, 1),
                ("del", 2, None),
                ("add", None, 2),
                ("add", None, 3),
                ("add", None, 4),
                ("gap", None, None),
                ("ctx", 9, 12),
                ("add", None, 13),
                ("note", None, None),
            ],
        )
        self.assertEqual(lines[1]["text"], "    return a - b")
        self.assertEqual(lines[-1]["text"], "... (3 more diff lines)")

    def test_an_edit_asks_with_its_diff_on_the_card(self):
        io, session = make_io()
        io.tool_call("Update", "calc.py")
        card = io.tool_id
        asks, thread = answer_asks(session, "no")
        self.assertEqual(io.permission_ask("Edit calc.py?", subject=DIFF, always="edits"), "no")
        thread.join(5)

        diffs = [msg for msg in session.history if msg["type"] == "diff"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["tool_id"], card)
        self.assertEqual(diffs[0]["file"], "calc.py")
        self.assertEqual(asks[0]["tool_id"], card)
        self.assertIsNone(asks[0]["subject"])
        self.assertFalse([msg for msg in session.history if msg["type"] == "system"])

    def test_a_command_asks_with_the_command(self):
        io, session = make_io()
        io.tool_call("Bash", "pytest -q")
        asks, thread = answer_asks(session, "yes")
        self.assertEqual(io.permission_ask("Run this command?", subject="pytest -q"), "yes")
        thread.join(5)
        self.assertEqual(asks[0]["subject"], "pytest -q")
        # Shown on the question, not again as output
        self.assertFalse([msg for msg in session.history if msg["type"] == "system"])

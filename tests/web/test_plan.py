import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import git

from loom import plans
from loom.coders import Coder
from loom.llm import litellm
from loom.models import Model
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import FakeLLM, call, reply, tool_results
from tests.web.test_webio import answer_asks, make_io

PLAN = "# Fix add\n\n1. Change `a - b` to `a + b` in calc.py.\n2. Run the tests.\n"


class TestPlanAsk(unittest.TestCase):
    def test_a_plan_ask_carries_the_plan_and_the_feedback(self):
        io, session = make_io()
        io.tool_call("Plan", "Fix add")
        card = io.tool_id
        asks, thread = answer_asks(session, "keep planning\nAlso add a test\nfor negatives")
        answer = io.choice_ask(
            "Approve this plan?",
            plans.CHOICES,
            default=plans.APPROVE_ASK,
            plan=dict(text=PLAN, path=".loom/plans/p.md"),
        )
        thread.join(5)

        self.assertEqual(answer, plans.KEEP_PLANNING)
        self.assertEqual(io.plan_feedback_ask(), "Also add a test\nfor negatives")
        # Taken once
        self.assertEqual(io.plan_feedback_ask(), "")

        ask = asks[0]
        self.assertEqual(ask["kind"], "plan")
        self.assertEqual(ask["plan"], dict(text=PLAN, path=".loom/plans/p.md"))
        self.assertEqual([c["value"] for c in ask["choices"]], plans.CHOICES)
        self.assertEqual(ask["default"], plans.APPROVE_ASK)
        self.assertEqual(ask["tool_id"], card)
        resolved = [msg for msg in session.history if msg["type"] == "ask_resolved"]
        self.assertTrue(resolved[0]["value"].startswith("keep planning"))

    def test_other_choices_have_no_plan(self):
        io, session = make_io()
        asks, thread = answer_asks(session, "edit")
        io.choice_ask("Approve the PRD?", ["approve", "edit", "reject"])
        thread.join(5)
        self.assertEqual(asks[0]["kind"], "choice")
        self.assertIsNone(asks[0]["plan"])

    def test_a_plan_shown_without_asking_is_a_message(self):
        io, session = make_io()
        io.plan_output(PLAN, ".loom/plans/p.md")
        text = "".join(m["text"] for m in session.history if m["type"] == "assistant_delta")
        self.assertIn("`.loom/plans/p.md`", text)
        self.assertIn("Change `a - b`", text)

    def test_mode_changes_reach_the_mode_pill(self):
        io, session = make_io()
        permissions = Permissions(io, mode="plan")
        permissions.mode = "accept-edits"
        self.assertEqual(session.snapshot["permission_mode"], "accept-edits")


class TestPlanRoundTrip(unittest.TestCase):
    def test_approving_on_the_plan_card_carries_out_the_plan(self):
        with GitTemporaryDirectory():
            repo = git.Repo.init()
            Path("calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
            repo.git.add(".")
            repo.git.commit("-m", "initial")

            session = WebSession(interrupt=lambda: None)
            io = WebIO(pretty=False, session=session)
            coder = Coder.create(
                Model("gpt-4o-mini"),
                "agent",
                io=io,
                map_tokens=0,
                stream=False,
                permissions=Permissions(io, mode="plan"),
            )
            session.started = True
            # As get_input sets it before the request
            session.update(permission_mode="plan")
            modes = []

            def browser():
                while True:
                    asks = [msg for msg in session.history if msg["type"] == "ask"]
                    if asks:
                        modes.append(session.snapshot["permission_mode"])
                        session.handle(
                            dict(type="answer", ask_id=asks[0]["ask_id"], value=plans.APPROVE_AUTO)
                        )
                        return
                    threading.Event().wait(0.01)

            thread = threading.Thread(target=browser, daemon=True)
            thread.start()
            llm = FakeLLM(
                reply(None, call("exit_plan_mode", plan=PLAN)),
                reply(
                    None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")
                ),
                reply("Done."),
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add")
            thread.join(5)

            asks = [msg for msg in session.history if msg["type"] == "ask"]
            self.assertEqual(len(asks), 1)
            self.assertEqual(asks[0]["kind"], "plan")
            self.assertEqual(asks[0]["plan"]["text"], PLAN)
            self.assertTrue(asks[0]["plan"]["path"].startswith(".loom/plans/"))
            self.assertEqual(modes, ["plan"])
            # The pill switched when the plan was approved, and the edit wasn't asked about
            self.assertEqual(session.snapshot["permission_mode"], "accept-edits")
            self.assertIn("a + b", Path("calc.py").read_text())
            self.assertIn("approved", tool_results(coder.done_messages)["call_1_0"])
            cards = [m for m in session.history if m["type"] == "tool_start"]
            self.assertEqual([c["name"] for c in cards], ["Plan", "Update"])

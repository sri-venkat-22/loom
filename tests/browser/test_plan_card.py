"""The plan card of loom --web, in a real (headless) Chromium. Skipped without Playwright's
Chromium, like the dashboard's tests."""

import threading
import unittest

from loom import plans
from loom.utils import GitTemporaryDirectory
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import make_repo
from tests.browser.test_dashboard import BrowserTest

PLAN = "# Add a --verbose flag\n\n1. Add the argument.\n2. Run `pytest -q`.\n"


class TestPlanCard(BrowserTest):
    def ask_plan(self, session):
        """Ask about PLAN on a thread, as the agent would. Returns (thread, answers): the
        choice and the feedback, once answered."""
        io = WebIO(pretty=False, session=session)
        answers = []

        def ask():
            io.tool_call("Plan", "Add a --verbose flag")
            choice = io.choice_ask(
                "Approve this plan?",
                plans.CHOICES,
                default=plans.APPROVE_ASK,
                plan=dict(text=PLAN, path=".loom/plans/20261004-add-a-verbose-flag.md"),
            )
            answers.append(choice)
            if choice == plans.KEEP_PLANNING:
                answers.append(io.plan_feedback_ask())

        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        return thread, answers

    def test_approve(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            session, url = self.serve(root)
            try:
                thread, answers = self.ask_plan(session)
                page = self.browser.new_page(viewport=dict(width=1280, height=900))
                page.goto(url)
                card = page.get_by_label("Plan", exact=True)
                card.get_by_role("heading", name="Add a --verbose flag").wait_for()
                self.assertIn("20261004-add-a-verbose-flag.md", card.inner_text())
                card.get_by_role("button", name="Approve and auto-accept edits").click()
                thread.join(5)
                self.assertEqual(answers, [plans.APPROVE_AUTO])
                card.get_by_text("Approved · edits are accepted automatically").wait_for()
                page.close()
            finally:
                self.watcher.stop()

    def test_keep_planning_with_feedback(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            session, url = self.serve(root)
            try:
                thread, answers = self.ask_plan(session)
                page = self.browser.new_page(viewport=dict(width=1280, height=900))
                page.goto(url)
                card = page.get_by_label("Plan", exact=True)
                card.get_by_label("Plan feedback").fill("Also add a test")
                card.get_by_role("button", name="Keep planning").click()
                thread.join(5)
                self.assertEqual(answers, [plans.KEEP_PLANNING, "Also add a test"])
                card.get_by_text("Kept planning, with feedback").wait_for()
                page.close()
            finally:
                self.watcher.stop()


if __name__ == "__main__":
    unittest.main()

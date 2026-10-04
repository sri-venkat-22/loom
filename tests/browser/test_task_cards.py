"""Sub-agents' Task cards in loom --web, in a real (headless) Chromium: tasks running at
once side by side, a question on a sub-agent's own card, and a task's stop button. Skipped
without Playwright's Chromium, like the dashboard's tests."""

import re
import sys
import threading
import unittest
from unittest.mock import patch

from loom.llm import litellm
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory
from loom.web.backend.server import start_server
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import call, make_coder, make_repo, reply
from tests.basic.test_subagents import PromptsLLM, task_call
from tests.browser.test_dashboard import BrowserTest, free_port

SLEEP = f'"{sys.executable}" -c "import time; time.sleep(30)"'


class TestTaskCards(BrowserTest):
    def run_agent(self, root, llm):
        """Serve root, and run the agent on a thread with llm. Returns (url, coder, thread)."""
        session = WebSession(interrupt=lambda: None)
        io = WebIO(pretty=False, session=session)
        io.root = root
        session.update(cwd=root, model="gpt-4o-mini")
        url = start_server(session, port=free_port(), open_browser=False, io=io)
        session.started = True
        coder = make_coder(io, Permissions(io))

        def work():
            with patch.object(litellm, "completion", llm):
                session.start_turn("go")
                coder.run(with_message="go", preproc=False)
                session.end_turn()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.session = session
        return url, coder, thread

    def test_parallel_tasks_a_question_and_a_stop(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            proceed = threading.Event()

            def gated(then):
                def scripted(kwargs):
                    proceed.wait(20)
                    return then

                return scripted

            llm = PromptsLLM(
                parent=[
                    reply(
                        "Mapping it in parallel.",
                        task_call("Read calc.", "Read the adder"),
                        task_call("Run echo.", "Run a check", agent="general"),
                        task_call("Sleep.", "Wait a while", agent="general"),
                    ),
                    reply("All three are back."),
                ],
                **{
                    "Read calc.": [
                        reply(None, call("read_file", path="calc.py")),
                        gated(reply("calc.py:2 subtracts.")),
                    ],
                    "Run echo.": [reply(None, call("bash", command="echo checked")), reply("Ok.")],
                    "Sleep.": [reply(None, call("bash", command=SLEEP))],
                },
            )
            url, coder, thread = self.run_agent(root, llm)
            try:
                self.check_cards(url, coder, thread, proceed)
            finally:
                # Whatever happened, nothing is left waiting for the browser
                proceed.set()
                self.session.close()

    def check_cards(self, url, coder, thread, proceed):
        from playwright.sync_api import expect

        page = self.browser.new_page(viewport=dict(width=1280, height=900))
        page.goto(url)

        batch = page.get_by_label("Parallel tasks")
        batch.wait_for()
        cards = batch.locator("section")
        expect(cards).to_have_count(3)
        read = page.get_by_label("Task 1: Read the adder")
        check = page.get_by_label("Task 2: Run a check")
        wait = page.get_by_label("Task 3: Wait a while")
        # Side by side
        first, second = read.bounding_box(), check.bounding_box()
        self.assertAlmostEqual(first["y"], second["y"], delta=2)
        self.assertLess(first["x"] + first["width"], second["x"] + 1)
        self.assertIn("explore", read.inner_text())

        # Each general task asks on its own Bash card, inside its Task card, one
        # question at a time
        allow = page.get_by_role("button", name=re.compile(r"^Allow\b"))
        asked = []
        for _ in range(2):
            asking = cards.filter(has=allow)
            expect(asking).to_have_count(1)
            label = asking.get_attribute("aria-label")
            description = label.split(": ", 1)[1]
            self.assertIn(f"[general: {description}] Run this command?", asking.inner_text())
            asking.get_by_role("button", name=re.compile(r"^Allow\b")).click()
            expect(page.get_by_label(label).filter(has=allow)).to_have_count(0)
            asked.append(label)
        self.assertEqual(sorted(asked), ["Task 2: Run a check", "Task 3: Wait a while"])
        check.get_by_text("Done (1 tool use").wait_for()

        # The sleeping task's stop button stops just it
        wait.get_by_role("button", name="Stop").click()
        wait.get_by_text("Stopped by you").wait_for()

        proceed.set()
        read.get_by_text("Done (1 tool use").wait_for()
        page.get_by_text("All three are back.").wait_for()
        thread.join(10)
        self.assertEqual([t.status for t in coder.session.tasks], ["done", "done", "stopped"])

        # A finished task folds to its outcome, and opens to its whole transcript
        self.assertNotIn("Read 2 lines", read.inner_text())
        read.get_by_role("button", name="Read the adder").click()
        read.get_by_text("Read 2 lines").wait_for()
        self.assertIn("Read calc.", read.inner_text())
        self.assertIn("calc.py:2 subtracts.", read.inner_text())
        page.close()


if __name__ == "__main__":
    unittest.main()

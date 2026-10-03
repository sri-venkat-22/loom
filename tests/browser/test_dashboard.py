"""Click through the /project dashboard of loom --web in a real (headless) Chromium.

Needs Playwright and its Chromium (pip install playwright; playwright install chromium);
skipped without them.
"""

import contextlib
import io
import os
import socket
import unittest
from unittest.mock import patch

import git

from loom.utils import GitTemporaryDirectory
from loom.web.backend.project import ProjectWatcher, project_state
from loom.web.backend.server import start_server
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import make_repo
from tests.basic.test_project_report import run_fixture_project


def chromium():
    """A headless Chromium from Playwright, or None if there's none to launch."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    playwright = sync_playwright().start()
    try:
        return playwright, playwright.chromium.launch()
    except Exception:
        playwright.stop()
        return None, None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.playwright, self.browser = chromium()
        if not self.browser:
            self.skipTest("needs Playwright's Chromium")
        self.memory_store = patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"})
        self.memory_store.start()

    def tearDown(self):
        self.memory_store.stop()
        self.browser.close()
        self.playwright.stop()

    def serve(self, root):
        """loom --web's server for the project in root, without a coder: the test reads
        what the browser sends from the session's inputs."""
        session = WebSession(interrupt=lambda: None)
        web_io = WebIO(pretty=False, session=session)
        web_io.root = root
        web_io.git = git.Repo(root)
        session.update(cwd=root, model="gpt-4o-mini", **project_state(root))
        self.watcher = ProjectWatcher(session, root).start()
        url = start_server(session, port=free_port(), open_browser=False, io=web_io)
        return session, url

    def test_click_through_the_timeline(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            with contextlib.redirect_stdout(io.StringIO()):
                run_fixture_project()
            session, url = self.serve(root)
            try:
                self.click_through(session, url)
            finally:
                self.watcher.stop()

    def click_through(self, session, url):
        from playwright.sync_api import expect

        page = self.browser.new_page(viewport=dict(width=1280, height=900))
        page.goto(url)

        # Clicking a phase in the breadcrumb opens the dashboard at its card
        page.locator("nav").get_by_role("button", name="building").click()
        building = page.locator("#phase-building")
        building.wait_for()
        self.assertIn("2 runs", building.inner_text())
        self.assertIn("1 fix round", building.inner_text())
        pane = page.locator('aside[aria-label="Side pane"]')
        summary = pane.inner_text()
        self.assertIn("PROJECT COMPLETE", summary.upper())
        self.assertIn("A command-line tool that adds two numbers", summary)

        # Its decisions unfold
        building.get_by_role("button", name="2 decisions").click()
        self.assertIn("Approved the build summary", building.inner_text())

        # The diff of its latest run, then of its first
        building.get_by_role("button", name="Diff", exact=True).click()
        aside = pane
        aside.get_by_text("return a + b").wait_for()
        self.assertIn("adder.py", aside.inner_text())
        page.get_by_label("Run").select_option("1")
        # The first run wrote adder.py, with the bug
        expect(aside.get_by_text("return a + b")).to_have_count(0)
        self.assertIn("return a - b", aside.inner_text())
        aside.get_by_role("button", name="← Timeline").click()

        # A document, in the editor
        page.locator("#phase-design").get_by_role("button", name="Document").click()
        page.locator(".monaco-editor").wait_for()
        aside.get_by_text("loom-project/3-architecture.md").wait_for()
        aside.get_by_role("button", name="← Timeline").click()

        # The report menu
        page.get_by_role("button", name="Download report").click()
        items = page.get_by_role("menuitem").all_inner_texts()
        self.assertEqual(items, ["Markdown", "Word (.docx)", "PDF"])
        page.get_by_role("button", name="Close the menu").click()

        # Going back asks first, then sends the command
        testing = page.locator("#phase-testing")
        testing.get_by_role("button", name="Go back here").click()
        self.assertTrue(session.inputs.empty())
        testing.get_by_role("button", name="Go back", exact=True).click()
        self.assertEqual(session.inputs.get(timeout=5), "/project back testing")

        page.close()


if __name__ == "__main__":
    unittest.main()

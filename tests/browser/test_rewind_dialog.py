"""The rewind button and dialog of loom --web, in a real (headless) Chromium. Skipped
without Playwright's Chromium, like the dashboard's tests."""

import unittest

import git

from loom.utils import GitTemporaryDirectory
from loom.web.backend.project import ProjectWatcher, project_state
from loom.web.backend.server import start_server
from loom.web.backend.session import WebSession
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import make_repo
from tests.browser.test_dashboard import BrowserTest, free_port

DETAIL = dict(
    id="20261004-101500-ab12",
    time="2026-10-04T10:15:00",
    prompt="add subtract\nand a test for it",
    kind="request",
    conversation=True,
    git=True,
    busy=None,
    error=None,
    changes=dict(changed=["calc.py"], created=["sub.py"], deleted=["notes.txt"]),
)


class TestRewindDialog(BrowserTest):
    def serve_with_checkpoint(self, root):
        session = WebSession(interrupt=lambda: None)
        web_io = WebIO(pretty=False, session=session)
        web_io.root = root
        web_io.git = git.Repo(root)
        # The sidebar lists the checkpoints under the current conversation
        web_io.conversation_id = "c1"
        web_io.checkpoint_changes = lambda checkpoint_id: (
            DETAIL if checkpoint_id == DETAIL["id"] else None
        )
        session.update(cwd=root, model="gpt-4o-mini", **project_state(root))
        self.watcher = ProjectWatcher(session, root).start()
        session.start_turn("add subtract\nand a test for it")
        turn = session.turn_id
        session.end_turn()
        item = dict(
            id=DETAIL["id"],
            number=1,
            time=DETAIL["time"],
            prompt=DETAIL["prompt"],
            kind="request",
            conversation=True,
            turn_id=turn,
        )
        session.emit("checkpoints", conversation="c1", git=True, items=[item])
        url = start_server(session, port=free_port(), open_browser=False, io=web_io)
        return session, url

    def test_rewind_from_a_message(self):
        from playwright.sync_api import expect

        with GitTemporaryDirectory() as root:
            make_repo()
            session, url = self.serve_with_checkpoint(root)
            try:
                page = self.browser.new_page(viewport=dict(width=1280, height=900))
                page.goto(url)
                page.get_by_text("and a test for it").hover()
                page.get_by_role("button", name="Rewind to before this message").click()
                dialog = page.get_by_role("dialog", name="Rewind")
                dialog.get_by_text("1 file changed, 1 created since, 1 deleted since").wait_for()
                text = dialog.inner_text()
                self.assertIn("add subtract", text)
                self.assertIn("created since: deleted", text)
                self.assertIn("deleted since: restored", text)

                dialog.get_by_role("button", name="Code and conversation").click()
                self.assertEqual(
                    session.inputs.get(timeout=5), f"/rewind {DETAIL['id']} both --yes"
                )
                # The request is back in the input, to change and send again
                composer = page.get_by_placeholder("Reply to loom, or type / for commands")
                expect(composer).to_have_value(DETAIL["prompt"])
                expect(page.get_by_role("dialog")).to_have_count(0)
                page.close()
            finally:
                self.watcher.stop()

    def test_the_sidebar_lists_the_checkpoints(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            session, url = self.serve_with_checkpoint(root)
            try:
                page = self.browser.new_page(viewport=dict(width=1280, height=900))
                page.goto(url)
                checkpoints = page.get_by_label("Checkpoints")
                checkpoints.get_by_text("add subtract").click()
                dialog = page.get_by_role("dialog", name="Rewind")
                dialog.get_by_role("button", name="Code only").click()
                self.assertEqual(
                    session.inputs.get(timeout=5), f"/rewind {DETAIL['id']} code --yes"
                )
                page.close()
            finally:
                self.watcher.stop()


if __name__ == "__main__":
    unittest.main()

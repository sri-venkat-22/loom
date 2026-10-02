import re
import unittest
from pathlib import Path

from loom.commands import Commands
from loom.io import InputOutput
from loom.web.backend.webio import WEB_COMMANDS

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "loom" / "web" / "frontend" / "src"


def browser_commands():
    """The commands the frontend handles itself, from LOCAL_COMMANDS in localCommands.ts."""
    source = (FRONTEND / "lib" / "localCommands.ts").read_text()
    return re.findall(r'cmd: "(/[a-z-]+)"', source)


class TestWebCommands(unittest.TestCase):
    def test_the_web_uis_commands_dont_hide_loom_s(self):
        loom = set(Commands(InputOutput(pretty=False, fancy_input=False), None).get_commands())
        browser = browser_commands()
        self.assertEqual(set(browser), {"/files", "/memory", "/terminal", "/theme"})
        for cmd in browser + [command["cmd"] for command in WEB_COMMANDS]:
            self.assertNotIn(cmd, loom, f"{cmd} would hide loom's own {cmd}")


class TestWebDocs(unittest.TestCase):
    def test_the_docs_cover_the_options(self):
        docs = (ROOT / "loom" / "docs" / "web.md").read_text()
        for option in ("--web", "--port", "--no-browser"):
            self.assertIn(option, docs)
        for cmd in browser_commands() + [command["cmd"] for command in WEB_COMMANDS]:
            self.assertIn(f"`{cmd}", docs, f"web.md doesn't mention {cmd}")

    def test_the_screenshot_exists(self):
        readme = (ROOT / "README.md").read_text()
        docs = (ROOT / "loom" / "docs" / "web.md").read_text()
        for base, text in ((ROOT, readme), (ROOT / "loom" / "docs", docs)):
            images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
            self.assertTrue(images)
            for image in images:
                self.assertTrue((base / image).is_file(), f"no image {image}")
        self.assertIn("loom/docs/web.md", readme)

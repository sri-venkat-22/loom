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
    source = (FRONTEND / "lib" / "localCommands.ts").read_text(encoding="utf-8")
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
        docs = (ROOT / "loom" / "docs" / "web.md").read_text(encoding="utf-8")
        for option in ("--web", "--port", "--no-browser"):
            self.assertIn(option, docs)
        for cmd in browser_commands() + [command["cmd"] for command in WEB_COMMANDS]:
            self.assertIn(f"`{cmd}", docs, f"web.md doesn't mention {cmd}")

    def test_the_screenshot_exists(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "loom" / "docs" / "web.md").read_text(encoding="utf-8")
        for base, text in ((ROOT, readme), (ROOT / "loom" / "docs", docs)):
            images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
            self.assertTrue(images)
            for image in images:
                self.assertTrue((base / image).is_file(), f"no image {image}")
        self.assertIn("loom/docs/web.md", readme)


class TestBuiltFrontend(unittest.TestCase):
    """loom/web/static is committed, so `pip install git+...` ships the web UI."""

    def test_the_build_is_complete(self):
        static = ROOT / "loom" / "web" / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        assets = re.findall(r'(?:src|href)="/(assets/[^"]+)"', index)
        self.assertTrue(any(asset.endswith(".js") for asset in assets), index)
        self.assertTrue(any(asset.endswith(".css") for asset in assets), index)
        for asset in assets:
            self.assertTrue((static / asset).is_file(), f"index.html needs {asset}")

    def test_the_build_is_in_git(self):
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files", "loom/web/static/index.html"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if tracked.returncode:
            self.skipTest("not a git checkout")
        self.assertEqual(tracked.stdout.strip(), "loom/web/static/index.html")

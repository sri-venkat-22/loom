"""The web tools' cards in loom --web, in a real (headless) Chromium. Skipped without
Playwright's Chromium, like the dashboard's tests."""

import unittest

from loom import webfetch, websearch
from loom.utils import GitTemporaryDirectory
from loom.web.backend.webio import WebIO
from tests.basic.test_agent import make_repo
from tests.browser.test_dashboard import BrowserTest


class TestWebCards(BrowserTest):
    def test_search_and_fetch_cards_show_their_links(self):
        with GitTemporaryDirectory() as root:
            make_repo()
            session, url = self.serve(root)
            try:
                io = WebIO(pretty=False, session=session)
                results = [
                    websearch.Result(
                        "FastAPI release notes", "https://fastapi.example/release", "0.200 is out"
                    ),
                    websearch.Result("PyPI: fastapi", "https://pypi.example/project/fastapi/"),
                ]
                io.tool_call("WebSearch", '"fastapi latest"', args=dict(query="fastapi latest"))
                io.tool_result("2 results")
                io.tool_done(websearch.format_results("fastapi latest", "brave", results))
                io.tool_call(
                    "WebFetch", "fastapi.example", args=dict(url="http://fastapi.example/r")
                )
                io.tool_result("Fetched 12 KB")
                body = "(Redirected to https://fastapi.example/release.)\n# Release notes"
                io.tool_done(webfetch.wrap("https://fastapi.example/release", body))

                page = self.browser.new_page(viewport=dict(width=1280, height=900))
                page.goto(url)
                links = page.get_by_label("Links")
                links.first.wait_for()
                search, fetch = links.all()
                found = search.get_by_role("link").evaluate_all("els => els.map(e => e.href)")
                self.assertEqual(
                    found,
                    ["https://fastapi.example/release", "https://pypi.example/project/fastapi/"],
                )
                self.assertIn("0.200 is out", search.inner_text())
                self.assertIn("pypi.example", search.inner_text())
                self.assertEqual(
                    fetch.get_by_role("link").get_attribute("href"),
                    "https://fastapi.example/release",
                )
                self.assertEqual(search.get_by_role("link").first.get_attribute("target"), "_blank")
                page.close()
            finally:
                self.watcher.stop()


if __name__ == "__main__":
    unittest.main()

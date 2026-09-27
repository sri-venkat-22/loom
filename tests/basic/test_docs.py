import re
import unittest
from pathlib import Path

from loom import urls

DOCS = Path(__file__).resolve().parents[2] / "loom" / "docs"


def github_anchor(heading):
    anchor = heading.strip().lower()
    anchor = re.sub(r"[^\w\- ]", "", anchor)
    return anchor.replace(" ", "-")


class TestDocs(unittest.TestCase):
    def test_doc_urls_point_at_real_pages(self):
        prefix = urls.docs + "/"
        doc_urls = [
            value
            for name, value in vars(urls).items()
            if isinstance(value, str) and value.startswith(prefix)
        ]
        self.assertTrue(doc_urls)

        for url in doc_urls:
            page, _, anchor = url[len(prefix) :].partition("#")
            path = DOCS / page
            self.assertTrue(path.is_file(), f"{url}: no such doc {path}")

            if anchor:
                headings = re.findall(r"^#+ (.+)$", path.read_text(encoding="utf-8"), re.M)
                anchors = {github_anchor(h) for h in headings}
                self.assertIn(anchor, anchors, f"{url}: no such heading in {page}")

    def test_relative_links_between_docs(self):
        for path in DOCS.glob("*.md"):
            for target in re.findall(r"\]\(([\w\-]+\.md)\)", path.read_text(encoding="utf-8")):
                self.assertTrue((DOCS / target).is_file(), f"{path.name} links to {target}")

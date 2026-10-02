import hashlib
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom.memory import (
    CHROMA_DIR,
    DB_FILE,
    MEMORY_DIR,
    ProjectDB,
    ProjectMemory,
    ProjectMemoryError,
    chroma_installed,
    chunk_markdown,
    terms,
)
from loom.phases import PHASES, PHASES_BY_KEY
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory

PRD = """# PRD: swap

## Overview
Students swap used textbooks on campus.

## Functional requirements
FR-1: list a textbook with its ISBN and condition.
FR-2: search the listed textbooks by title.

## Non-functional requirements
NFR-1: pages load in under a second for 1000 concurrent students.
"""

ARCHITECTURE = """# Architecture: swap

## Technology stack
| Layer | Choice | Why |
|-------|--------|-----|
| Database | PostgreSQL | Relational data and full-text search |

## Deployment
A container on Fly.io.

```python
# Not a heading: inside a code block
def main(): ...
```
"""


def make_state(root):
    from loom.orchestrator import ProjectState

    return ProjectState.new(root, "A textbook swap for students")


class TestChunks(unittest.TestCase):
    def test_chunks_follow_the_headings(self):
        chunks = chunk_markdown(PRD)
        titles = [title for title, _ in chunks]
        self.assertEqual(
            titles,
            [
                "PRD: swap",
                "PRD: swap > Overview",
                "PRD: swap > Functional requirements",
                "PRD: swap > Non-functional requirements",
            ],
        )
        self.assertIn("FR-2: search the listed textbooks", chunks[2][1])

    def test_code_blocks_are_not_headings(self):
        chunks = chunk_markdown(ARCHITECTURE)
        titles = [title for title, _ in chunks]
        self.assertNotIn("Not a heading: inside a code block", " ".join(titles))
        self.assertIn("def main()", chunks[-1][1])
        self.assertEqual(titles[-1], "Architecture: swap > Deployment")

    def test_long_sections_are_split(self):
        text = "# Notes\n\n" + "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
        text += "\n\n" + "x" * 3000
        chunks = chunk_markdown(text, limit=500)
        self.assertGreater(len(chunks), 5)
        for title, body in chunks:
            self.assertEqual(title, "Notes")
            self.assertLessEqual(len(body), 500)
        joined = "".join(body for _, body in chunks)
        self.assertEqual(joined.count("x"), 3000)

    def test_terms(self):
        self.assertEqual(terms("The databases and the Database"), ["database", "database"])
        # Short words go, numbers stay
        self.assertEqual(terms("FR-1 is a hard limit"), ["fr", "1", "hard", "limit"])


class TestProjectDB(unittest.TestCase):
    def test_state_round_trip(self):
        with GitTemporaryDirectory() as root:
            db = ProjectDB(root)
            self.assertIsNone(db.load_state())
            state = make_state(root)
            state.start("idea")
            state.finish("idea", "GO")
            state.save()
            self.assertTrue((Path(root) / DB_FILE).is_file())
            # The memory is local, so git ignores it
            self.assertEqual((Path(root) / MEMORY_DIR / ".gitignore").read_text(), "*\n")

            data = db.load_state()
            self.assertEqual(data["idea"], "A textbook swap for students")
            self.assertEqual(data["phases"]["idea"]["verdict"], "GO")
            self.assertEqual(list(data["phases"]), [phase.key for phase in PHASES])
            self.assertEqual([e["event"] for e in data["history"]], ["new", "start", "finish"])

            # Only new history entries are added
            state.approve("idea")
            state.save()
            state.save()
            self.assertEqual(db.count("history"), 4)

            # The phases table can be queried directly
            with db.connect() as conn:
                row = conn.execute("SELECT status, verdict FROM phases WHERE key='idea'").fetchone()
            self.assertEqual(tuple(row), ("approved", "GO"))

            db.clear()
            self.assertIsNone(db.load_state())
            self.assertEqual(db.count("history"), 0)

    def test_decisions(self):
        with GitTemporaryDirectory() as root:
            db = ProjectDB(root)
            self.assertEqual(db.decisions(), [])
            first = db.add_decision("design", "Design agent", "decision", "Use PostgreSQL", "FTS")
            db.add_decision("design", "founder", "approved", "Approved the architecture")
            db.add_decision("planning", "founder", "rejected", "Asked for changes: add ISBN")
            self.assertEqual(first["id"], 1)
            self.assertEqual(len(db.decisions()), 3)
            self.assertEqual(
                [d["text"] for d in db.decisions(kinds=["decision", "rejected"])],
                ["Use PostgreSQL", "Asked for changes: add ISBN"],
            )
            self.assertEqual(db.decisions(phase="design")[0]["reason"], "FTS")
            with self.assertRaises(ValueError):
                db.add_decision("design", "founder", "maybe", "Unknown kind")

    def test_not_a_database(self):
        with GitTemporaryDirectory() as root:
            path = Path(root) / DB_FILE
            path.parent.mkdir(parents=True)
            path.write_text("not a database")
            with self.assertRaises(ProjectMemoryError):
                ProjectDB(root).load_state()

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_memory_linked_outside_the_project(self):
        with GitTemporaryDirectory() as root, IgnorantTemporaryDirectory() as outside:
            Path(root, ".loom").mkdir()
            os.symlink(outside, Path(root) / MEMORY_DIR)
            with self.assertRaises(ProjectMemoryError):
                ProjectDB(root).add_decision(None, "founder", "decision", "x")
            self.assertEqual(os.listdir(outside), [])


class TestKeywordSearch(unittest.TestCase):
    def setUp(self):
        self.dir = GitTemporaryDirectory()
        self.root = self.dir.__enter__()
        self.memory = ProjectMemory(self.root, store="keyword")
        self.memory.index_document(PHASES_BY_KEY["planning"], PRD)
        self.memory.index_document(PHASES_BY_KEY["design"], ARCHITECTURE)

    def tearDown(self):
        self.dir.__exit__(None, None, None)

    def test_finds_the_relevant_passage(self):
        hits = self.memory.search("which database do we use?")
        self.assertEqual(hits[0].phase, "design")
        self.assertEqual(hits[0].title, "Architecture: swap > Technology stack")
        self.assertIn("PostgreSQL", hits[0].text)
        self.assertEqual(hits[0].source, PHASES_BY_KEY["design"].document)

        hits = self.memory.search("how many concurrent students")
        self.assertIn("NFR-1", hits[0].text)
        self.assertEqual(self.memory.search("kubernetes helm"), [])
        self.assertEqual(self.memory.search("  "), [])

    def test_filters(self):
        hits = self.memory.search("textbooks database", phases=["planning"])
        self.assertTrue(hits)
        self.assertTrue(all(hit.phase == "planning" for hit in hits))
        hits = self.memory.search(
            "textbooks database", exclude_sources=[PHASES_BY_KEY["planning"].document]
        )
        self.assertTrue(all(hit.phase == "design" for hit in hits))
        self.assertEqual(self.memory.search("database", kinds=["decision"]), [])

    def test_decisions_are_searchable(self):
        decision = self.memory.record_decision(
            "design", "Use Redis for sessions", "fast  and\nsimple", source="Design agent"
        )
        self.assertEqual(decision["reason"], "fast and simple")
        hits = self.memory.search("sessions redis")
        self.assertEqual(hits[0].kind, "decision")
        self.assertEqual(hits[0].text, "Use Redis for sessions\nWhy: fast and simple")
        self.assertEqual(hits[0].source, "Design agent")

    def test_documents_are_replaced(self):
        planning = PHASES_BY_KEY["planning"]
        chunks = self.memory.stats()["chunks"]
        # Unchanged: nothing to do
        self.assertFalse(self.memory.index_document(planning, PRD))
        self.assertTrue(self.memory.index_document(planning, "# PRD: swap\n\nOnly ebooks now."))
        self.assertEqual(self.memory.search("ISBN condition"), [])
        self.assertEqual(self.memory.search("ebooks")[0].phase, "planning")
        self.assertLess(self.memory.stats()["chunks"], chunks)

    def test_clear(self):
        self.memory.record_decision(None, "Target students")
        self.memory.clear()
        self.assertEqual(self.memory.stats(), dict(decisions=0, chunks=0))
        self.assertEqual(self.memory.search("textbooks"), [])
        # Indexing the same text again works after a clear
        self.assertTrue(self.memory.index_document(PHASES_BY_KEY["planning"], PRD))

    def test_backend(self):
        self.assertEqual(self.memory.backend, "keyword index (BM25)")
        self.assertIsNone(self.memory.vector_index())


def fake_embedding_function():
    """A bag-of-words embedding, so the ChromaDB tests don't download a model."""
    from chromadb.api.types import Documents, EmbeddingFunction

    class FakeEmbedding(EmbeddingFunction[Documents]):
        def __init__(self):
            pass

        @staticmethod
        def name():
            return "loom-test-fake"

        def get_config(self):
            return {}

        @staticmethod
        def build_from_config(config):
            return FakeEmbedding()

        def __call__(self, input):
            vectors = []
            for text in input:
                vector = [0.0] * 128
                for word in terms(text):
                    vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 128] += 1
                vectors.append(vector)
            return vectors

    return FakeEmbedding()


@unittest.skipUnless(chroma_installed(), "chromadb isn't installed")
class TestChromaSearch(unittest.TestCase):
    def test_searches_with_chroma(self):
        with GitTemporaryDirectory() as root:
            embed = fake_embedding_function()
            memory = ProjectMemory(root, store="chroma", embedding_function=embed)
            memory.index_document(PHASES_BY_KEY["planning"], PRD)
            memory.index_document(PHASES_BY_KEY["design"], ARCHITECTURE)
            memory.record_decision("design", "Use PostgreSQL full-text search", source="Design")

            self.assertTrue(memory.backend.startswith("ChromaDB"))
            hits = memory.search("postgresql database", 3)
            self.assertIsNotNone(memory.chroma)
            self.assertIn("PostgreSQL", hits[0].text)
            self.assertTrue((Path(root) / CHROMA_DIR).is_dir())

            hits = memory.search("postgresql", 5, kinds=["decision"])
            self.assertEqual([hit.kind for hit in hits], ["decision"])
            hits = memory.search(
                "students", 5, phases=["planning", "design"], exclude_sources=["nothing"]
            )
            self.assertTrue(hits)

            # A changed document replaces its vectors at the next search
            memory.index_document(PHASES_BY_KEY["design"], "# Architecture\n\nSQLite only.")
            hits = memory.search("database", 1, phases=["design"], kinds=["document"])
            self.assertEqual(hits[0].text, "# Architecture\n\nSQLite only.")
            ids = memory.chroma.collection.get()["ids"]
            self.assertEqual(sorted(ids), sorted(c["id"] for c in memory.db.chunks()))

            # A new memory object (a new loom session) finds the same vectors
            again = ProjectMemory(root, store="chroma", embedding_function=embed)
            self.assertEqual(again.search("ISBN condition", 1)[0].phase, "planning")

            memory.clear()
            self.assertEqual(memory.chroma.collection.count(), 0)

    def test_falls_back_to_keywords(self):
        with GitTemporaryDirectory() as root:
            io = MagicMock()
            memory = ProjectMemory(root, io, store="chroma", embedding_function=MagicMock())
            memory.index_document(PHASES_BY_KEY["planning"], PRD)
            with patch("loom.memory.ChromaIndex", side_effect=RuntimeError("no model")):
                hits = memory.search("ISBN")
            self.assertIn("ISBN", hits[0].text)
            self.assertIn("no model", io.tool_warning.call_args[0][0])
            self.assertIn("ChromaDB unavailable: RuntimeError: no model", memory.backend)


class TestLegacyProjectFile(unittest.TestCase):
    def test_moves_project_json_into_the_database(self):
        from loom.orchestrator import LEGACY_STATE_FILE, ProjectState

        with GitTemporaryDirectory() as root:
            state = make_state(root)
            state.start("idea")
            state.finish("idea", "GO")
            legacy = Path(root) / LEGACY_STATE_FILE
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps(state.data))

            loaded = ProjectState.load(root)
            self.assertEqual(loaded.status("idea"), "review")
            self.assertFalse(legacy.exists())
            self.assertTrue(legacy.with_name("project.json.migrated").exists())
            self.assertEqual(ProjectState.load(root).phase_data("idea")["verdict"], "GO")


class TestMemoryTools(unittest.TestCase):
    def test_recall_and_record_decision(self):
        from loom import tools

        with GitTemporaryDirectory() as root:
            memory = ProjectMemory(root, store="keyword")
            memory.index_document(PHASES_BY_KEY["design"], ARCHITECTURE)
            coder = MagicMock(shared_memory=memory, phase=PHASES_BY_KEY["launch"])

            action = tools.prepare(coder, "record_decision", dict(decision="Deploy on Fly.io"))
            self.assertEqual(action.kind, "memory")
            self.assertIn("Recorded decision #1", action.run())
            self.assertEqual(memory.decisions()[0]["source"], "Launch agent")

            action = tools.prepare(coder, "recall", dict(query="fly.io deploy", limit="2"))
            result = action.run()
            self.assertTrue(re.search(r"\[1\] Decision \(Launch, by the Launch agent\)", result))
            self.assertIn("[2] Design: architecture document", result)
            self.assertEqual(action.summary, "Found 2 passages")

            action = tools.prepare(coder, "recall", dict(query="kubernetes"))
            self.assertIn("Nothing in the project memory", action.run())
            with self.assertRaises(tools.ToolError):
                tools.prepare(coder, "recall", dict(query="x", phase="marketing"))

            # The coding agent outside a project has no memory tools
            plain = MagicMock(shared_memory=None, mcp=None)
            with self.assertRaises(tools.ToolError):
                tools.prepare(plain, "recall", dict(query="x"))
            self.assertNotIn("recall", [s["function"]["name"] for s in tools.schemas()])


if __name__ == "__main__":
    unittest.main()

"""
Shared memory for a loom project (see loom/orchestrator.py): a database of the project's
state and decisions, and a vector store for looking up earlier context.

It all lives in .loom/memory/, which has its own .gitignore:

  project.db  SQLite, the source of truth. The orchestrator's state machine (the meta,
              phases and history tables), the decisions the founder and the phase agents
              made, and the chunks of the phase documents and decisions that search uses.
  chroma/     A ChromaDB collection of the same chunks with their embeddings, when
              chromadb is installed (the memory extra). It's derived from project.db and
              brought back in line with it before each search.

Search uses ChromaDB's embeddings when it can, and otherwise a BM25 keyword index over
the chunks in project.db: when chromadb isn't installed, its embedding model can't be
downloaded, or LOOM_MEMORY_STORE=keyword. So memory works without any extra packages.
"""

import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MEMORY_DIR = ".loom/memory"
DB_FILE = f"{MEMORY_DIR}/project.db"
CHROMA_DIR = f"{MEMORY_DIR}/chroma"
COLLECTION = "project"
SCHEMA_VERSION = 1

# "chroma", "keyword", or unset to use ChromaDB when it's installed
STORE_ENV = "LOOM_MEMORY_STORE"
CHUNK_CHARS = 1200

# Who decided: the founder, a phase agent by name, or the project's template
FOUNDER = "founder"
# The kinds of decision. "approved" is only kept for the record; the rest are context the
# phase agents get. "check" is the outcome of the template's checks after a phase.
DECISION_KINDS = ("decision", "approved", "edited", "rejected", "sent back", "override", "check")
CONTEXT_KINDS = ("decision", "edited", "rejected", "sent back", "override", "check")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phases (
    key TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    status TEXT NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    verdict TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    phase TEXT,
    event TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    phase TEXT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    phase TEXT,
    source TEXT,
    title TEXT,
    text TEXT NOT NULL,
    terms TEXT NOT NULL,
    length INTEGER NOT NULL,
    hash TEXT NOT NULL
);
"""

TABLES = ("meta", "phases", "history", "decisions", "chunks")


class ProjectMemoryError(Exception):
    pass


def now():
    return datetime.now().isoformat(timespec="seconds")


def inside(root, path):
    """Whether path is inside root after following symlinks, so loom never reads or writes
    memory through a link out of the project."""
    root = Path(root).resolve()
    real = Path(path).resolve()
    return real == root or root in real.parents


def check_inside(root, path, is_file=False):
    if not inside(root, path):
        raise ProjectMemoryError(f"{path} isn't in the project")
    if is_file and (path.exists() or path.is_symlink()) and not Path(path).resolve().is_file():
        raise ProjectMemoryError(f"{path} isn't a file")


# Chunks


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def chunk_markdown(text, limit=CHUNK_CHARS):
    """Split a Markdown document into (title, text) chunks of at most about limit
    characters: at its headings, then at blank lines. A chunk's title is its heading path,
    like "PRD: adder > Functional requirements"."""
    sections = []
    path = []
    lines = []
    fenced = False

    def flush():
        body = "\n".join(lines).strip()
        if body:
            sections.append((" > ".join(title for _, title in path), body))

    for line in text.splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
        match = None if fenced else HEADING_RE.match(line)
        if match:
            flush()
            lines = []
            level = len(match.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, match.group(2).strip()))
        lines.append(line)
    flush()

    chunks = []
    for title, body in sections:
        piece = ""
        for para in re.split(r"\n\s*\n", body):
            while len(para) > limit:
                if piece:
                    chunks.append((title, piece))
                    piece = ""
                chunks.append((title, para[:limit]))
                para = para[limit:]
            if piece and len(piece) + len(para) + 2 > limit:
                chunks.append((title, piece))
                piece = ""
            piece = f"{piece}\n\n{para}" if piece else para
        if piece.strip():
            chunks.append((title, piece))
    return chunks


# Keyword search


WORD_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    """a about after all also an and any are as at be been but by can could did do does
    for from had has have how if in into is it its may more most must no not of on only
    or our should so such than that the their them then there these they this those to
    up use used using was we were what when where which while who will with would you
    your""".split()
)


def stem(word):
    # Just plurals: "databases" finds "database"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def terms(text):
    return [
        stem(word)
        for word in WORD_RE.findall(text.lower())
        if word not in STOPWORDS and (len(word) > 1 or word.isdigit())
    ]


def bm25(query_terms, docs, k1=1.5, b=0.75):
    """Scores of docs, a list of (term counts, length), for query_terms."""
    if not docs:
        return []
    avg = sum(length for _, length in docs) / len(docs) or 1
    scores = [0.0] * len(docs)
    for term in set(query_terms):
        having = sum(1 for counts, _ in docs if term in counts)
        if not having:
            continue
        idf = math.log(1 + (len(docs) - having + 0.5) / (having + 0.5))
        for i, (counts, length) in enumerate(docs):
            tf = counts.get(term, 0)
            if tf:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
    return scores


@dataclass
class Hit:
    id: str
    kind: str  # "document", "decision" or "idea"
    phase: str
    source: str  # the document's path, or who made the decision
    title: str
    text: str
    score: float


# The database


class ProjectDB:
    """The SQLite database in .loom/memory/project.db. Each operation opens and closes its
    own connection, so nothing keeps the file open."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / DB_FILE

    def exists(self):
        return self.path.exists() or self.path.is_symlink()

    @contextmanager
    def connect(self):
        check_inside(self.root, self.path.parent)
        check_inside(self.root, self.path, is_file=True)
        make_memory_dir(self.root)
        try:
            conn = sqlite3.connect(self.path)
        except sqlite3.Error as err:
            raise ProjectMemoryError(f"Unable to open {self.path}: {err}")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.executescript(SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
                )
                yield conn
        except sqlite3.DatabaseError as err:
            raise ProjectMemoryError(f"Unable to use {self.path}: {err}")
        finally:
            conn.close()

    # Meta

    def get_meta(self, key, conn=None):
        if conn is None:
            if not self.exists():
                return None
            with self.connect() as conn:
                return self.get_meta(key, conn)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_meta(self, key, value, conn=None):
        if conn is None:
            with self.connect() as conn:
                return self.set_meta(key, value, conn)
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    # The project's state

    def load_state(self):
        """The orchestrator's state, as ProjectState keeps it, or None if there's none."""
        if not self.exists():
            return None
        with self.connect() as conn:
            data = self.get_meta("project", conn)
            if data is None:
                return None
            data["phases"] = {
                row["key"]: json.loads(row["data"])
                for row in conn.execute("SELECT key, data FROM phases ORDER BY number")
            }
            data["history"] = [
                {key: row[key] for key in ("time", "phase", "event", "note") if row[key]}
                for row in conn.execute("SELECT * FROM history ORDER BY id")
            ]
        return data

    def save_state(self, data, numbers):
        """Save ProjectState's data. numbers maps phase keys to their numbers."""
        project = {key: value for key, value in data.items() if key not in ("phases", "history")}
        with self.connect() as conn:
            self.set_meta("project", project, conn)
            for key, phase in data["phases"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO phases VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        numbers.get(key, 0),
                        phase["status"],
                        phase.get("runs", 0),
                        phase.get("verdict"),
                        json.dumps(phase),
                    ),
                )
            # The history only grows, so only its new entries are written
            saved = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            history = data["history"]
            if saved > len(history):
                conn.execute("DELETE FROM history")
                saved = 0
            conn.executemany(
                "INSERT INTO history (time, phase, event, note) VALUES (?, ?, ?, ?)",
                [
                    (entry["time"], entry.get("phase"), entry["event"], entry.get("note"))
                    for entry in history[saved:]
                ],
            )

    def clear(self):
        """Forget everything: the state, decisions and chunks."""
        if not self.exists():
            return
        with self.connect() as conn:
            for table in TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    # Decisions

    def add_decision(self, phase, source, kind, text, reason=""):
        if kind not in DECISION_KINDS:
            raise ValueError(f"unknown decision kind {kind!r}")
        decision = dict(
            time=now(), phase=phase, source=source, kind=kind, text=text, reason=reason or None
        )
        with self.connect() as conn:
            cur = conn.execute(
                (
                    "INSERT INTO decisions (time, phase, source, kind, text, reason)"
                    " VALUES (:time, :phase, :source, :kind, :text, :reason)"
                ),
                decision,
            )
            decision["id"] = cur.lastrowid
        return decision

    def decisions(self, kinds=None, phase=None):
        if not self.exists():
            return []
        sql = "SELECT * FROM decisions"
        where, args = [], []
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        if phase:
            where.append("phase = ?")
            args.append(phase)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql + " ORDER BY id", args)]

    # Chunks

    def replace_chunks(self, prefix, chunks):
        """Replace the chunks whose ids start with prefix by chunks, a list of dicts with
        id, kind, phase, source, title and text."""
        with self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE substr(id, 1, ?) = ?", (len(prefix), prefix))
            for chunk in chunks:
                counted = terms(f"{chunk['title']}\n{chunk['text']}")
                digest = hashlib.sha1(
                    f"{chunk['title']}\n{chunk['text']}".encode("utf-8")
                ).hexdigest()
                conn.execute(
                    "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk["id"],
                        chunk["kind"],
                        chunk.get("phase"),
                        chunk.get("source"),
                        chunk.get("title") or "",
                        chunk["text"],
                        json.dumps(Counter(counted)),
                        len(counted),
                        digest,
                    ),
                )

    def chunks(self, ids=None):
        if not self.exists():
            return []
        with self.connect() as conn:
            if ids is None:
                rows = conn.execute("SELECT * FROM chunks ORDER BY id").fetchall()
            else:
                rows = []
                for start in range(0, len(ids), 500):
                    part = ids[start : start + 500]
                    rows += conn.execute(
                        f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(part))})", part
                    ).fetchall()
        return [dict(row) for row in rows]

    def count(self, table):
        if not self.exists():
            return 0
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def make_memory_dir(root):
    memory_dir = Path(root) / MEMORY_DIR
    memory_dir.mkdir(parents=True, exist_ok=True)
    ignore = memory_dir / ".gitignore"
    if not ignore.exists() and not ignore.is_symlink():
        # The memory is local to this checkout, like a cache: the documents are in git
        ignore.write_text("*\n", encoding="utf-8")


def matches(chunk, phases=None, kinds=None, exclude_sources=None):
    if phases and chunk["phase"] not in phases:
        return False
    if kinds and chunk["kind"] not in kinds:
        return False
    if exclude_sources and chunk["source"] in exclude_sources:
        return False
    return True


# The vector store


def chroma_installed():
    return importlib.util.find_spec("chromadb") is not None


class ChromaIndex:
    """The chunks in a persistent ChromaDB collection, embedded by ChromaDB's default
    embedding function (all-MiniLM-L6-v2, which it downloads once) unless given one."""

    name = "ChromaDB"

    def __init__(self, path, embedding_function=None):
        import chromadb
        from chromadb.config import Settings

        self.client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        kwargs = dict(name=COLLECTION, metadata={"hnsw:space": "cosine"})
        if embedding_function is not None:
            kwargs["embedding_function"] = embedding_function
        self.collection = self.client.get_or_create_collection(**kwargs)

    def sync(self, chunks):
        """Make the collection hold exactly chunks, embedding the new and changed ones."""
        stored = self.collection.get(include=["metadatas"])
        hashes = {
            id: (meta or {}).get("hash") for id, meta in zip(stored["ids"], stored["metadatas"])
        }
        wanted = {chunk["id"]: chunk for chunk in chunks}
        stale = [id for id in hashes if id not in wanted]
        if stale:
            self.collection.delete(ids=stale)
        changed = [chunk for chunk in chunks if hashes.get(chunk["id"]) != chunk["hash"]]
        for start in range(0, len(changed), 100):
            part = changed[start : start + 100]
            self.collection.upsert(
                ids=[chunk["id"] for chunk in part],
                documents=[f"{chunk['title']}\n{chunk['text']}".strip() for chunk in part],
                metadatas=[
                    dict(
                        kind=chunk["kind"],
                        phase=chunk["phase"] or "",
                        source=chunk["source"] or "",
                        hash=chunk["hash"],
                    )
                    for chunk in part
                ],
            )
        return len(changed)

    def search(self, query, limit, phases=None, kinds=None, exclude_sources=None):
        """[(id, similarity)] of the chunks nearest to query."""
        conditions = []
        if phases:
            conditions.append(dict(phase={"$in": list(phases)}))
        if kinds:
            conditions.append(dict(kind={"$in": list(kinds)}))
        if exclude_sources:
            # ChromaDB's "not in" operator
            conditions.append(dict(source={"$nin": list(exclude_sources)}))
        where = None
        if len(conditions) == 1:
            where = conditions[0]
        elif conditions:
            where = {"$and": conditions}
        total = self.collection.count()
        if not total:
            return []
        res = self.collection.query(
            query_texts=[query], n_results=min(limit, total), where=where, include=["distances"]
        )
        return [(id, 1 - distance) for id, distance in zip(res["ids"][0], res["distances"][0])]


class ProjectMemory:
    """The project's shared memory: its database, and search over the phase documents and
    decisions in it."""

    def __init__(self, root, io=None, store=None, embedding_function=None):
        self.root = Path(root)
        self.io = io
        self.db = ProjectDB(root)
        self.store = (store or os.environ.get(STORE_ENV) or "auto").strip().lower()
        self.embedding_function = embedding_function
        self.chroma = None
        # Why ChromaDB isn't used, when it isn't
        self.chroma_problem = None
        self.synced = False

    # Which store searches

    def vector_index(self):
        """The ChromaDB index, in line with the database, or None to search by keyword."""
        if self.store == "keyword" or self.chroma_problem:
            return None
        if self.chroma is None:
            if not chroma_installed():
                self.chroma_problem = "chromadb isn't installed"
                return None
            try:
                path = self.root / CHROMA_DIR
                check_inside(self.root, path)
                make_memory_dir(self.root)
                self.chroma = ChromaIndex(path, self.embedding_function)
            except Exception as err:
                return self.give_up_on_chroma(err)
        if not self.synced:
            try:
                chunks = self.db.chunks()
                if chunks and self.io and not self.model_downloaded():
                    self.io.tool_output(
                        "Downloading ChromaDB's embedding model for project memory (80 MB, once)..."
                    )
                self.chroma.sync(chunks)
                self.synced = True
            except Exception as err:
                return self.give_up_on_chroma(err)
        return self.chroma

    def model_downloaded(self):
        if self.embedding_function is not None:
            return True
        try:
            from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
                ONNXMiniLM_L6_V2,
            )
        except Exception:
            return True
        return (Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / "onnx" / "model.onnx").exists()

    def give_up_on_chroma(self, err):
        self.chroma_problem = f"{err.__class__.__name__}: {err}".strip().split("\n", 1)[0]
        self.chroma = None
        if self.io:
            self.io.tool_warning(
                f"Project memory can't use ChromaDB ({self.chroma_problem}); searching by"
                " keyword instead."
            )
        return None

    @property
    def backend(self):
        """Which store searches, as shown to the user."""
        if self.store == "keyword":
            return "keyword index (BM25)"
        if self.chroma_problem:
            return f"keyword index (BM25); ChromaDB unavailable: {self.chroma_problem}"
        if self.store == "chroma" or chroma_installed():
            return "ChromaDB vector store (all-MiniLM-L6-v2 embeddings)"
        return "keyword index (BM25); install the memory extra for ChromaDB"

    def changed(self):
        self.synced = False

    # State

    def clear(self):
        """Forget the project: its state, decisions and the search index."""
        self.db.clear()
        self.changed()
        if (self.root / CHROMA_DIR).exists():
            # Syncing with the now empty database empties the collection
            self.vector_index()

    # Decisions

    def record_decision(self, phase, text, reason="", source=FOUNDER, kind="decision"):
        text = " ".join(str(text).split())
        reason = " ".join(str(reason or "").split())
        decision = self.db.add_decision(phase, source, kind, text, reason)
        body = text + (f"\nWhy: {reason}" if reason else "")
        self.db.replace_chunks(
            f"decision:{decision['id']}:",
            [
                dict(
                    id=f"decision:{decision['id']}:0",
                    kind="decision",
                    phase=phase,
                    source=source,
                    title=f"Decision by the {source}" if source != FOUNDER else "Founder decision",
                    text=body,
                )
            ],
        )
        self.changed()
        return decision

    def decisions(self, kinds=None, phase=None):
        return self.db.decisions(kinds, phase)

    # Documents

    def index_text(self, key, kind, phase, source, text, title=""):
        """Index a document under key (like "doc:planning"), replacing its earlier version.
        Returns whether it changed."""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if self.db.get_meta(f"hash:{key}") == digest:
            return False
        if kind == "document":
            pieces = chunk_markdown(text)
        else:
            pieces = [(title, text)]
        self.db.replace_chunks(
            f"{key}:",
            [
                dict(id=f"{key}:{i:03d}", kind=kind, phase=phase, source=source, title=t, text=body)
                for i, (t, body) in enumerate(pieces)
            ],
        )
        self.db.set_meta(f"hash:{key}", digest)
        self.changed()
        return True

    def index_document(self, phase, text):
        return self.index_text(f"doc:{phase.key}", "document", phase.key, phase.document, text)

    # Search

    def search(self, query, limit=5, phases=None, kinds=None, exclude_sources=None):
        """The chunks most relevant to query, best first, as Hits."""
        query = str(query or "").strip()
        if not query:
            return []
        index = self.vector_index()
        if index:
            try:
                found = index.search(query, limit, phases, kinds, exclude_sources)
            except Exception as err:
                self.give_up_on_chroma(err)
            else:
                rows = {row["id"]: row for row in self.db.chunks([id for id, _ in found])}
                return [self.hit(rows[id], score) for id, score in found if id in rows]
        return self.keyword_search(query, limit, phases, kinds, exclude_sources)

    def keyword_search(self, query, limit=5, phases=None, kinds=None, exclude_sources=None):
        chunks = [
            chunk for chunk in self.db.chunks() if matches(chunk, phases, kinds, exclude_sources)
        ]
        docs = [(json.loads(chunk["terms"]), chunk["length"]) for chunk in chunks]
        scores = bm25(terms(query), docs)
        ranked = sorted(
            (pair for pair in zip(scores, range(len(chunks))) if pair[0] > 0), reverse=True
        )
        return [self.hit(chunks[i], score) for score, i in ranked[:limit]]

    def hit(self, row, score):
        return Hit(
            row["id"], row["kind"], row["phase"], row["source"], row["title"], row["text"], score
        )

    def stats(self):
        return dict(
            decisions=self.db.count("decisions"),
            chunks=self.db.count("chunks"),
        )

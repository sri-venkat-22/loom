"""The web UI's side pane reads the project through these: its files, a file's text, and
the /project shared memory. The chat itself goes over the /ws WebSocket."""

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException

from loom.memory import ProjectDB, ProjectMemory, ProjectMemoryError
from loom.models import MODEL_ALIASES

# Largest file the viewer opens
MAX_FILE_BYTES = 1_000_000
MAX_HITS = 20
MAX_DECISIONS = 100


def make_router(io):
    """The /api routes, for the WebIO the Coder uses."""
    router = APIRouter(prefix="/api")

    def root():
        if io is None or not io.root:
            raise HTTPException(503, "loom isn't ready yet")
        return Path(io.root)

    @router.get("/files")
    def files():
        """The project's files, which are in the chat, and which are read-only there."""
        return dict(root=str(root()), **io.files)

    @router.get("/file")
    def file(path: str):
        """The text of one of the project's files."""
        base = root().resolve()
        known = set(io.files["files"]) | set(io.files["chat"]) | set(io.files["read_only"])
        if path not in known:
            raise HTTPException(404, f"{path} isn't one of the project's files")
        full = (base / path).resolve()
        try:
            full.relative_to(base)
        except ValueError:
            # Read-only files can be outside the project; only ones loom was given
            if path not in io.files["read_only"]:
                raise HTTPException(404, f"{path} isn't in the project")
        try:
            size = full.stat().st_size
            if size > MAX_FILE_BYTES:
                raise HTTPException(413, f"{path} is too big to show ({size:,} bytes)")
            data = full.read_bytes()
        except OSError as err:
            raise HTTPException(404, f"Unable to read {path}: {err.strerror or err}")
        if b"\0" in data[:8000]:
            raise HTTPException(415, f"{path} isn't a text file")
        return dict(path=path, text=data.decode("utf-8", errors="replace"))

    @router.get("/memory")
    def memory(q: str = ""):
        """Search the /project shared memory, or list its decisions when q is empty."""
        base = root()
        result = dict(available=False, store=None, query=q, hits=[], decisions=[])
        if not ProjectDB(base).exists():
            return result
        memory = ProjectMemory(base)
        try:
            if q.strip():
                result["hits"] = [asdict(hit) for hit in memory.search(q, limit=MAX_HITS)]
                result["store"] = memory.backend
            else:
                result["decisions"] = memory.decisions()[-MAX_DECISIONS:][::-1]
        except ProjectMemoryError as err:
            raise HTTPException(500, str(err))
        result["available"] = True
        return result

    @router.get("/models")
    def models():
        """Model names /model accepts by alias, for the model picker."""
        return dict(aliases=[dict(alias=k, model=v) for k, v in sorted(MODEL_ALIASES.items())])

    return router

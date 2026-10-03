"""The web UI's side pane reads the project through these: its files, a file's text, the
/project shared memory and its report. The chat itself goes over the /ws WebSocket."""

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from loom.memory import ProjectDB, ProjectMemory, ProjectMemoryError
from loom.models import MODEL_ALIASES
from loom.orchestrator import ProjectState, TransitionError
from loom.phases import PHASES
from loom.project_report import FORMATS, ProjectReport, ReportError, export_bytes
from loom.sessions import list_sessions

from .changes import branch_base, changes

# The /project documents, which can be read as soon as a phase writes them, before loom's
# own list of the project's files includes them
DOCUMENTS = {phase.document for phase in PHASES}

# Largest file the viewer opens
MAX_FILE_BYTES = 1_000_000
MAX_HITS = 20
MAX_DECISIONS = 100
MAX_SESSIONS = 50

REPORT_TYPES = dict(
    md="text/markdown; charset=utf-8",
    html="text/html; charset=utf-8",
    docx="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    pdf="application/pdf",
)


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
        if path not in known | DOCUMENTS:
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

    @router.get("/sessions")
    def sessions():
        """The project's saved conversations, newest first, for the sessions sidebar."""
        root()
        current = io.conversation_id
        found = []
        if io.sessions_dir:
            for session_id, updated, title, messages in list_sessions(
                io.sessions_dir, limit=MAX_SESSIONS
            ):
                found.append(
                    dict(
                        id=session_id,
                        updated=updated,
                        title=title,
                        messages=messages,
                        current=session_id == current,
                    )
                )
        if current and not any(session["current"] for session in found):
            # A new conversation isn't saved until it has a message
            found.insert(0, dict(id=current, updated=None, title="", messages=0, current=True))
        return dict(saved=bool(io.sessions_dir), sessions=found)

    @router.get("/changes")
    def changes_(base: str = "session"):
        """The files that differ from the commit loom started at (base=session), or from
        where the branch left main (base=branch), with their diffs."""
        project = root()
        if not io.git:
            return dict(available=False, base=None, label=None, files=[])
        if base == "branch":
            commit, branch = branch_base(io.git)
            label = f"since {branch}" if branch else None
        else:
            commit, label = io.base_commit, "since loom started"
        if not commit:
            return dict(available=False, base=None, label=label, files=[])
        try:
            files = changes(io.git, project, commit)
        except Exception as err:
            raise HTTPException(500, f"Unable to diff against {commit[:7]}: {err}")
        return dict(available=True, base=commit[:7], label=label, files=files)

    def project(base):
        """The /project in base, and its decisions."""
        if not ProjectDB(base).exists():
            raise HTTPException(404, "There is no project here.")
        try:
            state = ProjectState.load(base)
            decisions = ProjectMemory(base).decisions()
        except (TransitionError, ProjectMemoryError) as err:
            raise HTTPException(500, str(err))
        if state is None:
            raise HTTPException(404, "There is no project here.")
        return state, decisions

    @router.get("/project/report")
    def project_report(format: str = "md"):
        """The /project's report, to download as md, html, docx or pdf. Word needs pandoc
        (without it the report comes as HTML), and PDF a PDF engine too."""
        base = root()
        if format not in FORMATS:
            raise HTTPException(400, f"Unknown format {format!r}: use one of {', '.join(FORMATS)}")
        state, decisions = project(base)
        snapshot = getattr(getattr(io, "web", None), "snapshot", None) or {}
        models = dict(main=snapshot.get("model"), weak=snapshot.get("weak_model"))
        report = ProjectReport(base, state, decisions, io.git, models)
        try:
            data, fmt = export_bytes(report.markdown(), format)
        except ReportError as err:
            raise HTTPException(501, str(err))
        return Response(
            data,
            media_type=REPORT_TYPES[fmt],
            headers={"Content-Disposition": f'attachment; filename="report.{fmt}"'},
        )

    @router.get("/models")
    def models():
        """Model names /model accepts by alias, for the model picker."""
        return dict(aliases=[dict(alias=k, model=v) for k, v in sorted(MODEL_ALIASES.items())])

    return router

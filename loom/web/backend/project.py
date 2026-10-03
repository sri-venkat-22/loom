"""The /project orchestrator's progress, for the web UI's phase breadcrumb and the project
dashboard in the side pane.

It's read straight from the project's SQLite database, read-only, so watching it never
writes to the database or creates it, and works while a phase agent runs.
"""

import json
import sqlite3
import threading
from pathlib import Path

from loom.memory import DB_FILE
from loom.orchestrator import ProjectState
from loom.phases import PHASES, SPEC

# How often the watcher looks for changes to the project
WATCH_SECONDS = 0.5

HISTORY_FIELDS = ("time", "phase", "event", "note")


def connect(root):
    """A read-only connection to the project's database, or None if it has none."""
    path = Path(root) / DB_FILE
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def read_project(root):
    """(idea, {phase key: status}) of the project in root, or None if it has none."""
    try:
        conn = connect(root)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'project'").fetchone()
            if not row:
                return None
            project = json.loads(row[0])
            statuses = dict(conn.execute("SELECT key, status FROM phases").fetchall())
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError):
        return None
    return project.get("idea") or "", statuses


def read_state(root):
    """(the project's ProjectState, its decisions) in root, read-only, or None if it has
    none. The state isn't tied to the database, so saving it does nothing."""
    try:
        conn = connect(root)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'project'").fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            data["phases"] = {
                row["key"]: json.loads(row["data"])
                for row in conn.execute("SELECT key, data FROM phases ORDER BY number")
            }
            data["history"] = [
                {key: row[key] for key in HISTORY_FIELDS if row[key]}
                for row in conn.execute("SELECT * FROM history ORDER BY id")
            ]
            decisions = [dict(row) for row in conn.execute("SELECT * FROM decisions ORDER BY id")]
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError):
        return None
    for phase in PHASES:
        data["phases"].setdefault(phase.key, dict(status="pending", runs=0))
    return ProjectState(None, data), decisions


def project_state(root):
    """The session fields that describe the project: its idea, the phase it's in (None
    when there's no project or it's complete) and every phase's status."""
    found = read_project(root)
    idea, statuses = found if found else (None, {})
    phases = [
        dict(key=phase.key, title=phase.title, status=statuses.get(phase.key, "pending"))
        for phase in PHASES
    ]
    current = None
    if found:
        current = next((p["key"] for p in phases if p["status"] != "approved"), None)
    return dict(project=idea, phase=current, phases=phases)


NO_TIMELINE = dict(
    available=False,
    idea=None,
    created=None,
    current=None,
    complete=False,
    template=None,
    tdd=False,
    workers=None,
    totals=None,
    phases=[],
    decisions=[],
)


def spec_summary(spec):
    """Test-driven Building's acceptance tests, for the dashboard: {status, document,
    tests, locked}, or None."""
    if not spec:
        return None
    return dict(
        status=spec.get("status", "pending"),
        document=SPEC.document,
        tests=list(spec.get("tests") or []),
        locked=bool(spec.get("locked")),
    )


def package_summaries(packages):
    """Parallel Building's work packages, for the dashboard."""
    return [
        dict(
            id=pid,
            title=record.get("title") or pid,
            status=record.get("status", "pending"),
            wave=record.get("wave"),
            cost=record.get("cost") or 0,
            runs=record.get("runs") or 0,
            attempts=record.get("attempts"),
            error=record.get("error"),
        )
        for pid, record in (packages or {}).items()
    ]


def timeline(root):
    """What the project dashboard shows of the project in root: every phase with its
    status, verdict, metrics, runs, decisions and history, and the project's totals."""
    found = read_state(root)
    if not found:
        return dict(NO_TIMELINE)
    state, decisions = found
    current = state.current
    phases = []
    for phase in PHASES:
        data = state.phase_data(phase.key)
        phases.append(
            dict(
                key=phase.key,
                number=phase.number,
                title=phase.title,
                agent=phase.agent,
                document=phase.document,
                document_title=phase.document_title,
                produces=phase.produces,
                status=data["status"],
                stale=bool(data.get("stale")),
                verdict=data.get("verdict"),
                current=phase is current,
                metrics=state.metrics(phase.key),
                run_log=state.run_log(phase.key),
                fix_rounds=state.fix_rounds(phase.key),
                decisions=[d for d in decisions if d.get("phase") == phase.key],
                history=[h for h in state.history if h.get("phase") == phase.key],
                checks=[
                    dict(command=check["command"], passed=check["passed"])
                    for check in data.get("checks") or []
                ],
                spec=spec_summary(data.get("spec")),
                packages=package_summaries(data.get("packages")),
            )
        )
    template = state.template_info
    return dict(
        available=True,
        idea=state.idea,
        created=state.data.get("created"),
        current=current.key if current else None,
        complete=current is None,
        template=dict(name=template["name"], source=template.get("source")) if template else None,
        tdd=state.tdd,
        workers=state.data.get("workers"),
        totals=state.totals(),
        phases=phases,
        # The decisions that aren't any phase's, like ones made once it was complete
        decisions=[d for d in decisions if not d.get("phase")],
    )


class ProjectWatcher:
    """Sends the session a new snapshot whenever the project's database changes, so the
    breadcrumb follows a /project run phase by phase, and the dashboard's timeline with
    it."""

    def __init__(self, session, root):
        self.session = session
        self.root = Path(root)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.watch, name="loom-web-project", daemon=True)

    def start(self):
        self.refresh_timeline()
        self.thread.start()
        return self

    def stop(self):
        self.stopped.set()

    def stamp(self):
        """When the database last changed: its file's, and its write-ahead log's."""
        found = []
        for name in (DB_FILE, DB_FILE + "-wal"):
            try:
                stat = (self.root / name).stat()
                found.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                found.append(None)
        return tuple(found) if found[0] else None

    def watch(self):
        last = self.stamp()
        while not self.stopped.wait(WATCH_SECONDS):
            stamp = self.stamp()
            if stamp == last:
                continue
            last = stamp
            self.refresh()

    def refresh(self):
        state = project_state(self.root)
        snapshot = self.session.snapshot
        if any(snapshot.get(key) != value for key, value in state.items()):
            self.session.update(**state)
        self.refresh_timeline()

    def refresh_timeline(self):
        found = timeline(self.root)
        latest = self.session.timeline
        if latest is None or {k: v for k, v in latest.items() if k != "type"} != found:
            self.session.emit("timeline", **found)

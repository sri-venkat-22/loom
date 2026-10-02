"""The /project orchestrator's progress, for the web UI's phase breadcrumb.

It's read straight from the project's SQLite database, read-only, so watching it never
writes to the database or creates it, and works while a phase agent runs.
"""

import json
import sqlite3
import threading
from pathlib import Path

from loom.memory import DB_FILE
from loom.phases import PHASES

# How often the watcher looks for changes to the project
WATCH_SECONDS = 0.5


def read_project(root):
    """(idea, {phase key: status}) of the project in root, or None if it has none."""
    path = Path(root) / DB_FILE
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
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


class ProjectWatcher:
    """Sends the session a new snapshot whenever the project's database changes, so the
    breadcrumb follows a /project run phase by phase."""

    def __init__(self, session, root):
        self.session = session
        self.root = Path(root)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.watch, name="loom-web-project", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopped.set()

    def stamp(self):
        try:
            stat = (self.root / DB_FILE).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

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

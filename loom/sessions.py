"""
Saved conversations. After every request loom saves the conversation to
.loom.sessions/<id>.json in the project root, so `loom --continue` can pick up the latest
one and `loom --resume ID` an older one.

A Session also holds the conversation state that outlives a single coder, like the
agent's to-do list, the plan approved in plan mode and the checkpoints /rewind goes back
to, so it survives switching chat modes.
"""

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = ".loom.sessions"
FORMAT_VERSION = 1
# Older sessions are deleted when a new one is saved
MAX_SESSIONS = 100


def new_session_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def now():
    return datetime.now().isoformat(timespec="seconds")


def jsonable(value):
    """Plain JSON data from messages, which can hold litellm's pydantic objects."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    for method in ("model_dump", "dict"):
        if hasattr(value, method):
            try:
                return jsonable(getattr(value, method)())
            except Exception:
                pass
    return str(value)


def get_title(messages):
    """The first line of the conversation's first request."""
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            line = msg["content"].strip().split("\n", 1)[0]
            return line[:80] + ("…" if len(line) > 80 else "")
    return ""


class SessionError(Exception):
    pass


class Session:
    def __init__(self, directory=None, session_id=None, data=None):
        """directory is where to save the session; None keeps it in memory only."""
        self.directory = Path(directory) if directory else None
        self.id = session_id or new_session_id()
        self.data = data or {}
        self.todos = list(self.data.get("todos") or [])
        self.created = self.data.get("created") or now()
        self.title = self.data.get("title") or ""
        # The approved plan's file (loom/plans.py), relative to the project root
        self.plan = self.data.get("plan")
        # Where /rewind can go back to, oldest first (loom/checkpoints.py)
        self.checkpoints = list(self.data.get("checkpoints") or [])
        # The sub-agents' tasks started in this conversation (loom/subagents.py)
        self.tasks = []

    @property
    def path(self):
        if not self.directory:
            return None
        return self.directory / f"{self.id}.json"

    @property
    def messages(self):
        return list(self.data.get("messages") or [])

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise SessionError(f"Unable to read session {path}: {err}")
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise SessionError(f"{path} is not a loom session")
        return cls(path.parent, path.stem, data)

    @classmethod
    def latest(cls, directory):
        """The most recently saved session in directory, or None."""
        paths = session_paths(directory)
        for path in paths:
            try:
                return cls.load(path)
            except SessionError:
                continue
        return None

    @classmethod
    def find(cls, directory, session_id):
        """The session whose id is or starts with session_id."""
        paths = session_paths(directory)
        exact = [path for path in paths if path.stem == session_id]
        matches = exact or [path for path in paths if path.stem.startswith(session_id)]
        if not matches:
            raise SessionError(f"No session {session_id!r} in {directory}")
        if len(matches) > 1:
            ids = ", ".join(path.stem for path in matches[:5])
            raise SessionError(f"Session id {session_id!r} is ambiguous: {ids}")
        return cls.load(matches[0])

    def restart(self):
        """Start a new conversation, as /clear does. The old one stays saved."""
        self.id = new_session_id()
        self.data = {}
        self.todos.clear()
        self.created = now()
        self.title = ""
        self.plan = None
        self.checkpoints = []
        self.tasks = []

    def to_dict(self, coder):
        messages = jsonable(coder.done_messages + coder.cur_messages)
        if not self.title:
            self.title = get_title(messages)
        return dict(
            version=FORMAT_VERSION,
            id=self.id,
            title=self.title,
            created=self.created,
            updated=now(),
            model=coder.main_model.name,
            edit_format=coder.edit_format,
            files=sorted(coder.get_inchat_relative_files()),
            read_only_files=sorted(str(fname) for fname in coder.abs_read_only_fnames),
            todos=jsonable(self.todos),
            plan=self.plan,
            checkpoints=jsonable(self.checkpoints),
            messages=messages,
        )

    def save(self, coder):
        """Save the coder's conversation. Returns False if there was nothing to save."""
        if not self.directory:
            return False
        path = self.path
        is_new = not path.exists()
        if is_new and not (coder.done_messages or coder.cur_messages):
            # Don't save empty conversations
            return False

        data = self.to_dict(coder)
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        self.data = data

        if is_new:
            for old in session_paths(self.directory)[MAX_SESSIONS:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return True


def session_paths(directory):
    """Saved session files, newest first."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    paths = [path for path in directory.glob("*.json") if path.is_file()]

    def mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    return sorted(paths, key=lambda path: (mtime(path), path.name), reverse=True)


def list_sessions(directory, limit=20):
    """(id, updated, title, number of messages) for the newest saved sessions."""
    res = []
    for path in session_paths(directory)[:limit]:
        try:
            session = Session.load(path)
        except SessionError:
            continue
        res.append(
            (session.id, session.data.get("updated", ""), session.title, len(session.messages))
        )
    return res

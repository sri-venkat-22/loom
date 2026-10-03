"""
Tools the agent coder gives the model: read, search, edit and run commands in the project.

Each tool's prepare function checks the model's arguments and returns an Action. The
agent asks Permissions about the action (showing the user its preview when it needs
approval) and then calls action.run() to get the text sent back to the model.

A ToolError is a problem the model should hear about (a missing file, an ambiguous
edit); it becomes the tool's result and never ends the agent loop.
"""

import difflib
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loom.utils import is_image_file

MAX_RESULT_CHARS = 30_000
READ_LINE_LIMIT = 2000
MAX_LINE_CHARS = 2000
MAX_LIST_ENTRIES = 500
MAX_MATCHES = 200
MAX_WALK_FILES = 20_000
MAX_GREP_FILE_BYTES = 1_000_000
BASH_TIMEOUT = 120
MAX_BASH_TIMEOUT = 600
MAX_PREVIEW_LINES = 60
# How long to wait for a killed command's output pipe to close
KILL_GRACE = 5

# Environment variables the agent's commands don't get: API keys and other credentials,
# which the code those commands run (a repo's tests, say) could otherwise read
SECRET_ENV_RE = re.compile(
    r"(KEY|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?|AUTH|(^|_)PAT)$"
    r"|API_?KEY|SECRET|^AWS_.*(KEY|TOKEN)",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
}


class ToolError(Exception):
    pass


@dataclass
class Action:
    """Something a tool is about to do, for Permissions to decide on."""

    kind: str  # "read", "edit", "bash", "todo", "mcp" or "memory"
    target: str  # what allow rules match: a path (relative if inside the project) or a command
    inside: bool  # the target is inside the project
    title: str  # a short description, like "Edit loom/io.py"
    run: Callable[[], str]
    preview: str = ""  # shown when asking the user, like a diff
    path: Path = None  # the file an edit changes or a read reads
    new_file: bool = False
    extra: dict = field(default_factory=dict)
    # Shown to the user as name(detail), like Read(loom/io.py)
    name: str = ""
    detail: str = ""
    # One line about the outcome, like "Read 120 lines", set by run()
    summary: str = ""
    # For edits: the file and how many lines change, shown instead of the diff
    changes: str = ""


# Paths


def project_root(coder):
    return Path(coder.root).resolve()


def resolve_path(coder, path):
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string")
    p = Path(os.path.expanduser(path.strip()))
    if not p.is_absolute():
        p = project_root(coder) / p
    return p.resolve()


def is_within(path, root):
    return path == root or root in path.parents


def is_inside(coder, abs_path):
    """Whether abs_path is in the project, after following symlinks."""
    return is_within(Path(abs_path).resolve(), project_root(coder))


def display_path(coder, abs_path):
    """Project-relative posix path for files inside the project, else the absolute path."""
    root = project_root(coder)
    for path in (Path(abs_path), Path(abs_path).resolve()):
        if is_within(path, root):
            return path.relative_to(root).as_posix() or "."
    return Path(abs_path).as_posix()


def check_ignored(coder, abs_path):
    if coder.repo and is_inside(coder, abs_path) and abs_path != project_root(coder):
        if coder.repo.ignored_file(display_path(coder, abs_path)):
            raise ToolError(f"{display_path(coder, abs_path)} is excluded by .loomignore")


# Globs


def _glob_body(pattern):
    res = ""
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            res += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            res += ".*"
            i += 2
            continue
        if c == "*":
            res += "[^/]*"
        elif c == "?":
            res += "[^/]"
        elif c == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                res += re.escape(c)
            else:
                inner = pattern[i + 1 : end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                res += "[" + inner.replace("\\", "\\\\") + "]"
                i = end + 1
                continue
        elif c == "{":
            end = pattern.find("}", i)
            if end == -1:
                res += re.escape(c)
            else:
                alternatives = pattern[i + 1 : end].split(",")
                res += "(?:" + "|".join(_glob_body(alt) for alt in alternatives) + ")"
                i = end + 1
                continue
        else:
            res += re.escape(c)
        i += 1
    return res


def glob_to_regex(pattern):
    """Compile a glob where * stays within a path segment, ** crosses them, and {a,b}
    picks alternatives."""
    return re.compile(_glob_body(pattern))


def glob_match(pattern, path):
    return glob_to_regex(pattern).fullmatch(path) is not None


# Listing project files


def list_files(coder, base):
    """(display path, absolute path) for files under base, skipping git-ignored files, junk
    directories and symlinks that lead out of the project (or out of base, outside it), whose
    contents reading them would need permission for. Uses git's view of the repo when base
    is inside it."""
    root = project_root(coder)
    limit = root if is_inside(coder, base) else Path(base).resolve()
    return [
        (shown, abs_path)
        for shown, abs_path in _list_files(coder, base, root)
        if is_within(abs_path.resolve(), limit)
    ]


def _list_files(coder, base, root):
    if coder.repo and is_inside(coder, base):
        names = set(coder.repo.get_tracked_files())
        try:
            others = coder.repo.repo.git.ls_files("--others", "--exclude-standard", "-z")
            names.update(name for name in others.split("\0") if name)
        except Exception:
            pass

        res = []
        for name in sorted(names):
            abs_path = root / name
            if base != root and base not in abs_path.parents:
                continue
            if coder.repo.ignored_file(name) or not abs_path.is_file():
                continue
            res.append((Path(name).as_posix(), abs_path))
        return res

    res = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            abs_path = Path(dirpath) / fname
            res.append((display_path(coder, abs_path), abs_path))
            if len(res) >= MAX_WALK_FILES:
                return res
    return res


def truncate(text, limit=MAX_RESULT_CHARS):
    """Keep the start and the (usually more useful) end of long output."""
    if len(text) <= limit:
        return text
    head = limit // 6
    tail = limit - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n\n... [{omitted:,} characters omitted] ...\n\n" + text[-tail:]


def read_text_file(coder, abs_path):
    if not abs_path.exists():
        raise ToolError(f"{display_path(coder, abs_path)} does not exist")
    if abs_path.is_dir():
        raise ToolError(f"{display_path(coder, abs_path)} is a directory, use list_dir")
    if is_image_file(str(abs_path)):
        raise ToolError(f"{display_path(coder, abs_path)} is an image, not a text file")
    data = abs_path.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{display_path(coder, abs_path)} is a binary file")
    text = data.decode(coder.io.encoding, errors="replace")
    # Match io.read_text, which opens files in text mode
    return text.replace("\r\n", "\n").replace("\r", "\n")


def count_changes(before, after):
    """(added lines, removed lines) between two versions of a file."""
    added = removed = 0
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def describe_changes(added, removed):
    parts = []
    if added:
        parts.append(plural(added, "addition"))
    if removed:
        parts.append(plural(removed, "removal"))
    return " and ".join(parts) or "no line changes"


def plural(num, word, words=None):
    return f"{num} {word if num == 1 else words or word + 's'}"


def make_diff(coder, abs_path, before, after):
    name = display_path(coder, abs_path)
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > MAX_PREVIEW_LINES:
        more = len(lines) - MAX_PREVIEW_LINES
        lines = lines[:MAX_PREVIEW_LINES] + [f"... ({more} more diff lines)"]
    return "\n".join(lines)


def dry_run_note(coder):
    return "(Dry run: nothing was written.) " if coder.dry_run else ""


def write_file_text(coder, abs_path, content):
    if coder.dry_run:
        return
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    coder.io.write_text(str(abs_path), content)


# The tools


def read_file(coder, path, offset=1, limit=READ_LINE_LIMIT):
    abs_path = resolve_path(coder, path)
    check_ignored(coder, abs_path)
    name = display_path(coder, abs_path)
    offset = max(1, offset)
    limit = max(1, limit)

    def run():
        lines = read_text_file(coder, abs_path).splitlines()
        if not lines:
            action.summary = "Empty file"
            return f"{name} is empty."
        if offset > len(lines):
            raise ToolError(f"offset {offset} is past the end of {name} ({len(lines)} lines)")
        chunk = lines[offset - 1 : offset - 1 + limit]
        out = "\n".join(
            f"{num:6}\t{line[:MAX_LINE_CHARS]}" for num, line in enumerate(chunk, offset)
        )
        last = offset + len(chunk) - 1
        action.summary = f"Read {plural(len(chunk), 'line')}"
        if offset > 1 or last < len(lines):
            out += f"\n\n(Showing lines {offset}-{last} of {len(lines)}; use offset to read more.)"
            action.summary += f" ({offset}-{last} of {len(lines)})"
        return out

    title = f"Read {name}"
    if limit != READ_LINE_LIMIT:
        title += f" (lines {offset}-{offset + limit - 1})"
    elif offset > 1:
        title += f" (from line {offset})"
    action = Action(
        "read",
        name,
        is_inside(coder, abs_path),
        title,
        run,
        path=abs_path,
        name="Read",
        detail=name,
    )
    return action


def list_dir(coder, path="."):
    abs_path = resolve_path(coder, path)
    name = display_path(coder, abs_path)

    def run():
        if not abs_path.is_dir():
            raise ToolError(f"{name} is not a directory")
        entries = sorted(os.scandir(abs_path), key=lambda e: (not e.is_dir(), e.name))
        lines = []
        for entry in entries:
            if entry.name == ".git":
                continue
            lines.append(entry.name + ("/" if entry.is_dir() else ""))
        action.summary = f"Listed {plural(len(lines), 'entry', 'entries')}"
        if not lines:
            return f"{name} is empty."
        if len(lines) > MAX_LIST_ENTRIES:
            more = len(lines) - MAX_LIST_ENTRIES
            lines = lines[:MAX_LIST_ENTRIES] + [f"... and {more} more entries"]
        return "\n".join(lines)

    action = Action(
        "read", name, is_inside(coder, abs_path), f"List {name}", run, name="List", detail=name
    )
    return action


def find_files(coder, pattern, path="."):
    base = resolve_path(coder, path)
    name = display_path(coder, base)
    pattern = pattern.strip()
    if pattern.startswith("./"):
        pattern = pattern[2:]
    regex = glob_to_regex(pattern)

    def run():
        if not base.is_dir():
            raise ToolError(f"{name} is not a directory")
        matches = [
            shown
            for shown, abs_path in list_files(coder, base)
            if regex.fullmatch(abs_path.relative_to(base).as_posix())
        ]
        action.summary = f"Found {plural(len(matches), 'file')}" if matches else "No files found"
        if not matches:
            return f"No files match {pattern} in {name}."
        res = "\n".join(matches[:MAX_MATCHES])
        if len(matches) > MAX_MATCHES:
            res += f"\n... and {len(matches) - MAX_MATCHES} more; use a narrower pattern."
        return res

    in_dir = f" in {name}" if name != "." else ""
    title = f"Glob {pattern}{in_dir}"
    action = Action(
        "read", name, is_inside(coder, base), title, run, name="Glob", detail=pattern + in_dir
    )
    return action


def grep(coder, pattern, path=".", glob=None, ignore_case=False, files_only=False):
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as err:
        raise ToolError(f"invalid regular expression {pattern!r}: {err}")
    base = resolve_path(coder, path)
    name = display_path(coder, base)
    if glob:
        # Like ripgrep: a glob without a slash matches file names at any depth
        glob_regex = glob_to_regex(glob)
        match_name = "/" not in glob

    def run():
        if base.is_file():
            candidates = [(name, base)]
        elif base.is_dir():
            candidates = list_files(coder, base)
        else:
            raise ToolError(f"{name} does not exist")

        results = []
        num_matches = 0
        matched_files = set()
        for shown, abs_path in candidates:
            if glob:
                subject = abs_path.name if match_name else abs_path.relative_to(base).as_posix()
                if not glob_regex.fullmatch(subject):
                    continue
            try:
                if abs_path.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                data = abs_path.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            text = data.decode(coder.io.encoding, errors="replace")
            for num, line in enumerate(text.splitlines(), 1):
                if not regex.search(line):
                    continue
                num_matches += 1
                matched_files.add(shown)
                if files_only:
                    results.append(shown)
                    break
                results.append(f"{shown}:{num}: {line.strip()[:300]}")
                if len(results) >= MAX_MATCHES:
                    break
            if len(results) >= MAX_MATCHES:
                break

        if not results:
            action.summary = "No matches"
            return f"No matches for {pattern!r} in {name}."
        if files_only:
            action.summary = f"Found {plural(len(results), 'file')}"
        else:
            action.summary = f"Found {plural(num_matches, 'match', 'matches')}"
            action.summary += f" in {plural(len(matched_files), 'file')}"
        res = "\n".join(results)
        if len(results) >= MAX_MATCHES:
            res += f"\n... stopped after {MAX_MATCHES} results; narrow the search."
            action.summary += f" (first {MAX_MATCHES})"
        return res

    title = f"Grep {pattern!r}" + (f" in {name}" if name != "." else "")
    detail = json.dumps(pattern) + (f" in {name}" if name != "." else "")
    if glob:
        title += f" ({glob})"
        detail += f", {glob}"
    action = Action("read", name, is_inside(coder, base), title, run, name="Grep", detail=detail)
    return action


def check_editable(coder, abs_path):
    check_ignored(coder, abs_path)
    if str(abs_path) in {str(Path(f).resolve()) for f in coder.abs_read_only_fnames}:
        raise ToolError(f"{display_path(coder, abs_path)} was added to the chat as read-only")
    if abs_path.is_dir():
        raise ToolError(f"{display_path(coder, abs_path)} is a directory")


def edit_file(coder, path, old_string, new_string, replace_all=False):
    abs_path = resolve_path(coder, path)
    check_editable(coder, abs_path)
    name = display_path(coder, abs_path)
    if not old_string:
        raise ToolError("old_string is empty; use write_file to create a file")
    if old_string == new_string:
        raise ToolError("old_string and new_string are the same")

    before = read_text_file(coder, abs_path)
    count = before.count(old_string)
    if count == 0:
        raise ToolError(
            f"old_string was not found in {name}. Read the file and copy the text exactly,"
            " including whitespace and indentation."
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string appears {count} times in {name}. Include more surrounding lines to"
            " make it unique, or set replace_all to replace every occurrence."
        )
    after = (
        before.replace(old_string, new_string)
        if replace_all
        else before.replace(old_string, new_string, 1)
    )

    def run():
        # The file may have changed while the user was deciding
        if read_text_file(coder, abs_path) != before:
            raise ToolError(f"{name} changed since the edit was prepared; read it again")
        write_file_text(coder, abs_path, after)
        n = count if replace_all else 1
        res = f"Edited {name}: replaced {n} occurrence{'s' if n > 1 else ''}."
        return dry_run_note(coder) + res

    changes = describe_changes(*count_changes(before, after))
    return Action(
        "edit",
        name,
        is_inside(coder, abs_path),
        f"Edit {name}",
        run,
        preview=make_diff(coder, abs_path, before, after),
        path=abs_path,
        name="Update",
        detail=name,
        summary=f"Updated {name} with {changes}",
        changes=f"{name}: {changes}",
    )


def write_file(coder, path, content):
    abs_path = resolve_path(coder, path)
    check_editable(coder, abs_path)
    name = display_path(coder, abs_path)
    exists = abs_path.exists()
    before = read_text_file(coder, abs_path) if exists else ""
    if exists and before == content:
        raise ToolError(f"{name} already has exactly this content")

    def run():
        write_file_text(coder, abs_path, content)
        num_lines = len(content.splitlines())
        verb = "Overwrote" if exists else "Created"
        return dry_run_note(coder) + f"{verb} {name} ({num_lines} lines)."

    num_lines = len(content.splitlines())
    if exists:
        changes = describe_changes(*count_changes(before, content))
        summary = f"Rewrote {name} with {changes}"
    else:
        changes = f"new file, {plural(num_lines, 'line')}"
        summary = f"Wrote {plural(num_lines, 'line')} to {name}"
    return Action(
        "edit",
        name,
        is_inside(coder, abs_path),
        f"{'Overwrite' if exists else 'Create'} {name}",
        run,
        preview=make_diff(coder, abs_path, before, content),
        path=abs_path,
        new_file=not exists,
        name="Write",
        detail=name,
        summary=summary,
        changes=f"{name}: {changes}",
    )


def kill_process_tree(proc):
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def scrub_env(env=None):
    """A copy of the environment without API keys, tokens, passwords and other secrets."""
    env = os.environ if env is None else env
    return {name: value for name, value in env.items() if not SECRET_ENV_RE.search(name)}


def finish_killed(proc, grace=None):
    """The output of a process after killing it. A process it started in a new session
    survives the kill and can keep the pipes open, so wait at most grace seconds and then
    close them. Returns (stdout, stderr) as bytes, with whatever was read."""
    grace = KILL_GRACE if grace is None else grace
    try:
        return proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired as err:
        out, errout = err.output, err.stderr
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    return out or b"", errout or b""


# The commands running now, {thread id: process}, so parallel builders' commands can be
# killed from the main thread
RUNNING = {}
RUNNING_LOCK = threading.Lock()


def kill_running(thread_ids):
    """Kill the commands that the threads with thread_ids are running, and everything they
    started."""
    with RUNNING_LOCK:
        procs = [proc for ident, proc in RUNNING.items() if ident in thread_ids]
    for proc in procs:
        kill_process_tree(proc)
    return len(procs)


def run_command(command, cwd, timeout):
    """Run a shell command without stdin. Returns (exit code or None on timeout, output)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # Own process group, so a timeout or ^C can kill everything the command started
        kwargs["start_new_session"] = True

    env = dict(scrub_env(), PAGER="cat", GIT_PAGER="cat")
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    ident = threading.get_ident()
    with RUNNING_LOCK:
        RUNNING[ident] = proc
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        out, _ = finish_killed(proc)
        code = None
    except KeyboardInterrupt:
        kill_process_tree(proc)
        finish_killed(proc)
        raise
    finally:
        with RUNNING_LOCK:
            RUNNING.pop(ident, None)
    return code, out.decode("utf-8", errors="replace")


def bash(coder, command, timeout=BASH_TIMEOUT):
    if not command.strip():
        raise ToolError("command is empty")
    timeout = min(max(1, timeout), MAX_BASH_TIMEOUT)
    command = command.strip()

    def run():
        code, output = run_command(command, coder.root, timeout)
        if code is None:
            status = f"Timed out after {timeout} seconds."
        else:
            status = f"Exit code: {code}"
        return f"{status}\n{truncate(output.rstrip()) or '(no output)'}"

    return Action(
        "bash",
        command,
        True,
        f"Run {command}",
        run,
        extra=dict(timeout=timeout),
        name="Bash",
        detail=command,
    )


# The to-do list

TODO_STATUSES = ("pending", "in_progress", "completed")


def parse_todos(todos):
    """Check the model's to-do list, allowing near misses like a JSON string or plain strings
    for items."""
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except ValueError:
            raise ToolError("todos must be a list of {content, status} objects")
    if not isinstance(todos, list):
        raise ToolError("todos must be a list of {content, status} objects")

    res = []
    for item in todos:
        if isinstance(item, str):
            item = dict(content=item)
        if not isinstance(item, dict) or not str(item.get("content") or "").strip():
            raise ToolError("each todo needs some content, like {content: 'Run the tests'}")
        status = str(item.get("status") or "pending").strip().lower()
        status = status.replace("-", "_").replace(" ", "_")
        if status not in TODO_STATUSES:
            raise ToolError(f"status must be one of {', '.join(TODO_STATUSES)}, not {status!r}")
        todo = dict(content=str(item["content"]).strip(), status=status)
        active_form = item.get("active_form") or item.get("activeForm")
        if active_form:
            todo["active_form"] = str(active_form).strip()
        res.append(todo)
    return res


def todo_write(coder, todos):
    todos = parse_todos(todos)

    def run():
        coder.todos[:] = todos
        done = sum(1 for todo in todos if todo["status"] == "completed")
        res = f"The to-do list is updated: {done} of {len(todos)} done."
        in_progress = [todo["content"] for todo in todos if todo["status"] == "in_progress"]
        if in_progress:
            res += f" In progress: {'; '.join(in_progress)}."
        elif done < len(todos):
            res += " Mark the next item in_progress when you start it."
        return res

    return Action("todo", "todos", True, "Update the to-do list", run, name="Update Todos")


# Project memory (loom/memory.py), for the phase agents of a /project

MAX_RECALL = 20


def shared_memory(coder):
    memory = getattr(coder, "shared_memory", None)
    if memory is None:
        raise ToolError("there is no project memory: only the agents of a /project have one")
    return memory


def format_hits(hits):
    """Search results from the project memory, as the model sees them."""
    from loom.phases import PHASES_BY_KEY

    parts = []
    for num, hit in enumerate(hits, 1):
        phase = PHASES_BY_KEY.get(hit.phase)
        where = phase.title if phase else "Project"
        if hit.kind == "decision":
            head = f"[{num}] Decision ({where}, by the {hit.source})"
        elif hit.kind == "idea":
            head = f"[{num}] The project idea"
        else:
            head = f"[{num}] {where}: {phase.document_title if phase else hit.source}"
            head += f" ({hit.source})"
            if hit.title:
                head += f" > {hit.title}"
        parts.append(f"{head}\n{hit.text}")
    return "\n\n".join(parts)


def recall(coder, query, phase=None, limit=5):
    memory = shared_memory(coder)
    if not query.strip():
        raise ToolError("query must say what to look for")
    phases = None
    if phase:
        from loom.phases import get_phase

        try:
            phases = [get_phase(phase).key]
        except KeyError:
            raise ToolError(f"there is no phase {phase!r}")
    limit = max(1, min(limit, MAX_RECALL))

    def run():
        hits = memory.search(query, limit, phases)
        action.summary = f"Found {plural(len(hits), 'passage')}"
        if not hits:
            return "Nothing in the project memory matches that."
        return truncate(format_hits(hits))

    action = Action(
        "memory",
        "project memory",
        True,
        f"Search the project memory for {query}",
        run,
        name="Recall",
        detail=query,
    )
    return action


def record_decision(coder, decision, reason=""):
    memory = shared_memory(coder)
    phase = getattr(coder, "phase", None)
    if not decision.strip():
        raise ToolError("decision must say what was decided")

    def run():
        saved = memory.record_decision(
            phase.key if phase else None,
            decision,
            reason,
            source=phase.agent if phase else "agent",
        )
        action.summary = f"Recorded decision #{saved['id']}"
        return (
            f"Recorded decision #{saved['id']}. The agents of later phases and the founder"
            " will see it."
        )

    shown = " ".join(decision.split())
    if len(shown) > 60:
        shown = shown[:59] + "…"
    action = Action(
        "memory",
        "project memory",
        True,
        "Record a decision",
        run,
        name="Record Decision",
        detail=shown,
    )
    return action


def tool(name, prepare, description, properties, required):
    return dict(
        name=name,
        prepare=prepare,
        schema=dict(
            type="function",
            function=dict(
                name=name,
                description=description,
                parameters=dict(type="object", properties=properties, required=required),
            ),
        ),
    )


PATH = dict(type="string", description="File path, relative to the project root.")

TOOLS = {
    t["name"]: t
    for t in [
        tool(
            "read_file",
            read_file,
            (
                "Read a text file. Returns numbered lines (the numbers are not part of the file)."
                f" Reads up to {READ_LINE_LIMIT} lines; use offset and limit for long files."
            ),
            dict(
                path=PATH,
                offset=dict(type="integer", description="Line number to start at (1-based)."),
                limit=dict(type="integer", description="Maximum number of lines to read."),
            ),
            ["path"],
        ),
        tool(
            "list_dir",
            list_dir,
            "List the files and subdirectories of a directory. Directories end with /.",
            dict(path=dict(type="string", description="Directory, relative to the project root.")),
            [],
        ),
        tool(
            "glob",
            find_files,
            (
                "Find files by name. Patterns are relative to path: * matches within a directory,"
                " ** across directories and {a,b} either, like **/*.py or src/**/test_*.{js,ts}."
                " Skips git-ignored files."
            ),
            dict(
                pattern=dict(type="string", description="Glob pattern."),
                path=dict(type="string", description="Directory to search (default: root)."),
            ),
            ["pattern"],
        ),
        tool(
            "grep",
            grep,
            (
                "Search file contents with a Python regular expression. Returns path:line: text"
                " for each matching line. Skips git-ignored and binary files."
            ),
            dict(
                pattern=dict(type="string", description="Regular expression to search for."),
                path=dict(
                    type="string", description="File or directory to search (default: root)."
                ),
                glob=dict(
                    type="string", description="Only search files matching this glob, like *.py."
                ),
                ignore_case=dict(type="boolean", description="Case-insensitive search."),
                files_only=dict(type="boolean", description="Return only the matching file names."),
            ),
            ["pattern"],
        ),
        tool(
            "edit_file",
            edit_file,
            (
                "Replace an exact string in an existing file. old_string must match the file"
                " exactly, including indentation, and must be unique unless replace_all is set."
                " Read the file first. The user may be asked to approve the edit."
            ),
            dict(
                path=PATH,
                old_string=dict(type="string", description="Exact text to replace."),
                new_string=dict(type="string", description="Replacement text."),
                replace_all=dict(type="boolean", description="Replace every occurrence."),
            ),
            ["path", "old_string", "new_string"],
        ),
        tool(
            "write_file",
            write_file,
            (
                "Create a file, or completely replace an existing one, with the given content."
                " Prefer edit_file for changing existing files. The user may be asked to"
                " approve it."
            ),
            dict(path=PATH, content=dict(type="string", description="The full file content.")),
            ["path", "content"],
        ),
        tool(
            "bash",
            bash,
            (
                "Run a shell command in the project root and return its exit code and output"
                " (stdout and stderr combined). There is no stdin, so interactive commands fail."
                f" Times out after {BASH_TIMEOUT} seconds by default. Use it for tests, builds,"
                " linters and git; use the other tools to read, search and edit files. The user"
                " may be asked to approve the command."
            ),
            dict(
                command=dict(type="string", description="The command to run."),
                timeout=dict(
                    type="integer",
                    description=(
                        f"Timeout in seconds (default {BASH_TIMEOUT}, max {MAX_BASH_TIMEOUT})."
                    ),
                ),
            ),
            ["command"],
        ),
        tool(
            "todo_write",
            todo_write,
            (
                "Write your to-do list for the current request, which the user sees. Send the"
                " whole list every time. Use it for work with several steps: write the list"
                " before you start, keep one item in_progress at a time, and mark each item"
                " completed as soon as it's done. Skip it for simple requests."
            ),
            dict(
                todos=dict(
                    type="array",
                    description="The whole list, in order.",
                    items=dict(
                        type="object",
                        properties=dict(
                            content=dict(
                                type="string",
                                description="What to do, like 'Run the tests'.",
                            ),
                            status=dict(type="string", enum=list(TODO_STATUSES)),
                            active_form=dict(
                                type="string",
                                description="Shown while in progress, like 'Running the tests'.",
                            ),
                        ),
                        required=["content", "status"],
                    ),
                )
            ),
            ["todos"],
        ),
    ]
}


# Only the phase agents of a /project have these, with their project's memory
PROJECT_TOOLS = {
    t["name"]: t
    for t in [
        tool(
            "recall",
            recall,
            (
                "Search the project's shared memory: the documents of the earlier phases (idea"
                " report, PRD, architecture, build summary, test report, deployment) and the"
                " decisions the founder and the agents made. Returns the most relevant"
                " passages. Use it to check what was decided before you decide something."
            ),
            dict(
                query=dict(type="string", description="What to look for, in plain words."),
                phase=dict(
                    type="string",
                    description="Only search this phase, like planning or design.",
                ),
                limit=dict(
                    type="integer",
                    description=f"How many passages to return (default 5, max {MAX_RECALL}).",
                ),
            ),
            ["query"],
        ),
        tool(
            "record_decision",
            record_decision,
            (
                "Record a significant decision in the project's shared memory, like a"
                " technology choice, a scope cut or a trade-off, so the agents of later phases"
                " and the founder see it. One decision per call."
            ),
            dict(
                decision=dict(type="string", description="What was decided, in one sentence."),
                reason=dict(type="string", description="Why, briefly."),
            ),
            ["decision"],
        ),
    ]
}

ALL_TOOLS = {**TOOLS, **PROJECT_TOOLS}


def schemas():
    return [t["schema"] for t in TOOLS.values()]


def project_schemas():
    return [t["schema"] for t in PROJECT_TOOLS.values()]


DISPLAY_NAMES = dict(
    read_file="Read",
    list_dir="List",
    glob="Glob",
    grep="Grep",
    edit_file="Update",
    write_file="Write",
    bash="Bash",
    todo_write="Update Todos",
    recall="Recall",
    record_decision="Record Decision",
)


def display_name(name):
    """How a tool call is shown to the user, like Read for read_file."""
    parts = name.split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp":
        return f"{parts[1]} - {parts[2]} (MCP)"
    return DISPLAY_NAMES.get(name, name)


def coerce_args(name, args):
    """Check the model's arguments against the tool's schema, converting near misses like
    "10" for an integer. Unknown arguments are dropped."""
    parameters = ALL_TOOLS[name]["schema"]["function"]["parameters"]
    properties = parameters["properties"]
    missing = [arg for arg in parameters["required"] if arg not in args]
    if missing:
        raise ToolError(f"missing required argument: {', '.join(missing)}")

    res = {}
    for arg, value in args.items():
        if arg not in properties or value is None:
            continue
        kind = properties[arg]["type"]
        if kind == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ToolError(f"{arg} must be an integer, got {value!r}")
        elif kind == "boolean" and isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes")
        elif kind == "string" and not isinstance(value, str):
            value = json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
        res[arg] = value
    return res


def describe_mcp_args(args):
    """Arguments shown after an MCP tool's name, like owner: "me", repo: "loom"."""
    parts = []
    for key, value in args.items():
        shown = json.dumps(value, ensure_ascii=False)
        if len(shown) > 40:
            shown = shown[:39] + "…"
        parts.append(f"{key}: {shown}")
    return ", ".join(parts)


def mcp_tool(coder, server, tool, args):
    from loom.mcp import McpError, format_result, mget

    tool_name = tool["name"]
    target = f"{server.name}__{tool_name}"
    preview = json.dumps(args, indent=2, ensure_ascii=False)
    if len(preview) > 3000:
        preview = preview[:3000] + "\n..."

    def run():
        try:
            result = server.call_tool(tool_name, args)
        except McpError as err:
            raise ToolError(str(err))
        text, is_error = format_result(result)
        if is_error:
            raise ToolError(truncate(text))
        return truncate(text)

    return Action(
        "mcp",
        target,
        True,
        f"Use {server.name} MCP tool {tool_name}",
        run,
        preview=preview,
        extra=dict(
            read_only=mget(tool.get("annotations"), "readOnlyHint") is True,
            server=server,
            tool=tool,
        ),
        name=f"{server.name} - {mget(tool, 'title') or tool_name} (MCP)",
        detail=describe_mcp_args(args),
    )


def prepare(coder, name, args):
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    if name in TOOLS or (name in PROJECT_TOOLS and getattr(coder, "shared_memory", None)):
        return ALL_TOOLS[name]["prepare"](coder, **coerce_args(name, args))

    mcp = getattr(coder, "mcp", None)
    found = mcp.find(name) if mcp else None
    if found:
        return mcp_tool(coder, *found, args)
    raise ToolError(f"there is no tool named {name!r}; available tools: {', '.join(TOOLS)}")

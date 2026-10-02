"""The code changes the web UI's changes pane shows: every file that differs from a base
commit, with its diff, like Claude Code's diff view.

The base is the commit loom was at when the browser started talking to it, so the pane
shows the session's work even though loom commits each change, or where the branch left
the main branch.
"""

import re

from .webio import parse_diff

# Untracked files bigger than this show as changed without their text
MAX_NEW_FILE_BYTES = 200_000
MAIN_BRANCHES = ("main", "master")


def branch_base(git):
    """The commit where the current branch left the main branch, and the main branch's
    name, or (None, None) if the repo has neither main nor master."""
    for name in MAIN_BRANCHES:
        try:
            git.git.rev_parse("--verify", "--quiet", f"refs/heads/{name}")
        except Exception:
            continue
        try:
            return git.git.merge_base(name, "HEAD").strip(), name
        except Exception:
            return None, None
    return None, None


def split_files(diff):
    """A multi-file git diff as one diff per file."""
    return [part for part in re.split(r"(?m)^(?=diff --git )", diff) if part.strip()]


def describe(part):
    """The file a one-file git diff is about, and how it changed."""
    header = part.split("\n@@", 1)[0]
    status = "modified"
    if "\nnew file mode" in header:
        status = "added"
    elif "\ndeleted file mode" in header:
        status = "deleted"
    elif "\nrename from " in header:
        status = "renamed"
    binary = "\nBinary files " in header or header.startswith("Binary files ")

    match = re.match(r"diff --git a/(.*) b/(.*)", header)
    old_path, path = (match.group(1), match.group(2)) if match else ("", "")
    for line in header.splitlines():
        if line.startswith("rename from "):
            old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            path = line[len("rename to ") :]
    if status == "deleted":
        path = old_path
    return path, old_path if status == "renamed" else None, status, binary


def file_change(part):
    path, old_path, status, binary = describe(part)
    _, lines = parse_diff(part)
    return dict(
        path=path,
        old_path=old_path,
        status=status,
        binary=binary,
        added=sum(1 for line in lines if line["kind"] == "add"),
        removed=sum(1 for line in lines if line["kind"] == "del"),
        lines=lines,
    )


def new_file_change(root, path):
    """An untracked file, as a change that adds all of it."""
    full = root / path
    try:
        data = full.read_bytes() if full.stat().st_size <= MAX_NEW_FILE_BYTES else None
    except OSError:
        return None
    binary = data is None or b"\0" in data[:8000]
    lines = []
    if not binary:
        text = data.decode("utf-8", errors="replace").splitlines()
        lines = [dict(kind="add", old=None, new=i, text=t) for i, t in enumerate(text, 1)]
    return dict(
        path=path,
        old_path=None,
        status="added",
        binary=binary,
        added=len(lines),
        removed=0,
        lines=lines,
    )


def changes(git, root, base):
    """Every file in the working tree of git (a GitPython Repo) that differs from the
    commit base, untracked files included, with its diff."""
    diff = git.git.diff(base, "-M", "--no-color", "--no-ext-diff", "--", ".")
    found = [file_change(part) for part in split_files(diff)]
    untracked = git.git.ls_files("--others", "--exclude-standard").splitlines()
    for path in untracked:
        change = new_file_change(root, path)
        if change:
            found.append(change)
    return sorted(found, key=lambda change: change["path"])

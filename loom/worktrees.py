"""
Git worktrees for parallel builders (loom/parallel.py): each work package is built in its
own checkout of the repo, .loom/worktrees/<id>, on its own branch, loom/build/<id>, and
merged back into the project's branch when it's done.

.loom/worktrees/ ignores itself (like .loom/memory/), so the builders' checkouts never
show up in the project's git status.
"""

import os
import shutil
import stat
from pathlib import Path

from git import GitCommandError

from loom.repo import ANY_GIT_ERROR

WORKTREES_DIR = ".loom/worktrees"
BRANCH_PREFIX = "loom/build/"


class WorktreeError(Exception):
    pass


def worktree_path(root, package_id):
    return Path(root) / WORKTREES_DIR / package_id


def branch_name(package_id):
    return BRANCH_PREFIX + package_id


def make_worktrees_dir(root):
    folder = Path(root) / WORKTREES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_bytes(b"*\n")
    return folder


def create(git, root, package_id, base):
    """A new worktree for the package, on its branch at the commit base, in place of any
    old one. Returns its path."""
    path = worktree_path(root, package_id)
    remove(git, root, package_id)
    make_worktrees_dir(root)
    try:
        git.git.worktree("add", "-f", "-B", branch_name(package_id), str(path), base)
    except ANY_GIT_ERROR as err:
        raise WorktreeError(f"Unable to make a worktree for {package_id}: {error_text(err)}")
    return path


def merge(git, branch, message):
    """Merge branch into the checkout git is (the project's). Returns the files that
    conflict, or [] when it merged. A conflicted merge is left in progress, for someone to
    resolve and finish_merge(), or abort_merge()."""
    try:
        git.git.merge("--no-ff", "--no-edit", "-m", message, branch)
        return []
    except GitCommandError as err:
        found = conflicts(git)
        if not found:
            if merging(git):
                abort_merge(git)
            raise WorktreeError(f"Unable to merge {branch}: {error_text(err)}")
        return found


def conflicts(git):
    """The files with merge conflicts in git's checkout."""
    try:
        return git.git.diff("--name-only", "--diff-filter=U").splitlines()
    except ANY_GIT_ERROR:
        return []


def merging(git):
    return (Path(git.git_dir) / "MERGE_HEAD").exists()


def abort_merge(git):
    try:
        git.git.merge("--abort")
    except ANY_GIT_ERROR:
        pass


def finish_merge(git, paths):
    """Commit the merge in progress, once paths (its conflicts) are resolved."""
    try:
        if paths:
            git.git.add("--", *paths)
        git.git.commit("--no-edit", "--no-verify")
    except ANY_GIT_ERROR as err:
        raise WorktreeError(f"Unable to finish the merge: {error_text(err)}")


def remove(git, root, package_id, branch=True):
    """Remove the package's worktree and, if branch, its branch."""
    path = worktree_path(root, package_id)
    if path.exists():
        try:
            git.git.worktree("remove", "--force", str(path))
        except ANY_GIT_ERROR:
            # Like on Windows when a file in it is still open: delete what can be
            shutil.rmtree(path, onerror=make_writable)
    try:
        git.git.worktree("prune")
    except ANY_GIT_ERROR:
        pass
    if branch:
        try:
            git.git.branch("-D", branch_name(package_id))
        except ANY_GIT_ERROR:
            pass


def cleanup_all(git, root):
    """Remove every builder's worktree and branch. Returns how many worktrees there were."""
    folder = Path(root) / WORKTREES_DIR
    ids = [path.name for path in folder.iterdir() if path.is_dir()] if folder.is_dir() else []
    for package_id in ids:
        remove(git, root, package_id, branch=False)
    try:
        branches = git.git.branch("--list", BRANCH_PREFIX + "*", "--format=%(refname:short)")
    except ANY_GIT_ERROR:
        branches = ""
    for branch in branches.splitlines():
        try:
            git.git.branch("-D", branch.strip())
        except ANY_GIT_ERROR:
            pass
    return len(ids)


def make_writable(func, path, info):
    """shutil.rmtree's onerror: read-only files, like git's objects on Windows, are made
    writable and deleted again."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def error_text(err):
    text = getattr(err, "stderr", None) or str(err)
    return " ".join(str(text).strip().split())

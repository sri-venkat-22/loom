"""
Checkpoints: before each request the agent handles, loom takes a snapshot of the project's
files and remembers where the conversation was, so /rewind can put the code, the
conversation or both back to how they were before any earlier request.

With git, a snapshot is a commit of the whole working tree: tracked files, changes the
agent's shell commands made, and untracked files .gitignore doesn't ignore. It's made
with loom's own index file (GIT_INDEX_FILE), so the user's index, HEAD, branches, stash
and `git status` don't change. A session's snapshots form a chain under the private ref
refs/loom/checkpoints/<session id>, which keeps them from being garbage-collected. Git
stores each version of a file once, so a snapshot only costs the files that changed, and
one of an unchanged tree reuses the previous snapshot.

Without git (--no-git), loom copies each file the agent is about to edit or write into
.loom/checkpoints/<checkpoint id>/, once per checkpoint. That can't cover files shell
commands change.

The checkpoints' details live in the session (sessions.py), so they survive --resume:
{id, time, prompt, kind, tree, commit, head, messages_len, messages_tail, todos}.

Restoring computes what differs between the snapshot and the files now: it writes the
changed files, deletes the files created since and recreates the deleted ones, only
inside the project, never in .git and never over files .gitignore ignores.
"""

import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from git.refs.reference import Reference

REF_PREFIX = "refs/loom/checkpoints/"
# loom's own index files, in the repo's .git folder
INDEX_FILE = "loom-checkpoint-index"
# Without git, the copies of the files the agent edits
BACKUP_DIR = ".loom/checkpoints"
MANIFEST = "files.json"
# Checkpoints kept per session; older ones are dropped
MAX_CHECKPOINTS = 100
# The longest prompt kept, which a conversation rewind puts back in the input
MAX_PROMPT_CHARS = 10_000
# loom's own files are never snapshotted or restored: its sessions, history, config and
# .loom/ (hooks, commands, plans, memory). Glob pathspecs, which git add accepts even when
# .gitignore ignores what they name
LOOM_FILES = (":(glob)**/.loom*", ":(glob)**/.loom*/**")
EXCLUDE = tuple(":(exclude," + spec[2:] for spec in LOOM_FILES)
# Snapshots keep files' bytes as they are and restores write them back the same, whatever
# the repo's line-ending settings: otherwise a file could come back with other line endings,
# which git status then reports as changed
RAW = ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false"]
# Git's empty tree, as the source of attributes: none, so no text, eol or filter
# conversions (--attr-source needs git 2.40)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# Git operations that leave the repo half-way, when a rewind would make a mess
BUSY_STATES = {
    "MERGE_HEAD": "a merge",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
}


class CheckpointError(Exception):
    pass


def now():
    return datetime.now().isoformat(timespec="seconds")


def new_checkpoint_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def first_line(text, limit=80):
    line = (text or "").strip().split("\n", 1)[0]
    return line[:limit] + ("…" if len(line) > limit else "")


def message_fingerprint(message):
    """A short hash of a chat message, to check the conversation still has it. The same
    before and after the session is saved and loaded."""
    from loom.sessions import jsonable

    data = json.dumps(jsonable(message), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:12]


@dataclass
class Changes:
    """How the files now differ from a checkpoint's, as paths relative to the project:
    changed in both, created since (a rewind deletes them) and deleted since (a rewind
    recreates them)."""

    changed: list = field(default_factory=list)
    created: list = field(default_factory=list)
    deleted: list = field(default_factory=list)

    @property
    def paths(self):
        return sorted(self.changed + self.created + self.deleted)

    def __bool__(self):
        return bool(self.changed or self.created or self.deleted)

    def __len__(self):
        return len(self.changed) + len(self.created) + len(self.deleted)

    def describe(self):
        """Like: 2 files changed, 1 created since, 1 deleted since."""
        parts = []
        if self.changed:
            parts.append(f"{plural(len(self.changed), 'file')} changed")
        if self.created:
            parts.append(f"{len(self.created)} created since")
        if self.deleted:
            parts.append(f"{len(self.deleted)} deleted since")
        return ", ".join(parts) or "no files changed"

    def short(self):
        """Like: 1 changed, 2 created, 1 deleted."""
        parts = [
            f"{len(paths)} {what}"
            for paths, what in (
                (self.changed, "changed"),
                (self.created, "created"),
                (self.deleted, "deleted"),
            )
            if paths
        ]
        return ", ".join(parts) or "no changes"

    def to_dict(self):
        return dict(changed=self.changed, created=self.created, deleted=self.deleted)


def plural(num, word):
    return f"{num} {word}{'' if num == 1 else 's'}"


def busy_state(git_dir):
    """What git operation is half-way in the repo, like "a merge", or None."""
    for name, what in BUSY_STATES.items():
        if (Path(git_dir) / name).exists():
            return what
    return None


class Checkpoints:
    """Takes and restores the checkpoints of the project in root. repo is loom's GitRepo,
    or None without git."""

    def __init__(self, io, root, repo=None):
        self.io = io
        self.repo = repo
        self.root = Path(repo.root if repo else root).resolve()
        # The name and email commit-tree uses when git has none configured
        self.identity = None
        # The options that make git leave files' bytes alone, once worked out
        self.raw = None

    @property
    def uses_git(self):
        return self.repo is not None

    # Git plumbing

    @property
    def git(self):
        return self.repo.repo.git

    def git_dir(self):
        return Path(self.repo.repo.git_dir)

    def index_path(self, name=INDEX_FILE):
        return self.git_dir() / name

    def raw_options(self):
        """git's options for reading and writing files' bytes as they are."""
        if self.raw is None:
            self.raw = list(RAW)
            if self.git_version() >= (2, 40):
                self.raw.append(f"--attr-source={EMPTY_TREE}")
        return self.raw

    def git_version(self):
        try:
            return tuple(self.repo.repo.git.version_info[:2])
        except Exception:
            return (0, 0)

    def with_index(self, index, *args, **kwargs):
        """Run a git command with index as the index file, leaving files' bytes alone."""
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        return self.git.execute(["git", *self.raw_options(), *args], env=env, **kwargs)

    def converts_files(self):
        """Whether git changes files as it reads them in this repo, with core.autocrlf or
        .gitattributes, so the user's index doesn't hold their bytes."""
        if (self.git_config("core.autocrlf") or "false").lower() in ("true", "input"):
            return True
        try:
            attributes = self.git.ls_files("-z", "--", ":(glob)**/.gitattributes")
        except Exception:
            return True
        return bool(attributes) or (self.git_dir() / "info" / "attributes").exists()

    def write_tree(self, index_name=INDEX_FILE):
        """Snapshot the working tree into the index file index_name and return its tree's
        id. The index file is kept between snapshots, so git only rereads changed files."""
        index = self.index_path(index_name)
        if not index.exists():
            real = self.git_dir() / "index"
            if real.exists() and not self.converts_files():
                # Start from the user's index, whose file times save rereading every file
                shutil.copyfile(real, index)
        try:
            self.with_index(index, "add", "-A", "--", ".", *EXCLUDE)
        except Exception:
            # A stale lock or a broken index file from a crash: start afresh, and leave
            # loom's files out in two steps, in case this git won't exclude them as it adds
            for path in (index, index.with_name(index.name + ".lock")):
                try:
                    path.unlink()
                except OSError:
                    pass
            self.with_index(index, "add", "-A", "--", ".")
            self.with_index(
                index, "rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", *LOOM_FILES
            )
        return self.with_index(index, "write-tree").strip()

    def ref(self, session_id):
        return REF_PREFIX + session_id

    def ref_commit(self, session_id):
        # Read in-process, which saves running git for every checkpoint
        try:
            return Reference(self.repo.repo, self.ref(session_id)).commit.hexsha
        except Exception:
            return None

    def head(self):
        try:
            return self.repo.repo.head.commit.hexsha
        except Exception:
            # No commits yet
            return None

    def tree_files(self, tree):
        """{path: (mode, blob id)} of a tree, recursively."""
        out = self.git.ls_tree("-r", "-z", "--full-tree", tree)
        files = {}
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            mode, _kind, blob = meta.split()
            files[path] = (mode, blob)
        return files

    def diff_trees(self, current, target):
        """Changes between the tree current (the files now) and target (a checkpoint's)."""
        changes = Changes()
        if current == target:
            return changes
        out = self.git.diff_tree("-r", "-z", "--no-renames", "--name-status", current, target)
        parts = out.split("\0")
        for status, path in zip(parts[::2], parts[1::2]):
            if status == "A":
                changes.deleted.append(path)
            elif status == "D":
                changes.created.append(path)
            elif status:
                changes.changed.append(path)
        return changes

    # Taking checkpoints

    def take(self, session, prompt, messages, kind="request"):
        """Checkpoint the files and the conversation, messages (done and current), before a
        request. Returns the checkpoint, or None if the snapshot failed."""
        checkpoint = dict(
            id=new_checkpoint_id(),
            time=now(),
            prompt=(prompt or "")[:MAX_PROMPT_CHARS],
            kind=kind,
            tree=None,
            commit=None,
            head=None,
            messages_len=len(messages),
            messages_tail=message_fingerprint(messages[-1]) if messages else None,
            todos=json.loads(json.dumps(session.todos, default=str)),
        )
        if self.uses_git:
            try:
                self.snapshot(session, checkpoint)
            except Exception as err:
                self.io.tool_warning(f"Unable to take a checkpoint for /rewind: {err}")
                return None
        session.checkpoints.append(checkpoint)
        self.prune(session)
        return checkpoint

    def snapshot(self, session, checkpoint):
        tree = self.write_tree()
        parent = self.ref_commit(session.id)
        previous = next((cp for cp in reversed(session.checkpoints) if cp.get("tree")), None)
        if parent and previous and previous["tree"] == tree:
            # Nothing changed since: the previous snapshot is this one too
            commit = previous["commit"]
        else:
            args = [
                "commit-tree",
                tree,
                "-m",
                f"loom checkpoint: {first_line(checkpoint['prompt'])}",
            ]
            if parent:
                args[2:2] = ["-p", parent]
            commit = self.git.execute(["git", *args], env=self.commit_env()).strip()
            self.git.update_ref(self.ref(session.id), commit)
        checkpoint.update(tree=tree, commit=commit, head=self.head())

    def commit_env(self):
        # commit-tree needs a name and email, which a fresh machine may not have configured
        if self.identity is None:
            self.identity = {}
            for var, value in (("NAME", "loom"), ("EMAIL", "loom@localhost")):
                if not self.git_config(f"user.{var.lower()}"):
                    for who in ("AUTHOR", "COMMITTER"):
                        self.identity[f"GIT_{who}_{var}"] = value
        env = dict(os.environ)
        for key, value in self.identity.items():
            env.setdefault(key, value)
        return env

    def git_config(self, key):
        try:
            return self.git.config("--get", key)
        except Exception:
            return None

    def prune(self, session):
        """Keep the newest MAX_CHECKPOINTS of the session."""
        extra = len(session.checkpoints) - MAX_CHECKPOINTS
        if extra <= 0:
            return
        dropped = session.checkpoints[:extra]
        del session.checkpoints[:extra]
        if not self.uses_git:
            for checkpoint in dropped:
                shutil.rmtree(self.backup_dir(checkpoint["id"]), ignore_errors=True)

    # Without git: copies of the files the agent edits

    def backup_dir(self, checkpoint_id):
        return self.root / BACKUP_DIR / checkpoint_id

    def backup(self, session, path):
        """Before the agent edits path, copy it into the latest checkpoint's folder, if it
        isn't there yet. Without git only."""
        if self.uses_git or not session.checkpoints:
            return
        path = Path(path).resolve()
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return
        if rel.split("/", 1)[0].startswith(".loom") or rel.split("/", 1)[0] == ".git":
            return
        checkpoint = session.checkpoints[-1]
        folder = self.backup_dir(checkpoint["id"])
        manifest = self.read_manifest(folder)
        if rel in manifest:
            return
        folder.mkdir(parents=True, exist_ok=True)
        ignore = folder.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_bytes(b"*\n")
        if path.is_file():
            target = folder / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            manifest[rel] = "file"
        else:
            manifest[rel] = "absent"
        (folder / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    def read_manifest(self, folder):
        try:
            return json.loads((Path(folder) / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def backed_up(self, session, checkpoint):
        """{path: (folder, "file" or "absent")}: how each file the agent edited after the
        checkpoint was before its first edit since."""
        res = {}
        later = session.checkpoints[session.checkpoints.index(checkpoint) :]
        for cp in later:
            folder = self.backup_dir(cp["id"])
            for rel, state in self.read_manifest(folder).items():
                res.setdefault(rel, (folder, state))
        return res

    # What changed since a checkpoint

    def changes(self, session, checkpoint, index_name=INDEX_FILE):
        """How the files now differ from checkpoint's. index_name is the index file to
        snapshot the files with, so another thread can use its own."""
        if self.uses_git:
            if not checkpoint.get("tree"):
                raise CheckpointError("this checkpoint has no snapshot of the files")
            changes = self.diff_trees(self.write_tree(index_name), checkpoint["tree"])
            # Files .gitignore ignores now, like ones a pattern added since covers, stay
            ignored = set(self.ignored(changes.paths))
            if ignored:
                changes = Changes(
                    *(
                        [path for path in paths if path not in ignored]
                        for paths in (changes.changed, changes.created, changes.deleted)
                    )
                )
            return changes

        changes = Changes()
        for rel, (folder, state) in sorted(self.backed_up(session, checkpoint).items()):
            path = self.root / rel
            if state == "absent":
                if path.exists():
                    changes.created.append(rel)
            elif not path.exists():
                changes.deleted.append(rel)
            elif path.read_bytes() != (folder / "files" / rel).read_bytes():
                changes.changed.append(rel)
        return changes

    def check_can_restore(self):
        """Raise CheckpointError if restoring the files now would make a mess."""
        if not self.uses_git:
            return
        busy = busy_state(self.git_dir())
        if busy:
            raise CheckpointError(
                f"git is in the middle of {busy}. Finish it or abort it first, then rewind."
            )

    # Restoring

    def restore(self, session, checkpoint, changes=None):
        """Put the files back to how they were at checkpoint. Returns the Changes made."""
        self.check_can_restore()
        if changes is None:
            changes = self.changes(session, checkpoint)
        if not changes:
            return changes
        if self.uses_git:
            self.restore_git(checkpoint, changes)
        else:
            self.restore_copies(session, checkpoint, changes)
        return changes

    def safe_path(self, rel):
        """The absolute path of a file a restore may write or delete, or None: inside the
        project, not in .git and not in loom's own files."""
        path = (self.root / rel).resolve()
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return None
        if not parts or parts[0] == ".git" or parts[0].startswith(".loom"):
            return None
        return path

    def restore_git(self, checkpoint, changes):
        # changes() left out the files .gitignore ignores now
        to_write = [rel for rel in changes.changed + changes.deleted if self.safe_path(rel)]

        for rel in changes.created:
            path = self.safe_path(rel)
            if path is None:
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            self.remove_empty_parents(path)

        if to_write:
            # git writes the files from the snapshot's tree, through its own index, with the
            # repo's line endings and file modes; the user's index isn't touched
            index = self.index_path(INDEX_FILE + "-restore")
            try:
                self.with_index(index, "read-tree", checkpoint["tree"])
                for rel in to_write:
                    path = self.safe_path(rel)
                    # A folder where the file goes, or a file where its folder goes
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    for parent in reversed(Path(rel).parents[:-1]):
                        spot = self.root / parent
                        if spot.is_file() or spot.is_symlink():
                            spot.unlink()
                self.with_index(
                    index,
                    "checkout-index",
                    "-f",
                    "-z",
                    "--stdin",
                    istream=self.paths_input(to_write),
                )
            finally:
                try:
                    index.unlink()
                except OSError:
                    pass

    def paths_input(self, paths):
        """A file of NUL-separated paths, for git's --stdin -z."""
        import tempfile

        tmp = tempfile.TemporaryFile()
        tmp.write("\0".join(paths).encode("utf-8") + b"\0")
        tmp.seek(0)
        return tmp

    def ignored(self, paths):
        """Which of paths .gitignore ignores now. Files git tracks aren't ignored."""
        if not paths:
            return []
        try:
            out = self.git.execute(
                ["git", "check-ignore", "-z", "--stdin"],
                istream=self.paths_input(paths),
                with_extended_output=True,
            )[1]
        except Exception:
            return []
        return [path for path in out.split("\0") if path]

    def remove_empty_parents(self, path):
        """Remove the folders path was in that are empty now, up to the project's root."""
        folder = path.parent
        while folder != self.root and self.root in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                return
            folder = folder.parent

    def restore_copies(self, session, checkpoint, changes):
        backed_up = self.backed_up(session, checkpoint)
        for rel in changes.paths:
            path = self.safe_path(rel)
            if path is None or rel not in backed_up:
                continue
            folder, state = backed_up[rel]
            if state == "absent":
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    self.remove_empty_parents(path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            # Replace the file whole: write a copy beside it, then move it into place
            tmp = path.with_name(path.name + ".loom-restore")
            shutil.copy2(folder / "files" / rel, tmp)
            os.replace(tmp, path)

    # Cleaning up

    def gc(self, sessions_dir, current=None):
        """Drop the checkpoints of sessions that are no longer saved in sessions_dir, but
        not the current session's. Returns how many it dropped: refs with git, checkpoint
        folders without."""
        from loom.sessions import Session, SessionError, session_paths

        live = {current.id} if current else set()
        live_checkpoints = {cp.get("id") for cp in current.checkpoints} if current else set()
        for path in session_paths(sessions_dir) if sessions_dir else []:
            live.add(path.stem)
            try:
                session = Session.load(path)
            except SessionError:
                continue
            live_checkpoints.update(cp.get("id") for cp in session.checkpoints)

        dropped = 0
        if self.uses_git:
            refs = self.git.for_each_ref("--format=%(refname)", REF_PREFIX).split()
            for ref in refs:
                if ref[len(REF_PREFIX) :] not in live:
                    self.git.update_ref("-d", ref)
                    dropped += 1
            return dropped

        folder = self.root / BACKUP_DIR
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_dir() and path.name not in live_checkpoints:
                    shutil.rmtree(path, ignore_errors=True)
                    dropped += 1
        return dropped


# Rewinding a coder: its files, its conversation or both (the /rewind command)

# Checkpoints whose conversation can be rewound: before a request. The others (before a
# /project phase, an agent step or a rewind) only rewind the code
CONVERSATION_KINDS = ("request",)
# Files listed when asking to restore them
MAX_LISTED = 20


def rewinds_conversation(checkpoint):
    return checkpoint.get("kind", "request") in CONVERSATION_KINDS


def describe(checkpoint):
    """How a checkpoint reads in a list: its request, or what it was taken before."""
    prompt = first_line(checkpoint.get("prompt")) or "(empty request)"
    kind = checkpoint.get("kind", "request")
    return prompt if kind in ("request", "phase", "rewind") else f"{prompt} (agent step)"


def conversation_cut(coder, checkpoint):
    """Where to cut the conversation (done and current messages) to go back to before
    checkpoint. Raises CheckpointError when the messages from then were summarized."""
    messages = coder.done_messages + coder.cur_messages
    num = checkpoint.get("messages_len", 0)
    if num <= len(messages):
        if num == 0 or message_fingerprint(messages[num - 1]) == checkpoint.get("messages_tail"):
            return num
    # The history was summarized since: find the request itself
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg.get("role") == "user" and msg.get("content") == checkpoint.get("prompt"):
            return index
    raise CheckpointError(
        "the conversation was summarized since then, so its messages from that point are"
        " gone. Rewind the code only, or start afresh with /clear."
    )


def rewind(coder, checkpoint, code=True, conversation=True, confirm=True):
    """Put coder's files, conversation or both back to how they were at checkpoint.
    Returns whether it did."""
    io = coder.io
    session = coder.session
    cut = None
    if conversation:
        if not rewinds_conversation(checkpoint):
            io.tool_error("This checkpoint only rewinds the code.")
            return False
        try:
            cut = conversation_cut(coder, checkpoint)
        except CheckpointError as err:
            io.tool_error(f"Unable to rewind the conversation: {err}")
            return False

    index = session.checkpoints.index(checkpoint)
    before = None
    if code:
        restored = restore_code(coder, checkpoint, confirm)
        if restored is False:
            return False
        before = restored

    if conversation:
        messages = coder.done_messages + coder.cur_messages
        coder.done_messages = messages[:cut]
        coder.cur_messages = []
        session.todos[:] = checkpoint.get("todos") or []
        # The later requests are gone from the conversation, and so are their checkpoints
        session.checkpoints[:] = session.checkpoints[:index] + ([before] if before else [])
        prompt = checkpoint.get("prompt") or ""
        io.conversation_rewound(coder)
        io.tool_output(f"Rewound the conversation to before: {first_line(prompt)}")
        if prompt.strip():
            io.placeholder = prompt
            io.tool_output("The request is back in the input, to change and send again.")

    coder.save_session()
    io.checkpoints_changed(coder)
    return True


def restore_code(coder, checkpoint, confirm=True):
    """Restore the files of checkpoint, after showing what changes and asking. Returns
    False if it didn't, or the checkpoint of the files as they were before (None when
    nothing changed or it couldn't be taken)."""
    io = coder.io
    checkpoints = coder.checkpoints
    session = coder.session
    try:
        checkpoints.check_can_restore()
        changes = checkpoints.changes(session, checkpoint)
    except CheckpointError as err:
        io.tool_error(f"Unable to rewind the code: {err}")
        return False
    except Exception as err:
        io.tool_error(f"Unable to read the checkpoint: {err}")
        return False

    prompt = first_line(checkpoint.get("prompt"))
    if not changes:
        io.tool_output("The files are already as they were then.")
        return None

    io.tool_output(f"Rewinding the code to before: {prompt}")
    io.tool_output(f"{changes.describe().capitalize()}:")
    lines = [f"  M {rel}" for rel in changes.changed]
    lines += [f"  - {rel}  (created since: deleted)" for rel in changes.created]
    lines += [f"  + {rel}  (deleted since: restored)" for rel in changes.deleted]
    if len(lines) > MAX_LISTED:
        lines = lines[:MAX_LISTED] + [f"  … and {len(lines) - MAX_LISTED} more"]
    for line in lines:
        io.tool_output(line)
    if not checkpoints.uses_git:
        io.tool_warning(
            "Without git, rewind covers the files the agent edited with its tools, not"
            " changes shell commands made."
        )
    if confirm and not io.confirm_ask("Restore these files?"):
        return False

    before = None
    if checkpoints.uses_git:
        # So the rewind can be undone with another /rewind
        before = checkpoints.take(
            session,
            f"(before the rewind to: {prompt})",
            coder.done_messages + coder.cur_messages,
            kind="rewind",
        )
    try:
        checkpoints.restore(session, checkpoint, changes)
    except (CheckpointError, OSError) as err:
        io.tool_error(f"Unable to rewind the code: {err}")
        return False
    except Exception as err:
        io.tool_error(f"Unable to rewind the code: {err}")
        return False
    io.tool_output(f"Restored {plural(len(changes), 'file')}.")
    commit_rewind(coder, checkpoint, changes)
    return before


def commit_rewind(coder, checkpoint, changes):
    """Commit the restored files, when loom commits its changes: a new commit, so history
    is never rewritten."""
    repo = coder.repo
    if not (repo and coder.auto_commits and not coder.dry_run):
        return
    try:
        status = repo.repo.git.status("--porcelain", "-z", "--", *changes.paths)
    except Exception:
        return
    paths = [entry[3:] for entry in status.split("\0") if len(entry) > 3]
    paths = [path for path in paths if path in set(changes.paths)]
    if not paths:
        return
    message = f"Rewind to before: {first_line(checkpoint.get('prompt'), 60)}"
    res = repo.commit(fnames=paths, message=message, loom_edits=True, coder=coder)
    if res:
        coder.show_auto_commit_outcome(res)

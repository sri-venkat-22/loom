import os
import unittest
from pathlib import Path
from unittest.mock import patch

import git

from loom.checkpoints import (
    BACKUP_DIR,
    MAX_CHECKPOINTS,
    REF_PREFIX,
    CheckpointError,
    Checkpoints,
    busy_state,
)
from loom.io import InputOutput
from loom.repo import GitRepo
from loom.sessions import Session
from loom.utils import ChdirTemporaryDirectory, GitTemporaryDirectory


def write(path, data):
    """Write bytes, so the tests see the same files on Windows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def read(path):
    """A file's text, with \r\n read as \n."""
    return Path(path).read_bytes().decode("utf-8").replace("\r\n", "\n")


def make_project():
    """A repo with a commit, a change the user hasn't committed and an untracked file."""
    repo = git.Repo.init()
    with repo.config_writer() as config:
        config.set_value("user", "name", "Tester")
        config.set_value("user", "email", "tester@example.com")
    write("calc.py", "def add(a, b):\n    return a - b\n")
    write("notes/todo.txt", "fix add\n")
    write(".gitignore", "build/\n.loom*\n")
    repo.git.add(".")
    repo.git.commit("-m", "initial")
    write("notes/todo.txt", "fix add\nthen subtract\n")
    write("scratch.py", "print('draft')\n")
    io = InputOutput(pretty=False, yes=True, fancy_input=False)
    return repo, io, GitRepo(io, None, ".")


def git_state(repo):
    """Everything a snapshot must leave alone."""
    return dict(
        status=repo.git.status("--porcelain", "--untracked-files=all"),
        head=repo.git.rev_parse("HEAD"),
        # git status itself refreshes the index's file times, so compare what it stages
        index=repo.git.ls_files("--stage"),
        stash=repo.git.stash("list"),
        log=repo.git.log("--branches", "--tags", "--oneline"),
        head_log=repo.git.log("--oneline"),
        branches=repo.git.branch("-a"),
    )


class TestSnapshots(unittest.TestCase):
    def test_a_snapshot_changes_nothing_the_user_sees(self):
        with GitTemporaryDirectory():
            repo, io, gitrepo = make_project()
            repo.git.stash("push", "--include-untracked", "-m", "keep me")
            repo.git.stash("apply")
            before = git_state(repo)

            session = Session()
            checkpoint = Checkpoints(io, ".", gitrepo).take(session, "fix add", [])

            self.assertEqual(git_state(repo), before)
            self.assertTrue(checkpoint["tree"])
            self.assertEqual(checkpoint["head"], before["head"])
            # The snapshot has the uncommitted change and the untracked file, under a
            # private ref, and not loom's own files
            files = Checkpoints(io, ".", gitrepo).tree_files(checkpoint["tree"])
            self.assertIn("scratch.py", files)
            self.assertIn("notes/todo.txt", files)
            ref = REF_PREFIX + session.id
            self.assertEqual(repo.git.rev_parse(ref), checkpoint["commit"])
            self.assertNotIn(ref, repo.git.branch("-a"))

    def test_the_chain_skips_unchanged_trees(self):
        with GitTemporaryDirectory():
            repo, io, gitrepo = make_project()
            checkpoints = Checkpoints(io, ".", gitrepo)
            session = Session()
            first = checkpoints.take(session, "one", [])
            same = checkpoints.take(session, "two", [dict(role="user", content="one")])
            self.assertEqual(same["commit"], first["commit"])
            write("calc.py", "def add(a, b):\n    return a + b\n")
            third = checkpoints.take(session, "three", [])
            self.assertNotEqual(third["commit"], first["commit"])
            self.assertEqual(repo.git.rev_parse(third["commit"] + "^"), first["commit"])
            self.assertEqual(len(session.checkpoints), 3)
            self.assertEqual(session.checkpoints[1]["messages_len"], 1)

    def test_loom_files_and_ignored_files_are_left_out(self):
        with GitTemporaryDirectory():
            repo, io, gitrepo = make_project()
            write(".loom.chat.history.md", "chat\n")
            write(".loom/hooks.json", "{}\n")
            write("build/out.bin", b"\0\1")
            checkpoints = Checkpoints(io, ".", gitrepo)
            files = checkpoints.tree_files(checkpoints.take(Session(), "x", [])["tree"])
            self.assertFalse([path for path in files if path.startswith((".loom", "build"))])

    def test_older_checkpoints_are_dropped(self):
        with GitTemporaryDirectory():
            repo, io, gitrepo = make_project()
            checkpoints = Checkpoints(io, ".", gitrepo)
            session = Session()
            with patch("loom.checkpoints.MAX_CHECKPOINTS", 4):
                for num in range(7):
                    checkpoints.take(session, f"request {num}", [])
            self.assertEqual(len(session.checkpoints), 4)
            self.assertEqual(session.checkpoints[0]["prompt"], "request 3")
            self.assertEqual(MAX_CHECKPOINTS, 100)


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = GitTemporaryDirectory()
        self.tmp.__enter__()
        self.repo, self.io, gitrepo = make_project()
        self.checkpoints = Checkpoints(self.io, ".", gitrepo)
        self.session = Session()
        self.checkpoint = self.checkpoints.take(self.session, "fix add", [])

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def rewind(self):
        changes = self.checkpoints.changes(self.session, self.checkpoint)
        self.checkpoints.restore(self.session, self.checkpoint, changes)
        return changes

    def test_edits_writes_and_shell_commands_are_undone(self):
        before = self.repo.git.status("--porcelain", "--untracked-files=all")
        index = self.repo.git.ls_files("--stage")
        # An edit_file, a write_file of a new file, a bash rm, a bash-made file and a rename
        write("calc.py", "def add(a, b):\n    return a + b\n")
        write("src/new_module.py", "x = 1\n")
        os.remove("notes/todo.txt")
        write("generated/report.txt", "made by a command\n")
        os.rename("scratch.py", "scratch_renamed.py")

        changes = self.rewind()
        self.assertEqual(changes.changed, ["calc.py"])
        self.assertEqual(
            sorted(changes.created),
            ["generated/report.txt", "scratch_renamed.py", "src/new_module.py"],
        )
        self.assertEqual(sorted(changes.deleted), ["notes/todo.txt", "scratch.py"])
        self.assertEqual(changes.describe(), "1 file changed, 3 created since, 2 deleted since")

        self.assertEqual(read("calc.py"), "def add(a, b):\n    return a - b\n")
        self.assertEqual(read("notes/todo.txt"), "fix add\nthen subtract\n")
        self.assertEqual(read("scratch.py"), "print('draft')\n")
        for gone in ("src/new_module.py", "generated/report.txt", "scratch_renamed.py"):
            self.assertFalse(Path(gone).exists(), gone)
        # Emptied folders go too
        self.assertFalse(Path("src").exists())
        self.assertFalse(Path("generated").exists())
        # Back to how git saw it, and the user's index wasn't used
        self.assertEqual(self.repo.git.status("--porcelain", "--untracked-files=all"), before)
        self.assertEqual(self.repo.git.ls_files("--stage"), index)
        # Nothing left to rewind
        self.assertFalse(self.checkpoints.changes(self.session, self.checkpoint))

    def test_ignored_files_are_untouched(self):
        write("build/old.o", "old\n")
        checkpoint = self.checkpoints.take(self.session, "build", [])
        write("build/old.o", "rebuilt\n")
        write("build/new.o", "new\n")
        write(".loom.chat.history.md", "the chat\n")
        changes = self.checkpoints.changes(self.session, checkpoint)
        self.assertFalse(changes)
        self.checkpoints.restore(self.session, checkpoint)
        self.assertEqual(read("build/old.o"), "rebuilt\n")
        self.assertTrue(Path("build/new.o").exists())
        self.assertTrue(Path(".loom.chat.history.md").exists())

    def test_a_file_ignored_since_is_not_overwritten(self):
        # scratch.py isn't tracked: once ignored, it's the user's to keep
        write("scratch.py", "mine\n")
        write("later.log", "made later\n")
        write(".gitignore", "build/\n.loom*\nscratch.py\n*.log\n")
        changes = self.rewind()
        self.assertEqual((changes.changed, changes.created), ([".gitignore"], []))
        self.assertEqual(read("scratch.py"), "mine\n")
        self.assertTrue(Path("later.log").exists())

    def test_line_endings_and_odd_names_survive(self):
        names = ["dir with space/file name.txt", "unicodé/naïve.py", "a/b/c/deep.txt"]
        for name in names:
            write(name, b"one\r\ntwo\r\n")
        checkpoint = self.checkpoints.take(self.session, "odd names", [])
        for name in names:
            os.remove(name)
        changes = self.checkpoints.changes(self.session, checkpoint)
        self.assertEqual(sorted(changes.deleted), sorted(names))
        self.checkpoints.restore(self.session, checkpoint, changes)
        for name in names:
            self.assertEqual(Path(name).read_bytes(), b"one\r\ntwo\r\n", name)

    def test_line_endings_come_back_as_they_were(self):
        # Like Windows runners: git converts line endings as it reads and writes files
        with self.repo.config_writer() as config:
            config.set_value("core", "autocrlf", "true")
        write("dos.txt", b"one\r\ntwo\r\n")
        checkpoints = Checkpoints(self.io, ".", self.checkpoints.repo)
        session = Session()
        checkpoint = checkpoints.take(session, "endings", [])
        status = self.repo.git.status("--porcelain", "--untracked-files=all")
        write("calc.py", "changed\n")
        write("dos.txt", b"changed\r\n")
        checkpoints.restore(session, checkpoint)
        self.assertEqual(Path("calc.py").read_bytes(), b"def add(a, b):\n    return a - b\n")
        self.assertEqual(Path("dos.txt").read_bytes(), b"one\r\ntwo\r\n")
        self.assertEqual(self.repo.git.status("--porcelain", "--untracked-files=all"), status)

    def test_a_file_replaced_by_a_folder(self):
        os.remove("scratch.py")
        write("scratch.py/inner.txt", "a folder now\n")
        self.rewind()
        self.assertEqual(read("scratch.py"), "print('draft')\n")

    def test_a_rewind_during_a_merge_is_refused(self):
        write("calc.py", "changed\n")
        Path(self.repo.git_dir, "MERGE_HEAD").write_bytes(b"0" * 40 + b"\n")
        self.assertEqual(busy_state(self.repo.git_dir), "a merge")
        with self.assertRaises(CheckpointError) as err:
            self.checkpoints.restore(self.session, self.checkpoint)
        self.assertIn("middle of a merge", str(err.exception))
        self.assertEqual(read("calc.py"), "changed\n")

        os.remove(Path(self.repo.git_dir, "MERGE_HEAD"))
        Path(self.repo.git_dir, "rebase-merge").mkdir()
        self.assertEqual(busy_state(self.repo.git_dir), "a rebase")

    def test_the_chain_survives_saving_and_resuming_the_session(self):
        directory = Path(".loom.sessions")
        session = Session(directory)
        checkpoints = self.checkpoints
        checkpoint = checkpoints.take(session, "fix add", [])

        class Coder:
            done_messages = [dict(role="user", content="fix add")]
            cur_messages = []
            abs_read_only_fnames = []
            edit_format = "agent"

            class main_model:
                name = "gpt-4o-mini"

            def get_inchat_relative_files(self):
                return []

        session.save(Coder())
        write("calc.py", "rewritten\n")
        resumed = Session.find(directory, session.id)
        self.assertEqual(resumed.checkpoints, session.checkpoints)
        checkpoints.restore(resumed, resumed.checkpoints[0])
        self.assertEqual(read("calc.py"), "def add(a, b):\n    return a - b\n")
        self.assertEqual(checkpoint["id"], resumed.checkpoints[0]["id"])

    def test_gc_drops_the_checkpoints_of_deleted_sessions(self):
        directory = Path(".loom.sessions")
        kept = Session(directory)
        self.checkpoints.take(kept, "kept", [])
        directory.mkdir()
        write(directory / f"{kept.id}.json", '{"messages": []}')
        gone = Session(directory)
        write("calc.py", "changed\n")
        self.checkpoints.take(gone, "gone", [])

        dropped = self.checkpoints.gc(directory, current=self.session)
        # The deleted session's, and this test's in-memory session stays
        self.assertEqual(dropped, 1)
        refs = self.repo.git.for_each_ref("--format=%(refname)", REF_PREFIX).split()
        self.assertEqual(sorted(refs), sorted([REF_PREFIX + kept.id, REF_PREFIX + self.session.id]))


class TestWithoutGit(unittest.TestCase):
    def test_copies_of_edited_files(self):
        with ChdirTemporaryDirectory() as root:
            write("calc.py", "a - b\n")
            io = InputOutput(pretty=False, yes=True, fancy_input=False)
            checkpoints = Checkpoints(io, root, None)
            session = Session()
            checkpoint = checkpoints.take(session, "fix", [])
            self.assertIsNone(checkpoint["tree"])

            checkpoints.backup(session, Path(root) / "calc.py")
            write("calc.py", "a + b\n")
            checkpoints.backup(session, Path(root) / "new.py")
            write("new.py", "x = 1\n")
            # A second edit of the same file keeps the first copy
            checkpoints.backup(session, Path(root) / "calc.py")
            write("calc.py", "a * b\n")

            later = checkpoints.take(session, "more", [])
            checkpoints.backup(session, Path(root) / "calc.py")
            write("calc.py", "a / b\n")
            self.assertTrue((Path(root) / BACKUP_DIR / later["id"]).is_dir())

            changes = checkpoints.changes(session, checkpoint)
            self.assertEqual((changes.changed, changes.created), (["calc.py"], ["new.py"]))
            checkpoints.restore(session, checkpoint, changes)
            self.assertEqual(read("calc.py"), "a - b\n")
            self.assertFalse(Path("new.py").exists())
            self.assertEqual((Path(root) / BACKUP_DIR / ".gitignore").read_bytes(), b"*\n")

            # gc drops the copies of checkpoints no session has
            self.assertEqual(checkpoints.gc(None, current=Session()), 2)


if __name__ == "__main__":
    unittest.main()

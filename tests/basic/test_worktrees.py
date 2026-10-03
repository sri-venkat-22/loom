import unittest
from pathlib import Path

import git

from loom import worktrees
from loom.utils import GitTemporaryDirectory
from loom.worktrees import WORKTREES_DIR, WorktreeError

from .test_agent import make_repo


def commit_in(path, name, data, message):
    """Write a file in the worktree at path and commit it there."""
    Path(path, name).parent.mkdir(parents=True, exist_ok=True)
    Path(path, name).write_bytes(data)
    tree = git.Repo(path)
    try:
        tree.git.add(name)
        tree.git.commit("-m", message)
        return tree.head.commit.hexsha
    finally:
        # On Windows its git processes would keep the worktree from being removed
        tree.close()


def text(path):
    """A file's text with \n line endings, which git checks out as \r\n on Windows."""
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


class TestWorktrees(unittest.TestCase):
    def test_create_merge_and_remove(self):
        with GitTemporaryDirectory() as root:
            repo = make_repo()
            base = repo.head.commit.hexsha
            path = worktrees.create(repo, root, "core", base)
            self.assertEqual(path, Path(root) / WORKTREES_DIR / "core")
            self.assertTrue((path / "calc.py").exists())
            tree = git.Repo(path)
            self.assertEqual(tree.active_branch.name, "loom/build/core")
            tree.close()

            commit_in(path, "src/core.py", b"def core():\n    return 1\n", "Add the core")
            # The project's checkout doesn't see the worktree
            self.assertFalse(repo.is_dirty(untracked_files=True))

            self.assertEqual(worktrees.merge(repo, "loom/build/core", "Merge core"), [])
            self.assertEqual(text("src/core.py"), b"def core():\n    return 1\n")
            self.assertEqual(repo.head.commit.message.strip(), "Merge core")
            self.assertEqual(len(repo.head.commit.parents), 2)

            worktrees.remove(repo, root, "core")
            self.assertFalse(path.exists())
            self.assertNotIn("loom/build/core", [b.name for b in repo.branches])

    def test_conflicts(self):
        with GitTemporaryDirectory() as root:
            repo = make_repo()
            base = repo.head.commit.hexsha
            first = worktrees.create(repo, root, "one", base)
            second = worktrees.create(repo, root, "two", base)
            commit_in(first, "calc.py", b"def add(a, b):\n    return a + b\n", "Fix add")
            commit_in(second, "calc.py", b"def add(a, b):\n    return b + a\n", "Fix add too")

            self.assertEqual(worktrees.merge(repo, "loom/build/one", "Merge one"), [])
            conflicts = worktrees.merge(repo, "loom/build/two", "Merge two")
            self.assertEqual(conflicts, ["calc.py"])
            self.assertTrue(worktrees.merging(repo))
            self.assertIn("<<<<<<<", Path("calc.py").read_text())

            # Resolved by hand, then finished
            Path("calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
            worktrees.finish_merge(repo, conflicts)
            self.assertFalse(worktrees.merging(repo))
            self.assertEqual(len(repo.head.commit.parents), 2)
            self.assertFalse(repo.is_dirty())

    def test_abort_and_failed_merges(self):
        with GitTemporaryDirectory() as root:
            repo = make_repo()
            base = repo.head.commit.hexsha
            path = worktrees.create(repo, root, "one", base)
            commit_in(path, "calc.py", b"x = 1\n", "Change calc")
            Path("calc.py").write_bytes(b"y = 2\n")
            repo.git.commit("-am", "Change calc here too")
            self.assertEqual(worktrees.merge(repo, "loom/build/one", "Merge one"), ["calc.py"])
            worktrees.abort_merge(repo)
            self.assertFalse(worktrees.merging(repo))
            self.assertEqual(text("calc.py"), b"y = 2\n")

            with self.assertRaises(WorktreeError):
                worktrees.merge(repo, "loom/build/nope", "Merge nothing")

    def test_cleanup_all_and_recreate(self):
        with GitTemporaryDirectory() as root:
            repo = make_repo()
            base = repo.head.commit.hexsha
            old = worktrees.create(repo, root, "core", base)
            commit_in(old, "a.py", b"a = 1\n", "Add a")
            # Making it again starts over from base
            new = worktrees.create(repo, root, "core", base)
            self.assertFalse((new / "a.py").exists())
            worktrees.create(repo, root, "cli", base)
            self.assertEqual(worktrees.cleanup_all(repo, root), 2)
            self.assertEqual(
                [p.name for p in (Path(root) / WORKTREES_DIR).iterdir()], [".gitignore"]
            )
            self.assertEqual([b.name for b in repo.branches if b.name.startswith("loom/")], [])
            self.assertEqual(worktrees.cleanup_all(repo, root), 0)


if __name__ == "__main__":
    unittest.main()

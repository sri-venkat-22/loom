import os
import unittest
from unittest.mock import MagicMock, patch

from loom.dump import dump  # noqa
from loom.linter import Linter


class TestLinter(unittest.TestCase):
    def setUp(self):
        self.linter = Linter(encoding="utf-8", root="/test/root")

    def test_init(self):
        self.assertEqual(self.linter.encoding, "utf-8")
        self.assertEqual(self.linter.root, "/test/root")
        self.assertIn("python", self.linter.languages)

    def test_set_linter(self):
        self.linter.set_linter("javascript", "eslint")
        self.assertEqual(self.linter.languages["javascript"], "eslint")

    def test_get_rel_fname(self):
        import os

        self.assertEqual(self.linter.get_rel_fname("/test/root/file.py"), "file.py")
        expected_path = os.path.normpath("../../other/path/file.py")
        actual_path = os.path.normpath(self.linter.get_rel_fname("/other/path/file.py"))
        self.assertEqual(actual_path, expected_path)

    @patch("subprocess.Popen")
    def test_run_cmd(self, mock_popen):
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout.read.side_effect = ("", None)
        mock_popen.return_value = mock_process

        result = self.linter.run_cmd("test_cmd", "test_file.py", "code")
        self.assertIsNone(result)

    def test_run_cmd_win(self):
        if os.name != "nt":
            self.skipTest("This test only runs on Windows")
        from pathlib import Path

        root = Path(__file__).parent.parent.parent.absolute().as_posix()
        linter = Linter(encoding="utf-8", root=root)
        result = linter.run_cmd("dir", "tests\\basic", "code")
        self.assertIsNone(result)

    @patch("subprocess.Popen")
    def test_run_cmd_with_errors(self, mock_popen):
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout.read.side_effect = ("Error message", None)
        mock_popen.return_value = mock_process

        result = self.linter.run_cmd("test_cmd", "test_file.py", "code")
        self.assertIsNotNone(result)
        self.assertIn("Error message", result.text)

    def test_run_cmd_with_special_chars(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.returncode = 1
            mock_process.stdout.read.side_effect = ("Error message", None)
            mock_popen.return_value = mock_process

            # Test with a file path containing special characters
            special_path = "src/(main)/product/[id]/page.tsx"
            result = self.linter.run_cmd("eslint", special_path, "code")

            # Verify that the command was constructed correctly
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args[0][0]

            self.assertIn(special_path, call_args)

            # The result should contain the error message
            self.assertIsNotNone(result)
            self.assertIn("Error message", result.text)


if __name__ == "__main__":
    unittest.main()


class TestFlake8Isolation(unittest.TestCase):
    def test_a_flake8_package_in_the_project_does_not_run(self):
        from pathlib import Path

        from loom.utils import (
            IgnorantTemporaryDirectory,
            get_pip_install,
            safe_path_flag,
        )

        with IgnorantTemporaryDirectory() as root:
            # python -m flake8 run in the project would import this instead of flake8
            shadow = Path(root) / "flake8"
            shadow.mkdir()
            (shadow / "__init__.py").write_text("")
            (shadow / "__main__.py").write_text("open('SHADOWED', 'w').close()\n")
            (Path(root) / "x.py").write_text("print(undefined_name)\n")

            res = Linter(root=root).flake8_lint("x.py")
            self.assertFalse((Path(root) / "SHADOWED").exists())
            self.assertIn("F821", res.text)

        self.assertEqual(get_pip_install(["x"])[1], safe_path_flag())

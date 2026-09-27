import time
import unittest
from unittest.mock import MagicMock

from requests.exceptions import ConnectionError, ReadTimeout

import loom
from loom.coders import Coder
from loom.commands import Commands
from loom.help import Help, fname_to_url, get_package_files
from loom.io import InputOutput
from loom.models import Model


class TestHelp(unittest.TestCase):
    @staticmethod
    def retry_with_backoff(func, max_time=60, initial_delay=1, backoff_factor=2):
        """
        Execute a function with exponential backoff retry logic.

        Args:
            func: Function to execute
            max_time: Maximum time in seconds to keep retrying
            initial_delay: Initial delay between retries in seconds
            backoff_factor: Multiplier for delay after each retry

        Returns:
            The result of the function if successful

        Raises:
            The last exception encountered if all retries fail
        """
        start_time = time.time()
        delay = initial_delay
        last_exception = None

        while time.time() - start_time < max_time:
            try:
                return func()
            except (ReadTimeout, ConnectionError) as e:
                last_exception = e
                time.sleep(delay)
                delay = min(delay * backoff_factor, 15)  # Cap max delay at 15 seconds

        # If we've exhausted our retry time, raise the last exception
        if last_exception:
            raise last_exception
        raise Exception("Retry timeout exceeded but no exception was caught")

    @classmethod
    def setUpClass(cls):
        io = InputOutput(pretty=False, yes=True)

        GPT35 = Model("gpt-3.5-turbo")

        coder = Coder.create(GPT35, None, io)
        commands = Commands(io, coder)

        help_coder_run = MagicMock(return_value="")
        loom.coders.HelpCoder.run = help_coder_run

        def run_help_command():
            try:
                commands.cmd_help("hi")
            except loom.commands.SwitchCoder:
                pass
            else:
                # If no exception was raised, fail the test
                assert False, "SwitchCoder exception was not raised"

        # Use retry with backoff for the help command that loads models
        cls.retry_with_backoff(run_help_command)

        help_coder_run.assert_called_once()

    def test_init(self):
        help_inst = Help()
        self.assertIsNotNone(help_inst.retriever)

    def test_ask_without_mock(self):
        help_instance = Help()
        question = "What is loom?"
        result = help_instance.ask(question)

        self.assertIn(f"# Question: {question}", result)
        self.assertIn("<doc", result)
        self.assertIn("</doc>", result)
        self.assertGreater(len(result), 100)  # Ensure we got a substantial response

        # Check for some expected content (adjust based on your actual help content)
        self.assertIn("loom", result.lower())
        self.assertIn("ai", result.lower())
        self.assertIn("chat", result.lower())

        # Assert that there are more than 5 <doc> entries
        self.assertGreater(result.count("<doc"), 5)

    def test_get_package_files(self):
        names = {path.name for path in get_package_files()}
        self.assertIn("index.md", names)
        self.assertIn("usage.md", names)
        self.assertIn("commands.md", names)

    def test_fname_to_url_unix(self):
        base = "https://github.com/sri-venkat-22/loom/blob/main/loom/docs"
        self.assertEqual(fname_to_url("loom/docs/index.md"), f"{base}/index.md")
        self.assertEqual(
            fname_to_url("/home/user/.venv/lib/python3.12/site-packages/loom/docs/usage.md"),
            f"{base}/usage.md",
        )
        # Only the last loom/docs pair counts
        self.assertEqual(
            fname_to_url("/home/user/loom/docs/project/loom/docs/git.md"), f"{base}/git.md"
        )

    def test_fname_to_url_windows(self):
        base = "https://github.com/sri-venkat-22/loom/blob/main/loom/docs"
        self.assertEqual(fname_to_url(r"loom\docs\index.md"), f"{base}/index.md")
        self.assertEqual(
            fname_to_url(r"C:\Users\user\venv\Lib\site-packages\loom\docs\usage.md"),
            f"{base}/usage.md",
        )

    def test_fname_to_url_edge_cases(self):
        # Paths that aren't loom docs
        self.assertEqual(fname_to_url("/home/user/project/docs/index.md"), "")
        self.assertEqual(fname_to_url(r"C:\Users\user\project\docs\index.md"), "")
        self.assertEqual(fname_to_url("/home/user/loom/docs"), "")
        self.assertEqual(fname_to_url("/home/user/loom/docs/notes.txt"), "")
        self.assertEqual(fname_to_url(""), "")


if __name__ == "__main__":
    unittest.main()

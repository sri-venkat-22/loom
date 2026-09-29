import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom.commands import Commands, SwitchCoder
from loom.custom_commands import (
    PROJECT_DIR,
    CustomCommand,
    CustomCommandError,
    find_commands,
)
from loom.io import InputOutput
from loom.llm import litellm
from loom.utils import GitTemporaryDirectory

from .test_agent import FakeLLM, make_coder, make_repo, reply
from .test_mcp import HomeDirMixin


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


REVIEW = """---
description: Review the diff for bugs
argument-hint: [focus]
---
Review `git diff HEAD` for bugs, focusing on $ARGUMENTS.
"""


class TestCustomCommand(unittest.TestCase):
    def load(self, text, name="cmd"):
        with GitTemporaryDirectory():
            return CustomCommand.load(name, write("cmd.md", text), "project")

    def test_front_matter(self):
        command = self.load(REVIEW, "review")
        self.assertEqual(command.description, "Review the diff for bugs")
        # An unquoted [focus] is a YAML list
        self.assertEqual(command.argument_hint, "[focus]")
        self.assertEqual(command.body, "Review `git diff HEAD` for bugs, focusing on $ARGUMENTS.")
        self.assertEqual(command.help_text(), "[focus]  Review the diff for bugs (project)")

    def test_expand_arguments(self):
        command = self.load(REVIEW)
        self.assertEqual(
            command.expand("  error handling "),
            "Review `git diff HEAD` for bugs, focusing on error handling.",
        )

        command = self.load('Rename $1 to $2 in $3.\nAll: "$ARGUMENTS"')
        self.assertEqual(
            command.expand('old_name "new name" src/app.py'),
            'Rename old_name to new name in src/app.py.\nAll: "old_name "new name" src/app.py"',
        )
        # Missing words are empty, and unbalanced quotes fall back to splitting on spaces
        self.assertEqual(command.expand("one"), 'Rename one to  in .\nAll: "one"')
        self.assertEqual(command.expand('a "b c'), 'Rename a to "b in c.\nAll: "a "b c"')

    def test_arguments_are_appended_without_placeholders(self):
        command = self.load("Write tests for the changed code.")
        self.assertEqual(command.expand(""), "Write tests for the changed code.")
        self.assertEqual(
            command.expand("in loom/io.py"), "Write tests for the changed code.\n\nin loom/io.py"
        )

    def test_no_front_matter_and_help_falls_back_to_the_first_line(self):
        command = self.load("Explain this code.\nIn detail.")
        self.assertEqual(command.description, "")
        self.assertEqual(command.help_text(), "Explain this code. (project)")

    def test_errors(self):
        for text in [
            "",
            "---\ndescription: nothing else\n---\n",
            "---\ndescription: [unclosed\n---\nprompt",
            "---\n- a list\n---\nprompt",
            "---\nchat-mode: shout\n---\nprompt",
        ]:
            with self.assertRaises(CustomCommandError, msg=text):
                self.load(text)


class TestFindCommands(HomeDirMixin, unittest.TestCase):
    def test_user_and_project_commands(self):
        with GitTemporaryDirectory() as root:
            user = Path(self.home.name) / ".loom" / "commands"
            write(user / "explain.md", "Explain $ARGUMENTS")
            write(user / "review.md", "my review")
            write(Path(PROJECT_DIR) / "review.md", "the project's review")
            write(Path(PROJECT_DIR) / "db" / "migrate.md", "Write a migration")
            write(Path(PROJECT_DIR) / "notes.txt", "not a command")
            write(Path(PROJECT_DIR) / "bad name.md", "spaces aren't allowed")

            found = find_commands(root)
            self.assertEqual(sorted(found), ["db:migrate", "explain", "review"])
            self.assertEqual(found["explain"][1], "user")
            # The project's command wins
            self.assertEqual(found["review"][1], "project")
            self.assertEqual(found["review"][0].read_text(), "the project's review")

            self.assertEqual(sorted(find_commands(None)), ["explain", "review"])


class TestSlashCommands(HomeDirMixin, unittest.TestCase):
    def test_running_a_custom_command_sends_the_prompt(self):
        with GitTemporaryDirectory():
            make_repo()
            write(Path(PROJECT_DIR) / "review.md", REVIEW)
            io = InputOutput(yes=True)
            coder = make_coder(io)
            llm = FakeLLM(reply("Looks fine."))

            self.assertIn("/review", coder.commands.get_commands())
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="/review error handling")

            sent = llm.requests[0]["messages"][-1]["content"]
            self.assertEqual(sent, "Review `git diff HEAD` for bugs, focusing on error handling.")
            self.assertEqual(coder.done_messages[0]["content"], sent)

    def test_prefixes_and_builtins(self):
        with GitTemporaryDirectory():
            make_repo()
            write(Path(PROJECT_DIR) / "review.md", "Review it.")
            # Can't replace loom's own commands
            write(Path(PROJECT_DIR) / "help.md", "Not the real help.")
            write(Path(PROJECT_DIR) / "chat_mode.md", "Not the real chat-mode.")
            io = InputOutput(yes=True)
            coder = make_coder(io)
            commands = coder.commands

            names = commands.get_commands()
            self.assertEqual(names.count("/help"), 1)
            self.assertNotIn("/chat_mode", names)
            self.assertEqual(sorted(commands.get_custom_commands()), ["review"])

            # A unique prefix works like it does for loom's commands
            self.assertEqual(commands.run("/revi"), "Review it.")

            io.tool_output = MagicMock()
            commands.run("/help")
            output = "\n".join(str(c[0][0]) for c in io.tool_output.call_args_list if c[0])
            self.assertIn("Custom commands:", output)
            self.assertIn("/review Review it. (project)", output)
            self.assertNotIn("Not the real help", output)

    def test_chat_mode_runs_in_that_mode(self):
        with GitTemporaryDirectory():
            make_repo()
            write(
                Path(PROJECT_DIR) / "explain.md",
                "---\nchat-mode: ask\n---\nExplain how $ARGUMENTS works.",
            )
            io = InputOutput(yes=True)
            coder = make_coder(io)
            with patch.object(Commands, "_generic_chat_command") as generic:
                coder.commands.run("/explain the agent loop")
            generic.assert_called_once_with("Explain how the agent loop works.", "ask")

            # Already in that mode: just sends the prompt
            write(
                Path(PROJECT_DIR) / "fix.md",
                "---\nchat-mode: agent\n---\nFix $ARGUMENTS.",
            )
            self.assertEqual(coder.commands.run("/fix the bug"), "Fix the bug.")

    def test_broken_command_shows_an_error(self):
        with GitTemporaryDirectory():
            make_repo()
            write(Path(PROJECT_DIR) / "empty.md", "---\ndescription: x\n---\n")
            io = InputOutput(yes=True)
            io.tool_error = MagicMock()
            coder = make_coder(io)
            self.assertIsNone(coder.commands.run("/empty"))
            self.assertIn("has no prompt", io.tool_error.call_args[0][0])

    def test_switch_coder_is_not_swallowed(self):
        with GitTemporaryDirectory():
            make_repo()
            write(Path(PROJECT_DIR) / "ask-it.md", "---\nchat-mode: ask\n---\nWhy?")
            io = InputOutput(yes=True)
            coder = make_coder(io)
            llm = FakeLLM()
            with patch.object(litellm, "completion", llm):
                with patch("loom.coders.base_coder.Coder.run") as run:
                    with self.assertRaises(SwitchCoder):
                        coder.commands.run("/ask-it")
            run.assert_called_once_with("Why?")


if __name__ == "__main__":
    unittest.main()

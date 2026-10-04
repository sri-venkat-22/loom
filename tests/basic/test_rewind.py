import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom import checkpoints as cp
from loom.coders import Coder
from loom.commands import Commands
from loom.io import InputOutput, choice_keys
from loom.llm import litellm
from loom.models import Model
from loom.permissions import Permissions
from loom.utils import ChdirTemporaryDirectory, GitTemporaryDirectory

from .test_agent import FakeLLM, call, make_coder, make_repo, reply

MAKE_FILE = f"{sys.executable} -c \"open('gen.txt', 'w').write('made by bash')\""

FIX_ADD = [
    reply(None, call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")),
    reply("Fixed add."),
]
ADD_SUBTRACT = [
    reply(
        None,
        call("todo_write", todos=[dict(content="Write sub.py", status="in_progress")]),
        call("write_file", path="sub.py", content="def sub(a, b):\n    return a - b\n"),
        call("bash", command=MAKE_FILE),
    ),
    reply("Added subtract."),
]


def quiet_io(**kwargs):
    return InputOutput(pretty=False, fancy_input=False, **kwargs)


def run_requests(coder, *requests):
    """Run (message, script) requests in coder, one after the other."""
    for message, script in requests:
        with patch.object(litellm, "completion", FakeLLM(*script)):
            coder.run(with_message=message)


def two_requests(io=None, **kwargs):
    io = io or quiet_io(yes=True)
    # Every command: on Windows, cmd.exe quoting never matches a narrower rule
    coder = make_coder(io, Permissions(io, allow=["bash"]), **kwargs)
    coder.commands = Commands(io, coder)
    run_requests(coder, ("fix add", FIX_ADD), ("add subtract", ADD_SUBTRACT))
    return coder


def commit_messages(repo):
    return [commit.message.strip() for commit in repo.iter_commits()]


class TestCheckpointsAreTaken(unittest.TestCase):
    def test_one_before_each_request(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder = two_requests()
            first, second = coder.session.checkpoints
            self.assertEqual((first["prompt"], second["prompt"]), ("fix add", "add subtract"))
            self.assertEqual(first["kind"], "request")
            self.assertEqual(first["messages_len"], 0)
            self.assertEqual(second["messages_len"], 4)
            self.assertEqual(second["head"], repo.head.commit.parents[0].hexsha)
            self.assertNotEqual(first["tree"], second["tree"])
            # The checkpoints didn't commit anything themselves
            self.assertEqual(len(commit_messages(repo)), 3)

    def test_steps_with_checkpoint_steps(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = two_requests(checkpoint_steps=True)
            kinds = [(c["kind"], c["prompt"]) for c in coder.session.checkpoints]
            self.assertEqual(
                kinds,
                [
                    ("request", "fix add"),
                    ("step", "Update(calc.py)"),
                    ("request", "add subtract"),
                    ("step", f"Write(sub.py), Bash({MAKE_FILE[:60]})"),
                ],
            )

    def test_phase_agents_are_checkpointed_by_the_orchestrator(self):
        from .test_orchestrator import SCRIPT_IDEA, run_project

        with GitTemporaryDirectory():
            make_repo()
            io = quiet_io(yes=None)
            io.choice_ask = MagicMock(return_value="reject")
            io.prompt_ask = MagicMock(return_value="")
            coder, orchestrator, llm, done = run_project(SCRIPT_IDEA, io=io)
            checkpoints = coder.session.checkpoints
            self.assertEqual(
                [(c["kind"], c["prompt"]) for c in checkpoints],
                [("phase", "/project: the Idea Check phase")],
            )

    def test_no_checkpoints_in_a_dry_run(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = two_requests(dry_run=True)
            self.assertEqual(coder.session.checkpoints, [])


class TestRewind(unittest.TestCase):
    def test_code_and_conversation(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            io = quiet_io(yes=True)
            coder = two_requests(io)
            done_before = list(coder.done_messages)
            self.assertTrue(Path("gen.txt").exists())

            # 1 is the newest: before "add subtract"
            coder.commands.cmd_rewind("1 both --yes")

            self.assertFalse(Path("sub.py").exists())
            self.assertFalse(Path("gen.txt").exists())
            self.assertIn("a + b", Path("calc.py").read_text())
            self.assertEqual(coder.done_messages, done_before[:4])
            self.assertEqual(coder.todos, [])
            self.assertEqual(io.placeholder, "add subtract")
            # A new commit; nothing was reset
            messages = commit_messages(repo)
            self.assertEqual(messages[0], "Rewind to before: add subtract")
            self.assertEqual(len(messages), 4)
            self.assertEqual(repo.git.status("--porcelain"), "")
            # The later request's checkpoint went with it, and the rewind can be undone
            kinds = [(c["kind"], c["prompt"]) for c in coder.session.checkpoints]
            self.assertEqual(
                kinds, [("request", "fix add"), ("rewind", "(before the rewind to: add subtract)")]
            )

            coder.commands.cmd_rewind("1 --yes")
            self.assertTrue(Path("sub.py").exists())
            self.assertEqual(Path("gen.txt").read_text(), "made by bash")
            self.assertEqual(coder.done_messages, done_before[:4])

    def test_code_only_keeps_the_conversation(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = two_requests()
            done_before = list(coder.done_messages)
            coder.commands.cmd_rewind("2 code --yes")
            self.assertIn("a - b", Path("calc.py").read_text())
            self.assertFalse(Path("sub.py").exists())
            self.assertEqual(coder.done_messages, done_before)
            self.assertEqual(len(coder.session.checkpoints), 3)

    def test_conversation_only_keeps_the_files(self):
        with GitTemporaryDirectory():
            make_repo()
            io = quiet_io(yes=True)
            coder = two_requests(io)
            coder.commands.cmd_rewind("2 conversation")
            self.assertEqual(coder.done_messages, [])
            self.assertEqual(io.placeholder, "fix add")
            self.assertTrue(Path("sub.py").exists())
            self.assertEqual(coder.session.checkpoints, [])

    def test_choosing_from_the_list(self):
        with GitTemporaryDirectory():
            make_repo()
            io = quiet_io(yes=None)
            io.permission_ask = MagicMock(return_value="yes")
            coder = two_requests(io)
            io.prompt_ask = MagicMock(return_value="2")
            io.choice_ask = MagicMock(return_value="code and conversation")
            io.confirm_ask = MagicMock(return_value=True)
            with patch.object(io, "tool_output") as output:
                coder.commands.cmd_rewind("")
            shown = [c.args[0] for c in output.call_args_list if c.args]
            self.assertIn("Checkpoints, newest first:", shown)
            listed = [line for line in shown if line.lstrip().startswith(("1 ", "2 "))]
            self.assertIn("add subtract  · 2 created since", listed[0])
            self.assertIn("fix add  · 1 changed, 2 created since", listed[1])

            question, choices = io.choice_ask.call_args.args
            self.assertEqual(
                choices,
                ["(c)ode and conversation", "c(o)de only", "co(n)versation only", "c(a)ncel"],
            )
            # It showed what would change, and asked
            io.confirm_ask.assert_called_once_with("Restore these files?")
            self.assertIn("  - sub.py  (created since: deleted)", shown)
            self.assertIn("a - b", Path("calc.py").read_text())
            self.assertEqual(coder.done_messages, [])

    def test_saying_no_changes_nothing(self):
        with GitTemporaryDirectory():
            make_repo()
            io = quiet_io(yes=None)
            io.permission_ask = MagicMock(return_value="yes")
            coder = two_requests(io)
            done_before = list(coder.done_messages)
            io.confirm_ask = MagicMock(return_value=False)
            coder.commands.cmd_rewind("2 both")
            self.assertTrue(Path("sub.py").exists())
            self.assertEqual(coder.done_messages, done_before)

    def test_a_summarized_history_is_found_by_its_request(self):
        with GitTemporaryDirectory():
            make_repo()
            io = quiet_io(yes=True)
            coder = two_requests(io)
            summary = [
                dict(role="user", content="(summary of fix add)"),
                dict(role="assistant", content="Ok."),
            ]
            coder.done_messages = summary + coder.done_messages[4:]
            coder.commands.cmd_rewind("1 conversation")
            self.assertEqual(coder.done_messages, summary)

            # Summarized away altogether: nothing changes
            coder = two_requests(quiet_io(yes=True))
            coder.done_messages = summary
            with patch.object(coder.io, "tool_error") as error:
                coder.commands.cmd_rewind("1 both --yes")
            self.assertIn("summarized", error.call_args.args[0])
            self.assertTrue(Path("sub.py").exists())

    def test_a_step_rewinds_only_the_code(self):
        with GitTemporaryDirectory():
            make_repo()
            coder = two_requests(checkpoint_steps=True)
            with patch.object(coder.io, "tool_error") as error:
                coder.commands.cmd_rewind("1 both --yes")
            self.assertIn("only rewinds the code", error.call_args.args[0])
            coder.commands.cmd_rewind("1 --yes")
            self.assertFalse(Path("sub.py").exists())
            self.assertFalse(Path("gen.txt").exists())
            self.assertEqual(len(coder.done_messages), 10)

    def test_a_rewind_during_a_merge_is_refused(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder = two_requests()
            Path(repo.git_dir, "MERGE_HEAD").write_bytes(repo.head.commit.hexsha.encode() + b"\n")
            with patch.object(coder.io, "tool_error") as error:
                coder.commands.cmd_rewind("2 both --yes")
            self.assertIn("middle of a merge", error.call_args.args[0])
            self.assertTrue(Path("sub.py").exists())
            self.assertEqual(len(coder.done_messages), 10)

    def test_bad_numbers_and_no_checkpoints(self):
        with GitTemporaryDirectory():
            make_repo()
            io = quiet_io(yes=True)
            coder = make_coder(io)
            commands = Commands(io, coder)
            with patch.object(io, "tool_output") as output:
                commands.cmd_rewind("")
            self.assertIn("no checkpoints yet", output.call_args.args[0])
            coder = two_requests(io)
            with patch.object(io, "tool_error") as error:
                coder.commands.cmd_rewind("7")
            self.assertIn("numbered 1 to 2", error.call_args.args[0])

    def test_without_git(self):
        with ChdirTemporaryDirectory():
            Path("calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
            io = quiet_io(yes=True)
            coder = Coder.create(
                Model("gpt-4o-mini"),
                "agent",
                io=io,
                map_tokens=0,
                stream=False,
                use_git=False,
                permissions=Permissions(io),
            )
            coder.commands = Commands(io, coder)
            self.assertIsNone(coder.repo)
            run_requests(coder, ("fix add", FIX_ADD))
            self.assertIn("a + b", Path("calc.py").read_text())
            with patch.object(io, "tool_warning") as warning:
                coder.commands.cmd_rewind("1 both --yes")
            self.assertIn("not changes shell commands made", warning.call_args.args[0])
            self.assertEqual(Path("calc.py").read_bytes(), b"def add(a, b):\n    return a - b\n")
            self.assertEqual(coder.done_messages, [])

    def test_gc(self):
        with GitTemporaryDirectory():
            repo = make_repo()
            coder = two_requests()
            coder.session.directory = Path(".loom.sessions")
            with patch.object(coder.io, "tool_output") as output:
                coder.commands.cmd_rewind("--gc")
            self.assertIn("0 deleted sessions", output.call_args.args[0])
            refs = repo.git.for_each_ref("--format=%(refname)", cp.REF_PREFIX).split()
            self.assertEqual(refs, [cp.REF_PREFIX + coder.session.id])


class TestChoiceKeys(unittest.TestCase):
    def test_marked_keys(self):
        keys = choice_keys(["(c)ode and conversation", "c(o)de only", "approve"])
        self.assertEqual(
            keys,
            {
                "code and conversation": ("c", "(C)ode and conversation"),
                "code only": ("o", "c(O)de only"),
                "approve": ("a", "(A)pprove"),
            },
        )

    def test_choice_ask_answers_by_key(self):
        io = quiet_io(yes=None)
        with patch("builtins.input", return_value="o") as terminal:
            answer = io.choice_ask("What?", ["(c)ode and conversation", "c(o)de only", "c(a)ncel"])
        self.assertEqual(answer, "code only")
        self.assertIn("(C)ode and conversation/c(O)de only/c(A)ncel", terminal.call_args.args[0])


class TestDoubleEsc(unittest.TestCase):
    def test_esc_esc_at_an_empty_prompt_opens_rewind(self):
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with GitTemporaryDirectory(), create_pipe_input() as pipe:
            make_repo()
            io = InputOutput(input=pipe, output=DummyOutput(), pretty=False)
            coder = make_coder(io)
            coder.commands = Commands(io, coder)
            pipe.send_text("\x1b\x1b")
            self.assertEqual(coder.get_input(), "/rewind")
            # With text typed, it doesn't
            pipe.send_text("fix\x1b\x1b it\r")
            self.assertNotEqual(coder.get_input(), "/rewind")


if __name__ == "__main__":
    unittest.main()

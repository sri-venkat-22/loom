import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom import plans
from loom.coders import Coder
from loom.commands import Commands
from loom.hooks import Hook, Hooks
from loom.io import InputOutput
from loom.llm import litellm
from loom.permissions import Permissions
from loom.sessions import Session
from loom.utils import GitTemporaryDirectory

from .test_agent import FakeLLM, call, make_coder, make_repo, reply, tool_results
from .test_hooks import HOOK_SCRIPT, hook_command, hook_log

PLAN = """# Fix add

1. In calc.py, change `a - b` to `a + b`.
2. Run the tests with pytest.

Risk: none.
"""

FIX = call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")


def tool_names(request):
    return [tool["function"]["name"] for tool in request["tools"]]


def interactive_io(*choices, feedback=""):
    """An io that answers the plan question with choices, in order, and allows every edit."""
    io = InputOutput(pretty=False, yes=None, fancy_input=False)
    io.choice_ask = MagicMock(side_effect=list(choices))
    io.plan_feedback_ask = MagicMock(return_value=feedback)
    io.permission_ask = MagicMock(return_value="yes")
    return io


def plan_coder(io, mode="plan"):
    return make_coder(io, Permissions(io, mode=mode))


class TestPlanTool(unittest.TestCase):
    def test_only_offered_in_plan_mode(self):
        with GitTemporaryDirectory():
            make_repo()
            for mode, offered in [("ask", False), ("accept-edits", False), ("plan", True)]:
                io = InputOutput(yes=True)
                coder = plan_coder(io, mode)
                llm = FakeLLM(reply("Nothing to do."))
                with patch.object(litellm, "completion", llm):
                    coder.run(with_message="hello")
                self.assertEqual("exit_plan_mode" in tool_names(llm.requests[0]), offered, mode)

    def test_called_outside_plan_mode_it_says_to_carry_on(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            coder = plan_coder(io, "accept-edits")
            llm = FakeLLM(reply(None, call("exit_plan_mode", plan=PLAN)), reply("Ok."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix it")
            result = list(tool_results(coder.done_messages).values())[0]
            self.assertIn("isn't in plan mode", result)
            self.assertFalse(Path(plans.PLANS_DIR).exists())

    def test_plan_files(self):
        with GitTemporaryDirectory():
            path = plans.save_plan(".", PLAN)
            self.assertEqual(path.parent, Path(plans.PLANS_DIR))
            self.assertTrue(path.name.endswith("-fix-add.md"))
            self.assertEqual((path.parent / ".gitignore").read_text(), "*\n")
            # Two plans in the same second get their own files
            self.assertNotEqual(plans.save_plan(".", PLAN), path)

            self.assertEqual(plans.plan_title("- **Add a flag**\nmore"), "Add a flag")
            self.assertEqual(plans.slugify("Add --verbose, & more!"), "add-verbose-more")
            long = "x" * (plans.PROMPT_CHARS + 50)
            self.assertIn("Read p.md for the rest", plans.brief(long, "p.md"))
            self.assertEqual(plans.brief(PLAN), PLAN.strip())


class TestApproval(unittest.TestCase):
    def run_plan(self, io, *replies, mode="plan", message="fix add"):
        coder = plan_coder(io, mode)
        llm = FakeLLM(*replies)
        with patch.object(litellm, "completion", llm):
            coder.run(with_message=message)
        return coder, llm

    def test_approve_and_auto_accept_edits_carries_on_in_the_same_request(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io(plans.APPROVE_AUTO)
            coder, llm = self.run_plan(
                io,
                reply("Here's my plan.", call("exit_plan_mode", plan=PLAN)),
                reply(None, FIX),
                reply("Done."),
            )

            self.assertEqual(coder.permissions.mode, "accept-edits")
            self.assertIn("a + b", Path("calc.py").read_text())
            # Approving auto-accepts the edit: nothing asked about it
            io.permission_ask.assert_not_called()
            # One request: the loop went on without a new user turn
            self.assertEqual(len(llm.requests), 3)
            self.assertEqual([m["role"] for m in coder.done_messages].count("user"), 1)

            result = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertIn("approved your plan", result)
            self.assertIn("accept-edits mode", result)
            self.assertIn("todo_write", result)

            # The plan left the tools and stayed in the system prompt for the request
            self.assertNotIn("exit_plan_mode", tool_names(llm.requests[1]))
            system = llm.requests[1]["messages"][0]["content"]
            self.assertIn("# The approved plan", system)
            self.assertIn("change `a - b` to `a + b`", system)
            self.assertNotIn("# Plan mode", system)
            self.assertIsNone(coder.active_plan)

            # Saved, and remembered by the session
            self.assertTrue(coder.session.plan.startswith(plans.PLANS_DIR + "/"))
            self.assertEqual(Path(coder.session.plan).read_text(), PLAN)
            ask = io.choice_ask.call_args
            self.assertEqual(ask.args[1], plans.CHOICES)
            self.assertEqual(ask.kwargs["plan"], dict(text=PLAN, path=coder.session.plan))

    def test_approve_and_ask_for_each_edit(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io(plans.APPROVE_ASK)
            coder, llm = self.run_plan(
                io, reply(None, call("exit_plan_mode", plan=PLAN)), reply(None, FIX), reply("Ok.")
            )
            self.assertEqual(coder.permissions.mode, "ask")
            io.permission_ask.assert_called_once()
            self.assertIn("a + b", Path("calc.py").read_text())
            self.assertIn("ask mode", tool_results(llm.requests[1]["messages"])["call_1_0"])

    def test_keep_planning_sends_the_feedback_and_stays_in_plan_mode(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io(plans.KEEP_PLANNING, plans.APPROVE_ASK, feedback="Add a test")
            coder, llm = self.run_plan(
                io,
                reply(None, call("exit_plan_mode", plan=PLAN)),
                reply(None, call("exit_plan_mode", plan=PLAN + "3. Add a test.\n")),
                reply("Ok."),
            )
            result = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertIn("didn't approve", result)
            self.assertIn("Add a test", result)
            # Still planning for the second step, then approved
            self.assertIn("exit_plan_mode", tool_names(llm.requests[1]))
            self.assertIn("# Plan mode", llm.requests[1]["messages"][0]["content"])
            self.assertEqual(coder.permissions.mode, "ask")
            self.assertEqual(len(list(Path(plans.PLANS_DIR).glob("*.md"))), 2)

    def test_keep_planning_without_feedback_waits_for_the_user(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io(plans.KEEP_PLANNING)
            coder, llm = self.run_plan(io, reply(None, call("exit_plan_mode", plan=PLAN)))
            self.assertEqual(len(llm.requests), 1)
            self.assertEqual(coder.permissions.mode, "plan")
            self.assertIn("Stop here", list(tool_results(coder.done_messages).values())[0])
            self.assertIsNone(coder.session.plan)

    def test_an_edited_plan_reaches_the_model(self):
        with GitTemporaryDirectory():
            make_repo()
            edited = PLAN.replace("Risk: none.", "Risk: also check subtract().")
            io = interactive_io(plans.EDIT_PLAN, plans.APPROVE_AUTO)
            with patch("loom.coders.agent_coder.pipe_editor", return_value=edited) as editor:
                coder, llm = self.run_plan(
                    io, reply(None, call("exit_plan_mode", plan=PLAN)), reply("Ok.")
                )
            editor.assert_called_once()
            self.assertEqual(editor.call_args.args[0], PLAN)

            result = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertIn("after editing it", result)
            self.assertIn("also check subtract()", result)
            self.assertEqual(Path(coder.session.plan).read_text(), edited)
            # The second question showed the edited plan
            self.assertEqual(io.choice_ask.call_args.kwargs["plan"]["text"], edited)

    def test_a_ui_with_its_own_editor_edits_the_plan(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io(plans.EDIT_PLAN, plans.APPROVE_ASK)
            io.edit_document = MagicMock(return_value=PLAN + "4. Ship it.\n")
            coder, llm = self.run_plan(
                io, reply(None, call("exit_plan_mode", plan=PLAN)), reply("Ok.")
            )
            text, path = io.edit_document.call_args.args
            self.assertEqual((text, path), (PLAN, coder.session.plan))
            self.assertIn("Ship it", tool_results(llm.requests[1]["messages"])["call_1_0"])

    def test_yes_always_shows_the_plan_and_stops(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True, fancy_input=False)
            io.plan_output = MagicMock()
            coder, llm = self.run_plan(io, reply(None, call("exit_plan_mode", plan=PLAN)))
            self.assertEqual(len(llm.requests), 1)
            self.assertEqual(coder.permissions.mode, "plan")
            self.assertIn("a - b", Path("calc.py").read_text())
            io.plan_output.assert_called_once()
            self.assertEqual(io.plan_output.call_args.args[0], PLAN)
            result = list(tool_results(coder.done_messages).values())[0]
            self.assertIn("nobody can approve", result)
            self.assertIsNone(coder.session.plan)

    def test_message_runs_show_the_plan_and_stop(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io()
            io.plan_output = MagicMock()
            coder = plan_coder(io)
            coder.one_shot = True
            llm = FakeLLM(reply(None, call("exit_plan_mode", plan=PLAN)))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add")
            io.choice_ask.assert_not_called()
            io.plan_output.assert_called_once()
            self.assertEqual(coder.permissions.mode, "plan")

    def test_plan_mode_from_bypass_approves_automatically(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io()
            coder = plan_coder(io, "bypass")
            coder.permissions.mode = "plan"
            self.assertTrue(coder.permissions.approves_plans())
            llm = FakeLLM(
                reply(None, call("exit_plan_mode", plan=PLAN)), reply(None, FIX), reply("Ok.")
            )
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add")
            io.choice_ask.assert_not_called()
            self.assertEqual(coder.permissions.mode, "bypass")
            self.assertIn("a + b", Path("calc.py").read_text())
            self.assertIn("bypass mode", tool_results(llm.requests[1]["messages"])["call_1_0"])

    def test_hooks_see_exit_plan_mode_as_ExitPlanMode(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("hook.py").write_text(HOOK_SCRIPT)
            io = interactive_io(plans.APPROVE_ASK)
            coder = plan_coder(io)
            coder.hooks = Hooks(
                io,
                [
                    Hook("PreToolUse", "ExitPlanMode", hook_command("ok"), 60, "test"),
                    Hook("PostToolUse", "exit_plan_mode", hook_command("ok"), 60, "test"),
                ],
                root=str(Path.cwd()),
            )
            llm = FakeLLM(reply(None, call("exit_plan_mode", plan=PLAN)), reply("Ok."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add")

            log = hook_log()
            self.assertEqual([e["hook_event_name"] for e in log], ["PreToolUse", "PostToolUse"])
            self.assertEqual({e["tool_name"] for e in log}, {"ExitPlanMode"})
            self.assertEqual(log[0]["tool_input"], dict(plan=PLAN))
            self.assertEqual(log[0]["permission_mode"], "plan")
            self.assertIn("approved", log[1]["tool_response"])

    def test_a_blocking_hook_keeps_the_plan_from_being_asked(self):
        with GitTemporaryDirectory():
            make_repo()
            Path("hook.py").write_text(HOOK_SCRIPT)
            io = interactive_io()
            coder = plan_coder(io)
            coder.hooks = Hooks(
                io,
                [Hook("PreToolUse", "ExitPlanMode", hook_command("block"), 60, "test")],
                root=str(Path.cwd()),
            )
            llm = FakeLLM(reply(None, call("exit_plan_mode", plan=PLAN)), reply("Ok."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add")
            io.choice_ask.assert_not_called()
            self.assertIn("Blocked", list(tool_results(coder.done_messages).values())[0])

    def test_the_approved_plan_is_saved_with_the_session(self):
        with GitTemporaryDirectory():
            make_repo()
            io = interactive_io(plans.APPROVE_ASK)
            coder = plan_coder(io)
            coder.session = Session(".loom.sessions")
            llm = FakeLLM(reply(None, call("exit_plan_mode", plan=PLAN)), reply("Ok."))
            with patch.object(litellm, "completion", llm):
                coder.run(with_message="fix add")
            saved = json.loads(coder.session.path.read_text())
            self.assertEqual(saved["plan"], coder.session.plan)
            self.assertEqual(Session.load(coder.session.path).plan, coder.session.plan)


class TestPlanCommand(unittest.TestCase):
    def test_plan_switches_to_plan_mode_and_sends_the_request(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True, fancy_input=False)
            coder = make_coder(io)
            commands = Commands(io, coder)
            self.assertEqual(commands.cmd_plan("add a --verbose flag"), "add a --verbose flag")
            self.assertEqual(coder.permissions.mode, "plan")
            self.assertIsNone(commands.cmd_plan(""))
            self.assertEqual(commands.completions_plan(), ["show"])

    def test_plan_show(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True, fancy_input=False)
            io.plan_output = MagicMock()
            coder = make_coder(io)
            commands = Commands(io, coder)
            with patch.object(io, "tool_output") as output:
                commands.cmd_plan("show")
            self.assertIn("No plan has been approved", output.call_args.args[0])
            io.plan_output.assert_not_called()

            path = plans.save_plan(".", PLAN)
            coder.session.plan = path.as_posix()
            commands.cmd_plan("show")
            io.plan_output.assert_called_once_with(PLAN, path.as_posix())

    def test_plan_outside_the_agent_switches_to_it(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True, fancy_input=False)
            coder = Coder.create(
                make_coder(io).main_model, "ask", io=io, map_tokens=0, permissions=Permissions(io)
            )
            commands = Commands(io, coder)
            with patch.object(commands, "_generic_chat_command") as switch:
                commands.cmd_plan("refactor calc")
            switch.assert_called_once_with("refactor calc", "agent")
            self.assertEqual(coder.permissions.mode, "plan")


class TestPrintPlan(unittest.TestCase):
    def test_the_terminal_shows_the_plan_and_the_choices(self):
        io = InputOutput(pretty=False, yes=None, fancy_input=False)
        with patch("builtins.input", return_value="y") as terminal:
            with patch.object(io.console, "print") as printed:
                answer = io.choice_ask(
                    "Approve this plan?",
                    plans.CHOICES,
                    default=plans.APPROVE_ASK,
                    plan=dict(text=PLAN, path=".loom/plans/p.md"),
                )
        self.assertEqual(answer, plans.APPROVE_ASK)
        panel = printed.call_args_list[0].args[0]
        self.assertEqual(panel.title, "Plan · .loom/plans/p.md")
        question = terminal.call_args.args[0]
        self.assertIn(
            "(A)pprove and auto-accept edits/(Y)es, approve and ask for each edit", question
        )
        self.assertIn("(K)eep planning/(E)dit plan", question)


class TestOrchestrator(unittest.TestCase):
    def test_project_refuses_plan_mode_and_says_how_to_leave_it(self):
        from loom.orchestrator import Orchestrator

        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(pretty=False, yes=True, fancy_input=False)
            coder = plan_coder(io)
            orchestrator = Orchestrator(coder, state=MagicMock())
            with patch.object(io, "tool_error") as error, patch.object(io, "tool_output") as out:
                self.assertFalse(orchestrator.can_run())
            self.assertIn("plan mode", error.call_args.args[0])
            self.assertIn("/permissions ask", out.call_args.args[0])

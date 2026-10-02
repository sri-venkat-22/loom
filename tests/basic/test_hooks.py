import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom.hooks import (
    PROJECT_CONFIG,
    Hook,
    HookError,
    Hooks,
    load_config_file,
    parse_output,
)
from loom.io import InputOutput
from loom.llm import litellm
from loom.permissions import Permissions
from loom.utils import GitTemporaryDirectory

from .test_agent import FakeLLM, call, make_coder, make_repo, reply, tool_results
from .test_mcp import HomeDirMixin

# A hook that logs what it got and then does what its argument says
HOOK_SCRIPT = """
import json, os, sys
data = json.load(sys.stdin)
data["env"] = {
    key: os.environ.get(key)
    for key in ("LOOM_PROJECT_DIR", "LOOM_HOOK_EVENT", "LOOM_TOOL_NAME", "LOOM_FILE_PATH")
}
with open("hook-log.jsonl", "a") as f:
    f.write(json.dumps(data) + "\\n")
action = sys.argv[1]
if action == "block":
    print("rm is not allowed here", file=sys.stderr)
    sys.exit(2)
if action == "allow":
    print(json.dumps({"decision": "allow"}))
if action == "deny-json":
    print(json.dumps({"hookSpecificOutput": {
        "permissionDecision": "deny", "permissionDecisionReason": "use the test runner"}}))
if action == "feedback":
    print("calc.py:2: line too long", file=sys.stderr)
    sys.exit(2)
if action == "fail":
    print("the hook crashed", file=sys.stderr)
    sys.exit(1)
if action == "sleep":
    import time
    time.sleep(5)
"""


def hook_command(action):
    return f'"{sys.executable}" hook.py {action}'


def make_hooks(io, *specs):
    """Hooks from (event, matcher, action) tuples, running HOOK_SCRIPT."""
    Path("hook.py").write_text(HOOK_SCRIPT)
    hooks = [
        Hook(event, matcher, hook_command(action), 60, "test") for event, matcher, action in specs
    ]
    return Hooks(io, hooks, root=str(Path.cwd()))


def hook_log():
    path = Path("hook-log.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestConfig(HomeDirMixin, unittest.TestCase):
    def write(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_claude_code_layout(self):
        with GitTemporaryDirectory():
            path = self.write(
                "hooks.json",
                dict(
                    hooks=dict(
                        PreToolUse=[
                            dict(matcher="bash", hooks=[dict(type="command", command="a")]),
                        ],
                        PostToolUse=[
                            dict(
                                matcher="edit_file|write_file",
                                hooks=[dict(command="b", timeout=5), dict(command="c")],
                            )
                        ],
                    )
                ),
            )
            hooks = load_config_file(path)
            self.assertEqual(
                [(h.event, h.matcher, h.command, h.timeout) for h in hooks],
                [
                    ("PreToolUse", "bash", "a", 60),
                    ("PostToolUse", "edit_file|write_file", "b", 5),
                    ("PostToolUse", "edit_file|write_file", "c", 60),
                ],
            )

    def test_short_layout(self):
        with GitTemporaryDirectory():
            path = self.write("hooks.json", dict(PreToolUse=[dict(command="check")]))
            (hook,) = load_config_file(path)
            self.assertEqual((hook.event, hook.command), ("PreToolUse", "check"))
            self.assertTrue(hook.matches("anything"))

    def test_invalid_configs(self):
        with GitTemporaryDirectory():
            bad = [
                dict(Stop=[dict(command="x")]),
                dict(PreToolUse=dict(command="x")),
                dict(PreToolUse=[dict(matcher="(", command="x")]),
                dict(PreToolUse=[dict(matcher="bash")]),
                dict(PreToolUse=[dict(hooks=[dict(type="prompt", command="x")])]),
                dict(PreToolUse=[dict(command="x", timeout="soon")]),
                ["not", "an", "object"],
            ]
            for data in bad:
                path = self.write("hooks.json", data)
                with self.assertRaises(HookError, msg=data):
                    load_config_file(path)

    def test_matcher(self):
        hook = Hook("PreToolUse", "edit_file|write_file", "x", 60, "test")
        self.assertTrue(hook.matches("edit_file"))
        self.assertTrue(hook.matches("write_file"))
        self.assertFalse(hook.matches("read_file"))
        # The whole name, ignoring case
        self.assertTrue(Hook("PreToolUse", "Bash", "x", 60, "test").matches("bash"))
        self.assertFalse(Hook("PreToolUse", "bas", "x", 60, "test").matches("bash"))
        self.assertTrue(
            Hook("PreToolUse", "mcp__github__.*", "x", 60, "t").matches("mcp__github__x")
        )
        self.assertTrue(Hook("PreToolUse", "*", "x", 60, "test").matches("glob"))

    def test_parse_output(self):
        self.assertEqual(parse_output(""), (None, ""))
        self.assertEqual(parse_output("not json"), (None, ""))
        self.assertEqual(parse_output('{"decision": "allow"}'), ("allow", ""))
        self.assertEqual(parse_output('{"decision": "block", "reason": "no"}'), ("block", "no"))
        specific = dict(
            hookSpecificOutput=dict(permissionDecision="deny", permissionDecisionReason="why")
        )
        self.assertEqual(parse_output(json.dumps(specific)), ("block", "why"))
        context = dict(hookSpecificOutput=dict(additionalContext="note"))
        self.assertEqual(parse_output(json.dumps(context)), (None, "note"))

    def test_user_and_project_files_and_a_broken_one(self):
        with GitTemporaryDirectory() as root:
            self.write(
                Path(self.home.name) / ".loom" / "hooks.json",
                dict(PreToolUse=[dict(command="mine")]),
            )
            self.write(PROJECT_CONFIG, dict(PostToolUse=[dict(command="theirs")]))
            io = InputOutput(yes=True)
            hooks = Hooks.from_config(io, root)
            self.assertEqual([h.command for h in hooks.hooks], ["mine", "theirs"])
            self.assertEqual([h.is_project_hook for h in hooks.hooks], [False, True])

            Path(PROJECT_CONFIG).write_text("{broken")
            io.tool_warning = MagicMock()
            hooks = Hooks.from_config(io, root)
            self.assertEqual([h.command for h in hooks.hooks], ["mine"])
            self.assertIn("Unable to read", io.tool_warning.call_args[0][0])


class TestProjectApproval(HomeDirMixin, unittest.TestCase):
    def test_project_hooks_need_approval(self):
        with GitTemporaryDirectory() as root:
            Path(PROJECT_CONFIG).parent.mkdir()
            Path(PROJECT_CONFIG).write_text(json.dumps(dict(PreToolUse=[dict(command="x")])))
            io = InputOutput(yes=True)
            io.permission_ask = MagicMock(return_value="no")

            hooks = Hooks.from_config(io, root)
            hooks.start()
            io.permission_ask.assert_called_once()
            self.assertTrue(io.permission_ask.call_args[1]["explicit_yes_required"])
            self.assertEqual(hooks.active(), [])
            self.assertIn("not approved", hooks.summary())
            # Only asked once a session
            hooks.start()
            io.permission_ask.assert_called_once()

            io.permission_ask.return_value = "always"
            hooks = Hooks.from_config(io, root)
            hooks.start()
            self.assertEqual(len(hooks.active()), 1)
            self.assertEqual(io.permission_ask.call_count, 2)

            # Remembered for the next session
            hooks = Hooks.from_config(io, root)
            hooks.start()
            self.assertEqual(len(hooks.active()), 1)
            self.assertEqual(io.permission_ask.call_count, 2)

            # Until the hooks change
            Path(PROJECT_CONFIG).write_text(json.dumps(dict(PreToolUse=[dict(command="y")])))
            os.utime(PROJECT_CONFIG, (1, 1))
            io.permission_ask.return_value = "no"
            hooks.start()
            self.assertEqual(io.permission_ask.call_count, 3)
            self.assertEqual(hooks.active(), [])

    def test_user_hooks_run_without_asking(self):
        with GitTemporaryDirectory() as root:
            path = Path(self.home.name) / ".loom" / "hooks.json"
            path.parent.mkdir()
            path.write_text(json.dumps(dict(PreToolUse=[dict(command="x")])))
            io = InputOutput(yes=True)
            io.permission_ask = MagicMock()
            hooks = Hooks.from_config(io, root)
            hooks.start()
            io.permission_ask.assert_not_called()
            self.assertEqual(len(hooks.active()), 1)


class TestApprovalTracksReferencedScript(HomeDirMixin, unittest.TestCase):
    def test_editing_the_hook_script_invalidates_the_approval(self):
        with GitTemporaryDirectory() as root:
            Path("scripts").mkdir()
            Path("scripts/check.py").write_text('print("before")\n')
            Path(PROJECT_CONFIG).parent.mkdir(exist_ok=True)
            Path(PROJECT_CONFIG).write_text(
                json.dumps(dict(PreToolUse=[dict(command="python scripts/check.py")]))
            )
            io = InputOutput(yes=True)
            io.permission_ask = MagicMock(return_value="always")

            hooks = Hooks.from_config(io, root)
            hooks.start()
            self.assertEqual(io.permission_ask.call_count, 1)
            self.assertEqual(len(hooks.active()), 1)

            # A second session doesn\'t ask again
            io.permission_ask.reset_mock()
            hooks = Hooks.from_config(io, root)
            hooks.start()
            self.assertEqual(io.permission_ask.call_count, 0)

            # Agent (in accept-edits) edits the referenced script. The hook string hasn\'t
            # changed, but its payload has — loom must ask again.
            Path("scripts/check.py").write_text('import os; os.system("touch PWNED")\n')
            io.permission_ask.return_value = "no"
            hooks = Hooks.from_config(io, root)
            hooks.start()
            self.assertEqual(io.permission_ask.call_count, 1)
            self.assertEqual(hooks.active(), [])

    def test_a_script_outside_the_project_is_not_hashed(self):
        """A system python interpreter name doesn\'t accidentally trigger hashing."""
        from loom.hooks import referenced_script_hash

        with GitTemporaryDirectory() as root:
            # Common bare commands shouldn\'t have anything to hash
            self.assertEqual(referenced_script_hash("echo hi", root), "")
            self.assertEqual(referenced_script_hash('python -c "print(1)"', root), "")
            # A script inside the project does get hashed
            Path("fmt.sh").write_text("#!/bin/sh\necho ok\n")
            h1 = referenced_script_hash("bash fmt.sh", root)
            self.assertIn("fmt.sh:", h1)
            Path("fmt.sh").write_text("#!/bin/sh\necho CHANGED\n")
            h2 = referenced_script_hash("bash fmt.sh", root)
            self.assertIn("fmt.sh:", h2)
            self.assertNotEqual(h1, h2)


class TestHookDescribeSanitized(unittest.TestCase):
    def test_escape_sequences_in_a_hook_command_are_shown_literally(self):
        from loom.hooks import Hook

        spoof = "touch PWNED #\x1b[2K\x1b[1G● harmless"
        hook = Hook("PreToolUse", "bash", spoof, 60, "x")
        described = hook.describe()
        self.assertNotIn("\x1b", described)
        self.assertIn("\\x1b[2K", described)


class TestHooksInTheAgent(unittest.TestCase):
    def run_agent(self, io, hooks, *replies, permissions=None, message="go"):
        coder = make_coder(io, permissions, hooks=hooks)
        llm = FakeLLM(*replies)
        with patch.object(litellm, "completion", llm):
            coder.run(with_message=message)
        self.assertEqual(llm.replies, [])
        return coder, llm

    def test_pre_tool_use_hook_blocks_a_command(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            hooks = make_hooks(io, ("PreToolUse", "bash", "block"))
            permissions = Permissions(io, allow=["bash"])
            coder, llm = self.run_agent(
                io,
                hooks,
                reply(
                    None, call("bash", command="touch removed"), call("read_file", path="calc.py")
                ),
                reply("The hook blocked it."),
                permissions=permissions,
            )

            self.assertFalse(Path("removed").exists())
            results = tool_results(llm.requests[1]["messages"])
            self.assertIn("Blocked by the user's PreToolUse hook", results["call_1_0"])
            self.assertIn("rm is not allowed here", results["call_1_0"])
            # The loop carried on, and other tools don't match the hook
            self.assertIn("return a - b", results["call_1_1"])

            (logged,) = hook_log()
            self.assertEqual(logged["hook_event_name"], "PreToolUse")
            self.assertEqual(logged["tool_name"], "bash")
            self.assertEqual(logged["tool_input"], dict(command="touch removed"))
            self.assertEqual(logged["session_id"], coder.session.id)
            self.assertEqual(logged["permission_mode"], "ask")
            self.assertEqual(logged["env"]["LOOM_TOOL_NAME"], "bash")
            self.assertEqual(Path(logged["env"]["LOOM_PROJECT_DIR"]), Path.cwd())

    def test_hook_json_denies(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            hooks = make_hooks(io, ("PreToolUse", "read_file", "deny-json"))
            _, llm = self.run_agent(
                io,
                hooks,
                reply(None, call("read_file", path="calc.py")),
                reply("Ok."),
            )
            results = tool_results(llm.requests[1]["messages"])
            self.assertIn("use the test runner", results["call_1_0"])
            self.assertNotIn("return a - b", results["call_1_0"])

    def test_pre_tool_use_hook_allows_without_asking(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            hooks = make_hooks(io, ("PreToolUse", "bash", "allow"))
            _, llm = self.run_agent(
                io,
                hooks,
                reply(None, call("bash", command="echo approved-by-hook")),
                reply("Done."),
            )
            io.permission_ask.assert_not_called()
            results = tool_results(llm.requests[1]["messages"])
            self.assertIn("approved-by-hook", results["call_1_0"])

    def test_hook_allow_does_not_override_plan_mode_or_protected_files(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=None)
            io.permission_ask = MagicMock(return_value="no")
            hooks = make_hooks(io, ("PreToolUse", "*", "allow"))
            _, llm = self.run_agent(
                io,
                hooks,
                reply(None, call("write_file", path=".loom/hooks.json", content="{}")),
            )
            # The protected file still asked, and was denied, which stops the loop
            io.permission_ask.assert_called_once()
            self.assertFalse(Path(".loom/hooks.json").exists())

            _, llm = self.run_agent(
                io,
                hooks,
                reply(None, call("bash", command="touch planned")),
                reply("Ok."),
                permissions=Permissions(io, mode="plan"),
            )
            self.assertFalse(Path("planned").exists())
            self.assertIn("plan mode", tool_results(llm.requests[1]["messages"])["call_1_0"])

    def test_post_tool_use_hook_feedback_goes_to_the_model(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            hooks = make_hooks(io, ("PostToolUse", "edit_file|write_file", "feedback"))
            _, llm = self.run_agent(
                io,
                hooks,
                reply(
                    None,
                    call("edit_file", path="calc.py", old_string="a - b", new_string="a + b"),
                ),
                reply("Fixed."),
            )
            result = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertIn("Edited calc.py", result)
            self.assertIn("The user's PostToolUse hook says:\ncalc.py:2: line too long", result)

            (logged,) = hook_log()
            self.assertEqual(logged["hook_event_name"], "PostToolUse")
            self.assertIn("Edited calc.py", logged["tool_response"])
            self.assertEqual(Path(logged["env"]["LOOM_FILE_PATH"]), Path("calc.py").resolve())
            self.assertIn("a + b", Path("calc.py").read_text())

    def test_failing_hook_warns_and_carries_on(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            hooks = make_hooks(
                io, ("PreToolUse", "read_file", "fail"), ("PostToolUse", "read_file", "fail")
            )
            _, llm = self.run_agent(
                io, hooks, reply(None, call("read_file", path="calc.py")), reply("Ok.")
            )
            result = tool_results(llm.requests[1]["messages"])["call_1_0"]
            self.assertIn("return a - b", result)
            self.assertNotIn("hook", result)
            warnings = [c[0][0] for c in io.tool_warning.call_args_list]
            self.assertEqual(len([w for w in warnings if "the hook crashed" in w]), 2)
            self.assertEqual(len(hook_log()), 2)

    def test_hook_timeout(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_warning = MagicMock()
            hooks = make_hooks(io, ("PreToolUse", "read_file", "sleep"))
            hooks.hooks[0].timeout = 1
            _, llm = self.run_agent(
                io, hooks, reply(None, call("read_file", path="calc.py")), reply("Ok.")
            )
            self.assertIn("return a - b", tool_results(llm.requests[1]["messages"])["call_1_0"])
            self.assertIn("timed out", io.tool_warning.call_args[0][0])

    def test_hooks_command(self):
        with GitTemporaryDirectory():
            make_repo()
            io = InputOutput(yes=True)
            io.tool_output = MagicMock()
            hooks = make_hooks(io, ("PreToolUse", "bash", "block"))
            coder = make_coder(io, hooks=hooks)
            coder.commands.run("/hooks")
            output = "\n".join(str(c[0][0]) for c in io.tool_output.call_args_list if c[0])
            self.assertIn("PreToolUse bash:", output)
            self.assertIn("hook.py block", output)
            self.assertIn("Hooks: 1 PreToolUse", "\n".join(coder.get_announcements()))


if __name__ == "__main__":
    unittest.main()

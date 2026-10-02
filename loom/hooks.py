"""
Hooks: shell commands loom runs before or after the agent uses a tool.

They are configured the way Claude Code does it, in ~/.loom/hooks.json (yours, for every
project) and the project's .loom/hooks.json (shared with the repo):

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "bash",
            "hooks": [{"type": "command", "command": "python scripts/check_command.py"}]
          }
        ],
        "PostToolUse": [
          {
            "matcher": "edit_file|write_file",
            "hooks": [{"type": "command", "command": "ruff format \\"$LOOM_FILE_PATH\\""}]
          }
        ]
      }
    }

matcher is a regular expression for the whole tool name (bash, edit_file, write_file,
read_file, list_dir, glob, grep, todo_write or mcp__SERVER__TOOL), ignoring case. Leave it
out, or use "" or "*", to match every tool.

A hook gets the tool call as JSON on stdin: hook_event_name, tool_name, tool_input, cwd,
session_id, permission_mode and, after the tool ran, tool_response. It runs in the project
root with LOOM_PROJECT_DIR, LOOM_HOOK_EVENT, LOOM_TOOL_NAME and, for tools with a path,
LOOM_FILE_PATH set.

Its exit code says what to do:
- 0: carry on. A PreToolUse hook can print {"decision": "allow"} to skip asking the user
  (plan mode and protected files still apply) or {"decision": "block", "reason": "..."}.
- 2: PreToolUse: don't run the tool; the hook's stderr tells the model why.
  PostToolUse: the tool already ran; stderr is sent to the model with its result.
- anything else: the hook failed. loom warns and carries on.

The project's hooks come with the repo, so loom asks before running them, and remembers
"always" in ~/.loom/hooks-approvals.json until they change.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loom.display import sanitize_for_display
from loom.tools import finish_killed, kill_process_tree

EVENTS = ("PreToolUse", "PostToolUse")
PROJECT_CONFIG = ".loom/hooks.json"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
# Exit code for "block this tool call" (PreToolUse) or "tell the model" (PostToolUse)
BLOCK_EXIT_CODE = 2
MAX_FEEDBACK_CHARS = 10_000


class HookError(Exception):
    pass


def user_config_file():
    return Path.home() / ".loom" / "hooks.json"


def approvals_file():
    return Path.home() / ".loom" / "hooks-approvals.json"


@dataclass
class Hook:
    event: str
    matcher: str
    command: str
    timeout: int
    source: str
    is_project_hook: bool = False

    def __post_init__(self):
        pattern = self.matcher.strip() if self.matcher else ""
        if pattern in ("", "*"):
            self.regex = None
            return
        try:
            self.regex = re.compile(pattern, re.IGNORECASE)
        except re.error as err:
            raise HookError(f"{self.source}: invalid matcher {self.matcher!r}: {err}")

    def matches(self, tool_name):
        return self.regex is None or self.regex.fullmatch(tool_name) is not None

    def describe(self):
        """A one-liner for the approval prompt. Sanitized so a hook command with escape
        sequences can\'t disguise what the user is about to approve."""
        line = f"{self.event} {self.matcher or '*'}: {self.command}"
        return sanitize_for_display(line, show_escapes=True)

    def config(self, root=None):
        """What goes into the approval hash. Includes the content of any local script this
        hook names, so the user is re-prompted if a hook that runs \'python scripts/x.py\'
        later has its scripts/x.py edited to do something else."""
        return [
            self.event,
            self.matcher,
            self.command,
            self.timeout,
            referenced_script_hash(self.command, root),
        ]


# Common ways a hook names a script to run: a direct interpreter, env prefix, or just the
# script path. The matched token is the first candidate; we also check argv[0].
SCRIPT_RUNNERS = {
    "python",
    "python2",
    "python3",
    "py",
    "pwsh",
    "powershell",
    "ruby",
    "node",
    "deno",
    "bun",
    "perl",
    "lua",
    "php",
    "bash",
    "sh",
    "zsh",
    "fish",
    "dash",
    "ksh",
    "awk",
    "sed",
    "tclsh",
    "osascript",
}


def referenced_script_hash(command, root):
    """A sha256 of each local script this command names, or "" when there\'s none to find.

    When the hook is \'python scripts/check.py\' and scripts/check.py is inside the
    project, its content goes into the hash: editing the script after the user approved
    the hook will invalidate the approval, so the user is asked again."""
    if not isinstance(command, str) or not command.strip():
        return ""
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    # Skip VAR=value env assignments at the front
    i = 0
    while i < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[i]):
        i += 1
    if i >= len(tokens):
        return ""

    cwd = Path(root) if root else None
    hashes = []
    first = Path(tokens[i]).name.lower()
    runner_arg = (
        tokens[i + 1]
        if first in SCRIPT_RUNNERS and i + 1 < len(tokens) and not tokens[i + 1].startswith("-")
        else None
    )
    for candidate in (tokens[i], runner_arg):
        if not candidate:
            continue
        for base in ((cwd,) if cwd else ()) + (None,):
            try:
                path = (base / candidate).resolve() if base else Path(candidate).resolve()
            except (OSError, RuntimeError):
                continue
            if cwd is not None:
                try:
                    cwd_real = cwd.resolve()
                except (OSError, RuntimeError):
                    cwd_real = None
                if cwd_real and cwd_real not in path.parents and path != cwd_real:
                    continue
            try:
                if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            hashes.append(f"{candidate}:{hashlib.sha256(data).hexdigest()}")
            break
    return "|".join(hashes)


def load_config_file(path, is_project=False):
    """The hooks in a config file. Accepts Claude Code's layout, with or without the outer
    "hooks" object, and entries with a command instead of a list of hooks."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise HookError(f"Unable to read {path}: {err}")
    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        data = data["hooks"]
    if not isinstance(data, dict):
        raise HookError(f"{path} should hold a JSON object with a hooks object")

    res = []
    for event, entries in data.items():
        if event not in EVENTS:
            raise HookError(f"{path}: unknown hook event {event!r}; use {' or '.join(EVENTS)}")
        if not isinstance(entries, list):
            raise HookError(f"{path}: {event} should be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise HookError(f"{path}: each {event} entry should be an object")
            matcher = entry.get("matcher") or ""
            if not isinstance(matcher, str):
                raise HookError(f"{path}: matcher should be a string")
            hooks = entry.get("hooks")
            if hooks is None and entry.get("command"):
                hooks = [entry]
            if not isinstance(hooks, list):
                raise HookError(f"{path}: each {event} entry needs a hooks list or a command")
            for hook in hooks:
                res.append(parse_hook(path, event, matcher, hook, is_project))
    return res


def parse_hook(path, event, matcher, hook, is_project):
    if not isinstance(hook, dict):
        raise HookError(f"{path}: each hook should be an object with a command")
    if hook.get("type", "command") != "command":
        raise HookError(f"{path}: only hooks of type command are supported")
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        raise HookError(f"{path}: a {event} hook has no command")
    timeout = hook.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise HookError(f"{path}: timeout should be a number of seconds")
    return Hook(
        event,
        matcher,
        command.strip(),
        min(int(timeout), MAX_TIMEOUT),
        str(path),
        is_project,
    )


@dataclass
class HookOutcome:
    """What the hooks for one tool call decided."""

    # "block" (don't run the tool), "allow" (don't ask the user) or None
    decision: str = None
    # Why a PreToolUse hook blocked, or what the PostToolUse hooks want the model to know
    message: str = ""


def parse_output(stdout):
    """(decision, message) from a hook's JSON output, if it printed any. Understands loom's
    {"decision", "reason"} and Claude Code's hookSpecificOutput."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None, ""
    if not isinstance(data, dict):
        return None, ""

    decision = data.get("decision")
    message = data.get("reason") or ""
    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict):
        decision = specific.get("permissionDecision") or decision
        message = specific.get("permissionDecisionReason") or message
        message = message or specific.get("additionalContext") or ""
    decision = {"deny": "block", "block": "block", "allow": "allow", "approve": "allow"}.get(
        str(decision).lower()
    )
    return decision, str(message)


def run_hook(hook, payload, cwd, env):
    """Run a hook with the payload on stdin. Returns (exit code or None on timeout, stdout,
    stderr)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            hook.command,
            shell=True,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    except OSError as err:
        return 127, "", str(err)

    try:
        out, err = proc.communicate(json.dumps(payload).encode(), timeout=hook.timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        out, err = finish_killed(proc)
        code = None
    except KeyboardInterrupt:
        kill_process_tree(proc)
        finish_killed(proc)
        raise
    return code, decode(out), decode(err)


def decode(data):
    return (data or b"").decode("utf-8", errors="replace").strip()


class Hooks:
    """The configured hooks, and running them around the agent's tool calls."""

    def __init__(self, io, hooks=None, root=None, config_files=()):
        self.io = io
        self.root = root
        self.hooks = list(hooks or [])
        # (path, is_project) for each file to reload when it changes
        self.config_files = list(config_files)
        self.mtimes = self.get_mtimes()
        # None until the user is asked about the project's hooks
        self.project_approved = None

    @classmethod
    def from_config(cls, io, root, use_default_files=True):
        files = []
        if use_default_files:
            files.append((user_config_file(), False))
            if root:
                files.append((Path(root) / PROJECT_CONFIG, True))
        res = cls(io, root=root, config_files=files)
        res.hooks = res.load()
        return res

    def load(self):
        """Read the config files. A broken file is skipped with a warning."""
        hooks = []
        for path, is_project in self.config_files:
            if not path.is_file():
                continue
            try:
                hooks += load_config_file(path, is_project)
            except HookError as err:
                self.io.tool_warning(str(err))
        return hooks

    def get_mtimes(self):
        res = []
        for path, _ in self.config_files:
            try:
                res.append(path.stat().st_mtime)
            except OSError:
                res.append(None)
        return res

    def refresh(self):
        """Reload the config files if they changed, so hooks can be edited mid-session."""
        mtimes = self.get_mtimes()
        if mtimes == self.mtimes:
            return
        self.mtimes = mtimes
        old = self.project_hash()
        self.hooks = self.load()
        if self.project_hash() != old:
            self.project_approved = None

    # Approving the project's hooks

    def project_hooks(self):
        return [hook for hook in self.hooks if hook.is_project_hook]

    def project_hash(self):
        configs = [hook.config(self.root) for hook in self.project_hooks()]
        return hashlib.sha256(json.dumps(configs).encode()).hexdigest()

    def load_approvals(self):
        try:
            data = json.loads(approvals_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def project_key(self):
        return str(Path(self.root).resolve())

    def is_approved(self):
        return self.load_approvals().get(self.project_key()) == self.project_hash()

    def save_approval(self):
        data = self.load_approvals()
        data[self.project_key()] = self.project_hash()
        try:
            path = approvals_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as err:
            self.io.tool_warning(f"Unable to save the approval to {approvals_file()}: {err}")

    def start(self):
        """Before a request: reload changed config and ask about new project hooks."""
        self.refresh()
        if self.project_approved is not None or not self.project_hooks():
            return
        if self.is_approved():
            self.project_approved = True
            return
        answer = self.io.permission_ask(
            f"Run the hooks in this project's {PROJECT_CONFIG}?",
            subject="\n".join(hook.describe() for hook in self.project_hooks()),
            always="trust them in this project",
            explicit_yes_required=True,
        )
        if answer == "always":
            self.save_approval()
        self.project_approved = answer in ("yes", "always")
        if not self.project_approved:
            self.io.tool_output(
                f"The hooks in {PROJECT_CONFIG} won't run this session; loom asks again if"
                " they change."
            )

    def active(self):
        return [hook for hook in self.hooks if not hook.is_project_hook or self.project_approved]

    def matching(self, event, tool_name):
        return [hook for hook in self.active() if hook.event == event and hook.matches(tool_name)]

    # Running them

    def run(self, event, coder, tool_name, tool_input, action, tool_response=None):
        """Run the hooks for an event and a tool call. Returns a HookOutcome."""
        hooks = self.matching(event, tool_name)
        outcome = HookOutcome()
        if not hooks:
            return outcome

        root = str(coder.root)
        payload = dict(
            hook_event_name=event,
            session_id=coder.session.id,
            cwd=root,
            permission_mode=coder.permissions.mode,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        if tool_response is not None:
            payload["tool_response"] = tool_response
        env = dict(
            os.environ,
            LOOM_PROJECT_DIR=root,
            LOOM_HOOK_EVENT=event,
            LOOM_TOOL_NAME=tool_name,
        )
        if action is not None and action.path:
            env["LOOM_FILE_PATH"] = str(action.path)

        messages = []
        for hook in hooks:
            code, out, err = run_hook(hook, payload, root, env)
            if code is None:
                self.io.tool_warning(
                    f"The {event} hook `{hook.command}` timed out after {hook.timeout}s."
                )
                continue
            if code == BLOCK_EXIT_CODE:
                decision, message = "block", err or out
            elif code == 0:
                decision, message = parse_output(out)
            else:
                detail = (err or out).splitlines()[-1:] or ["no output"]
                self.io.tool_warning(
                    f"The {event} hook `{hook.command}` failed (exit {code}): {detail[0]}"
                )
                continue

            message = message[:MAX_FEEDBACK_CHARS]
            if event == "PreToolUse":
                if decision == "block":
                    return HookOutcome("block", message or "(the hook gave no reason)")
                if decision == "allow":
                    outcome.decision = "allow"
            elif decision == "block" or message:
                messages.append(message)

        outcome.message = "\n\n".join(messages)
        return outcome

    def summary(self):
        counts = {}
        for hook in self.hooks:
            counts[hook.event] = counts.get(hook.event, 0) + 1
        res = ", ".join(f"{num} {event}" for event, num in counts.items())
        if self.project_hooks() and self.project_approved is False:
            res += f" (the {PROJECT_CONFIG} hooks are off: not approved)"
        return res

    def show(self):
        if not self.hooks:
            self.io.tool_output(
                f"No hooks are configured. Add them to {user_config_file()} or the project's"
                f" {PROJECT_CONFIG}."
            )
            return
        for hook in self.hooks:
            status = ""
            if hook.is_project_hook and self.project_approved is False:
                status = "  [not approved]"
            elif hook.is_project_hook and self.project_approved is None:
                status = "  [asks before the next request]"
            self.io.tool_output(f"{hook.describe()}{status}")
            self.io.tool_output(f"  from {hook.source}, timeout {hook.timeout}s")

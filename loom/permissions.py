"""
Decide which agent actions run without asking the user.

Modes:
- ask: reads inside the project run freely; edits and shell commands ask first.
- accept-edits: edits inside the project also run freely; shell commands still ask.
- plan: read-only. Edits and shell commands are refused, so the agent can only plan.

Allow rules skip the question for matching actions in ask and accept-edits mode:
  bash(pytest*)         shell commands matching a pattern (* matches anything)
  bash                  every shell command
  edit(src/**/*.py)     edits to files matching a glob, relative to the project root
  read(/etc/hosts)      reads outside the project
  mcp(github)           every tool of an MCP server
  mcp(github__get_*)    MCP tools matching a pattern, as SERVER__TOOL

MCP tools ask in ask and accept-edits mode. In plan mode only the ones their server
marks read-only run.

Rules come from --allow (or `allow:` in .loom.conf.yml) and from answering "always",
which saves the rule to .loom.permissions.json in the project root.

A PreToolUse hook (see loom/hooks.py) can also allow an action, like a rule would.

Edits to protected files always ask and need an explicit yes, whatever the mode, rules or
hooks: git internals (a hook runs on the next commit) and loom's own config (it can grant
rules, add hooks and custom commands).
"""

import glob as globlib
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

from loom.tools import glob_match

MODES = {
    "ask": "edits and shell commands need approval",
    "accept-edits": "edits are applied without asking, shell commands need approval",
    "plan": "read-only, edits and shell commands are refused",
}

KINDS = ("read", "edit", "bash", "mcp")

SETTINGS_FILE = ".loom.permissions.json"

RULE_RE = re.compile(r"^\s*(\w+)\s*(?:\((.*)\))?\s*$", re.DOTALL)

# Redirections that can't write to files, allowed inside otherwise-allowed commands
SAFE_REDIRECT_RE = re.compile(r"(?<![\w>&])\d?>&\d\b|(?<![\w>&])\d?>\s*/dev/null\b")

WILDCARD_RE = re.compile(r"[*?[]")

PROTECTED_PATHS = [
    "**/.git",
    "**/.git/**",
    # loom's config files, and .loom/ with the hooks and custom commands
    "**/.loom*",
    "**/.loom*/**",
    "**/.env",
    "**/.env.*",
]


def is_protected(path):
    return any(glob_match(pattern, path) for pattern in PROTECTED_PATHS)


class Rule:
    def __init__(self, kind, pattern=None):
        self.kind = kind
        self.pattern = pattern

    @classmethod
    def parse(cls, text):
        match = RULE_RE.match(text or "")
        if not match or match.group(1) not in KINDS:
            raise ValueError(
                f"Invalid permission rule {text!r}; use bash(COMMAND), edit(PATH), read(PATH) or"
                " mcp(SERVER or SERVER__TOOL), where * is a wildcard"
            )
        kind, pattern = match.groups()
        if pattern is not None:
            pattern = pattern.strip()
            if not pattern or pattern == "*":
                pattern = None
        return cls(kind, pattern)

    def __str__(self):
        if self.pattern is None:
            return self.kind
        return f"{self.kind}({self.pattern})"

    def __eq__(self, other):
        return isinstance(other, Rule) and str(self) == str(other)

    def __hash__(self):
        return hash(str(self))

    def is_exact(self):
        """True for patterns with no wildcards, like the commands saved by "always"."""
        pattern = re.sub(r"\[[*?[]\]", "", self.pattern or "")
        return self.pattern is not None and not WILDCARD_RE.search(pattern)

    def matches_path(self, path):
        return self.pattern is None or glob_match(self.pattern, path)


def split_command(command):
    """Split a shell command into the simple commands joined by ; && || | & or newlines.

    Returns None if the command uses anything that could hide another command or write a
    file: command substitution, process substitution, redirection or unbalanced quotes.
    """
    command = SAFE_REDIRECT_RE.sub(" ", command)
    segments = []
    cur = ""
    quote = None
    i = 0
    while i < len(command):
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            cur += c
            i += 1
            continue
        if c == "\\":
            cur += command[i : i + 2]
            i += 2
            continue
        if c == "`" or command.startswith("$(", i):
            return None
        if quote == '"':
            if c == '"':
                quote = None
            cur += c
            i += 1
            continue
        if c in "'\"":
            quote = c
        elif c in "<>":
            return None
        elif c in ";|&\n":
            segments.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1

    if quote:
        return None
    segments.append(cur)
    return [seg.strip() for seg in segments if seg.strip()]


def exact_rule(kind, target):
    """A rule that matches exactly this target and nothing else."""
    return Rule(kind, globlib.escape(target))


class Permissions:
    def __init__(self, io, mode="ask", allow=None, settings_file=None):
        if mode not in MODES:
            raise ValueError(f"Unknown permission mode {mode!r}; use one of: {', '.join(MODES)}")
        self.io = io
        self.mode = mode
        self.settings_file = Path(settings_file) if settings_file else None

        # (rule, where it came from)
        self.rules = []
        for text in allow or []:
            self.add_rule(Rule.parse(text), "--allow or config")
        self.load()

    def add_rule(self, rule, source):
        if rule not in [r for r, _ in self.rules]:
            self.rules.append((rule, source))

    def load(self):
        if not self.settings_file or not self.settings_file.exists():
            return
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            for text in data.get("allow", []):
                self.add_rule(Rule.parse(text), self.settings_file.name)
        except (OSError, ValueError, AttributeError) as err:
            self.io.tool_warning(f"Ignoring {self.settings_file}: {err}")

    def save_rule(self, rule):
        """Remember an allow rule for this project in the settings file."""
        self.add_rule(rule, self.settings_file.name if self.settings_file else "session")
        if not self.settings_file:
            return
        try:
            data = {}
            if self.settings_file.exists():
                data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            allow = data.setdefault("allow", [])
            if str(rule) not in allow:
                allow.append(str(rule))
            self.settings_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, AttributeError) as err:
            self.io.tool_warning(f"Unable to save the rule to {self.settings_file}: {err}")

    def rules_for(self, kind):
        return [rule for rule, _ in self.rules if rule.kind == kind]

    def is_allowed(self, action):
        """Whether an allow rule covers this action."""
        rules = self.rules_for(action.kind)
        if action.kind == "mcp":
            # mcp(github) covers every tool of the github server
            return any(
                rule.matches_path(action.target)
                or (rule.pattern and glob_match(rule.pattern + "__*", action.target))
                for rule in rules
            )
        if action.kind != "bash":
            return any(rule.matches_path(action.target) for rule in rules)

        command = action.target
        if any(rule.pattern is None for rule in rules):
            return True
        if any(rule.is_exact() and fnmatchcase(command, rule.pattern) for rule in rules):
            return True
        segments = split_command(command)
        if not segments:
            return False
        return all(any(fnmatchcase(seg, rule.pattern) for rule in rules) for seg in segments)

    def decide(self, action, hook_allowed=False):
        """Returns "allow", "ask" or "deny" for an action, without asking anyone.
        hook_allowed means a PreToolUse hook approved it, which works like an allow rule."""
        if action.kind == "todo":
            # Only changes loom's own to-do list
            return "allow"
        if action.kind == "read":
            if action.inside or hook_allowed or self.is_allowed(action):
                return "allow"
            return "ask"

        if self.mode == "plan":
            if action.kind == "mcp" and action.extra.get("read_only"):
                return "allow"
            return "deny"
        if action.kind == "edit" and is_protected(action.target):
            return "ask"
        if hook_allowed or self.is_allowed(action):
            return "allow"
        if action.kind == "edit" and action.inside and self.mode == "accept-edits":
            return "allow"
        return "ask"

    def request(self, action, hook_allowed=False):
        """Decide on an action, asking the user if needed.

        Returns (outcome, message) where outcome is "allow", "deny" (refused by the mode,
        the agent should carry on) or "user-deny" (the user said no, the agent should stop
        and wait for them). message explains a refusal to the model.
        """
        decision = self.decide(action, hook_allowed)
        if decision == "allow":
            return "allow", ""
        if decision == "deny":
            return (
                "deny",
                (
                    "Refused: loom is in plan mode, which only allows reading and searching. Don't"
                    " try to make changes; finish investigating and present your plan. The user can"
                    " switch modes with /permissions."
                ),
            )

        if action.kind == "read":
            question = f"Allow reading {action.target} (outside the project)?"
            always = None
        elif action.kind == "edit":
            question = f"{'Create' if action.new_file else 'Edit'} {action.target}?"
            always = None
            if is_protected(action.target):
                question += " (a protected file: it can change how git or loom runs)"
            elif not action.inside:
                question += " (outside the project)"
            else:
                always = "accept all edits this session"
        elif action.kind == "mcp":
            server, _, tool = action.target.partition("__")
            question = f"Use the {server} MCP tool {tool}?"
            always = f"always allow this tool (saved to {SETTINGS_FILE})"
        else:
            question = "Run this command?"
            always = f"always allow this command (saved to {SETTINGS_FILE})"

        if action.kind in ("edit", "mcp"):
            subject = action.preview
        elif "\n" in action.target or len(action.target) > 60:
            # Too long for the one-line summary of the call shown before the question
            subject = action.target
        else:
            subject = None
        explicit = action.kind in ("bash", "mcp") or (
            action.kind == "edit" and is_protected(action.target)
        )
        answer = self.io.permission_ask(
            question,
            subject=subject,
            always=always,
            explicit_yes_required=explicit,
        )

        if answer == "always":
            if action.kind == "edit":
                self.mode = "accept-edits"
                self.io.tool_output(
                    "Edits in the project will be applied without asking for the rest of this"
                    " session. Use /permissions ask to go back."
                )
            else:
                self.save_rule(exact_rule(action.kind, action.target))
            return "allow", ""
        if answer == "yes":
            return "allow", ""

        if explicit and self.io.yes is True:
            message = (
                "Refused: shell commands, MCP tools and edits to protected files need explicit"
                " approval, and --yes-always doesn't give it. Allow commands with --allow"
                " 'bash(PATTERN)' and MCP tools with --allow 'mcp(SERVER)'."
            )
            self.io.tool_warning(message)
            return "user-deny", message
        return (
            "user-deny",
            (
                "The user denied this action. Stop and wait for the user to tell you how to"
                " proceed; don't try to work around it."
            ),
        )

    def cycle_mode(self):
        """Switch to the next mode: ask, accept-edits, plan, then ask again."""
        modes = list(MODES)
        self.mode = modes[(modes.index(self.mode) + 1) % len(modes)]
        return self.mode

    def describe(self):
        return f"{self.mode} ({MODES[self.mode]})"

    def show(self):
        self.io.tool_output(f"Permission mode: {self.describe()}")
        if self.rules:
            self.io.tool_output("Allow rules:")
            for rule, source in self.rules:
                self.io.tool_output(f"  {rule}  [{source}]")
        else:
            self.io.tool_output("No allow rules.")

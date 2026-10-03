"""
Decide which agent actions run without asking the user.

Modes:
- ask: reads inside the project run freely; edits and shell commands ask first.
- accept-edits: edits inside the project also run freely; shell commands still ask.
- plan: read-only. Edits and shell commands are refused, so the agent can only plan.
- bypass: everything runs without asking, protected files and loom's other questions
  included. Answering (B)ypass to any approval question turns it on for the session.

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

The project's rules come with the repo (.loom.permissions.json, and `allow:` in a
.loom.conf.yml or .env inside the project), so loom asks before using them, and remembers
"always" in ~/.loom/permissions-approvals.json. Rules the user adds with "always" or
/permissions allow are approved as they're saved.

A PreToolUse hook (see loom/hooks.py) can also allow an action, like a rule would.

Edits to protected files always ask and need an explicit yes, whatever the mode, rules or
hooks: git internals (a hook runs on the next commit) and loom's own config (it can grant
rules, add hooks and custom commands).
"""

import glob as globlib
import json
import os
import re
import unicodedata
from fnmatch import fnmatchcase
from pathlib import Path

from loom.tools import glob_match

# User-maintained list of MCP tools loom treats as read-only in plan mode: a JSON file
# whose "allow" field holds mcp(SERVER) or mcp(SERVER__TOOL) entries, with * wildcards
MCP_READONLY_FILE = "mcp-readonly.json"

MODES = {
    "ask": "edits and shell commands need approval",
    "accept-edits": "edits are applied without asking, shell commands need approval",
    "plan": "read-only, edits and shell commands are refused",
    "bypass": "everything runs without asking",
}

# The modes Shift-Tab cycles through; bypass is only chosen on purpose
CYCLED_MODES = ("ask", "accept-edits", "plan")

KINDS = ("read", "edit", "bash", "mcp")

SETTINGS_FILE = ".loom.permissions.json"

RULE_RE = re.compile(r"^\s*(\w+)\s*(?:\((.*)\))?\s*$", re.DOTALL)

# Redirections that can't write to files, allowed inside otherwise-allowed commands. The
# target has to end the word: >&1.txt writes a file called 1.txt
SAFE_REDIRECT_RE = re.compile(r"(?<![\w>&])\d?>(?:&\d|\s*/dev/null)(?![^\s;&|])")
# The same for cmd.exe, which has nul instead of /dev/null
SAFE_CMD_REDIRECT_RE = re.compile(r"(?<![\w>&])\d?>(?:&\d|\s*nul)(?![^\s;&|])", re.IGNORECASE)

WILDCARD_RE = re.compile(r"[*?[]")

# Shell syntax split_command doesn't parse, so commands using it never match a rule:
# ANSI-C $'...' and locale $"..." quotes (\' doesn't end them), here-documents and
# process substitution
UNPARSED_SHELL_RE = re.compile(r"\$'|\$\"|<<\s*\S|\$<|<\(|>\(")

# cmd.exe, which runs commands on Windows, doesn't treat ' or \ as quoting, escapes with ^
# and expands %VARIABLES% before splitting the line
UNPARSED_CMD_CHARS = "'\"^%!`"

# Characters HFS+ ignores in file names, so .g\u200cit is the .git directory there
IGNORED_NAME_CHARS_RE = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")

PROTECTED_PATHS = [
    "**/.git",
    "**/.git/**",
    # loom's config files, and .loom/ with the hooks and custom commands
    "**/.loom*",
    "**/.loom*/**",
    "**/.env",
    "**/.env.*",
]


def canonical_path(path):
    """path as a case-insensitive file system (APFS, NTFS) sees it, so .GIT/hooks or
    .Loom.conf.yml can't dodge the protected patterns: lower case, NFC, / separators, no
    HFS+-ignored characters, and no trailing dots, spaces or :streams, which Windows drops."""
    text = unicodedata.normalize("NFC", str(path)).replace("\\", "/")
    text = unicodedata.normalize("NFC", IGNORED_NAME_CHARS_RE.sub("", text).casefold())
    parts = []
    for part in text.split("/"):
        if part not in (".", ".."):
            part = part.split(":", 1)[0] if ":" in part[1:] else part
            part = part.rstrip(". ") or part
        parts.append(part)
    return "/".join(parts)


def is_protected(path):
    path = canonical_path(path)
    return any(glob_match(pattern, path) for pattern in PROTECTED_PATHS)


def protects(action):
    """Whether an edit changes a protected file, by its project path or its real path."""
    if action.kind != "edit":
        return False
    return is_protected(action.target) or (action.path is not None and is_protected(action.path))


def approvals_file():
    return Path.home() / ".loom" / "permissions-approvals.json"


def mcp_readonly_file():
    return Path.home() / ".loom" / MCP_READONLY_FILE


def load_mcp_readonly_rules():
    """Rules from ~/.loom/mcp-readonly.json, which the user maintains. The server's own
    annotations.readOnlyHint is never trusted here: a buggy or hostile server could claim
    a destructive tool is safe, and in plan mode loom would run it with no prompt."""
    try:
        data = json.loads(mcp_readonly_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rules = []
    for text in (data.get("allow") if isinstance(data, dict) else None) or []:
        try:
            rule = Rule.parse(text)
        except (ValueError, TypeError):
            continue
        if rule.kind == "mcp":
            rules.append(rule)
    return rules


def mcp_action_matches_rule(action, rule):
    """Whether a Rule matches an MCP action. Uses the (server, tool) names from the
    action\'s extra, so a server named \'github\' can\'t be shadowed by one with __ in its
    name (which load_config_file now rejects anyway), and so a rule written exactly as
    mcp(SERVER) or mcp(SERVER__TOOL) matches only that pair."""
    server = action.extra.get("server") if action.extra else None
    tool = action.extra.get("tool") if action.extra else None
    server_name = getattr(server, "name", None)
    tool_name = tool.get("name") if isinstance(tool, dict) else None
    if server_name is None or tool_name is None:
        # Fall back to the string target; still safe since \'__\' in server names is rejected
        if rule.pattern is None:
            return True
        return glob_match(rule.pattern, action.target) or glob_match(
            rule.pattern + "__*", action.target
        )
    if rule.pattern is None:
        return True
    # mcp(github) covers every tool of the github server; mcp(github__get_*) picks tools
    pattern = rule.pattern
    if "__" not in pattern:
        return glob_match(pattern, server_name)
    rule_server, _, rule_tool = pattern.partition("__")
    return glob_match(rule_server, server_name) and glob_match(rule_tool, tool_name)


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


def split_command(command, windows=None):
    """Split a shell command into the simple commands joined by ; && || | & or newlines.

    Returns None if the command uses anything that could hide another command or write a
    file: command substitution, process substitution, redirection, unbalanced quotes, or
    quoting this parser doesn't understand. It fails closed: such commands need a rule
    that allows every command, or the user's approval.
    """
    if windows is None:
        windows = os.name == "nt"
    if UNPARSED_SHELL_RE.search(command):
        return None
    if windows:
        # No quotes or escapes, so splitting on the separators is all cmd.exe would do
        command = SAFE_CMD_REDIRECT_RE.sub(" ", command)
        if any(c in command for c in UNPARSED_CMD_CHARS + "<>") or "$(" in command:
            return None
        segments = re.split(r"[;|&\n]", command)
        return [seg.strip() for seg in segments if seg.strip()]

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
    def __init__(self, io, mode="ask", allow=None, settings_file=None, project_allow=None):
        """allow holds the user's rules (--allow or their own config). project_allow holds
        rules from config files that came with the repo, which need approval like the ones in
        settings_file."""
        if mode not in MODES:
            raise ValueError(f"Unknown permission mode {mode!r}; use one of: {', '.join(MODES)}")
        self.io = io
        self.mode = mode
        self.settings_file = Path(settings_file) if settings_file else None

        # (rule, where it came from)
        self.rules = []
        for text in allow or []:
            self.add_rule(Rule.parse(text), "--allow or config")
        # The project's rules that aren't approved yet, used once the user approves them
        self.pending = []
        for text, source in project_allow or []:
            self.add_project_rule(Rule.parse(text), source)
        self.load()
        # None until the user is asked about the pending rules
        self.project_approved = None

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, mode):
        self._mode = mode
        # In bypass mode loom's other yes/no questions don't ask either
        self.io.bypass_permissions = mode == "bypass"

    def copy_for(self, io):
        """The same permissions for another io, like a parallel builder's: the same mode
        and the same rules (an "always" answer to one is an answer for all). It doesn't
        ask about the project's rules again."""
        copy = Permissions(io, mode=self.mode)
        copy.settings_file = self.settings_file
        copy.rules = self.rules
        copy.pending = []
        copy.project_approved = bool(self.project_approved)
        return copy

    def add_rule(self, rule, source):
        if rule not in [r for r, _ in self.rules]:
            self.rules.append((rule, source))

    def add_project_rule(self, rule, source):
        """A rule that came with the repo: used right away if the user approved it before."""
        if str(rule) in self.approved_rules():
            self.add_rule(rule, source)
        elif rule not in [r for r, _ in self.rules + self.pending]:
            self.pending.append((rule, source))

    def load(self):
        if not self.settings_file or not self.settings_file.exists():
            return
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            rules = [Rule.parse(text) for text in data.get("allow", [])]
        except (OSError, ValueError, AttributeError, TypeError) as err:
            self.io.tool_warning(f"Ignoring {self.settings_file}: {err}")
            return
        for rule in rules:
            self.add_project_rule(rule, self.settings_file.name)

    # Approving the project's rules

    def project_key(self):
        if not self.settings_file:
            return None
        return str(self.settings_file.parent.resolve())

    def load_approvals(self):
        try:
            data = json.loads(approvals_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def approved_rules(self):
        """The project's rules the user said to always trust."""
        approved = self.load_approvals().get(self.project_key()) if self.project_key() else None
        return set(approved) if isinstance(approved, list) else set()

    def save_approval(self, rules):
        key = self.project_key()
        if not key:
            return
        data = self.load_approvals()
        approved = data.get(key) if isinstance(data.get(key), list) else []
        data[key] = approved + [str(rule) for rule in rules if str(rule) not in approved]
        try:
            path = approvals_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as err:
            self.io.tool_warning(f"Unable to save the approval to {approvals_file()}: {err}")

    def start(self):
        """Before a request: ask about the project's rules that aren't approved yet."""
        if self.project_approved is not None or not self.pending:
            return
        sources = sorted({source for _, source in self.pending})
        answer = self.io.permission_ask(
            f"Use the allow rules from this project's {' and '.join(sources)}?",
            subject="\n".join(f"{rule}  [{source}]" for rule, source in self.pending),
            always="trust them in this project",
            explicit_yes_required=True,
        )
        if answer == "always":
            self.save_approval([rule for rule, _ in self.pending])
        self.project_approved = answer in ("yes", "always")
        if self.project_approved:
            for rule, source in self.pending:
                self.add_rule(rule, source)
            self.pending = []
        else:
            self.io.tool_output(
                "The project's allow rules won't be used this session; loom asks again next"
                " session."
            )

    def save_rule(self, rule):
        """Remember an allow rule for this project in the settings file. The user chose it,
        so it's approved too."""
        self.add_rule(rule, self.settings_file.name if self.settings_file else "session")
        self.pending = [(r, source) for r, source in self.pending if r != rule]
        if not self.settings_file:
            return
        self.save_approval([rule])
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
            return any(mcp_action_matches_rule(action, rule) for rule in rules)
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
        if action.kind in ("todo", "memory") or self.mode == "bypass":
            # Only loom's own to-do list or project memory, or the user said to stop asking
            return "allow"
        if action.kind == "read":
            if action.inside or hook_allowed or self.is_allowed(action):
                return "allow"
            return "ask"

        if self.mode == "plan":
            # Only user-maintained ~/.loom/mcp-readonly.json rules let an MCP tool run
            # without asking. The server\'s own readOnlyHint is advice, not permission:
            # a mislabeled or hostile tool could claim to be safe when it isn\'t.
            if action.kind == "mcp":
                for rule in load_mcp_readonly_rules():
                    if mcp_action_matches_rule(action, rule):
                        return "allow"
                return "ask"
            return "deny"
        if protects(action):
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
            if protects(action):
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

        if action.kind == "edit":
            # The diff only when asked for; otherwise the file and how many lines change
            subject = action.preview if self.io.agent_diffs else action.changes or action.preview
        elif action.kind == "mcp":
            subject = action.preview
        elif "\n" in action.target or len(action.target) > 60:
            # Too long for the one-line summary of the call shown before the question
            subject = action.target
        else:
            subject = None
        explicit = action.kind in ("bash", "mcp") or protects(action)
        answer = self.io.permission_ask(
            question,
            subject=subject,
            always=always,
            explicit_yes_required=explicit,
            bypass="stop asking for the rest of this session",
        )

        if answer == "bypass":
            self.mode = "bypass"
            self.io.tool_warning(
                "Bypassing permissions: edits, commands and everything else run without asking"
                " for the rest of this session. Use /permissions ask to go back."
            )
            return "allow", ""
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
        if self.mode not in CYCLED_MODES:
            self.mode = CYCLED_MODES[0]
        else:
            index = CYCLED_MODES.index(self.mode)
            self.mode = CYCLED_MODES[(index + 1) % len(CYCLED_MODES)]
        return self.mode

    def describe(self):
        return f"{self.mode} ({MODES[self.mode]})"

    def show(self):
        self.io.tool_output(f"Permission mode: {self.describe()}")
        if self.rules or self.pending:
            self.io.tool_output("Allow rules:")
            for rule, source in self.rules:
                self.io.tool_output(f"  {rule}  [{source}]")
            status = (
                "not approved" if self.project_approved is False else "asks before the next request"
            )
            for rule, source in self.pending:
                self.io.tool_output(f"  {rule}  [{source}, {status}]")
        else:
            self.io.tool_output("No allow rules.")

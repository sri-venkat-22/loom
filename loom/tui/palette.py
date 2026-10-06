"""
The / palette: the commands that match what's typed after /, best first, each with what
it does. /help lists the same commands from the same registry, grouped, so the two
can't drift apart.

Matching is by name first: /d lists the commands that start with d, /dif narrows to
/diff, and from two letters on, letters in order match too (/rwd finds /rewind). From
three letters on, a word in a command's description matches as well, ranked after the
names.
"""

import difflib
import re
from dataclasses import dataclass

from rich.text import Text

GROUPS = (
    ("agent", ("agent", "plan", "permissions", "todos", "tasks", "agents", "mcp", "hooks")),
    ("project", ("project",)),
    ("files", ("add", "drop", "read-only", "ls", "map", "map-refresh", "web", "paste")),
    ("conversation", ("clear", "reset", "compact", "rewind", "undo", "sessions", "resume", "copy")),
    ("modes", ("ask", "code", "architect", "context", "chat-mode", "ok")),
    ("shell and git", ("run", "test", "lint", "commit", "diff", "git")),
    ("usage", ("cost", "tokens")),
    (
        "settings",
        (
            "model",
            "models",
            "editor-model",
            "weak-model",
            "effort",
            "reasoning-effort",
            "think-tokens",
            "settings",
            "multiline-mode",
            "editor",
            "edit",
            "voice",
            "copy-context",
            "save",
            "load",
        ),
    ),
    ("help", ("help", "report", "exit", "quit")),
)
GROUP_OF = {name: group for group, names in GROUPS for name in names}
GROUP_ORDER = {group: num for num, (group, _) in enumerate(GROUPS)}


@dataclass(frozen=True)
class Entry:
    name: str  # like /diff
    description: str
    group: str


# A description longer than this is cut, at a word
MAX_DESCRIPTION = 72


def first_sentence(doc):
    """A command's description: the first line of its docstring, up to its first full
    stop, without the usage that follows a colon."""
    line = (doc or "").strip().split("\n", 1)[0].strip()
    match = re.match(r"(.+?\.)(?:\s|$)", line)
    line = (match.group(1)[:-1] if match else line).strip()
    line = re.split(r":\s+/|:\s*$", line)[0].strip()
    if len(line) > MAX_DESCRIPTION:
        line = line[: MAX_DESCRIPTION - 1].rsplit(" ", 1)[0].rstrip(",;:(") + "…"
    return line


def entries(commands):
    """Every command of commands (loom/commands.py), its description and its group."""
    res = []
    custom = commands.get_custom_commands()
    for name in commands.get_commands():
        bare = name[1:]
        method = getattr(commands, "cmd_" + bare.replace("-", "_"), None)
        if method is not None:
            description = first_sentence(method.__doc__)
            group = GROUP_OF.get(bare, "other")
        else:
            description = custom_description(bare, custom.get(bare))
            group = "custom"
        res.append(Entry(name, description, group))
    return res


def custom_description(name, found):
    if not found:
        return ""
    from loom.custom_commands import CustomCommand, CustomCommandError

    try:
        return CustomCommand.load(name, *found).help_text()
    except CustomCommandError as err:
        return f"Error: {err}"


def subsequence_span(query, name):
    """How many characters of name the letters of query span, in order, or None."""
    pos = start = -1
    for char in query:
        pos = name.find(char, pos + 1)
        if pos < 0:
            return None
        if start < 0:
            start = pos
    return pos - start + 1 if start >= 0 else 0


def rank(query, entry):
    """How well query (what's typed after /) matches entry: lower is better, None for no
    match."""
    query = query.lower()
    name = entry.name[1:].lower()
    if not query:
        # Everything, in /help's order
        return (0, GROUP_ORDER.get(entry.group, len(GROUP_ORDER)))
    if name.startswith(query):
        return (0,)
    span = subsequence_span(query, name) if len(query) >= 2 else None
    if span is not None:
        return (1, span, len(name))
    if len(query) >= 3:
        words = re.findall(r"[a-z0-9]+", entry.description.lower())
        if any(word.startswith(query) for word in words):
            return (2, len(name))
    return None


def matches(query, all_entries):
    """The entries query matches, best first."""
    ranked = []
    for entry in all_entries:
        score = rank(query, entry)
        if score is not None:
            ranked.append((score, entry.name, entry))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in ranked]


def suggest(word, names):
    """The command most like word, which isn't one, or None."""
    close = difflib.get_close_matches(word, names, n=1, cutoff=0.6)
    return close[0] if close else None


def help_lines(theme, all_entries, width=96):
    """The palette for /help: every command in its group, as rich Text lines."""
    order = [group for group, _ in GROUPS] + ["other", "custom"]
    pad = max((len(entry.name) for entry in all_entries), default=8) + 3
    lines = []
    for group in order:
        members = [entry for entry in all_entries if entry.group == group]
        if not members:
            continue
        if lines:
            lines.append(Text(""))
        rule = theme.glyph("rule")
        title = Text(f"  {rule}{rule} {group} ", style=theme.style("faint"))
        title.append(rule * max(0, width - len(title.plain) - 2), style=theme.style("edge"))
        lines.append(title)
        for entry in sorted(members, key=lambda entry: entry.name):
            line = Text("  ")
            line.append(f"{entry.name:{pad}}", style=theme.style("fg"))
            line.append(entry.description, style=theme.style("dim"))
            lines.append(line)
    return lines

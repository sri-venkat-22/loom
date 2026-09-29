"""
Custom slash commands: prompts you save as Markdown files and run by name.

    .loom/commands/review.md          /review, in this project (commit it to share it)
    ~/.loom/commands/explain.md       /explain, in every project
    .loom/commands/db/migrate.md      /db:migrate

Running one sends the file's text as your message. In it, $ARGUMENTS stands for everything
typed after the command and $1, $2, ... for its words (quote a phrase to keep it one
word). If the text uses neither, what you type is added to the end.

Optional YAML front matter describes the command for /help and can run it in another chat
mode, the way /ask does:

    ---
    description: Explain how a part of the code works
    argument-hint: <file or function>
    chat-mode: ask
    ---
    Explain how $ARGUMENTS works...

A project command overrides a personal one with the same name. loom's own commands, like
/help, can't be overridden.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

PROJECT_DIR = ".loom/commands"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
ARG_RE = re.compile(r"\$(ARGUMENTS|\d+)")
FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
CHAT_MODES = ("agent", "ask", "code", "architect", "context")


class CustomCommandError(Exception):
    pass


def user_dir():
    return Path.home() / ".loom" / "commands"


def command_dirs(root):
    """The directories holding custom commands, later ones overriding earlier ones."""
    dirs = [(user_dir(), "user")]
    if root:
        dirs.append((Path(root) / PROJECT_DIR, "project"))
    return dirs


def find_commands(root):
    """{name: (path, "user" or "project")} for every custom command file."""
    res = {}
    for directory, source in command_dirs(root):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if not path.is_file():
                continue
            parts = path.relative_to(directory).with_suffix("").parts
            name = ":".join(parts)
            if NAME_RE.match(name):
                res[name] = (path, source)
    return res


@dataclass
class CustomCommand:
    name: str
    path: Path
    source: str
    body: str
    description: str = ""
    argument_hint: str = ""
    chat_mode: str = ""

    @classmethod
    def load(cls, name, path, source):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise CustomCommandError(f"Unable to read {path}: {err}")

        meta = {}
        match = FRONT_MATTER_RE.match(text)
        if match:
            try:
                meta = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError as err:
                raise CustomCommandError(f"{path} has invalid front matter: {err}")
            if not isinstance(meta, dict):
                raise CustomCommandError(f"{path}: the front matter should be key: value lines")
            text = text[match.end() :]

        body = text.strip()
        if not body:
            raise CustomCommandError(f"{path} has no prompt")
        chat_mode = str(meta.get("chat-mode") or meta.get("chat_mode") or "").strip()
        if chat_mode and chat_mode not in CHAT_MODES:
            raise CustomCommandError(
                f"{path}: chat-mode should be one of {', '.join(CHAT_MODES)}, not {chat_mode!r}"
            )
        hint = meta.get("argument-hint") or meta.get("argument_hint") or ""
        if isinstance(hint, list):
            # An unquoted [hint] is YAML for a list
            hint = " ".join(f"[{item}]" for item in hint)
        return cls(
            name=name,
            path=Path(path),
            source=source,
            body=body,
            description=str(meta.get("description") or "").strip(),
            argument_hint=str(hint).strip(),
            chat_mode=chat_mode,
        )

    def expand(self, args):
        """The prompt, with the arguments filled in."""
        args = (args or "").strip()
        if not ARG_RE.search(self.body):
            return f"{self.body}\n\n{args}" if args else self.body

        try:
            words = shlex.split(args)
        except ValueError:
            words = args.split()

        def replace(match):
            key = match.group(1)
            if key == "ARGUMENTS":
                return args
            num = int(key)
            return words[num - 1] if 0 < num <= len(words) else ""

        return ARG_RE.sub(replace, self.body)

    def help_text(self):
        """The description shown by /help."""
        res = self.description or first_line(self.body)
        if self.argument_hint:
            res = f"{self.argument_hint}  {res}"
        return f"{res} ({self.source})"


def first_line(text, limit=70):
    line = text.strip().split("\n", 1)[0].strip()
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line

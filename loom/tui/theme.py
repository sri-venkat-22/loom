"""
The terminal's look as data: colours, glyphs, spinner frames, the logo and the gradient
stops of the effort track. Nothing that draws the terminal may hard-code any of them.

loom has two built-in themes, loom-dark (the default) and loom-light (--light-mode).
A theme file changes any part of one, with no code change:

    ~/.loom/theme.toml       yours, for every project
    .loom/theme.toml         the project's
    --theme PATH             one more, on top

each applied over the last. The keys are those of the built-in data below, in TOML:

    name = "loom-dark"              # the built-in to start from

    [color]
    accent = "#cf9a4c"

    [glyph]
    selected = "❯"

    [glyph.ascii]
    selected = ">"

    [spinner]
    style = "braille"               # blocks, weave, shuttle, braille or ascii

    [logo]
    intro = "weave"                 # how LOOM arrives at launch

A key loom doesn't know, or a value it can't use, is refused with the file and the key
named, and the file isn't used.

degrade() fits a theme to what the terminal can show: truecolor, then 256 colours, then
none (NO_COLOR, --no-pretty), unicode glyphs or their ASCII twins (dumb terminals,
encodings without them), and animation or none (--no-animation, pipes). It changes how
things are drawn, never what loom does.
"""

import codecs
import copy
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

INTROS = ("selvedge", "weave", "shuttle", "threading", "beatup", "fill", "none")

COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")

# The glyphs a unicode terminal must be able to show for the theme's own glyphs to be used
UNICODE_SAMPLE = "❯━┆▁█⏵◌≡⊞▣✓✗⊘⟳⊗▸…"

WORDMARK = (
    "██      ██████  ██████  ██   ██",
    "██      ██  ██  ██  ██  ███ ███",
    "██      ██  ██  ██  ██  ███████",
    "██      ██  ██  ██  ██  ██ █ ██",
    "██████  ██████  ██████  ██   ██",
)

DARK = {
    "name": "loom-dark",
    # The Pygments style of code in the model's replies
    "code_theme": "gruvbox-dark",
    "color": {
        # A warm charcoal ground, gold as stroke. "default" is the terminal's own colour:
        # loom never paints the terminal's background, so its text keeps its colour too.
        "ground": "#21201e",
        "inset": "#2a2825",
        "edge": "#3a3733",
        "fg": "default",
        "dim": "#958d84",
        "faint": "#605a53",
        "accent": "#cf9a4c",
        "accent_dim": "#8a6a34",
        "highlight": "#e0b064",
        "ok": "#8a9a63",
        "fail": "#c26a52",
        "info": "#6f9490",
        "selection": "#3a3226",
    },
    "gradient": {
        # The effort track, Faster to Smarter: teal through gold and back
        "effort": ["#6f9490", "#cf9a4c", "#e0b064", "#cf9a4c", "#6f9490"],
        # The same five stops for a 256-colour terminal, which shows them still
        "effort_256": [66, 137, 179, 215, 222],
        # One pass of the sweep across the track; 0 keeps it still
        "sweep_ms": 5500,
    },
    "glyph": {
        "prompt": ">",
        "selected": "❯",
        "mode": "⏵⏵",
        "cursor": "█",
        "warp": "┆",
        "weft": "━",
        "rule": "─",
        "tool": "●",
        "result": "⎿",
        "dot": "·",
        "arrow": "→",
        "ellipsis": "…",
        # The phases of /project fill in as they go: an empty ring, then lines, a grid,
        # a solid block, the block checked, and up it goes
        "idea": "◌",
        "planning": "≡",
        "design": "⊞",
        "building": "█",
        "testing": "▣",
        "launch": "▲",
        "ok": "✓",
        "fail": "✗",
        "hard_fail": "⊘",
        "cached": "⟳",
        "denied": "⊗",
        "queued": "·",
        "running": "▸",
        "review": "◆",
        "unevaluated": "—",
        "todo_done": "☒",
        "todo_active": "◼",
        "todo_pending": "☐",
        "slider_mark": "▲",
        "track": ["◀", "▶"],
        "bar": "█",
        "bar_track": "░",
        # NO_COLOR keeps the glyphs above. A dumb terminal, or one whose encoding lacks
        # them, gets these.
        "ascii": {
            "prompt": ">",
            "selected": ">",
            "mode": ">>",
            "cursor": "#",
            "warp": ":",
            "weft": "=",
            "rule": "-",
            "tool": "*",
            "result": "|",
            "dot": "-",
            "arrow": "->",
            "ellipsis": "...",
            "idea": "o",
            "planning": "=",
            "design": "#",
            "building": "@",
            "testing": "%",
            "launch": "^",
            "ok": "[ok]",
            "fail": "[x]",
            "hard_fail": "[!!]",
            "cached": "[~]",
            "denied": "[no]",
            "queued": ".",
            "running": ">",
            "review": "[?]",
            "unevaluated": "-",
            "todo_done": "[x]",
            "todo_active": "[>]",
            "todo_pending": "[ ]",
            "slider_mark": "^",
            "track": ["<", ">"],
            "bar": "#",
            "bar_track": ".",
        },
    },
    "spinner": {
        # One of the sets below, unless frames are given
        "style": "blocks",
        "frames": [],
        "ascii": ["|", "/", "-", "\\"],
        "interval_ms": 100,
    },
    "spinners": {
        "blocks": ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"],
        "weave": ["▘", "▝", "▗", "▖"],
        "shuttle": ["❯··", "·❯·", "··❯", "··❯", "·❯·", "❯··"],
        "braille": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"],
        "ascii": ["|", "/", "-", "\\"],
    },
    "logo": {
        # The shuttle crossing the warp: the same ❯ as the selection in every picker
        "mark": ["┆┆┆┆┆", "━━❯━━", "┆┆┆┆┆"],
        "mark_ascii": [":::::", "==>==", ":::::"],
        "glyph": "❯",
        "wordmark": list(WORDMARK),
        "wordmark_ascii": [row.replace("█", "#") for row in WORDMARK],
        # How LOOM arrives at launch: selvedge, weave, shuttle, threading, beatup, fill,
        # or none for no wordmark
        "intro": "selvedge",
    },
    "limits": {
        # No animation repaints faster than this
        "max_fps": 15,
    },
}

LIGHT_CHANGES = {
    "name": "loom-light",
    "code_theme": "gruvbox-light",
    "color": {
        "ground": "#f6f3ee",
        "inset": "#ece7df",
        "edge": "#d6cfc4",
        "dim": "#6f675e",
        "faint": "#9b9288",
        "accent": "#a8732b",
        "accent_dim": "#c9a46a",
        "highlight": "#c4862a",
        "ok": "#5f7a32",
        "fail": "#b0482f",
        "info": "#3d7570",
        "selection": "#efe2c9",
    },
    "gradient": {
        "effort": ["#3d7570", "#a8732b", "#c4862a", "#a8732b", "#3d7570"],
        "effort_256": [30, 94, 136, 172, 178],
    },
}


class ThemeError(Exception):
    """A theme file loom can't use."""


def merge(base, changes):
    """base with changes applied: tables merge key by key, anything else is replaced."""
    res = copy.deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(res.get(key), dict):
            res[key] = merge(res[key], value)
        else:
            res[key] = copy.deepcopy(value)
    return res


BUILTIN = {
    "loom-dark": DARK,
    "loom-light": merge(DARK, LIGHT_CHANGES),
}


@dataclass(frozen=True)
class Capabilities:
    """What the terminal can show."""

    # "truecolor", "256", "16" or "none"
    color: str = "truecolor"
    unicode: bool = True
    animate: bool = True
    # A terminal loom can draw pickers and a toolbar on, rather than a pipe
    interactive: bool = True

    @classmethod
    def detect(cls, stream=None, pretty=True, animation=True, environ=None):
        environ = os.environ if environ is None else environ
        stream = stream or sys.stdout
        try:
            tty = bool(stream.isatty())
        except (AttributeError, ValueError):
            tty = False
        term = environ.get("TERM", "")
        dumb = term in ("dumb", "unknown")
        no_color = environ.get("NO_COLOR", "") != ""

        if not tty or dumb or not pretty or no_color:
            color = "none"
        elif environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
            color = "truecolor"
        elif "256" in term:
            color = "256"
        else:
            color = "16"

        unicode = tty and not dumb and encodes(getattr(stream, "encoding", None), UNICODE_SAMPLE)
        interactive = tty and not dumb
        return cls(
            color=color,
            unicode=unicode,
            animate=interactive and animation,
            interactive=interactive,
        )


def encodes(encoding, text):
    """Whether text can be written in encoding."""
    if not encoding:
        return False
    try:
        codecs.lookup(encoding)
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


# A plain, colourless, ASCII terminal: what pipes and tests get
PLAIN = Capabilities(color="none", unicode=False, animate=False, interactive=False)


@dataclass(frozen=True)
class Theme:
    data: dict
    caps: Capabilities = field(default_factory=Capabilities)
    # The theme files applied, for /settings and error messages
    sources: tuple = ()

    @property
    def name(self):
        return self.data["name"]

    def degrade(self, caps):
        """This theme, drawn for a terminal with caps."""
        return replace(self, caps=caps)

    # Colours

    def color(self, name):
        """A colour as #rrggbb, or None for the terminal's own or a terminal without colour."""
        if self.caps.color == "none":
            return None
        value = self.data["color"].get(name, name)
        if value == "default" or not COLOR_RE.match(str(value)):
            return None
        return expand_hex(value)

    def raw_color(self, name):
        """A colour as #rrggbb whatever the terminal, or None for "default"."""
        value = self.data["color"].get(name, name)
        return expand_hex(value) if COLOR_RE.match(str(value)) else None

    def style(self, fg=None, bg=None, bold=False, dim=False, italic=False, strike=False):
        """A rich style: fg and bg are colour names like "accent". Attributes stay
        without colour, as NO_COLOR asks."""
        parts = []
        if bold:
            parts.append("bold")
        if dim:
            parts.append("dim")
        if italic:
            parts.append("italic")
        if strike:
            parts.append("strike")
        fg = self.color(fg) if fg else None
        bg = self.color(bg) if bg else None
        if fg:
            parts.append(fg)
        if bg:
            parts.append(f"on {bg}")
        return " ".join(parts) or "none"

    def pt(self, fg=None, bg=None, bold=False, italic=False):
        """The same as style(), for prompt_toolkit."""
        parts = []
        fg = self.color(fg) if fg else None
        bg = self.color(bg) if bg else None
        if fg:
            parts.append(fg)
        if bg:
            parts.append(f"bg:{bg}")
        if bold:
            parts.append("bold")
        if italic:
            parts.append("italic")
        return " ".join(parts)

    def gradient(self, name="effort"):
        """The stops of a gradient as #rrggbb: the colours, or for a 256-colour terminal
        its own stops, which are shown still."""
        if self.caps.color == "256":
            return [xterm_hex(index) for index in self.data["gradient"][f"{name}_256"]]
        return [expand_hex(stop) for stop in self.data["gradient"][name]]

    @property
    def sweep_ms(self):
        """How long one pass of a gradient's sweep takes, or 0 when it stays still."""
        if self.caps.color != "truecolor" or not self.caps.animate:
            return 0
        return self.data["gradient"]["sweep_ms"]

    # Glyphs

    def glyph(self, name):
        glyphs = self.data["glyph"]
        if not self.caps.unicode:
            glyphs = glyphs["ascii"]
        return glyphs[name]

    @property
    def spinner_frames(self):
        spinner = self.data["spinner"]
        if not self.caps.unicode:
            return tuple(spinner["ascii"])
        return tuple(spinner["frames"] or self.data["spinners"][spinner["style"]])

    @property
    def frame_interval(self):
        """Seconds between frames of an animation: the spinner's pace, never faster than
        max_fps allows."""
        return max(self.data["spinner"]["interval_ms"] / 1000, 1 / self.max_fps)

    @property
    def max_fps(self):
        return self.data["limits"]["max_fps"]

    # The logo

    @property
    def mark(self):
        logo = self.data["logo"]
        return list(logo["mark"] if self.caps.unicode else logo["mark_ascii"])

    @property
    def wordmark(self):
        logo = self.data["logo"]
        return list(logo["wordmark"] if self.caps.unicode else logo["wordmark_ascii"])

    @property
    def intro(self):
        return self.data["logo"]["intro"]

    @property
    def code_theme(self):
        return self.data["code_theme"]


def expand_hex(color):
    color = str(color)
    if len(color) == 4:
        color = "#" + "".join(char * 2 for char in color[1:])
    return color.lower()


def xterm_hex(index):
    """The colour xterm shows for a 256-colour index."""
    index = int(index)
    if index < 16:
        basic = (
            "#000000 #800000 #008000 #808000 #000080 #800080 #008080 #c0c0c0"
            " #808080 #ff0000 #00ff00 #ffff00 #0000ff #ff00ff #00ffff #ffffff"
        ).split()
        return basic[index]
    if index < 232:
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        red, green, blue = levels[index // 36], levels[index // 6 % 6], levels[index % 6]
        return f"#{red:02x}{green:02x}{blue:02x}"
    gray = 8 + (index - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


# Loading theme files


def load_theme(root=None, name=None, path=None, home=None):
    """The theme: the built-in called name (loom-dark if None), or the one a theme file
    names, with ~/.loom/theme.toml, root's .loom/theme.toml and path applied in that
    order. Raises ThemeError for a theme loom doesn't have or a file it can't use."""
    home = Path.home() if home is None else Path(home)
    files = [home / ".loom" / "theme.toml"]
    if root:
        files.append(Path(root) / ".loom" / "theme.toml")
    if path:
        files.append(Path(path))

    changes = []
    for fname in files:
        if fname == Path(path or "") and not fname.is_file():
            raise ThemeError(f"There is no theme file {fname}.")
        if not fname.is_file():
            continue
        data = read_toml(fname)
        check(data, DARK, fname)
        changes.append((fname, data))

    if name is None:
        # The last file to name a built-in theme picks it
        named = [data["name"] for _, data in changes if data.get("name") in BUILTIN]
        name = named[-1] if named else "loom-dark"
    if name not in BUILTIN:
        raise ThemeError(f"There is no {name} theme; use one of: {', '.join(BUILTIN)}.")

    data = BUILTIN[name]
    for _fname, change in changes:
        data = merge(data, change)
    return Theme(data, sources=tuple(str(fname) for fname, _ in changes))


def read_toml(fname):
    try:
        text = Path(fname).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ThemeError(f"Unable to read {fname}: {err}")
    try:
        return parse_toml(text)
    except ValueError as err:
        raise ThemeError(f"{fname} isn't valid TOML: {err}")


def check(data, reference, fname, prefix=""):
    """Refuse keys loom doesn't know and values it can't use, loudly."""
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in reference:
            raise ThemeError(f"{fname}: loom has no theme setting {dotted}.")
        expected = reference[key]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ThemeError(f"{fname}: {dotted} should be a table, like [{dotted}].")
            check(value, expected, fname, dotted + ".")
            continue
        problem = check_value(dotted, value, expected)
        if problem:
            raise ThemeError(f"{fname}: {dotted} {problem}.")


def check_value(key, value, expected):
    """What's wrong with value for key, or None."""
    section, _, name = key.rpartition(".")
    if key == "code_theme":
        from pygments.styles import get_all_styles

        if value not in set(get_all_styles()):
            return "should be a Pygments style, like gruvbox-dark or monokai"
        return None
    if section == "color":
        if value == "default" or (isinstance(value, str) and COLOR_RE.match(value)):
            return None
        return 'should be a colour like "#cf9a4c", or "default" for the terminal\'s own'
    if key == "logo.intro":
        if value not in INTROS:
            return f"should be one of {', '.join(INTROS)}"
        return None
    if key == "spinner.style":
        if value not in DARK["spinners"]:
            return f"should be one of {', '.join(DARK['spinners'])}"
        return None
    if key == "limits.max_fps":
        if not is_int(value) or not 1 <= value <= 60:
            return "should be a whole number from 1 to 60"
        return None
    if key == "spinner.interval_ms":
        if not is_int(value) or value < 30:
            return "should be a whole number of milliseconds, at least 30"
        return None
    if key == "gradient.sweep_ms":
        if not is_int(value) or value < 0:
            return "should be a whole number of milliseconds, or 0 to keep it still"
        return None
    if key.startswith("gradient.") and key.endswith("_256"):
        if (
            not isinstance(value, list)
            or len(value) < 2
            or not all(is_int(v) and 0 <= v <= 255 for v in value)
        ):
            return "should be a list of at least two colour numbers from 0 to 255"
        return None
    if section == "gradient":
        if (
            not isinstance(value, list)
            or len(value) < 2
            or not all(isinstance(v, str) and COLOR_RE.match(v) for v in value)
        ):
            return 'should be a list of at least two colours like "#cf9a4c"'
        return None
    if isinstance(expected, list):
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            return "should be a list of strings"
        if key.endswith("track") and len(value) != 2:
            return "should be two glyphs, for the left end and the right end"
        if not value and key != "spinner.frames":
            return "can't be empty"
        return None
    if isinstance(expected, str):
        if not isinstance(value, str) or (not value and section.startswith("glyph")):
            return "should be a string" if not isinstance(value, str) else "can't be empty"
        return None
    return None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# TOML: tomllib where Python has it (3.11), otherwise the part of TOML a theme file needs


def parse_toml(text):
    try:
        import tomllib
    except ImportError:
        return parse_toml_subset(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ValueError(str(err))


TABLE_RE = re.compile(r"^\[\s*([A-Za-z0-9_.\- ]+?)\s*\]$")
KEY_RE = re.compile(r"^([A-Za-z0-9_\-]+)\s*=\s*(.*)$")


def parse_toml_subset(text):
    """Tables, keys, strings, numbers, booleans and arrays of them (across lines too)."""
    data = {}
    table = data
    lines = text.splitlines()
    num = 0
    while num < len(lines):
        line = strip_comment(lines[num]).strip()
        num += 1
        if not line:
            continue
        match = TABLE_RE.match(line)
        if match:
            table = data
            for part in match.group(1).split("."):
                table = table.setdefault(part.strip(), {})
                if not isinstance(table, dict):
                    raise ValueError(f"line {num}: {match.group(1)} is already a value")
            continue
        match = KEY_RE.match(line)
        if not match:
            raise ValueError(f"line {num}: expected key = value")
        key, value = match.groups()
        # An array can go on over several lines
        while value.count("[") > value.count("]") and num < len(lines):
            value += " " + strip_comment(lines[num]).strip()
            num += 1
        parsed, rest = parse_value(value.strip(), num)
        if rest.strip():
            raise ValueError(f"line {num}: unexpected {rest.strip()!r}")
        table[key] = parsed
    return data


def strip_comment(line):
    """line without its # comment, minding # inside strings."""
    quote = None
    escaped = False
    for pos, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:pos]
    return line


def parse_value(text, num):
    """(the value at the start of text, the rest of text)."""
    if text.startswith('"'):
        end = 1
        res = []
        while end < len(text):
            char = text[end]
            if char == "\\":
                code = text[end + 1 : end + 2]
                if code == "u":
                    res.append(chr(int(text[end + 2 : end + 6], 16)))
                    end += 6
                    continue
                res.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(code, code))
                end += 2
                continue
            if char == '"':
                return "".join(res), text[end + 1 :]
            res.append(char)
            end += 1
        raise ValueError(f"line {num}: unterminated string")
    if text.startswith("'"):
        end = text.find("'", 1)
        if end < 0:
            raise ValueError(f"line {num}: unterminated string")
        return text[1:end], text[end + 1 :]
    if text.startswith("["):
        items = []
        rest = text[1:].lstrip()
        while not rest.startswith("]"):
            item, rest = parse_value(rest, num)
            items.append(item)
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest.startswith("]"):
                raise ValueError(f"line {num}: expected , or ] in an array")
        return items, rest[1:]
    match = re.match(r"(true|false|[+-]?\d[\d_]*(?:\.\d+)?)", text)
    if not match:
        raise ValueError(f"line {num}: can't read {text!r}")
    word = match.group(1)
    if word in ("true", "false"):
        return word == "true", text[match.end() :]
    number = word.replace("_", "")
    return (float(number) if "." in number else int(number)), text[match.end() :]


# The theme the terminal uses

_current = None


def current():
    """The theme in use: what main set, or loom-dark fitted to stdout."""
    global _current
    if _current is None:
        _current = Theme(BUILTIN["loom-dark"]).degrade(Capabilities.detect())
    return _current


def set_current(theme):
    global _current
    _current = theme

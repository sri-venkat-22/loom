"""Keeping text from the model or a repo from controlling the terminal."""

import re

# Terminal escape sequences: CSI (ESC [ or 0x9b), OSC (ESC ] or 0x9d, up to BEL or ST),
# DCS/SOS/PM/APC strings, and the other ESC sequences
ANSI_ESCAPE_RE = re.compile(
    "(?:\x1b\\[|\x9b)[0-?]*[ -/]*[@-~]"
    "|(?:\x1b\\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\\\|\x9c)?"
    "|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\\\|\x9c)?"
    "|\x1b[ -/]*[0-~]"
)
# Characters that control the terminal or reorder text (bidi overrides), but newline and tab
CONTROL_CHARS_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def sanitize_for_display(text, show_escapes=False):
    """text that can't control the terminal: what a model or a repo wrote could otherwise
    move the cursor, erase lines or hide text, and make an approval question show something
    other than what runs.

    By default escape sequences are dropped (like the colors in a command's output) and
    carriage returns become newlines. show_escapes keeps every character visible instead,
    so ESC shows as \\x1b, for text the user approves, like a command or a diff. Other
    control characters always show that way."""
    text = str(text)
    if not show_escapes:
        text = ANSI_ESCAPE_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return CONTROL_CHARS_RE.sub(escape_char, text)


def escape_char(match):
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"

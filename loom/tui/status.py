"""
The line under the spinner while loom works: what it's doing, then the facts that change,
all on one line repainted no faster than the theme's max_fps:

    ▅ Running the tests… (12s · step 4 · $0.03 · esc to interrupt)

What it's doing shimmers in gold on a truecolor terminal; the facts stay faint.
"""

import time
from dataclasses import dataclass

from rich.text import Text

from loom.tui.paint import shimmer

# One pass of the shimmer across the activity
SHIMMER_SECONDS = 2.2


@dataclass(frozen=True)
class Activity:
    """What the spinner says: like "Running the tests" and ("12s", "step 4")."""

    text: str
    details: tuple = ()

    def __str__(self):
        text = self.text.rstrip(".…") + "…"
        return f"{text} ({' · '.join(self.details)})" if self.details else text


def format_elapsed(seconds):
    """A wait so far, like 9s, 1m 05s or 1h 02m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def status_line(theme, frame, activity, now=None):
    """The spinner's line: frame, then activity (an Activity or a string)."""
    now = time.time() if now is None else now
    if not isinstance(activity, Activity):
        activity = Activity(str(activity))
    line = Text()
    line.append(frame, style=theme.style("accent"))
    line.append(" ")

    text = activity.text.rstrip(".…") + theme.glyph("ellipsis")
    base, light = theme.color("accent"), theme.color("highlight")
    if base and light and theme.caps.color == "truecolor" and theme.caps.animate:
        line += shimmer(text, base, light, (now % SHIMMER_SECONDS) / SHIMMER_SECONDS)
    else:
        line.append(text, style=theme.style("accent"))

    if activity.details:
        dot = f" {theme.glyph('dot')} "
        line.append(" (", style=theme.style("faint"))
        for num, detail in enumerate(activity.details):
            if num:
                line.append(dot, style=theme.style("faint"))
            style = "faint" if "esc" in detail else "dim"
            line.append(detail, style=theme.style(style))
        line.append(")", style=theme.style("faint"))
    return line

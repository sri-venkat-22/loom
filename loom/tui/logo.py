"""
The mark and the wordmark, and the ways LOOM arrives at launch.

The mark is a shuttle crossing the warp, the same ❯ that marks the choice in every picker:

    ┆┆┆┆┆
    ━━❯━━
    ┆┆┆┆┆

Each intro is one loom mechanism, as a few frames at no more than the theme's max_fps:

    selvedge   the letters appear, the finished edge draws itself underneath and the
               shuttle parks at its end. Under a second; the default.
    weave      rows land top to bottom, each one woven in from the left
    shuttle    the ❯ crosses once and the cloth exists behind it
    threading  the wordmark comes down through the warp
    beatup     loose and wide, then beaten square against a gold selvedge
    fill       grey cloth fills with gold from the left
    none       no wordmark

Without animation every one of them shows its last frame.
"""

import time

from rich.console import Group
from rich.live import Live
from rich.text import Text

INDENT = "  "


def mark_lines(theme):
    """The mark's three rows: the warp held faint, the shuttle crossing in gold."""
    rows = theme.mark
    styles = [theme.style("faint"), theme.style("accent"), theme.style("faint")]
    return [Text(row, style=styles[min(num, 2)]) for num, row in enumerate(rows)]


def wordmark_width(theme):
    return max(len(row) for row in theme.wordmark)


def plain_rows(theme, style):
    return [Text(INDENT + row, style=style) for row in theme.wordmark]


def selvedge_row(theme, drawn, parked):
    """The edge under the wordmark: drawn (0 to 1) of its line, and the shuttle at its end
    once parked (0 hidden, 1 coming in, 2 parked)."""
    width = wordmark_width(theme)
    line_width = width - 2
    cells = round(line_width * drawn)
    row = Text(INDENT)
    row.append(theme.glyph("weft") * cells, style=theme.style("accent"))
    row.append(" " * (line_width - cells))
    shuttle = theme.data["logo"]["glyph"] if theme.caps.unicode else theme.glyph("selected")
    if parked == 1:
        row.append(shuttle + " ", style=theme.style("accent_dim"))
    elif parked == 2:
        row.append(" " + shuttle, style=theme.style("accent", bold=True))
    return row


def ease_out(amount):
    return 1 - (1 - amount) ** 3


def frames_selvedge(theme):
    for style in ("faint", "dim", "fg"):
        yield plain_rows(theme, theme.style(style)) + [selvedge_row(theme, 0, 0)]
    rows = plain_rows(theme, theme.style("fg"))
    draws = 6
    for num in range(1, draws + 1):
        yield rows + [selvedge_row(theme, ease_out(num / draws), 0)]
    yield rows + [selvedge_row(theme, 1, 1)]
    yield rows + [selvedge_row(theme, 1, 2)]


def clipped(text, cells):
    """The first cells of text, the rest blank."""
    return text[:cells] + " " * max(0, len(text) - cells)


def frames_weave(theme):
    rows = theme.wordmark
    width = wordmark_width(theme)
    picks, stagger = 5, 2
    total = stagger * (len(rows) - 1) + picks
    for frame in range(1, total + 1):
        res = []
        for num, row in enumerate(rows):
            progress = min(max(frame - num * stagger, 0), picks) / picks
            cells = round(width * ease_out(progress))
            # The row being woven is gold at its leading edge
            text = Text(INDENT + clipped(row, cells), style=theme.style("fg"))
            if 0 < progress < 1 and cells:
                text.stylize(
                    theme.style("accent"), len(INDENT) + max(0, cells - 3), len(INDENT) + cells
                )
            res.append(text)
        yield res


def frames_shuttle(theme):
    rows = theme.wordmark
    width = wordmark_width(theme)
    passes = 14
    shuttle = theme.data["logo"]["glyph"] if theme.caps.unicode else theme.glyph("selected")
    middle = len(rows) // 2
    for frame in range(passes + 1):
        pos = round((width + 1) * frame / passes) - 1
        res = []
        for num, row in enumerate(rows):
            text = Text(INDENT)
            if 0 <= pos < width:
                # The cloth behind the shuttle, the shuttle on the middle row and its
                # thread above and below
                text.append(row[:pos], style=theme.style("fg"))
                if num == middle:
                    text.append(shuttle, style=theme.style("accent", bold=True))
                else:
                    text.append(theme.glyph("warp"), style=theme.style("accent_dim"))
                text.append(" " * (width - pos - 1))
            else:
                text.append(clipped(row, max(pos, 0)), style=theme.style("fg"))
            res.append(text)
        yield res
    yield plain_rows(theme, theme.style("fg"))


def frames_threading(theme):
    rows = theme.wordmark
    width = wordmark_width(theme)
    warp = theme.glyph("warp")
    drops = 8
    for frame in range(drops + 1):
        shown = round(len(rows) * ease_out(frame / drops))
        res = []
        for num, row in enumerate(rows):
            text = Text(INDENT)
            for col in range(width):
                char = row[col] if col < len(row) else " "
                if num < shown and char != " ":
                    text.append(char, style=theme.style("fg"))
                elif (col + frame) % 3 == 0:
                    # The warp runs behind the letters, sliding down as they come
                    text.append(warp, style=theme.style("edge"))
                else:
                    text.append(" ")
            res.append(text)
        yield res
    yield plain_rows(theme, theme.style("fg"))


def stretch(row, scale):
    """row resampled to scale times its width, like the reed's spacing before beat-up."""
    width = max(1, round(len(row) * scale))
    return "".join(row[min(int(col / scale), len(row) - 1)] for col in range(width))


def frames_beatup(theme):
    selvedge = "▌" if theme.caps.unicode else "|"
    beats = [
        (1.24, "faint", "faint"),
        (1.16, "faint", "faint"),
        (1.08, "dim", "faint"),
        (0.965, "fg", "highlight"),
        (1.012, "fg", "accent"),
        (1.0, "fg", "accent_dim"),
    ]
    for scale, style, edge in beats:
        yield [
            Text(" ", style=None)
            + Text(selvedge, style=theme.style(edge))
            + Text(stretch(row, scale), style=theme.style(style))
            for row in theme.wordmark
        ]


def frames_fill(theme):
    rows = theme.wordmark
    width = wordmark_width(theme)
    fills = 12
    for frame in range(fills + 1):
        yield fill_rows(theme, rows, round(width * frame / fills))


def fill_rows(theme, rows, cells):
    res = []
    for row in rows:
        text = Text(INDENT)
        text.append(row[:cells], style=theme.style("accent"))
        text.append(row[cells:], style=theme.style("faint"))
        res.append(text)
    return res


def wordmark_fill(theme, fraction):
    """The wordmark as a progress bar: fraction of it gold."""
    rows = theme.wordmark
    return fill_rows(theme, rows, round(wordmark_width(theme) * min(max(fraction, 0), 1)))


INTRO_FRAMES = dict(
    selvedge=frames_selvedge,
    weave=frames_weave,
    shuttle=frames_shuttle,
    threading=frames_threading,
    beatup=frames_beatup,
    fill=frames_fill,
)


def intro_frames(theme, intro=None):
    """The frames of an intro, each a list of rows: [] for none."""
    intro = intro or theme.intro
    make = INTRO_FRAMES.get(intro)
    return list(make(theme)) if make else []


def play_intro(console, theme, intro=None):
    """Show the wordmark arriving, or its last frame without animation. Returns whether
    anything was shown."""
    frames = intro_frames(theme, intro)
    if not frames or console.width < wordmark_width(theme) + len(INDENT) + 4:
        return False
    if not theme.caps.animate:
        for row in frames[-1]:
            console.print(row, no_wrap=True, overflow="crop")
        return True
    delay = 1 / theme.max_fps
    live = Live(console=console, auto_refresh=False, transient=False)
    try:
        live.start()
        for frame in frames:
            live.update(Group(*frame), refresh=True)
            time.sleep(delay)
    finally:
        live.stop()
    return True

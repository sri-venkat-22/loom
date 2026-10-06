"""
Inline pickers, drawn under the conversation (never full screen) and erased once
answered, so the scrollback keeps the one-line answer and not the menu:

      Run this command?
    ❯ 1. Yes
      2. Yes, and always allow this command
      3. No, and tell loom what to do instead
      ↑/↓ to move · Enter to choose · y a n · Esc for no

The keys of the typed answers they replace still pick at once, so y is still yes.

The effort slider: ←/→ walk its stops, the track carries the theme's gradient (sliding
on a truecolor terminal, five still stops on a 256-colour one, ASCII with neither), and
reaching the top stop sets the track alight for a moment.

Each returns the index of what was picked, or the cancel value for Esc. ^C raises
KeyboardInterrupt and ^D EOFError, as a prompt does.
"""

import random
import textwrap
import time
from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.utils import get_cwidth

from loom.tui.paint import mix, steps, sweep

COLOR_DEPTHS = {
    "truecolor": ColorDepth.DEPTH_24_BIT,
    "256": ColorDepth.DEPTH_8_BIT,
    "16": ColorDepth.DEPTH_4_BIT,
    "none": ColorDepth.DEPTH_1_BIT,
}


@dataclass
class Option:
    label: str
    # A letter that picks it at once, like y for yes
    key: str = None
    # What picking it does, shown dim beside it
    detail: str = ""


def run_app(theme, get_text, key_bindings, input=None, output=None, refresh=None):
    control = FormattedTextControl(get_text, focusable=True, show_cursor=False)
    app = Application(
        layout=Layout(HSplit([Window(control, wrap_lines=False, dont_extend_height=True)])),
        key_bindings=key_bindings,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
        color_depth=COLOR_DEPTHS.get(theme.caps.color, ColorDepth.DEPTH_8_BIT),
        refresh_interval=refresh,
        input=input,
        output=output,
    )
    # Esc on its own answers at once, rather than waiting to see if a key follows
    app.ttimeoutlen = 0.05
    app.timeoutlen = 0.3
    return app.run()


def columns():
    try:
        return get_app().output.get_size().columns
    except Exception:
        return 80


def truncate(text, width, ellipsis="…"):
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    res = ""
    for char in text:
        if get_cwidth(res + char + ellipsis) > width:
            break
        res += char
    return res + ellipsis


def select(theme, question, options, default=0, cancel=None, hint=None, input=None, output=None):
    """Ask question; returns the index of the option picked, or cancel for Esc."""
    state = dict(index=default)
    count = len(options)
    label_width = max(get_cwidth(option.label) for option in options)
    selected_glyph = theme.glyph("selected")

    def get_text():
        width = columns()
        res = []
        for line in textwrap.wrap(question, max(width - 4, 20)) or [""]:
            res += [(theme.pt("fg", bold=True), "  " + line), ("", "\n")]
        bar = min(
            width - 1, max(get_cwidth(option.detail) for option in options) + label_width + 12
        )
        for num, option in enumerate(options):
            current = num == state["index"]
            row_bg = theme.pt(bg="selection") if current else ""
            marker = selected_glyph if current else " " * get_cwidth(selected_glyph)
            number = f"{num + 1}. " if count <= 9 else ""
            label = option.label + " " * (label_width - get_cwidth(option.label))
            used = 2 + get_cwidth(marker) + len(number) + label_width
            detail = truncate(option.detail, width - used - 4) if option.detail else ""
            row = [
                (f"{row_bg} {theme.pt('accent', bold=True)}", marker + " "),
                (f"{row_bg} {theme.pt('faint')}", number),
                (f"{row_bg} {theme.pt('accent' if current else 'fg', bold=current)}", label),
            ]
            if detail:
                row.append((f"{row_bg} {theme.pt('dim')}", "   " + detail))
                used += 3 + get_cwidth(detail)
            if current and used < bar:
                row.append((row_bg, " " * (bar - used)))
            res += row + [("", "\n")]
        res.append(
            (
                theme.pt("faint"),
                "  " + truncate(hint or default_hint(theme, options, cancel), width - 3),
            )
        )
        return res

    kb = KeyBindings()
    letters = {option.key.lower(): num for num, option in enumerate(options) if option.key}

    def move(step):
        state["index"] = (state["index"] + step) % count

    @kb.add("up")
    @kb.add("c-p")
    @kb.add("s-tab")
    def _(event):
        move(-1)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("tab")
    def _(event):
        move(1)

    if "k" not in letters:
        kb.add("k")(lambda event: move(-1))
    if "j" not in letters:
        kb.add("j")(lambda event: move(1))

    @kb.add("enter")
    def _(event):
        event.app.exit(result=state["index"])

    def pick(num):
        return lambda event: event.app.exit(result=num)

    for letter, num in letters.items():
        kb.add(letter)(pick(num))
    if count <= 9:
        for num in range(count):
            kb.add(str(num + 1))(pick(num))

    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result=cancel)

    add_interrupts(kb)
    return run_app(theme, get_text, kb, input=input, output=output)


def default_hint(theme, options, cancel):
    dot = f" {theme.glyph('dot')} "
    parts = ["↑/↓ to move" if theme.caps.unicode else "up/down to move", "Enter to choose"]
    keys = [option.key for option in options if option.key]
    if keys:
        parts.append(" ".join(keys))
    if cancel is not None and 0 <= cancel < len(options):
        parts.append(f"Esc for {options[cancel].label.split(',')[0].lower()}")
    elif cancel is not None:
        parts.append("Esc to cancel")
    return dot.join(parts)


def add_interrupts(kb):
    @kb.add("c-c")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @kb.add("c-d")
    def _(event):
        event.app.exit(exception=EOFError, style="class:exiting")


# The effort slider


@dataclass
class Stop:
    label: str
    detail: str = ""


# How long the marker takes to slide to the next stop, and the top stop's flare lasts
SLIDE_SECONDS = 0.18
FLARE_SECONDS = 0.9


class Canvas:
    """Rows of cells, each a character and a prompt_toolkit style."""

    def __init__(self, rows, width):
        self.width = width
        self.cells = [[("", " ") for _ in range(width)] for _ in range(rows)]

    def put(self, row, col, text, style=""):
        for char in text:
            if 0 <= row < len(self.cells) and 0 <= col < self.width:
                self.cells[row][col] = (style, char)
            col += 1

    def style_at(self, row, col):
        return self.cells[row][col][0] if 0 <= col < self.width else ""

    def char_at(self, row, col):
        return self.cells[row][col][1] if 0 <= col < self.width else " "

    def fragments(self, shift=0):
        res = []
        for num, row in enumerate(self.cells):
            if shift > 0:
                row = [("", " ")] * shift + row[:-shift]
            elif shift < 0:
                row = row[-shift:] + [("", " ")] * -shift
            # Trailing blanks would wrap a narrow terminal
            while row and row[-1] == ("", " "):
                row = row[:-1]
            for style, char in row:
                if res and res[-1][0] == style:
                    res[-1] = (style, res[-1][1] + char)
                else:
                    res.append((style, char))
            if num < len(self.cells) - 1:
                res.append(("", "\n"))
        return res


def stop_columns(count, start, width):
    """The column of each stop along a track starting at start, width wide."""
    if count == 1:
        return [start + width // 2]
    return [start + round((width - 1) * (0.06 + 0.88 * num / (count - 1))) for num in range(count)]


def slider(
    theme,
    title,
    stops,
    index=0,
    left="Faster",
    right="Smarter",
    hint=None,
    input=None,
    output=None,
    clock=time.monotonic,
):
    """Pick one of stops with ←/→; returns its index, or None for Esc."""
    state = dict(index=index, previous=index, moved=-10.0, flare=-10.0)
    count = len(stops)
    animate = theme.caps.animate
    ends = theme.glyph("track")
    rng = random.Random(7)
    embers = [
        dict(row=rng.choice((1, 2, 3)), dy=rng.choice((-1, 0, 0, 1)), delay=num * 0.05, glyph=glyph)
        for num, glyph in enumerate(["▒", "▓", "░", "▒", "·", "░"])
    ]

    def get_text():
        now = clock()
        width = columns()
        left_text = f"  {left} {ends[0]} "
        right_text = f" {ends[1]} {right}"
        track_width = max(12, min(width - len(left_text) - len(right_text) - 2, 64))
        start = len(left_text)
        cols = stop_columns(count, start, track_width)
        canvas = Canvas(7, max(width - 1, start + track_width + len(right_text)))
        flaring = animate and now - state["flare"] < FLARE_SECONDS
        flare = (now - state["flare"]) / FLARE_SECONDS if flaring else 1.0

        canvas.put(0, 2, title, theme.pt("fg", bold=True))

        # The track, its ends and their words
        canvas.put(1, 0, left_text, theme.pt("dim"))
        hot = flaring and flare < 0.75
        canvas.put(
            1, start + track_width, right_text, theme.pt("highlight" if hot else "dim", bold=hot)
        )
        draw_track(canvas, theme, start, track_width, now, flaring, flare)

        # The labels, evenly spaced, each centred on its stop
        last_end = 0
        for num, (stop, col) in enumerate(zip(stops, cols)):
            label_col = max(col - len(stop.label) // 2, last_end + 1)
            current = num == state["index"]
            canvas.put(
                2, label_col, stop.label, theme.pt("accent" if current else "dim", bold=current)
            )
            last_end = label_col + len(stop.label)

        # The marker, sliding from the stop it left
        slide = min(1.0, (now - state["moved"]) / SLIDE_SECONDS) if animate else 1.0
        eased = 1 - (1 - slide) ** 3
        marker_col = round(
            cols[state["previous"]] + (cols[state["index"]] - cols[state["previous"]]) * eased
        )
        if flaring and flare < 0.7:
            draw_boost(canvas, theme, marker_col, now)
        canvas.put(
            3,
            marker_col,
            theme.glyph("slider_mark"),
            theme.pt("highlight" if flaring else "accent", bold=True),
        )
        if flaring:
            draw_flare(canvas, theme, marker_col, flare, embers, now - state["flare"])

        stop = stops[state["index"]]
        canvas.put(5, 2, stop.label, theme.pt("accent", bold=True))
        if stop.detail:
            dash = " — " if theme.caps.unicode else " - "
            canvas.put(
                5,
                2 + len(stop.label),
                dash + truncate(stop.detail, width - len(stop.label) - 8),
                theme.pt("dim"),
            )
        dot = f" {theme.glyph('dot')} "
        keys = "←/→" if theme.caps.unicode else "left/right"
        canvas.put(
            6,
            2,
            hint or dot.join([f"{keys} to adjust", "Enter to confirm", "Esc to cancel"]),
            theme.pt("faint"),
        )

        # The top stop shakes the slider for a moment
        shift = 0
        if flaring and flare < 0.35:
            shift = (-1, 1, -1, 1, 0)[min(int(flare / 0.07), 4)]
        return canvas.fragments(shift)

    def move(step):
        new = min(max(state["index"] + step, 0), count - 1)
        if new == state["index"]:
            return
        state["previous"], state["index"], state["moved"] = state["index"], new, clock()
        if new == count - 1 and count > 1:
            state["flare"] = clock()

    kb = KeyBindings()

    @kb.add("left")
    @kb.add("h")
    @kb.add("down")
    def _(event):
        move(-1)

    @kb.add("right")
    @kb.add("l")
    @kb.add("up")
    def _(event):
        move(1)

    @kb.add("home")
    def _(event):
        move(-count)

    @kb.add("end")
    def _(event):
        move(count)

    for num in range(min(count, 9)):

        @kb.add(str(num + 1))
        def _(event, num=num):
            move(num - state["index"])

    @kb.add("enter")
    def _(event):
        event.app.exit(result=state["index"])

    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result=None)

    add_interrupts(kb)
    refresh = 1 / theme.max_fps if animate else None
    return run_app(theme, get_text, kb, input=input, output=output, refresh=refresh)


def solid(theme, name):
    """A theme colour as #rrggbb, even one that's the terminal's own ("default")."""
    return theme.raw_color(name) or "#808080"


def draw_track(canvas, theme, start, width, now, flaring, flare):
    if not theme.caps.unicode:
        canvas.put(1, start, "-" * width, theme.pt("dim"))
        return
    weft = theme.glyph("weft")
    if theme.caps.color == "none":
        canvas.put(1, start, weft * width)
        return
    stops = theme.gradient("effort")
    if theme.caps.color == "256":
        colors = steps(stops, width)
    else:
        sweep_ms = theme.sweep_ms
        phase = (now * 1000 % sweep_ms) / sweep_ms if sweep_ms else 0.0
        colors = sweep(stops, width, phase)
    hot, ground, scorch = (
        solid(theme, "highlight"),
        solid(theme, "ground"),
        solid(theme, "accent_dim"),
    )
    for num, color in enumerate(colors):
        style = color
        if flaring and flare < 0.4 and theme.caps.color == "truecolor":
            # Scorched for a moment, cooling back to the gradient
            heat = 1 - flare / 0.4
            style = f"{mix(color, hot, heat * 0.6)} bg:{mix(ground, scorch, heat * 0.5)}"
        canvas.put(1, start + num, weft, style)


def draw_boost(canvas, theme, col, now):
    """The trail of the marker's boost to the top stop."""
    trail = ["··░░▒▒▓▓", "·░░▒▒▓▓█", "░░▒▒▓▓██", "·░▒▒▓▓█▓"][int(now / 0.05) % 4]
    if not theme.caps.unicode:
        trail = ".:-=+*#%"
    for num, char in enumerate(trail):
        fade = num / len(trail)
        color = mix(solid(theme, "accent_dim"), solid(theme, "accent"), fade)
        canvas.put(
            3, col - len(trail) + num, char, theme.pt(color) if theme.caps.color != "none" else ""
        )


def draw_flare(canvas, theme, col, flare, embers, elapsed):
    """The ring and the embers thrown off when the marker reaches the top stop."""
    if theme.caps.color == "none" or not theme.caps.unicode:
        return
    hot, cold = solid(theme, "highlight"), solid(theme, "faint")
    # A ring spreading from the marker along the label and marker rows
    radius = 1 + flare * 14
    if flare < 0.6:
        ring = mix(hot, cold, flare / 0.6)
        for row, spread in ((2, 1.0), (3, 1.0), (4, 0.6)):
            reach = round(radius * spread)
            for side in (-1, 1):
                pos = col + side * reach
                if canvas.char_at(row, pos) == " ":
                    canvas.put(row, pos, "·", ring)
    # Embers flying back down the track, cooling as they go
    for ember in embers:
        age = elapsed - ember["delay"]
        if age <= 0 or age > 0.6:
            continue
        pos = col - round(age * 70)
        row = ember["row"] + (ember["dy"] if age > 0.3 else 0)
        # Over the track, or through the gaps between words
        if row == 1 or canvas.char_at(row, pos) == " ":
            canvas.put(row, pos, ember["glyph"], mix(hot, cold, age / 0.6))

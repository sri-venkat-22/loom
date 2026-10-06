"""Colour maths for the terminal: gradients, the sweep along the effort track, the shimmer
on the status line, and rich Text turned into the escape codes a raw writer prints."""

import io
import math

from rich.console import Console
from rich.text import Text


def hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[pos : pos + 2], 16) for pos in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a, b, amount):
    """The colour amount of the way from a to b, both #rrggbb."""
    a, b = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(x + (y - x) * amount for x, y in zip(a, b))


def gradient_at(stops, pos):
    """The colour at pos (0 to 1) along evenly spaced stops."""
    if len(stops) == 1:
        return stops[0]
    pos = min(max(pos, 0.0), 1.0) * (len(stops) - 1)
    index = min(int(pos), len(stops) - 2)
    return mix(stops[index], stops[index + 1], pos - index)


def sweep(stops, width, phase=0.0):
    """A colour for each of width cells of a track whose gradient is twice as wide as the
    track and slides along it, phase (0 to 1) of the way through one pass. The stops
    should end where they start, like the effort track's teal-gold-teal, so the slide
    has no seam."""
    if width <= 0:
        return []
    res = []
    for cell in range(width):
        pos = (cell / max(width - 1, 1)) / 2 + phase
        res.append(gradient_at(stops, pos % 1.0))
    return res


def steps(stops, width):
    """A colour for each of width cells, split into one block per stop: the still bar a
    256-colour terminal gets."""
    if width <= 0:
        return []
    return [stops[min(cell * len(stops) // width, len(stops) - 1)] for cell in range(width)]


def shimmer(text, base, light, phase, band=6):
    """text in base, with a band of light passing over it, phase (0 to 1) of the way
    through one pass; the band starts and ends off the text, so passes don't jump."""
    res = Text()
    span = len(text) + band * 2
    center = phase * span - band
    for pos, char in enumerate(text):
        distance = abs(pos - center)
        amount = max(0.0, 1 - distance / band) if band else 0.0
        amount = 0.5 - math.cos(amount * math.pi) / 2
        res.append(char, style=mix(base, light, amount))
    return res


_consoles = {}


def ansi(text, color_system="truecolor"):
    """rich Text as a string with escape codes, for writers that print raw text."""
    if color_system in (None, "none"):
        return text.plain if isinstance(text, Text) else str(text)
    system = {"256": "256", "16": "standard"}.get(color_system, "truecolor")
    console = _consoles.get(system)
    if console is None:
        console = _consoles[system] = Console(
            file=io.StringIO(),
            force_terminal=True,
            color_system=system,
            width=10_000,
            legacy_windows=False,
        )
    with console.capture() as capture:
        console.print(text, end="", soft_wrap=True, highlight=False)
    return capture.get()

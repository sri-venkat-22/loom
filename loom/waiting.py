#!/usr/bin/env python

"""
Thread-based, killable spinner utility.

Use it like:

    from loom.waiting import WaitingSpinner

    spinner = WaitingSpinner("Waiting for LLM")
    spinner.start()
    ...  # long task
    spinner.stop()
"""

import sys
import threading
import time

from rich.console import Console

from loom.tui import theme as tui_theme
from loom.tui.paint import ansi
from loom.tui.status import status_line


class Spinner:
    """
    A spinner and the line beside it, repainted in place while loom waits.

    Its frames, colours and pace come from the theme (loom/tui/theme.py): blocks filling
    up by default, ASCII where unicode can't be shown, and nothing at all when the output
    isn't a terminal. The line shows after half a second, so quick waits don't flicker.
    """

    last_frame_idx = 0  # Class variable to store the last frame index

    def __init__(self, text, width: int = 7):
        # text is a string, an Activity (loom/tui/status.py), or a function returning
        # either, for a line that changes, like an elapsed time
        self.text = text
        self.start_time = time.time()
        self.last_update = 0.0
        self.visible = False
        self.is_tty = sys.stdout.isatty()
        self.console = Console()
        self.theme = tui_theme.current()
        self.frames = self.theme.spinner_frames
        self.frame_idx = Spinner.last_frame_idx % len(self.frames)
        self.last_display_len = 0  # Width of the last line shown

    def _next_frame(self) -> str:
        frame = self.frames[self.frame_idx]
        self.frame_idx = (self.frame_idx + 1) % len(self.frames)
        Spinner.last_frame_idx = self.frame_idx  # Update class variable
        return frame

    def render(self, frame=None, now=None):
        """The line as it would show now, as rich Text."""
        text = self.text() if callable(self.text) else self.text
        return status_line(self.theme, frame or self.frames[self.frame_idx], text, now)

    def step(self, text=None) -> None:
        if text is not None:
            self.text = text

        if not self.is_tty:
            return

        now = time.time()
        if not self.visible and now - self.start_time >= 0.5:
            self.visible = True
            self.last_update = 0.0
            self.console.show_cursor(False)

        if not self.visible or now - self.last_update < self.theme.frame_interval:
            return

        self.last_update = now
        line = self.render(self._next_frame(), now)

        # One screen line: leave a margin so the cursor never wraps
        max_width = max(self.console.width - 2, 0)
        line.truncate(max_width, overflow="ellipsis")
        width = line.cell_len

        # Spaces clear what's left of a longer previous line
        padding = " " * max(0, self.last_display_len - width)
        sys.stdout.write("\r" + ansi(line, self.theme.caps.color) + padding + "\r")
        sys.stdout.flush()
        self.last_display_len = width

    def end(self) -> None:
        if self.visible and self.is_tty:
            clear_len = self.last_display_len  # Use the length of the last displayed content
            sys.stdout.write("\r" + " " * clear_len + "\r")
            sys.stdout.flush()
            self.console.show_cursor(True)
        self.visible = False


class WaitingSpinner:
    """Background spinner that can be started/stopped safely."""

    def __init__(self, text: str = "Waiting for LLM", delay: float = None):
        self.spinner = Spinner(text)
        # The theme's pace, so the shimmer moves smoothly
        self.delay = delay or self.spinner.theme.frame_interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        while not self._stop_event.is_set():
            self.spinner.step()
            time.sleep(self.delay)
        self.spinner.end()

    def start(self):
        """Start the spinner in a background thread."""
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self):
        """Request the spinner to stop and wait briefly for the thread to exit."""
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.delay)
        self.spinner.end()

    # Allow use as a context-manager
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def main():
    spinner = Spinner("Running spinner...")
    try:
        for _ in range(100):
            time.sleep(0.15)
            spinner.step()
        print("Success!")
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        spinner.end()


if __name__ == "__main__":
    main()

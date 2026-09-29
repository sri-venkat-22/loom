"""
Esc to interrupt: while the agent works, watch the terminal for the Esc key and interrupt
the main thread the way ^C does, so a streaming reply, a running command or an MCP call
stops and loom returns to the prompt.

The terminal doesn't echo keys while the listener runs. Other text typed meanwhile is
kept, and InputOutput puts it back as the start of the next prompt, so typing ahead
still works.
"""

import os
import re
import signal
import sys
import threading
import time

ESC = b"\x1b"

# Arrow keys and the like send Esc followed by more bytes. Wait this long for the rest
# before deciding that Esc was pressed on its own.
SEQUENCE_WAIT = 0.05

# Escape sequences (CSI and SS3), and Alt+key, in typed-ahead text
ESCAPE_SEQUENCE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|O.|.)?", re.DOTALL)


def interrupt_main_thread():
    """Raise KeyboardInterrupt in the main thread, even if it's blocked reading a socket or
    waiting for a command. Returns False if SIGINT isn't handled by Python."""
    if not callable(signal.getsignal(signal.SIGINT)):
        return False
    if hasattr(signal, "pthread_kill"):
        # A real signal to the main thread interrupts the system call it's blocked in
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    else:
        import _thread

        _thread.interrupt_main()
    return True


def clean_typed_text(text):
    """Apply backspaces and drop escape sequences and control characters."""
    text = ESCAPE_SEQUENCE_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    res = []
    for char in text:
        if char in "\x7f\x08":
            if res:
                res.pop()
        elif char == "\n" or char == "\t" or char >= " ":
            res.append(char)
    return "".join(res)


class EscListener:
    pressed = False  # Esc was pressed since the listener started
    pending = False  # an interrupt was sent that nobody has claimed yet

    def __init__(self, stdin=None):
        self.stdin = stdin or sys.stdin
        self.typed = ""
        self._thread = None
        self._stop = threading.Event()
        self._fd = None
        self._saved_attrs = None

    @staticmethod
    def supported(stdin=None):
        stdin = stdin or sys.stdin
        try:
            if not (stdin and stdin.isatty()):
                return False
        except (AttributeError, ValueError):
            return False
        if os.name == "nt":
            return True
        try:
            import termios  # noqa: F401
        except ImportError:
            return False
        return threading.current_thread() is threading.main_thread()

    @property
    def running(self):
        return self._thread is not None

    def start(self):
        if self._thread:
            return
        self.pressed = False
        self._stop.clear()

        if os.name == "nt":
            target = self._watch_windows
        else:
            import termios

            self._fd = self.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(self._fd)
            attrs = termios.tcgetattr(self._fd)
            # No line buffering or echo, but keep ISIG so ^C still sends SIGINT
            attrs[3] &= ~(termios.ICANON | termios.ECHO)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
            target = self._watch_posix

        self._thread = threading.Thread(target=target, name="esc-listener", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop watching and restore the terminal. An Esc interrupt that arrives while
        stopping is dropped: the work it would have interrupted is over."""
        if not self._thread:
            return
        try:
            self._stop.set()
            self._thread.join()
            # Let an interrupt the thread sent just now arrive here, not somewhere later
            time.sleep(0)
        except KeyboardInterrupt:
            if not self.consume_escape():
                raise
        finally:
            self._thread = None
            self._restore_terminal()

    def _restore_terminal(self):
        if self._saved_attrs is None:
            return
        import termios

        try:
            termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_attrs)
        except (termios.error, OSError, ValueError):
            pass
        self._saved_attrs = None

    def consume_escape(self):
        """Whether the KeyboardInterrupt being handled came from Esc, rather than ^C."""
        pending = self.pending
        self.pending = False
        return pending

    def take_typed(self):
        typed = clean_typed_text(self.typed)
        self.typed = ""
        return typed

    def _escape(self):
        # One interrupt per start(): a second could land in the first one's cleanup
        if self.pressed:
            return
        self.pressed = True
        self.pending = True
        if not interrupt_main_thread():
            self.pending = False

    def _watch_posix(self):
        import select

        fd = self._fd
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(fd, 1024)
                if data.endswith(ESC):
                    ready, _, _ = select.select([fd], [], [], SEQUENCE_WAIT)
                    if not ready:
                        self.typed += data[:-1].decode(errors="ignore")
                        self._escape()
                        continue
                    data += os.read(fd, 1024)
            except (OSError, ValueError):
                return
            if not data:
                return
            self.typed += data.decode(errors="ignore")

    def _watch_windows(self):
        import msvcrt

        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                # Arrow and function keys arrive as two characters
                msvcrt.getwch()
            elif char == "\x1b":
                self._escape()
            else:
                self.typed += char

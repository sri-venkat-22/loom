"""What loom's main thread, which runs the Coder, shares with the web server's thread.

The Coder runs on the main thread exactly as in the terminal, with a WebIO in place of the
terminal's InputOutput. WebIO turns output into messages for the browser with emit(), and
blocks on wait_for_input() and ask() for the browser's replies. The server thread passes
those replies in with handle(), and relays emitted messages to every connected browser.
"""

import itertools
import queue
import threading

from loom.esc import interrupt_main_thread

from .protocol import event

# How often the main thread's waits wake up, so ^C in the terminal still interrupts them
POLL_SECONDS = 0.1


class WebSession:
    def __init__(self, interrupt=interrupt_main_thread):
        self.lock = threading.RLock()
        self.snapshot = event(
            "session",
            version="",
            model="",
            weak_model="",
            edit_format="",
            cwd="",
            files=[],
            read_only_files=[],
            commands=[],
            tokens=dict(sent=0, received=0),
            cost=0.0,
            phase=None,
            busy=False,
        )
        self.history = []
        self.clients = {}
        self.inputs = queue.Queue()
        self.answers = {}
        self.ids = itertools.count(1)
        # Raises KeyboardInterrupt in the main thread, for the browser's Esc. Returns False
        # if it can't, like when loom was started with SIGINT ignored.
        self.interrupt = interrupt
        # Whether the server is listening, so questions can go to the browser
        self.started = False
        # The server stopped: waits end with EOFError, which ends Coder.run()
        self.closed = False
        self.turn_id = None
        # Esc was pressed in the browser and the Coder hasn't seen it yet
        self.cancelled = False
        self.turn_cancelled = False
        # Whether the main thread was signalled for it; if not, check_cancel() interrupts
        self.signalled = False

    @property
    def busy(self):
        return self.turn_id is not None

    def next_id(self, prefix):
        return f"{prefix}{next(self.ids)}"

    # Messages to the browser

    def emit(self, type, **payload):
        """Send a message to every connected browser, and keep it for ones that connect
        later. The session message is kept as the latest snapshot instead."""
        message = event(type, **payload)
        with self.lock:
            if type == "session":
                self.snapshot = message
            else:
                self.history.append(message)
            # Queue while holding the lock, so every browser sees messages in order
            for client, loop in list(self.clients.items()):
                try:
                    loop.call_soon_threadsafe(client.put_nowait, message)
                except RuntimeError:
                    # Its event loop is closed
                    del self.clients[client]
        return message

    def update(self, **fields):
        """Change some of the session's state and send the new snapshot."""
        with self.lock:
            snapshot = {key: value for key, value in self.snapshot.items() if key != "type"}
            snapshot.update(fields)
            return self.emit("session", **snapshot)

    def connect(self, client, loop):
        """Register a browser's asyncio.Queue, served by loop. Returns the messages it has
        missed: the snapshot followed by the conversation so far."""
        with self.lock:
            self.clients[client] = loop
            return [self.snapshot] + list(self.history)

    def disconnect(self, client):
        with self.lock:
            self.clients.pop(client, None)

    # Messages from the browser

    def handle(self, message):
        """Act on a message from the browser. Returns False if it isn't a valid one."""
        if not isinstance(message, dict):
            return False
        kind = message.get("type")
        if kind == "input":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                return False
            self.inputs.put(text)
            return True
        if kind == "answer":
            ask_id = message.get("ask_id")
            value = message.get("value")
            if not isinstance(ask_id, str) or not isinstance(value, str):
                return False
            return self.answer(ask_id, value)
        if kind == "cancel":
            return self.cancel()
        return False

    def answer(self, ask_id, value):
        with self.lock:
            answers = self.answers.get(ask_id)
        if not answers:
            return False
        try:
            answers.put_nowait(value)
        except queue.Full:
            return False
        return True

    def cancel(self):
        """Stop the current work, like Esc in the terminal. Returns False if loom is idle."""
        with self.lock:
            if not self.busy or self.cancelled:
                return False
            self.cancelled = True
            self.turn_cancelled = True
            self.signalled = False
        self.signalled = bool(self.interrupt())
        return True

    def check_cancel(self):
        """In the main thread: raise KeyboardInterrupt for an Esc that couldn't be signalled.
        Questions and streamed replies check this as they wait."""
        with self.lock:
            if self.cancelled and not self.signalled:
                # Only once: consume_cancel() still reports it to the Coder
                self.signalled = True
                raise KeyboardInterrupt

    def consume_cancel(self):
        """Whether the KeyboardInterrupt being handled came from the browser's Esc."""
        with self.lock:
            cancelled = self.cancelled
            self.cancelled = False
            return cancelled

    def close(self):
        self.closed = True

    # Waiting on the browser, from the main thread

    def wait(self, waiting):
        """Return the next item from the queue waiting, polling so ^C can interrupt."""
        while True:
            if self.closed:
                raise EOFError
            self.check_cancel()
            try:
                return waiting.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue

    def wait_for_input(self):
        """Finish the current turn and return the user's next message."""
        self.end_turn()
        text = self.wait(self.inputs)
        self.start_turn(text)
        return text

    def start_turn(self, text):
        with self.lock:
            self.turn_id = self.next_id("t")
            self.cancelled = False
            self.turn_cancelled = False
            self.emit("user", text=text)
            self.emit("turn_start", turn_id=self.turn_id)
            self.update(busy=True)

    def end_turn(self):
        with self.lock:
            if not self.busy:
                return
            self.cancelled = False
            status = "cancelled" if self.turn_cancelled else "done"
            turn_id = self.turn_id
            self.turn_id = None
            # The browser has loom's new state by the time it hears the turn ended
            self.update(busy=False)
            self.emit("turn_end", turn_id=turn_id, status=status)

    def ask(self, kind, question, choices=(), default="", subject=None, tool_id=None):
        """Ask the browser a question and wait for its answer: one of the choices' values,
        or free text for a prompt. subject is what it's about, like a command to run, and
        tool_id the card of the tool call that needs the answer."""
        ask_id = self.next_id("a")
        answers = queue.Queue(maxsize=1)
        with self.lock:
            self.answers[ask_id] = answers
        self.emit(
            "ask",
            ask_id=ask_id,
            kind=kind,
            question=question,
            choices=[dict(value=value, label=label) for value, label in choices],
            default=default,
            subject=subject,
            tool_id=tool_id,
        )
        value = None
        try:
            value = self.wait(answers)
            return value
        finally:
            with self.lock:
                self.answers.pop(ask_id, None)
            # A null value means the question was dropped, by Esc, ^C or the server stopping
            self.emit("ask_resolved", ask_id=ask_id, value=value)

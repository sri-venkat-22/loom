"""
How a sub-agent (loom/subagents.py) talks to the user: through a SubAgentIO wrapped around
the parent's io.

- Its tool calls are shown indented under the parent's Task line. In a terminal the last
  few roll in place, with a count of the rest; with --verbose, or when the output isn't a
  terminal, every one is printed as it comes:

      ● Task(Explore auth flow)
        ⎿  Read(src/auth/session.py)
           Grep("login_required")
           … +9 more tool uses
        ⎿  Done (12 tool uses · 31k tokens · 40s)

- Its replies are hidden: only its report goes to the parent.
- Its questions say which sub-agent asks, like [explore: auth flow] Run this command?
- Everything it shows, says and is told is kept as its transcript, which the session
  saves (see Session.save_task) for /tasks.
"""

import contextlib
import functools
import threading
import time

from rich.console import Group
from rich.live import Live
from rich.text import Text

from loom.io import tool_call_parts

ASKS = ("confirm_ask", "permission_ask", "choice_ask", "prompt_ask")
# Not shown: the report goes to the parent, and the usage to the parent's report
HIDDEN = (
    "usage_output",
    "ai_output",
    "rule",
    "llm_started",
    "checkpoints_changed",
    "conversation_rewound",
)
# How many of a running task's latest tool calls a terminal shows
ROLLING_LINES = 3
FIRST_PREFIX = "  ⎿  "
PREFIX = "     "
# How much of a tool's result the transcript keeps; the model got all of it
MAX_TRANSCRIPT_RESULT = 2000


class NullStream:
    """A Markdown stream that shows nothing."""

    def update(self, text, final=False):
        pass


def live_ok(io):
    """Whether io can redraw lines in place: a pretty terminal."""
    console = getattr(io, "console", None)
    return bool(io.pretty and console is not None and console.is_terminal)


def more_line(hidden, style=None):
    word = "tool use" if hidden == 1 else "tool uses"
    return Text(f"{PREFIX}… +{hidden} more {word}", style=style)


class TaskBoard:
    """Shows the tool calls of running tasks in the terminal, under their Task lines."""

    def __init__(self, io, verbose=False):
        self.io = io
        self.rolling = not verbose and live_ok(io)
        self.views = []
        self.live = None
        self.lock = threading.RLock()

    def view(self, task):
        """The part of the board for task."""
        with self.lock:
            view = TaskView(self, task)
            self.views.append(view)
            return view

    def style(self, name):
        return name if self.io.pretty else None

    def update(self):
        """Show what changed: redraw the rolling lines."""
        if not self.rolling:
            return
        with self.lock:
            if self.live is None:
                self.live = Live(
                    console=self.io.console,
                    get_renderable=self.render,
                    refresh_per_second=8,
                    transient=True,
                )
                self.live.start()
            else:
                self.live.refresh()

    def render(self):
        with self.lock:
            return Group(*[line for view in self.views for line in view.block()])

    def pause(self):
        """Before a question, and when the tasks are done: stop redrawing, and leave the
        lines shown on the screen."""
        with self.lock:
            if self.live is None:
                return
            self.live.stop()
            self.live = None
            for view in self.views:
                for line in view.block():
                    self.print(line)
                view.printed = len(view.lines)

    def close(self):
        self.pause()

    def print(self, text):
        self.io.append_chat_history(text.plain, linebreak=True, blockquote=True)
        self.io._print_text(text, no_wrap=True, overflow="ellipsis")


class TaskView:
    """One task's lines on a TaskBoard: its tool calls and its warnings."""

    def __init__(self, board, task, window=ROLLING_LINES):
        self.board = board
        self.task = task
        self.window = window
        # (Text, whether it's a tool call) for every line, and how many are on the
        # screen for good
        self.lines = []
        self.printed = 0

    def tool_call(self, name, detail=""):
        name, detail = tool_call_parts(name, detail, self.board.io.console.width - 9)
        text = Text()
        text.append(name, style=self.board.style("bold"))
        if detail:
            text.append(f"({detail})")
        self.add(text, call=True)

    def note(self, text, style=None):
        for line in text.strip().splitlines():
            self.add(Text(line, style=self.board.style(style or "dim")), call=False)

    def add(self, text, call):
        board = self.board
        with board.lock:
            self.lines.append((text, call))
            if board.rolling:
                board.update()
                return
            board.print(self.prefixed(text, len(self.lines) - 1 == 0))
            self.printed = len(self.lines)

    def block(self):
        """The lines to show: the latest few, and how many tool calls are hidden."""
        start = max(self.printed, len(self.lines) - self.window)
        hidden = sum(1 for _, call in self.lines[self.printed : start] if call)
        res = [
            self.prefixed(text, index == start and self.printed == 0)
            for index, (text, _) in enumerate(self.lines[start:], start)
        ]
        if hidden:
            res.append(more_line(hidden, self.board.style("dim")))
        return res

    def prefixed(self, text, first):
        line = Text(FIRST_PREFIX if first else PREFIX, style=self.board.style("dim"))
        return line + text


class SubAgentIO:
    """The io of a sub-agent doing task, wrapped around the parent's io. view shows its
    tool calls."""

    def __init__(self, io, task, view=None):
        own = vars(self)
        own.update(
            main=io,
            task=task,
            view=view or TaskBoard(io).view(task),
            # No spinners or live Markdown from a sub-agent
            pretty=False,
            # Everything it showed, said and was told: {time, kind, ...}
            transcript=[],
        )

    def __getattr__(self, name):
        if name in ASKS:
            return functools.partial(self.ask, name)
        if name in HIDDEN:
            return lambda *args, **kwargs: None
        return getattr(self.main, name)

    def __setattr__(self, name, value):
        # Never on the parent's io
        vars(self)[name] = value

    def record(self, kind, **data):
        self.transcript.append(dict(time=round(time.time(), 3), kind=kind, **data))

    # Questions

    def ask(self, name, *args, **kwargs):
        """Ask with the parent's io, the question saying which sub-agent asks."""
        label = f"[{self.task.label}] "
        if args and isinstance(args[0], str):
            args = (label + args[0],) + args[1:]
        elif isinstance(kwargs.get("question"), str):
            kwargs["question"] = label + kwargs["question"]
        self.view.board.pause()
        answer = getattr(self.main, name)(*args, **kwargs)
        question = args[0] if args else kwargs.get("question")
        self.record("ask", question=question, answer=answer)
        return answer

    # What it shows

    def tool_call(self, name, detail="", args=None):
        self.record("tool_call", name=name, detail=detail, args=args)
        self.view.tool_call(name, detail)

    def tool_result(self, lines, error=False, styles=None):
        if isinstance(lines, str):
            lines = lines.splitlines() or [""]
        self.record("tool_result", lines=[str(line) for line in lines], error=error)

    def tool_done(self, result, error=False):
        result = str(result)
        if len(result) > MAX_TRANSCRIPT_RESULT:
            result = result[:MAX_TRANSCRIPT_RESULT] + "…"
        self.record("tool_done", result=result, error=error)

    def todo_output(self, todos):
        self.record("todos", todos=[dict(todo) for todo in todos])

    def diff_output(self, diff, indent=""):
        self.record("diff", diff=diff)

    def tool_output(self, *messages, log_only=False, bold=False):
        text = " ".join(str(message) for message in messages)
        if not text.strip():
            return
        self.record("output", text=text)
        if not log_only:
            self.view.note(text)

    def tool_warning(self, message="", strip=True):
        self.record("warning", text=str(message))
        if str(message).strip():
            self.view.note(str(message), self.main.tool_warning_color)

    def tool_error(self, message="", strip=True):
        self.record("error", text=str(message))
        if str(message).strip():
            self.view.note(str(message), self.main.tool_error_color)

    def assistant_output(self, message, pretty=None):
        self.record("assistant", text=message)

    def user_input(self, inp, log_only=True):
        self.record("user", text=inp)

    def append_chat_history(self, text, linebreak=False, blockquote=False, strip=True):
        # The parent's chat history gets the lines shown, not the sub-agent's own
        pass

    def get_assistant_mdstream(self):
        return NullStream()

    @contextlib.contextmanager
    def esc_interrupts(self):
        # The parent's Esc listener is already running
        yield


def show_transcript(io, record):
    """Show a saved task (Session.load_tasks) the way it ran: its prompt, its tool calls
    with their outcomes, its questions and its report."""
    from loom.subagents import describe_record

    io.tool_call(f"Task {record['number']}", record.get("description", ""))
    io.tool_result(describe_record(record))
    for event in record.get("events") or []:
        kind = event.get("kind")
        if kind == "user":
            io.tool_output()
            io.display_user_input("> " + str(event.get("text") or "").strip())
        elif kind == "tool_call":
            io.tool_call(event.get("name") or "tool", event.get("detail") or "")
        elif kind == "tool_result":
            io.tool_result(event.get("lines") or [""], error=event.get("error", False))
        elif kind == "ask":
            io.tool_output(f"{event.get('question', '').strip()} {event.get('answer')}")
        elif kind == "warning":
            io.tool_warning(event.get("text", ""))
        elif kind == "error":
            io.tool_error(event.get("text", ""))
        elif kind == "todos":
            io.todo_output(event.get("todos") or [])
        elif kind == "assistant" and str(event.get("text") or "").strip():
            io.assistant_output(event["text"], pretty=io.pretty)
    if record.get("status") not in ("done",) and record.get("report"):
        io.tool_output()
        io.tool_output("Report:")
        io.assistant_output(record["report"], pretty=io.pretty)

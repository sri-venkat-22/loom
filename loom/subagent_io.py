"""
How a sub-agent (loom/subagents.py) talks to the user: through a SubAgentIO wrapped around
the parent's io.

- Its tool calls are shown indented under the parent's Task line.
- Its replies are hidden: only its report goes to the parent.
- Its questions say which sub-agent asks, like [explore: auth flow] Run this command?
- Everything it shows, says and is told is kept as its transcript.
"""

import contextlib
import functools
import time

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


class NullStream:
    """A Markdown stream that shows nothing."""

    def update(self, text, final=False):
        pass


class SubAgentIO:
    """The io of a sub-agent doing task, wrapped around the parent's io."""

    def __init__(self, io, task):
        own = vars(self)
        own.update(
            main=io,
            task=task,
            # No spinners or live Markdown from a sub-agent
            pretty=False,
            # (time, kind, data) for everything it showed, said or was told
            transcript=[],
            calls=0,
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
        self.transcript.append(dict(time=time.time(), kind=kind, **data))

    # Questions

    def ask(self, name, *args, **kwargs):
        """Ask with the parent's io, the question saying which sub-agent asks."""
        label = f"[{self.task.label}] "
        if args and isinstance(args[0], str):
            args = (label + args[0],) + args[1:]
        elif isinstance(kwargs.get("question"), str):
            kwargs["question"] = label + kwargs["question"]
        answer = getattr(self.main, name)(*args, **kwargs)
        question = args[0] if args else kwargs.get("question")
        self.record("ask", question=question, answer=answer)
        return answer

    # What it shows

    def tool_call(self, name, detail="", args=None):
        self.record("tool_call", name=name, detail=detail, args=args)
        self.calls += 1
        name, detail = tool_call_parts(name, detail, self.main.console.width - 9)
        text = Text()
        text.append(
            "  ⎿  " if self.calls == 1 else "     ", style="dim" if self.main.pretty else None
        )
        text.append(name, style="bold" if self.main.pretty else None)
        if detail:
            text.append(f"({detail})")
        self.show(text)

    def tool_result(self, lines, error=False, styles=None):
        if isinstance(lines, str):
            lines = lines.splitlines() or [""]
        self.record("tool_result", lines=list(lines), error=error)

    def tool_done(self, result, error=False):
        self.record("tool_done", result=result, error=error)

    def todo_output(self, todos):
        self.record("todos", todos=[dict(todo) for todo in todos])

    def diff_output(self, diff, indent=""):
        self.record("diff", diff=diff)

    def tool_output(self, *messages, log_only=False, bold=False):
        text = " ".join(str(message) for message in messages)
        self.record("output", text=text)
        if text.strip() and not log_only:
            self.note(text)

    def tool_warning(self, message="", strip=True):
        self.record("warning", text=str(message))
        if str(message).strip():
            self.note(str(message), self.main.tool_warning_color)

    def tool_error(self, message="", strip=True):
        self.record("error", text=str(message))
        if str(message).strip():
            self.note(str(message), self.main.tool_error_color)

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

    def note(self, text, color=None):
        """Show a line from the sub-agent, like a warning, indented under the Task line."""
        style = color if self.main.pretty and color else ("dim" if self.main.pretty else None)
        for line in text.strip().splitlines():
            self.show(Text("     " + line, style=style))

    def show(self, text):
        self.main.append_chat_history(text.plain, linebreak=True, blockquote=True)
        self.main._print_text(text, no_wrap=True, overflow="ellipsis")

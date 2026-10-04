"""How the web UI shows sub-agents (loom/subagents.py).

A task's Task card is the parent's tool card for the task call. Each sub-agent's own tool
calls become cards nested in it: their messages carry agent_id (the task's number) and
parent_id (the Task card's id). A task message keeps the Task card's status and numbers up
to date, and its stop button sends stop_task. Tasks that run at once share a batch, and
their Task cards are made here, since the parent doesn't show those calls itself.
"""

import json
from contextlib import contextmanager

from loom.display import sanitize_for_display

from .protocol import MAX_TOOL_OUTPUT


def todo_lines(todos):
    """A to-do list as a card's lines and their styles, like the terminal's checklist."""
    marks = dict(completed=("☒", "done"), in_progress=("◼", "bold"))
    lines, styles = [], []
    for todo in todos:
        mark, style = marks.get(todo.get("status"), ("☐", None))
        lines.append(f"{mark} {todo.get('content', '')}")
        styles.append(style)
    return lines, styles


class WebTaskBoard:
    """The web UI's TaskBoard (loom/subagent_io.py): cards instead of lines."""

    def __init__(self, io, headers=False):
        self.io = io
        self.web = io.web
        self.headers = headers
        self.views = []
        self.batch = self.web.next_id("b")
        # One task's Task card is the parent's open tool card
        self.parent_card = None if headers else io.tool_id
        self.web.task_stopper = self.stop

    def view(self, task):
        if self.headers:
            card = self.web.next_id("c")
            args = dict(
                description=task.description, prompt=task.prompt, agent=task.agent_type.name
            )
            self.web.emit(
                "tool_start",
                id=card,
                name="Task",
                detail=sanitize_for_display(task.description, show_escapes=True),
                args=json.dumps(args, indent=2, ensure_ascii=False),
            )
        else:
            card = self.parent_card
        view = WebTaskView(self, task, card)
        self.views.append(view)
        view.changed()
        return view

    def update(self):
        pass

    @contextmanager
    def asking(self, view):
        """A task's question goes on the card of the call it's about."""
        io = self.io
        if view is None:
            yield
            return
        saved = io.tool_id
        io.tool_id = view.current or view.card
        try:
            yield
        finally:
            io.tool_id = saved

    def close(self):
        if self.web.task_stopper == self.stop:
            self.web.task_stopper = None
        if not self.headers:
            return
        for view in self.views:
            view.end_card()
            failed = view.task.status not in ("done", "incomplete")
            output = sanitize_for_display(view.task.result() if view.task.child else "")
            self.web.emit(
                "tool_end",
                id=view.card,
                status="failed" if failed else "done",
                output=output[:MAX_TOOL_OUTPUT],
            )

    def stop(self, agent_id):
        """The stop button of task agent_id. Returns whether it was running."""
        for view in self.views:
            if str(view.task.number) == agent_id:
                return view.task.stop()
        return False


class WebTaskView:
    """One task's cards, nested in its Task card."""

    def __init__(self, board, task, card):
        self.board = board
        self.task = task
        self.card = card
        # The sub-agent's tool card that's open
        self.current = None
        self.failed = False

    def emit(self, type, **payload):
        return self.board.web.emit(
            type, agent_id=str(self.task.number), parent_id=self.card, **payload
        )

    def tool_call(self, name, detail="", args=None):
        self.end_card()
        self.current = self.board.web.next_id("c")
        self.failed = False
        if args is not None:
            try:
                args = json.dumps(args, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                args = str(args)
        self.emit(
            "tool_start",
            id=self.current,
            name=sanitize_for_display(name, show_escapes=True),
            detail=sanitize_for_display(detail or "", show_escapes=True),
            args=args,
        )

    def tool_result(self, lines, error=False, styles=None):
        from .webio import line_style

        lines = [sanitize_for_display(str(line)) for line in lines]
        styles = [line_style(style) for style in (styles or [])]
        styles += [None] * (len(lines) - len(styles))
        if not self.current:
            self.note("\n".join(lines), level="error" if error else "info")
            return
        self.failed = self.failed or error
        self.emit("tool_output", id=self.current, lines=lines, error=error, styles=styles)

    def tool_done(self, result, error=False):
        self.end_card(result, error)

    def end_card(self, result="", error=False):
        if not self.current:
            return
        output = sanitize_for_display(str(result or ""))
        if len(output) > MAX_TOOL_OUTPUT:
            output = output[:MAX_TOOL_OUTPUT] + "\n…"
        status = "failed" if error or self.failed else "done"
        self.emit("tool_end", id=self.current, status=status, output=output)
        self.current = None

    def diff(self, diff):
        from .webio import parse_diff

        file, lines = parse_diff(sanitize_for_display(diff, show_escapes=True))
        self.emit(
            "diff", id=self.board.web.next_id("d"), tool_id=self.current, file=file, lines=lines
        )

    def todos(self, todos):
        lines, styles = todo_lines(todos)
        if self.current:
            self.emit("tool_output", id=self.current, lines=lines, error=False, styles=styles)

    def note(self, text, style=None, level="info"):
        if text.strip():
            self.emit("system", level=level, text=sanitize_for_display(text).strip("\n"))

    def changed(self):
        """Tell the browser how the task is doing."""
        task = self.task
        if task.status not in ("pending", "running"):
            self.end_card()
        self.board.web.emit(
            "task",
            agent_id=str(task.number),
            parent_id=self.card,
            batch=self.board.batch,
            number=task.number,
            description=task.description,
            agent=task.agent_type.name,
            model=task.model.name,
            prompt=task.prompt,
            status=task.status,
            summary=task.summary() if task.status not in ("pending", "running") else "",
            tool_uses=task.tool_uses,
            tokens=task.tokens,
            cost=round(task.cost, 6),
            seconds=round(task.seconds, 1),
        )

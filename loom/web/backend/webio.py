"""WebIO: the InputOutput `loom --web` gives the Coder, so it talks to the browser.

Output still goes to the terminal and the chat history file as usual, and is also sent to
the browser. Input and questions come from the browser once the server is listening;
before that, like for startup questions, they're asked in the terminal.
"""

import json
from contextlib import contextmanager

from rich.text import Text

from loom import __version__
from loom.display import sanitize_for_display
from loom.io import HUNK_RE, InputOutput
from loom.reasoning_tags import REASONING_END, REASONING_START

from .session import WebSession


def split_reasoning(text):
    """Split a reply as the Coder shows it, with its thinking between REASONING_START and
    REASONING_END, into (thinking, answer)."""
    start = text.find(REASONING_START)
    if start < 0:
        return "", text.strip()
    before = text[:start]
    rest = text[start + len(REASONING_START) :]
    end = rest.find(REASONING_END)
    if end < 0:
        # Still thinking
        return rest.strip(), before.strip()
    return rest[:end].strip(), (before + rest[end + len(REASONING_END) :]).strip()


class WebMarkdownStream:
    """Stands in for the terminal's MarkdownStream: streams a reply to the browser."""

    def __init__(self, session):
        self.session = session
        self.id = session.next_id("m")
        self.reasoning = ""
        self.text = ""
        self.ended = False

    def update(self, text, final=False):
        if self.ended:
            return
        self.session.check_cancel()
        reasoning, text = split_reasoning(text)
        if (reasoning, text) != (self.reasoning, self.text):
            if reasoning.startswith(self.reasoning) and text.startswith(self.text):
                self.session.emit(
                    "assistant_delta",
                    id=self.id,
                    text=text[len(self.text) :],
                    reasoning=reasoning[len(self.reasoning) :],
                    replace=False,
                )
            else:
                self.session.emit(
                    "assistant_delta", id=self.id, text=text, reasoning=reasoning, replace=True
                )
            self.reasoning, self.text = reasoning, text
        if final:
            self.ended = True
            self.session.emit("assistant_end", id=self.id)


class WebPrompt:
    """Stands in for prompt_toolkit's PromptSession, which InputOutput's questions read
    their answers from, and asks the browser instead."""

    app = None
    history = None

    def __init__(self, io):
        self.io = io

    def prompt(self, message, default="", **kwargs):
        ask = self.io.pending_ask or dict(kind="prompt", question=message.strip(), choices=())
        if ask["kind"] == "prompt":
            default = default or ""
        else:
            default = ask.get("default", "")
        return self.io.web.ask(
            ask["kind"],
            ask["question"],
            ask["choices"],
            default,
            subject=ask.get("subject"),
            tool_id=self.io.tool_id,
        )


# Longest tool result the browser gets, for the expanded card
MAX_TOOL_OUTPUT = 20_000


def plain(message):
    if isinstance(message, Text):
        return message.plain
    return str(message)


def parse_diff(diff):
    """The file a unified diff changes, and its lines as dicts of kind (add, del, ctx, gap
    between hunks, or note), old and new line numbers, and text."""
    file = ""
    lines = []
    old = new = 0
    in_hunk = False
    for line in diff.splitlines():
        match = HUNK_RE.match(line)
        if match:
            if in_hunk:
                lines.append(dict(kind="gap", old=None, new=None, text=""))
            in_hunk = True
            old, new = int(match.group(1)), int(match.group(2))
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                file = line[4:].strip()
                if file.startswith("b/"):
                    file = file[2:]
            continue
        if line.startswith("-"):
            lines.append(dict(kind="del", old=old, new=None, text=line[1:]))
            old += 1
        elif line.startswith("+"):
            lines.append(dict(kind="add", old=None, new=new, text=line[1:]))
            new += 1
        elif line.startswith(" ") or not line:
            lines.append(dict(kind="ctx", old=old, new=new, text=line[1:]))
            old += 1
            new += 1
        else:
            # Like "... (12 more diff lines)"
            lines.append(dict(kind="note", old=None, new=None, text=line))
    return file, lines


def line_style(style):
    """The browser's name for a rich style the terminal shows a result line in."""
    if not style:
        return None
    style = str(style).lower()
    if "strike" in style:
        return "done"
    if "yellow" in style or "orange" in style:
        return "warning"
    if "red" in style or style.startswith("#f"):
        return "error"
    if "bold" in style:
        return "bold"
    if "dim" in style:
        return "dim"
    return None


class WebIO(InputOutput):
    def __init__(self, *args, session=None, **kwargs):
        # No prompt_toolkit: the terminal isn't where loom reads from
        kwargs["fancy_input"] = False
        super().__init__(*args, **kwargs)
        self.web = session or WebSession()
        self.web_prompt = WebPrompt(self)
        # What the question being asked is, for the browser to show it with buttons
        self.pending_ask = None
        # The tool call being shown, whose results and diffs go on its card
        self.tool_id = None
        self.tool_failed = False

    @property
    def prompt_session(self):
        # Questions go to the browser once it can be reached, and to the terminal before
        if getattr(self, "web", None) and self.web.started:
            return self.web_prompt
        return None

    @prompt_session.setter
    def prompt_session(self, value):
        pass

    @property
    def agent_diffs(self):
        # Every edit shows its diff, and an edit's question shows it with y and n to answer
        return True

    @agent_diffs.setter
    def agent_diffs(self, value):
        pass

    # Input

    def get_input(
        self,
        root,
        rel_fnames,
        addable_rel_fnames,
        commands,
        abs_read_only_fnames=None,
        edit_format=None,
        cycle_mode=None,
    ):
        if not self.web.started:
            return super().get_input(
                root,
                rel_fnames,
                addable_rel_fnames,
                commands,
                abs_read_only_fnames,
                edit_format,
                cycle_mode,
            )
        self.end_tool()
        coder = getattr(commands, "coder", None)
        self.web.update(**self.session_state(coder, root, rel_fnames, commands))
        inp = self.web.wait_for_input()
        self.add_to_input_history(inp)
        self.user_input(inp)
        return inp

    def session_state(self, coder, root, rel_fnames, commands):
        state = dict(
            version=__version__,
            cwd=str(root),
            files=sorted(rel_fnames),
            commands=list_commands(commands),
        )
        if coder:
            model = coder.main_model
            state.update(
                model=model.name,
                weak_model=model.weak_model.name if model.weak_model else "",
                edit_format=coder.edit_format,
                read_only_files=sorted(coder.get_rel_fname(f) for f in coder.abs_read_only_fnames),
                tokens=dict(sent=coder.total_tokens_sent, received=coder.total_tokens_received),
                cost=coder.total_cost,
            )
        return state

    # Esc comes from the browser, as a cancel message

    @contextmanager
    def esc_interrupts(self):
        yield

    def consume_esc(self):
        return self.web.consume_cancel()

    # Questions

    @contextmanager
    def asking(self, kind, question, choices, default, subject=None):
        # The question shows its subject, so it isn't also shown as output
        if subject:
            subject = sanitize_for_display(subject, show_escapes=True)
        self.pending_ask = dict(
            kind=kind, question=question, choices=choices, default=default, subject=subject
        )
        try:
            yield
        finally:
            self.pending_ask = None

    def confirm_ask(
        self,
        question,
        default="y",
        subject=None,
        explicit_yes_required=False,
        group=None,
        allow_never=False,
    ):
        choices = [("y", "Yes"), ("n", "No")]
        if group and group.show_group:
            if not explicit_yes_required:
                choices.append(("a", "All"))
            choices.append(("s", "Skip all"))
            allow_never = True
        if allow_never:
            choices.append(("d", "Don't ask again"))
        with self.asking("confirm", question, choices, default[:1].lower(), subject):
            return super().confirm_ask(
                question,
                default=default,
                subject=subject,
                explicit_yes_required=explicit_yes_required,
                group=group,
                allow_never=allow_never,
            )

    def permission_ask(
        self, question, subject=None, always=None, explicit_yes_required=False, bypass=None
    ):
        choices = [("yes", "Yes"), ("no", "No")]
        if always:
            choices.append(("always", f"Always: {always}"))
        if bypass:
            choices.append(("bypass", f"Bypass permissions: {bypass}"))
        if subject and subject.startswith("--- "):
            # An edit: the browser shows the diff on the tool's card, with y and n to answer
            self.diff_output(sanitize_for_display(subject, show_escapes=True))
            shown = None
        else:
            shown = subject
        with self.asking("permission", question, choices, "yes", shown):
            return super().permission_ask(
                question,
                subject=shown,
                always=always,
                explicit_yes_required=explicit_yes_required,
                bypass=bypass,
            )

    def choice_ask(self, question, choices, default=None, yes_choice=None, no_choice=None):
        labels = [(choice, choice.capitalize()) for choice in choices]
        with self.asking("choice", question, labels, default or choices[0]):
            return super().choice_ask(
                question, choices, default=default, yes_choice=yes_choice, no_choice=no_choice
            )

    def prompt_ask(self, question, default="", subject=None):
        with self.asking("prompt", question, (), default, subject):
            return super().prompt_ask(question, default=default, subject=subject)

    # Output

    def tool_output(self, *messages, log_only=False, bold=False):
        super().tool_output(*messages, log_only=log_only, bold=bold)
        if not log_only and not self.pending_ask:
            self.emit_system("info", " ".join(plain(message) for message in messages))

    def tool_warning(self, message="", strip=True):
        super().tool_warning(message, strip)
        self.emit_system("warning", plain(message))

    def tool_error(self, message="", strip=True):
        super().tool_error(message, strip)
        self.emit_system("error", plain(message))

    def usage_output(self, report):
        # The status line shows the tokens
        super().tool_output(report, log_only=True)

    def emit_system(self, level, text):
        # InputOutput.__init__ can warn before the session exists
        if getattr(self, "web", None) and text.strip():
            self.web.emit("system", level=level, text=text.strip("\n"))

    # Agent tool calls, as cards

    def tool_call(self, name, detail="", args=None):
        super().tool_call(name, detail, args=args)
        self.end_tool()
        self.tool_id = self.web.next_id("c")
        self.tool_failed = False
        if args is not None:
            try:
                args = json.dumps(args, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                args = str(args)
        self.web.emit(
            "tool_start",
            id=self.tool_id,
            name=sanitize_for_display(name, show_escapes=True),
            detail=sanitize_for_display(detail or "", show_escapes=True),
            args=args,
        )

    def tool_result(self, lines, error=False, styles=None):
        super().tool_result(lines, error=error, styles=styles)
        if isinstance(lines, str):
            lines = lines.splitlines() or [""]
        lines = [sanitize_for_display(str(line)) for line in lines]
        styles = [line_style(style) for style in (styles or [])]
        styles += [None] * (len(lines) - len(styles))
        if not self.tool_id:
            self.emit_system("error" if error else "info", "\n".join(lines))
            return
        if error:
            self.tool_failed = True
        self.web.emit("tool_output", id=self.tool_id, lines=lines, error=error, styles=styles)

    def tool_done(self, result, error=False):
        super().tool_done(result, error)
        self.end_tool(result, error)

    def end_tool(self, result=None, error=False):
        """Finish the open tool card, if there is one."""
        if not self.tool_id:
            return
        output = sanitize_for_display(result or "")
        if len(output) > MAX_TOOL_OUTPUT:
            output = output[:MAX_TOOL_OUTPUT] + "\n…"
        failed = error or self.tool_failed
        self.web.emit(
            "tool_end", id=self.tool_id, status="failed" if failed else "done", output=output
        )
        self.tool_id = None

    def diff_output(self, diff, indent=""):
        super().diff_output(diff, indent)
        file, lines = parse_diff(diff)
        self.web.emit(
            "diff", id=self.web.next_id("d"), tool_id=self.tool_id, file=file, lines=lines
        )

    # Replies

    def get_assistant_mdstream(self):
        self.end_tool()
        return WebMarkdownStream(self.web)

    def assistant_output(self, message, pretty=None):
        super().assistant_output(message, pretty)
        self.end_tool()
        if message:
            stream = WebMarkdownStream(self.web)
            stream.update(message, final=True)


def list_commands(commands):
    """The slash commands, with the first line of their help, for the browser to offer."""
    if not commands:
        return []
    listed = []
    for cmd in sorted(commands.get_commands()):
        method = getattr(commands, "cmd_" + cmd[1:].replace("-", "_"), None)
        doc = (method.__doc__ or "").strip().splitlines() if method else []
        listed.append(dict(cmd=cmd, desc=doc[0] if doc else ""))
    return listed

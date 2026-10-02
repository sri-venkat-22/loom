"""WebIO: the InputOutput `loom --web` gives the Coder, so it talks to the browser.

Output still goes to the terminal and the chat history file as usual, and is also sent to
the browser. Input and questions come from the browser once the server is listening;
before that, like for startup questions, they're asked in the terminal.
"""

from contextlib import contextmanager

from rich.text import Text

from loom import __version__
from loom.io import InputOutput
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
        return self.io.web.ask(ask["kind"], ask["question"], ask["choices"], default)


def plain(message):
    if isinstance(message, Text):
        return message.plain
    return str(message)


class WebIO(InputOutput):
    def __init__(self, *args, session=None, **kwargs):
        # No prompt_toolkit: the terminal isn't where loom reads from
        kwargs["fancy_input"] = False
        super().__init__(*args, **kwargs)
        self.web = session or WebSession()
        self.web_prompt = WebPrompt(self)
        # What the question being asked is, for the browser to show it with buttons
        self.pending_ask = None

    @property
    def prompt_session(self):
        # Questions go to the browser once it can be reached, and to the terminal before
        if getattr(self, "web", None) and self.web.started:
            return self.web_prompt
        return None

    @prompt_session.setter
    def prompt_session(self, value):
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
    def asking(self, kind, question, choices, default):
        self.pending_ask = dict(kind=kind, question=question, choices=choices, default=default)
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
        with self.asking("confirm", question, choices, default[:1].lower()):
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
        with self.asking("permission", question, choices, "yes"):
            return super().permission_ask(
                question,
                subject=subject,
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
        with self.asking("prompt", question, (), default):
            return super().prompt_ask(question, default=default, subject=subject)

    # Output

    def tool_output(self, *messages, log_only=False, bold=False):
        super().tool_output(*messages, log_only=log_only, bold=bold)
        if not log_only:
            self.emit_system("info", " ".join(plain(message) for message in messages))

    def tool_warning(self, message="", strip=True):
        super().tool_warning(message, strip)
        self.emit_system("warning", plain(message))

    def tool_error(self, message="", strip=True):
        super().tool_error(message, strip)
        self.emit_system("error", plain(message))

    def _print_text(self, text, **kwargs):
        # Tool calls, their results, diffs and to-do lists
        super()._print_text(text, **kwargs)
        self.emit_system("info", plain(text).rstrip("\n"))

    def emit_system(self, level, text):
        # InputOutput.__init__ can warn before the session exists
        if getattr(self, "web", None) and text.strip():
            self.web.emit("system", level=level, text=text.strip("\n"))

    def get_assistant_mdstream(self):
        return WebMarkdownStream(self.web)

    def assistant_output(self, message, pretty=None):
        super().assistant_output(message, pretty)
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

import base64
import functools
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import webbrowser
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition, has_completions, is_searching
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.output.vt100 import is_dumb_terminal
from prompt_toolkit.shortcuts import CompleteStyle, PromptSession
from prompt_toolkit.styles import Style
from pygments.lexers import MarkdownLexer, guess_lexer_for_filename
from pygments.token import Token
from rich.color import ColorParseError
from rich.columns import Columns
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.style import Style as RichStyle
from rich.text import Text

from loom.display import sanitize_for_display
from loom.esc import EscListener
from loom.mdstream import MarkdownStream

from .dump import dump  # noqa: F401
from .editor import pipe_editor
from .utils import is_image_file

# Constants
NOTIFICATION_MESSAGE = "Loom is waiting for your input"


def ensure_hash_prefix(color):
    """Ensure hex color values have a # prefix."""
    if not color:
        return color
    if isinstance(color, str) and color.strip() and not color.startswith("#"):
        # Check if it's a valid hex color (3 or 6 hex digits)
        if all(c in "0123456789ABCDEFabcdef" for c in color) and len(color) in (3, 6):
            return f"#{color}"
    return color


def restore_multiline(func):
    """Decorator to restore multiline mode after function execution"""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        orig_multiline = self.multiline_mode
        self.multiline_mode = False
        try:
            return func(self, *args, **kwargs)
        except Exception:
            raise
        finally:
            self.multiline_mode = orig_multiline

    return wrapper


def pause_esc(func):
    """Stop watching for Esc while asking the user something, so the question gets the keys."""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        listener = self.esc_listener
        if not (listener and listener.running):
            return func(self, *args, **kwargs)
        listener.stop()
        try:
            return func(self, *args, **kwargs)
        finally:
            listener.start()

    return wrapper


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def numbered_diff_lines(diff):
    """(line number, marker, text) for each line of a unified diff, where the marker is -, +
    or a space. Removed lines have their old line number, others their new one. A gap
    between hunks is None."""
    res = []
    old = new = 0
    in_hunk = False
    for line in diff.splitlines():
        match = HUNK_RE.match(line)
        if match:
            if in_hunk:
                res.append(None)
            in_hunk = True
            old, new = int(match.group(1)), int(match.group(2))
            continue
        if not in_hunk:
            # The --- and +++ file headers
            continue
        if line.startswith("-"):
            res.append((old, "-", line[1:]))
            old += 1
        elif line.startswith("+"):
            res.append((new, "+", line[1:]))
            new += 1
        elif line.startswith(" ") or not line:
            res.append((new, " ", line[1:]))
            old += 1
            new += 1
        else:
            # Like "... (12 more diff lines)"
            res.append(("", "", line))
    return res


def count_diff_changes(diff):
    """(added lines, removed lines) in a unified diff."""
    lines = numbered_diff_lines(diff)
    added = sum(1 for line in lines if line and line[1] == "+")
    removed = sum(1 for line in lines if line and line[1] == "-")
    return added, removed


class CommandCompletionException(Exception):
    """Raised when a command should use the normal autocompleter instead of
    command-specific completion."""

    pass


@dataclass
class ConfirmGroup:
    preference: str = None
    show_group: bool = True

    def __init__(self, items=None):
        if items is not None:
            self.show_group = len(items) > 1


class AutoCompleter(Completer):
    def __init__(
        self, root, rel_fnames, addable_rel_fnames, commands, encoding, abs_read_only_fnames=None
    ):
        self.addable_rel_fnames = addable_rel_fnames
        self.rel_fnames = rel_fnames
        self.encoding = encoding
        self.abs_read_only_fnames = abs_read_only_fnames or []

        fname_to_rel_fnames = defaultdict(list)
        for rel_fname in addable_rel_fnames:
            fname = os.path.basename(rel_fname)
            if fname != rel_fname:
                fname_to_rel_fnames[fname].append(rel_fname)
        self.fname_to_rel_fnames = fname_to_rel_fnames

        self.words = set()

        self.commands = commands
        self.command_completions = dict()
        if commands:
            self.command_names = self.commands.get_commands()

        for rel_fname in addable_rel_fnames:
            self.words.add(rel_fname)

        for rel_fname in rel_fnames:
            self.words.add(rel_fname)

        all_fnames = [Path(root) / rel_fname for rel_fname in rel_fnames]
        if abs_read_only_fnames:
            all_fnames.extend(abs_read_only_fnames)

        self.all_fnames = all_fnames
        self.tokenized = False

    def tokenize(self):
        if self.tokenized:
            return
        self.tokenized = True

        for fname in self.all_fnames:
            try:
                with open(fname, "r", encoding=self.encoding) as f:
                    content = f.read()
            except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
                continue
            try:
                lexer = guess_lexer_for_filename(fname, content)
            except Exception:  # On Windows, bad ref to time.clock which is deprecated
                continue

            tokens = list(lexer.get_tokens(content))
            self.words.update(
                (token[1], f"`{token[1]}`") for token in tokens if token[0] in Token.Name
            )

    def get_command_completions(self, document, complete_event, text, words):
        if len(words) == 1 and not text[-1].isspace():
            partial = words[0].lower()
            candidates = [cmd for cmd in self.command_names if cmd.startswith(partial)]
            for candidate in sorted(candidates):
                yield Completion(candidate, start_position=-len(words[-1]))
            return

        if len(words) <= 1 or text[-1].isspace():
            return

        cmd = words[0]
        partial = words[-1].lower()

        matches, _, _ = self.commands.matching_commands(cmd)
        if len(matches) == 1:
            cmd = matches[0]
        elif cmd not in matches:
            return

        raw_completer = self.commands.get_raw_completions(cmd)
        if raw_completer:
            yield from raw_completer(document, complete_event)
            return

        if cmd not in self.command_completions:
            candidates = self.commands.get_completions(cmd)
            self.command_completions[cmd] = candidates
        else:
            candidates = self.command_completions[cmd]

        if candidates is None:
            return

        candidates = [word for word in candidates if partial in word.lower()]
        for candidate in sorted(candidates):
            yield Completion(candidate, start_position=-len(words[-1]))

    def get_completions(self, document, complete_event):
        self.tokenize()

        text = document.text_before_cursor
        words = text.split()
        if not words:
            return

        if text and text[-1].isspace():
            # don't keep completing after a space
            return

        if text[0] == "/":
            try:
                yield from self.get_command_completions(document, complete_event, text, words)
                return
            except CommandCompletionException:
                # Fall through to normal completion
                pass

        candidates = self.words
        candidates.update(set(self.fname_to_rel_fnames))
        candidates = [word if type(word) is tuple else (word, word) for word in candidates]

        last_word = words[-1]

        # Only provide completions if the user has typed at least 3 characters
        if len(last_word) < 3:
            return

        completions = []
        for word_match, word_insert in candidates:
            if word_match.lower().startswith(last_word.lower()):
                completions.append((word_insert, -len(last_word), word_match))

                rel_fnames = self.fname_to_rel_fnames.get(word_match, [])
                if rel_fnames:
                    for rel_fname in rel_fnames:
                        completions.append((rel_fname, -len(last_word), rel_fname))

        for ins, pos, match in sorted(completions):
            yield Completion(ins, start_position=pos, display=match)


class InputOutput:
    num_error_outputs = 0
    num_user_asks = 0
    clipboard_watcher = None
    bell_on_next_input = False
    notifications_command = None
    # Set by Permissions in bypass mode: every yes/no question is answered yes
    bypass_permissions = False
    # Show full diffs of the agent's edits, instead of the file and its line counts
    agent_diffs = False
    # UIs that show things themselves, like the web UI, set these to functions; the
    # terminal leaves them None. command_output is run_cmd's output callback, for the
    # output of /run and the like, and edit_document(text, path) lets the user edit a
    # /project document, returning the new text.
    command_output = None
    edit_document = None

    def __init__(
        self,
        pretty=True,
        yes=None,
        input_history_file=None,
        chat_history_file=None,
        input=None,
        output=None,
        user_input_color="blue",
        tool_output_color=None,
        tool_error_color="red",
        tool_warning_color="#FFA500",
        assistant_output_color="blue",
        completion_menu_color=None,
        completion_menu_bg_color=None,
        completion_menu_current_color=None,
        completion_menu_current_bg_color=None,
        code_theme="default",
        encoding="utf-8",
        line_endings="platform",
        dry_run=False,
        llm_history_file=None,
        editingmode=EditingMode.EMACS,
        fancy_input=True,
        file_watcher=None,
        multiline_mode=False,
        root=".",
        notifications=False,
        notifications_command=None,
    ):
        self.placeholder = None
        self.interrupted = False
        self.esc_listener = None
        self.never_prompts = set()
        self.editingmode = editingmode
        self.multiline_mode = multiline_mode
        self.bell_on_next_input = False
        self.notifications = notifications
        if notifications and notifications_command is None:
            self.notifications_command = self.get_default_notification_command()
        else:
            self.notifications_command = notifications_command

        no_color = os.environ.get("NO_COLOR")
        if no_color is not None and no_color != "":
            pretty = False

        self.user_input_color = ensure_hash_prefix(user_input_color) if pretty else None
        self.tool_output_color = ensure_hash_prefix(tool_output_color) if pretty else None
        self.tool_error_color = ensure_hash_prefix(tool_error_color) if pretty else None
        self.tool_warning_color = ensure_hash_prefix(tool_warning_color) if pretty else None
        self.assistant_output_color = ensure_hash_prefix(assistant_output_color)
        self.completion_menu_color = ensure_hash_prefix(completion_menu_color) if pretty else None
        self.completion_menu_bg_color = (
            ensure_hash_prefix(completion_menu_bg_color) if pretty else None
        )
        self.completion_menu_current_color = (
            ensure_hash_prefix(completion_menu_current_color) if pretty else None
        )
        self.completion_menu_current_bg_color = (
            ensure_hash_prefix(completion_menu_current_bg_color) if pretty else None
        )

        self.code_theme = code_theme

        self.input = input
        self.output = output

        self.pretty = pretty
        if self.output:
            self.pretty = False

        self.yes = yes

        self.input_history_file = input_history_file
        if self.input_history_file:
            try:
                Path(self.input_history_file).parent.mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError) as e:
                self.tool_warning(f"Could not create directory for input history: {e}")
                self.input_history_file = None
        self.llm_history_file = llm_history_file
        if chat_history_file is not None:
            self.chat_history_file = Path(chat_history_file)
        else:
            self.chat_history_file = None

        self.encoding = encoding
        valid_line_endings = {"platform", "lf", "crlf"}
        if line_endings not in valid_line_endings:
            raise ValueError(
                f"Invalid line_endings value: {line_endings}. "
                f"Must be one of: {', '.join(valid_line_endings)}"
            )
        self.newline = (
            None if line_endings == "platform" else "\n" if line_endings == "lf" else "\r\n"
        )
        self.dry_run = dry_run

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.append_chat_history(f"\n# loom chat started at {current_time}\n\n")

        self.prompt_session = None
        self.is_dumb_terminal = is_dumb_terminal()

        if self.is_dumb_terminal:
            self.pretty = False
            fancy_input = False

        if fancy_input:
            # Initialize PromptSession only if we have a capable terminal
            session_kwargs = {
                "input": self.input,
                "output": self.output,
                "lexer": PygmentsLexer(MarkdownLexer),
                "editing_mode": self.editingmode,
            }
            if self.editingmode == EditingMode.VI:
                session_kwargs["cursor"] = ModalCursorShapeConfig()
            if self.input_history_file is not None:
                session_kwargs["history"] = FileHistory(self.input_history_file)
            try:
                self.prompt_session = PromptSession(**session_kwargs)
                self.console = Console()  # pretty console
            except Exception as err:
                self.console = Console(force_terminal=False, no_color=True)
                self.tool_error(f"Can't initialize prompt toolkit: {err}")  # non-pretty
        else:
            self.console = Console(force_terminal=False, no_color=True)  # non-pretty
            if self.is_dumb_terminal:
                self.tool_output("Detected dumb terminal, disabling fancy input and pretty output.")

        self.file_watcher = file_watcher
        self.root = root

        # Validate color settings after console is initialized
        self._validate_color_settings()

    def _validate_color_settings(self):
        """Validate configured color strings and reset invalid ones."""
        color_attributes = [
            "user_input_color",
            "tool_output_color",
            "tool_error_color",
            "tool_warning_color",
            "assistant_output_color",
            "completion_menu_color",
            "completion_menu_bg_color",
            "completion_menu_current_color",
            "completion_menu_current_bg_color",
        ]
        for attr_name in color_attributes:
            color_value = getattr(self, attr_name, None)
            if color_value:
                try:
                    # Try creating a style to validate the color
                    RichStyle(color=color_value)
                except ColorParseError as e:
                    self.console.print(
                        "[bold red]Warning:[/bold red] Invalid configuration for"
                        f" {attr_name}: '{color_value}'. {e}. Disabling this color."
                    )
                    setattr(self, attr_name, None)  # Reset invalid color to None

    def _get_style(self):
        style_dict = {}
        if not self.pretty:
            return Style.from_dict(style_dict)

        if self.user_input_color:
            style_dict.setdefault("", self.user_input_color)
            style_dict.update(
                {
                    "pygments.literal.string": f"bold italic {self.user_input_color}",
                }
            )

        # Conditionally add 'completion-menu' style
        completion_menu_style = []
        if self.completion_menu_bg_color:
            completion_menu_style.append(f"bg:{self.completion_menu_bg_color}")
        if self.completion_menu_color:
            completion_menu_style.append(self.completion_menu_color)
        if completion_menu_style:
            style_dict["completion-menu"] = " ".join(completion_menu_style)

        # Conditionally add 'completion-menu.completion.current' style
        completion_menu_current_style = []
        if self.completion_menu_current_bg_color:
            completion_menu_current_style.append(self.completion_menu_current_bg_color)
        if self.completion_menu_current_color:
            completion_menu_current_style.append(f"bg:{self.completion_menu_current_color}")
        if completion_menu_current_style:
            style_dict["completion-menu.completion.current"] = " ".join(
                completion_menu_current_style
            )

        return Style.from_dict(style_dict)

    def read_image(self, filename):
        try:
            with open(str(filename), "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read())
                return encoded_string.decode("utf-8")
        except OSError as err:
            self.tool_error(f"{filename}: unable to read: {err}")
            return
        except FileNotFoundError:
            self.tool_error(f"{filename}: file not found error")
            return
        except IsADirectoryError:
            self.tool_error(f"{filename}: is a directory")
            return
        except Exception as e:
            self.tool_error(f"{filename}: {e}")
            return

    def read_text(self, filename, silent=False):
        if is_image_file(filename):
            return self.read_image(filename)

        try:
            with open(str(filename), "r", encoding=self.encoding) as f:
                return f.read()
        except FileNotFoundError:
            if not silent:
                self.tool_error(f"{filename}: file not found error")
            return
        except IsADirectoryError:
            if not silent:
                self.tool_error(f"{filename}: is a directory")
            return
        except OSError as err:
            if not silent:
                self.tool_error(f"{filename}: unable to read: {err}")
            return
        except UnicodeError as e:
            if not silent:
                self.tool_error(f"{filename}: {e}")
                self.tool_error("Use --encoding to set the unicode encoding.")
            return

    def write_text(self, filename, content, max_retries=5, initial_delay=0.1):
        """
        Writes content to a file, retrying with progressive backoff if the file is locked.

        Writes atomically: content is encoded first, then written to a temporary file
        next to the destination, then renamed into place. So a mis-encoded string (like a
        lone surrogate from the model) raises before the destination is touched, and a
        crash mid-write leaves the destination unchanged.

        :param filename: Path to the file to write.
        :param content: Content to write to the file.
        :param max_retries: Maximum number of retries if a file lock is encountered.
        :param initial_delay: Initial delay (in seconds) before the first retry.
        """
        if self.dry_run:
            return

        # Normalize line endings up front, matching what open() with self.newline would do:
        # self.newline is "" (keep what\'s there), None (write platform native), or an explicit
        # terminator like "\n", "\r\n".
        if self.newline not in ("", None):
            content = content.replace("\r\n", "\n").replace("\n", self.newline)
        elif self.newline is None and os.linesep != "\n":
            content = content.replace("\r\n", "\n").replace("\n", os.linesep)

        try:
            data = content.encode(self.encoding, errors="strict")
        except UnicodeEncodeError as err:
            self.tool_error(
                f"Unable to write {filename}: can't encode as {self.encoding} ({err})."
                " The file was not touched."
            )
            raise

        filename = str(filename)
        dirname = os.path.dirname(filename) or "."
        delay = initial_delay
        for attempt in range(max_retries):
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(
                    prefix="." + os.path.basename(filename) + ".", suffix=".tmp", dir=dirname
                )
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                try:
                    os.replace(tmp, filename)
                except PermissionError:
                    # Windows can't replace a file another program has open, but can
                    # write into it, as loom did before writing atomically
                    if os.name != "nt" or not os.path.isfile(filename):
                        raise
                    with open(filename, "wb") as f:
                        f.write(data)
                    os.unlink(tmp)
                return
            except PermissionError as err:
                if tmp and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    self.tool_error(
                        f"Unable to write file {filename} after {max_retries} attempts: {err}"
                    )
                    raise
            except OSError as err:
                if tmp and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                self.tool_error(f"Unable to write file {filename}: {err}")
                raise

    def rule(self):
        if self.pretty:
            style = dict(style=self.user_input_color) if self.user_input_color else dict()
            self.console.rule(**style)
        else:
            print()

    def interrupt_input(self):
        if self.prompt_session and self.prompt_session.app:
            # Store any partial input before interrupting
            self.placeholder = self.prompt_session.app.current_buffer.text
            self.interrupted = True
            self.prompt_session.app.exit()

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
        """Read the user's next message. cycle_mode, if given, is called on Shift-Tab to switch
        modes and returns the new edit_format label for the prompt."""
        self.rule()

        # Ring the bell if needed
        self.ring_bell()

        rel_fnames = list(rel_fnames)
        show = ""
        if rel_fnames:
            rel_read_only_fnames = [
                get_rel_fname(fname, root) for fname in (abs_read_only_fnames or [])
            ]
            show = self.format_files_for_input(rel_fnames, rel_read_only_fnames)

        files_show = show
        self.prompt_prefix = self.get_prompt_prefix(edit_format)
        show = files_show + self.prompt_prefix

        inp = ""
        multiline_input = False

        style = self._get_style()

        completer_instance = ThreadedCompleter(
            AutoCompleter(
                root,
                rel_fnames,
                addable_rel_fnames,
                commands,
                self.encoding,
                abs_read_only_fnames=abs_read_only_fnames,
            )
        )

        def suspend_to_bg(event):
            """Suspend currently running application."""
            event.app.suspend_to_background()

        kb = KeyBindings()

        @kb.add(Keys.ControlZ, filter=Condition(lambda: hasattr(signal, "SIGTSTP")))
        def _(event):
            "Suspend to background with ctrl-z"
            suspend_to_bg(event)

        @kb.add("c-space")
        def _(event):
            "Ignore Ctrl when pressing space bar"
            event.current_buffer.insert_text(" ")

        @kb.add("c-up")
        def _(event):
            "Navigate backward through history"
            event.current_buffer.history_backward()

        @kb.add("c-down")
        def _(event):
            "Navigate forward through history"
            event.current_buffer.history_forward()

        @kb.add("c-x", "c-e")
        def _(event):
            "Edit current input in external editor (like Bash)"
            buffer = event.current_buffer
            current_text = buffer.text

            # Open the editor with the current text
            edited_text = pipe_editor(input_data=current_text, suffix="md")

            # Replace the buffer with the edited text, strip any trailing newlines
            buffer.text = edited_text.rstrip("\n")

            # Move cursor to the end of the text
            buffer.cursor_position = len(buffer.text)

        @kb.add("enter", eager=True, filter=~is_searching)
        def _(event):
            "Handle Enter key press"
            if self.multiline_mode and not (
                self.editingmode == EditingMode.VI
                and event.app.vi_state.input_mode == InputMode.NAVIGATION
            ):
                # In multiline mode and if not in vi-mode or vi navigation/normal mode,
                # Enter adds a newline
                event.current_buffer.insert_text("\n")
            else:
                # In normal mode, Enter submits
                event.current_buffer.validate_and_handle()

        if cycle_mode:

            @kb.add("s-tab", filter=~has_completions)
            def _(event):
                "Cycle the agent's permission mode with Shift-Tab"
                nonlocal show
                self.prompt_prefix = self.get_prompt_prefix(cycle_mode())
                show = ("" if multiline_input else files_show) + self.prompt_prefix
                event.app.invalidate()

        empty_emacs_prompt = Condition(
            lambda: self.editingmode == EditingMode.EMACS
            and not get_app().current_buffer.text.strip()
        )

        @kb.add("escape", "escape", filter=empty_emacs_prompt & ~has_completions & ~is_searching)
        def _(event):
            "Esc Esc at an empty prompt opens /rewind, like Claude Code"
            event.current_buffer.text = "/rewind"
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter", eager=True, filter=~is_searching)  # This is Alt+Enter
        def _(event):
            "Handle Alt+Enter key press"
            if self.multiline_mode:
                # In multiline mode, Alt+Enter submits
                event.current_buffer.validate_and_handle()
            else:
                # In normal mode, Alt+Enter adds a newline
                event.current_buffer.insert_text("\n")

        while True:
            if multiline_input:
                show = self.prompt_prefix

            try:
                if self.prompt_session:
                    # Use placeholder if set, then clear it
                    default = self.placeholder or ""
                    self.placeholder = None

                    self.interrupted = False
                    if not multiline_input:
                        if self.file_watcher:
                            self.file_watcher.start()
                        if self.clipboard_watcher:
                            self.clipboard_watcher.start()

                    def get_continuation(width, line_number, is_soft_wrap):
                        return self.prompt_prefix

                    # A callable, so Shift-Tab can change the prompt while it's shown
                    line = self.prompt_session.prompt(
                        lambda: show,
                        default=default,
                        completer=completer_instance,
                        reserve_space_for_menu=4,
                        complete_style=CompleteStyle.MULTI_COLUMN,
                        style=style,
                        key_bindings=kb,
                        complete_while_typing=True,
                        prompt_continuation=get_continuation,
                    )
                else:
                    line = input(show)

                # Check if we were interrupted by a file change
                if self.interrupted:
                    line = line or ""
                    if self.file_watcher:
                        cmd = self.file_watcher.process_changes()
                        return cmd

            except EOFError:
                raise
            except Exception as err:
                import traceback

                self.tool_error(str(err))
                self.tool_error(traceback.format_exc())
                return ""
            except UnicodeEncodeError as err:
                self.tool_error(str(err))
                return ""
            finally:
                if self.file_watcher:
                    self.file_watcher.stop()
                if self.clipboard_watcher:
                    self.clipboard_watcher.stop()

            if line.strip("\r\n") and not multiline_input:
                stripped = line.strip("\r\n")
                if stripped == "{":
                    multiline_input = True
                    multiline_tag = None
                    inp += ""
                elif stripped[0] == "{":
                    # Extract tag if it exists (only alphanumeric chars)
                    tag = "".join(c for c in stripped[1:] if c.isalnum())
                    if stripped == "{" + tag:
                        multiline_input = True
                        multiline_tag = tag
                        inp += ""
                    else:
                        inp = line
                        break
                else:
                    inp = line
                    break
                continue
            elif multiline_input and line.strip():
                if multiline_tag:
                    # Check if line is exactly "tag}"
                    if line.strip("\r\n") == f"{multiline_tag}}}":
                        break
                    else:
                        inp += line + "\n"
                # Check if line is exactly "}"
                elif line.strip("\r\n") == "}":
                    break
                else:
                    inp += line + "\n"
            elif multiline_input:
                inp += line + "\n"
            else:
                inp = line
                break

        print()
        self.user_input(inp)
        return inp

    def get_prompt_prefix(self, edit_format):
        prefix = edit_format or ""
        if self.multiline_mode:
            prefix += (" " if edit_format else "") + "multi"
        return prefix + "> "

    def add_to_input_history(self, inp):
        if not self.input_history_file:
            return
        try:
            FileHistory(self.input_history_file).append_string(inp)
            # Also add to the in-memory history if it exists
            if self.prompt_session and self.prompt_session.history:
                self.prompt_session.history.append_string(inp)
        except OSError as err:
            self.tool_warning(f"Unable to write to input history file: {err}")

    def get_input_history(self):
        if not self.input_history_file:
            return []

        fh = FileHistory(self.input_history_file)
        return fh.load_history_strings()

    def log_llm_history(self, role, content):
        if not self.llm_history_file:
            return
        timestamp = datetime.now().isoformat(timespec="seconds")
        try:
            Path(self.llm_history_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.llm_history_file, "a", encoding="utf-8") as log_file:
                log_file.write(f"{role.upper()} {timestamp}\n")
                log_file.write(content + "\n")
        except (PermissionError, OSError) as err:
            self.tool_warning(f"Unable to write to llm history file {self.llm_history_file}: {err}")
            self.llm_history_file = None

    def display_user_input(self, inp):
        if self.pretty and self.user_input_color:
            style = dict(style=self.user_input_color)
        else:
            style = dict()

        self.console.print(Text(inp), **style)

    def user_input(self, inp, log_only=True):
        if not log_only:
            self.display_user_input(inp)

        prefix = "####"
        if inp:
            hist = inp.splitlines()
        else:
            hist = ["<blank>"]

        hist = f"  \n{prefix} ".join(hist)

        hist = f"""
{prefix} {hist}"""
        self.append_chat_history(hist, linebreak=True)

    # OUTPUT

    def ai_output(self, content):
        hist = "\n" + content.strip() + "\n\n"
        self.append_chat_history(hist)

    def offer_url(self, url, prompt="Open URL for more info?", allow_never=True):
        """Offer to open a URL in the browser, returns True if opened."""
        if url in self.never_prompts:
            return False
        if self.confirm_ask(prompt, subject=url, allow_never=allow_never):
            webbrowser.open(url)
            return True
        return False

    @pause_esc
    @restore_multiline
    def confirm_ask(
        self,
        question,
        default="y",
        subject=None,
        explicit_yes_required=False,
        group=None,
        allow_never=False,
    ):
        self.num_user_asks += 1

        # Ring the bell if needed
        self.ring_bell()

        question_id = (question, subject)

        if question_id in self.never_prompts:
            return False
        if self.bypass_permissions:
            self.append_chat_history(
                f"{question.strip()} y (bypass)", linebreak=True, blockquote=True
            )
            return True
        question = sanitize_for_display(question, show_escapes=True)
        if subject:
            subject = sanitize_for_display(subject, show_escapes=True)

        if group and not group.show_group:
            group = None
        if group:
            allow_never = True

        valid_responses = ["yes", "no", "skip", "all"]
        options = " (Y)es/(N)o"
        if group:
            if not explicit_yes_required:
                options += "/(A)ll"
            options += "/(S)kip all"
        if allow_never:
            options += "/(D)on't ask again"
            valid_responses.append("don't")

        if default.lower().startswith("y"):
            question += options + " [Yes]: "
        elif default.lower().startswith("n"):
            question += options + " [No]: "
        else:
            question += options + f" [{default}]: "

        if subject:
            self.tool_output()
            if "\n" in subject:
                lines = subject.splitlines()
                max_length = max(len(line) for line in lines)
                padded_lines = [line.ljust(max_length) for line in lines]
                padded_subject = "\n".join(padded_lines)
                self.tool_output(padded_subject, bold=True)
            else:
                self.tool_output(subject, bold=True)

        style = self._get_style()

        def is_valid_response(text):
            if not text:
                return True
            return text.lower() in valid_responses

        if self.yes is True:
            res = "n" if explicit_yes_required else "y"
        elif self.yes is False:
            res = "n"
        elif group and group.preference:
            res = group.preference
            self.user_input(f"{question}{res}", log_only=False)
        else:
            while True:
                try:
                    if self.prompt_session:
                        res = self.prompt_session.prompt(
                            question,
                            style=style,
                            complete_while_typing=False,
                        )
                    else:
                        res = input(question)
                except EOFError:
                    # Treat EOF (Ctrl+D) as if the user pressed Enter
                    res = default
                    break

                if not res:
                    res = default
                    break
                res = res.lower()
                good = any(valid_response.startswith(res) for valid_response in valid_responses)
                if good:
                    break

                error_message = f"Please answer with one of: {', '.join(valid_responses)}"
                self.tool_error(error_message)

        res = res.lower()[0]

        if res == "d" and allow_never:
            self.never_prompts.add(question_id)
            hist = f"{question.strip()} {res}"
            self.append_chat_history(hist, linebreak=True, blockquote=True)
            return False

        if explicit_yes_required:
            is_yes = res == "y"
        else:
            is_yes = res in ("y", "a")

        is_all = res == "a" and group is not None and not explicit_yes_required
        is_skip = res == "s" and group is not None

        if group:
            if is_all and not explicit_yes_required:
                group.preference = "all"
            elif is_skip:
                group.preference = "skip"

        hist = f"{question.strip()} {res}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)

        return is_yes

    @pause_esc
    @restore_multiline
    def permission_ask(
        self, question, subject=None, always=None, explicit_yes_required=False, bypass=None
    ):
        """Ask the user to approve an agent action. Returns "yes", "no", "always" or "bypass".

        always labels the (A)lways option and bypass the (B)ypass permissions option; each is
        only offered when it's given. With --yes-always the answer is "yes", unless
        explicit_yes_required. In bypass mode it's "yes" without asking.
        """
        if self.bypass_permissions:
            self.append_chat_history(
                f"{question.strip()} yes (bypass)", linebreak=True, blockquote=True
            )
            return "yes"
        self.num_user_asks += 1
        self.ring_bell()
        # What the user approves has to be shown as it is
        question = sanitize_for_display(question, show_escapes=True)
        if subject:
            subject = sanitize_for_display(subject, show_escapes=True)

        choices = ["yes", "no"]
        options = " (Y)es/(N)o"
        if always:
            choices.append("always")
            options += f"/(A)lways: {always}"
        if bypass:
            choices.append("bypass")
            options += f"/(B)ypass permissions: {bypass}"
        question += options + " [Yes]: "

        if subject:
            if subject.startswith("--- "):
                self.diff_output(subject, indent="     ")
            else:
                self.tool_output(subject, bold=True)

        if self.yes is True:
            res = "no" if explicit_yes_required else "yes"
        elif self.yes is False:
            res = "no"
        else:
            style = self._get_style()
            while True:
                try:
                    if self.prompt_session:
                        res = self.prompt_session.prompt(
                            question, style=style, complete_while_typing=False
                        )
                    else:
                        res = input(question)
                except EOFError:
                    res = "no"
                    break
                res = res.strip().lower() or "yes"
                matches = [choice for choice in choices if choice.startswith(res)]
                if matches:
                    res = matches[0]
                    break
                self.tool_error(f"Please answer with one of: {', '.join(choices)}")

        hist = f"{question.strip()} {res}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)
        if self.yes in (True, False):
            self.tool_output(hist)
        return res

    @pause_esc
    @restore_multiline
    def choice_ask(
        self,
        question,
        choices,
        default=None,
        yes_choice=None,
        no_choice=None,
        checkpoint=None,
        plan=None,
    ):
        """Ask the user to pick one of choices, like ["approve", "edit", "reject"], whose
        first letters must differ. Any prefix of a choice picks it, and Enter picks default
        (the first choice if None). With --yes-always the answer is yes_choice (default if
        None), and with --no it's no_choice (the last choice if None).

        A choice can mark another letter as its key, like "c(o)de only", for choices that
        start the same; the answer is the choice without the parentheses.

        checkpoint describes the project phase being reviewed, when the question is a
        /project checkpoint, for UIs that show those differently. plan is {text, path} when
        the question is whether to approve the agent's plan, which is shown first."""
        keys = choice_keys(choices)
        choices = list(keys)
        default = default or choices[0]
        self.num_user_asks += 1
        if plan:
            self.print_plan(plan["text"], plan.get("path"))
        self.ring_bell()
        question = sanitize_for_display(question, show_escapes=True)
        options = "/".join(shown for _key, shown in keys.values())
        question += f" {options} [{default.capitalize()}]: "

        if self.yes is True:
            res = yes_choice or default
        elif self.yes is False:
            res = no_choice or choices[-1]
        else:
            style = self._get_style()
            while True:
                try:
                    if self.prompt_session:
                        res = self.prompt_session.prompt(
                            question, style=style, complete_while_typing=False
                        )
                    else:
                        res = input(question)
                except EOFError:
                    res = no_choice or choices[-1]
                    break
                res = res.strip().lower() or default
                matches = [choice for choice, (key, _) in keys.items() if res == key]
                matches = matches or [choice for choice in choices if choice.startswith(res)]
                if matches:
                    res = matches[0]
                    break
                self.tool_error(f"Please answer with one of: {', '.join(choices)}")

        hist = f"{question.strip()} {res}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)
        if self.yes in (True, False):
            self.tool_output(hist)
        return res

    def print_plan(self, text, path=None):
        """Show a plan the agent presents, as rendered markdown in a box."""
        text = sanitize_for_display(text)
        for line in text.splitlines():
            self.append_chat_history(line, linebreak=True, blockquote=True, strip=False)
        title = f"Plan · {path}" if path else "Plan"
        body = Markdown(text, code_theme=self.code_theme) if self.pretty else Text(text)
        panel = Panel(
            body,
            title=title,
            title_align="left",
            border_style="cyan" if self.pretty else "none",
            padding=(0, 1),
        )
        try:
            self.console.print(panel)
        except UnicodeEncodeError:
            self.console.print(text.encode("ascii", errors="replace").decode("ascii"))

    def plan_output(self, text, path=None):
        """Show a plan the agent presents without asking about it, like when it's approved
        automatically or can't be approved here."""
        self.print_plan(text, path)

    def plan_feedback_ask(self):
        """After "keep planning": what the user wants changed in the plan, or ""."""
        return self.prompt_ask("What should change in the plan? (Enter to stop and say it later):")

    def permission_mode_changed(self, mode):
        """The agent's permission mode changed, like when a plan is approved. The terminal
        shows the mode at the next prompt."""

    def checkpoints_changed(self, coder):
        """coder's session has new or fewer checkpoints, for UIs that list them."""

    def conversation_rewound(self, coder):
        """/rewind cut coder's conversation back, for UIs that show the conversation."""

    def diff_output(self, diff, indent=""):
        """Show a unified diff with line numbers, removed lines in red and added lines in
        green."""
        diff = sanitize_for_display(diff, show_escapes=True)
        for line in diff.splitlines():
            self.append_chat_history(line, linebreak=True, blockquote=True, strip=False)

        lines = numbered_diff_lines(diff)
        width = max((len(str(line[0])) for line in lines if line), default=1)
        text = Text()
        for line in lines:
            if line is None:
                text.append(f"{indent}{'⋮':>{width}}\n", style="dim" if self.pretty else None)
                continue
            num, marker, content = line
            style = None
            if self.pretty:
                style = {"-": "red", "+": "green", "": "dim"}.get(marker)
            text.append(f"{indent}{num:>{width}} {marker} {content}".rstrip() + "\n", style=style)
        self._print_text(text, end="")

    # Agent tool calls

    @contextmanager
    def esc_interrupts(self):
        """While the block runs, pressing Esc interrupts it like ^C, when loom runs in an
        interactive terminal. Text typed meanwhile becomes the start of the next prompt."""
        if self.esc_listener or not self.prompt_session or self.input is not None:
            yield
            return
        if not EscListener.supported():
            yield
            return

        listener = EscListener()
        listener.start()
        self.esc_listener = listener
        try:
            yield
        finally:
            self.esc_listener = None
            listener.stop()
            typed = listener.take_typed().strip("\n")
            if typed.strip():
                self.placeholder = (self.placeholder or "") + typed

    def consume_esc(self):
        """Whether the KeyboardInterrupt being handled came from pressing Esc."""
        return bool(self.esc_listener and self.esc_listener.consume_escape())

    def _print_text(self, text, **kwargs):
        try:
            self.console.print(text, **kwargs)
        except UnicodeEncodeError:
            plain = text.plain if isinstance(text, Text) else str(text)
            plain = plain.replace("●", "*").replace("⎿", "|").replace("⋮", ":")
            self.console.print(plain.encode("ascii", errors="replace").decode("ascii"), **kwargs)

    def tool_call(self, name, detail="", args=None):
        """Show one line for a tool the agent is using, like: ● Read(loom/io.py)

        args are the call's arguments as the model sent them, for UIs that show them."""
        name, detail = tool_call_parts(name, detail, self.console.width - 4)
        shown = f"{name}({detail})" if detail else name
        self.append_chat_history(shown, linebreak=True, blockquote=True)

        text = Text()
        text.append("● ", style="green" if self.pretty else None)
        text.append(name, style="bold" if self.pretty else None)
        if detail:
            text.append(f"({detail})")
        self._print_text(text)

    def tool_result(self, lines, error=False, styles=None):
        """Show a tool's outcome, indented under its call:
          ⎿  Read 120 lines
        styles optionally gives a rich style for each line."""
        if isinstance(lines, str):
            lines = lines.splitlines() or [""]
        lines = [part for line in lines for part in sanitize_for_display(line).split("\n") or [""]]
        for line in lines:
            self.append_chat_history(line, linebreak=True, blockquote=True)

        default = None
        if self.pretty:
            default = self.tool_error_color if error else "dim"
        text = Text()
        for num, line in enumerate(lines):
            prefix = "  ⎿  " if num == 0 else "     "
            style = default
            if styles and num < len(styles) and styles[num] and self.pretty:
                style = styles[num]
            text.append(prefix, style="dim" if self.pretty else None)
            text.append(line + "\n", style=style)
        # One screen line per result line
        self._print_text(text, end="", no_wrap=True, overflow="ellipsis")

    def tool_done(self, result, error=False):
        """The agent's tool call finished, and result is what the model gets back. The
        terminal has already shown it compactly, with tool_result."""

    def usage_output(self, report, sent=0, received=0, cost=0.0):
        """Show the tokens and cost of a request: report says them, and sent, received and
        cost are the numbers, for UIs that add them up."""
        self.tool_output(report)

    def todo_output(self, todos):
        """Show the agent's to-do list as a checklist."""
        lines = []
        styles = []
        for todo in todos:
            status = todo.get("status")
            if status == "completed":
                lines.append(f"☒ {todo['content']}")
                styles.append("dim strike")
            elif status == "in_progress":
                lines.append(f"◼ {todo['content']}")
                styles.append("bold")
            else:
                lines.append(f"☐ {todo['content']}")
                styles.append("")
        if not lines:
            lines = ["(empty)"]
            styles = ["dim"]
        self.tool_result(lines, styles=styles)

    @pause_esc
    @restore_multiline
    def prompt_ask(self, question, default="", subject=None):
        self.num_user_asks += 1
        question = sanitize_for_display(question, show_escapes=True)
        if subject:
            subject = sanitize_for_display(subject, show_escapes=True)

        # Ring the bell if needed
        self.ring_bell()

        if subject:
            self.tool_output()
            self.tool_output(subject, bold=True)

        style = self._get_style()

        if self.yes is True:
            res = "yes"
        elif self.yes is False:
            res = "no"
        else:
            try:
                if self.prompt_session:
                    res = self.prompt_session.prompt(
                        question + " ",
                        default=default,
                        style=style,
                        complete_while_typing=True,
                    )
                else:
                    res = input(question + " ")
            except EOFError:
                # Treat EOF (Ctrl+D) as if the user pressed Enter
                res = default

        hist = f"{question.strip()} {res.strip()}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)
        if self.yes in (True, False):
            self.tool_output(hist)

        return res

    def _tool_message(self, message="", strip=True, color=None):
        if not isinstance(message, Text):
            message = sanitize_for_display(message)
        if message.strip():
            if "\n" in message:
                for line in message.splitlines():
                    self.append_chat_history(line, linebreak=True, blockquote=True, strip=strip)
            else:
                hist = message.strip() if strip else message
                self.append_chat_history(hist, linebreak=True, blockquote=True)

        if not isinstance(message, Text):
            message = Text(message)
        color = ensure_hash_prefix(color) if color else None
        style = dict(style=color) if self.pretty and color else dict()
        try:
            self.console.print(message, **style)
        except UnicodeEncodeError:
            # Fallback to ASCII-safe output
            if isinstance(message, Text):
                message = message.plain
            message = str(message).encode("ascii", errors="replace").decode("ascii")
            self.console.print(message, **style)

    def tool_error(self, message="", strip=True):
        self.num_error_outputs += 1
        self._tool_message(message, strip, self.tool_error_color)

    def tool_warning(self, message="", strip=True):
        self._tool_message(message, strip, self.tool_warning_color)

    def tool_output(self, *messages, log_only=False, bold=False):
        messages = [sanitize_for_display(message) for message in messages]
        if messages:
            hist = " ".join(messages)
            hist = f"{hist.strip()}"
            self.append_chat_history(hist, linebreak=True, blockquote=True)

        if log_only:
            return

        messages = list(map(Text, messages))
        style = dict()
        if self.pretty:
            if self.tool_output_color:
                style["color"] = ensure_hash_prefix(self.tool_output_color)
            style["reverse"] = bold

        style = RichStyle(**style)
        self.console.print(*messages, style=style)

    def get_assistant_mdstream(self):
        mdargs = dict(
            style=self.assistant_output_color,
            code_theme=self.code_theme,
            inline_code_lexer="text",
        )
        mdStream = MarkdownStream(mdargs=mdargs)
        return mdStream

    def assistant_output(self, message, pretty=None):
        if not message:
            self.tool_warning("Empty response received from LLM. Check your provider account?")
            return

        message = sanitize_for_display(message)
        show_resp = message

        # Coder will force pretty off if fence is not triple-backticks
        if pretty is None:
            pretty = self.pretty

        if pretty:
            show_resp = Markdown(
                message, style=self.assistant_output_color, code_theme=self.code_theme
            )
        else:
            show_resp = Text(message or "(empty response)")

        self.console.print(show_resp)

    def set_placeholder(self, placeholder):
        """Set a one-time placeholder text for the next input prompt."""
        self.placeholder = placeholder

    def print(self, message=""):
        print(message)

    def llm_started(self):
        """Mark that the LLM has started processing, so we should ring the bell on next input"""
        self.bell_on_next_input = True

    def get_default_notification_command(self):
        """Return a default notification command based on the operating system."""
        import platform

        system = platform.system()

        if system == "Darwin":  # macOS
            # Check for terminal-notifier first
            if shutil.which("terminal-notifier"):
                return f"terminal-notifier -title 'Loom' -message '{NOTIFICATION_MESSAGE}'"
            # Fall back to osascript
            return (
                f'osascript -e \'display notification "{NOTIFICATION_MESSAGE}" with title "Loom"\''
            )
        elif system == "Linux":
            # Check for common Linux notification tools
            for cmd in ["notify-send", "zenity"]:
                if shutil.which(cmd):
                    if cmd == "notify-send":
                        return f"notify-send 'Loom' '{NOTIFICATION_MESSAGE}'"
                    elif cmd == "zenity":
                        return f"zenity --notification --text='{NOTIFICATION_MESSAGE}'"
            return None  # No known notification tool found
        elif system == "Windows":
            # PowerShell notification
            return (
                "powershell -command"
                " \"[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
                f" [System.Windows.Forms.MessageBox]::Show('{NOTIFICATION_MESSAGE}',"
                " 'Loom')\""
            )

        return None  # Unknown system

    def ring_bell(self):
        """Ring the terminal bell if needed and clear the flag"""
        if self.bell_on_next_input and self.notifications:
            if self.notifications_command:
                try:
                    result = subprocess.run(
                        self.notifications_command, shell=True, capture_output=True
                    )
                    if result.returncode != 0 and result.stderr:
                        error_msg = result.stderr.decode("utf-8", errors="replace")
                        self.tool_warning(f"Failed to run notifications command: {error_msg}")
                except Exception as e:
                    self.tool_warning(f"Failed to run notifications command: {e}")
            else:
                print("\a", end="", flush=True)  # Ring the bell
            self.bell_on_next_input = False  # Clear the flag

    def toggle_multiline_mode(self):
        """Toggle between normal and multiline input modes"""
        self.multiline_mode = not self.multiline_mode
        if self.multiline_mode:
            self.tool_output(
                "Multiline mode: Enabled. Enter inserts newline, Alt-Enter submits text"
            )
        else:
            self.tool_output(
                "Multiline mode: Disabled. Alt-Enter inserts newline, Enter submits text"
            )

    def append_chat_history(self, text, linebreak=False, blockquote=False, strip=True):
        if blockquote:
            if strip:
                text = text.strip()
            text = "> " + text
        if linebreak:
            if strip:
                text = text.rstrip()
            text = text + "  \n"
        if not text.endswith("\n"):
            text += "\n"
        if self.chat_history_file is not None:
            try:
                self.chat_history_file.parent.mkdir(parents=True, exist_ok=True)
                with self.chat_history_file.open("a", encoding=self.encoding, errors="ignore") as f:
                    f.write(text)
            except (PermissionError, OSError) as err:
                print(f"Warning: Unable to write to chat history file {self.chat_history_file}.")
                print(err)
                self.chat_history_file = None  # Disable further attempts to write

    def format_files_for_input(self, rel_fnames, rel_read_only_fnames):
        if not self.pretty:
            read_only_files = []
            for full_path in sorted(rel_read_only_fnames or []):
                read_only_files.append(f"{full_path} (read only)")

            editable_files = []
            for full_path in sorted(rel_fnames):
                if full_path in rel_read_only_fnames:
                    continue
                editable_files.append(f"{full_path}")

            return "\n".join(read_only_files + editable_files) + "\n"

        output = StringIO()
        console = Console(file=output, force_terminal=False)

        read_only_files = sorted(rel_read_only_fnames or [])
        editable_files = [f for f in sorted(rel_fnames) if f not in rel_read_only_fnames]

        if read_only_files:
            # Use shorter of abs/rel paths for readonly files
            ro_paths = []
            for rel_path in read_only_files:
                abs_path = os.path.abspath(os.path.join(self.root, rel_path))
                ro_paths.append(Text(abs_path if len(abs_path) < len(rel_path) else rel_path))

            files_with_label = [Text("Readonly:")] + ro_paths
            read_only_output = StringIO()
            Console(file=read_only_output, force_terminal=False).print(Columns(files_with_label))
            read_only_lines = read_only_output.getvalue().splitlines()
            console.print(Columns(files_with_label))

        if editable_files:
            text_editable_files = [Text(f) for f in editable_files]
            files_with_label = text_editable_files
            if read_only_files:
                files_with_label = [Text("Editable:")] + text_editable_files
                editable_output = StringIO()
                Console(file=editable_output, force_terminal=False).print(Columns(files_with_label))
                editable_lines = editable_output.getvalue().splitlines()

                if len(read_only_lines) > 1 or len(editable_lines) > 1:
                    console.print()
            console.print(Columns(files_with_label))

        return output.getvalue()


def tool_call_parts(name, detail, width):
    """A tool call's name and detail, safe to show and fitting in width columns: the
    detail on one line (the first of a multi-line command, whitespace collapsed), cut with
    … if it's too long."""
    name = sanitize_for_display(name, show_escapes=True)
    detail = sanitize_for_display(detail or "", show_escapes=True)
    if detail:
        first, _, rest = detail.strip().partition("\n")
        detail = " ".join(first.split()) + (" …" if rest.strip() else "")
    width = max(20, width - len(name))
    if len(detail) > width:
        detail = detail[: width - 1] + "…"
    return name, detail


def choice_keys(choices):
    """{choice: (key, how it's shown)} for choice_ask's choices, in order: a choice's key
    is its first letter, or the letter it puts in parentheses, like c(o)de only, which
    shows as c(O)de only. The choices lose their parentheses."""
    res = {}
    for choice in choices:
        match = re.search(r"\((\w)\)", choice)
        if match:
            letter = match.group(1)
            plain = choice[: match.start()] + letter + choice[match.end() :]
            shown = choice[: match.start()] + f"({letter.upper()})" + choice[match.end() :]
            res[plain] = (letter.lower(), shown)
        else:
            res[choice] = (choice[:1].lower(), f"({choice[:1].upper()}){choice[1:]}")
    return res


def get_rel_fname(fname, root):
    try:
        return os.path.relpath(fname, root)
    except ValueError:
        return fname

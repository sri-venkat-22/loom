"""WebIO: the InputOutput `loom --web` gives the Coder, so it talks to the browser.

Output still goes to the terminal and the chat history file as usual, and is also sent to
the browser. Input and questions come from the browser once the server is listening;
before that, like for startup questions, they're asked in the terminal.
"""

import copy
import json
import re
from contextlib import contextmanager

from rich.text import Text

from loom import __version__
from loom.display import sanitize_for_display
from loom.io import HUNK_RE, InputOutput, choice_keys
from loom.permissions import MODES
from loom.phases import PHASES, get_phase
from loom.reasoning_tags import REASONING_END, REASONING_START
from loom.sessions import get_title

from .project import ProjectWatcher, project_state
from .protocol import MAX_TOOL_OUTPUT
from .session import WebSession
from .transcript import transcript


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
        value = self.io.web.ask(
            ask["kind"],
            ask["question"],
            ask["choices"],
            default,
            subject=ask.get("subject"),
            tool_id=self.io.tool_id,
            checkpoint=ask.get("checkpoint"),
            plan=ask.get("plan"),
        )
        if ask["kind"] == "plan" and value:
            # "keep planning" can carry the user's feedback on the lines after it
            value, _, feedback = value.partition("\n")
            self.io.plan_feedback = feedback.strip()
        return value


def plain(message):
    if isinstance(message, Text):
        return message.plain
    return str(message)


def parse_diff(diff):
    """The file a unified diff changes, and its lines as dicts of kind (add, del, ctx, gap
    for unchanged lines it leaves out, or note), old and new line numbers, and text. A
    gap's text says how many lines it hides, like "26 unmodified lines"."""
    file = ""
    lines = []
    old = new = 0
    in_hunk = False
    for line in diff.splitlines():
        match = HUNK_RE.match(line)
        if match:
            start = int(match.group(1))
            # The unchanged lines before the hunk; at the start, only if there are some
            hidden = start - (old if in_hunk else 1)
            if in_hunk or hidden > 0:
                text = f"{hidden} unmodified line{'' if hidden == 1 else 's'}" if hidden else ""
                lines.append(dict(kind="gap", old=None, new=None, text=text))
            in_hunk = True
            old, new = start, int(match.group(2))
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
        # Follows the /project orchestrator's progress, once the browser is talking to loom
        self.project_watcher = None
        # The tokens and cost of every request this session, the phase agents' included
        self.usage = dict(sent=0, received=0)
        self.cost = 0.0
        # The project's files, for the side pane's tree, as of the last prompt
        self.root = None
        self.files = dict(files=[], chat=[], read_only=[])
        # The saved conversation the chat shows, and where conversations are saved
        self.conversation_id = None
        self.sessions_dir = None
        # The git repo, and the commit it was at when the browser started talking to
        # loom, which the changes pane diffs against
        self.git = None
        self.base_commit = None
        # The output of the /run command being shown, for its card
        self.run_output = None
        # The current card already ended, like a finished /run's
        self.tool_closed = False
        # What the user wrote on the plan card with "keep planning"
        self.plan_feedback = ""
        # The coder whose checkpoints the rewind buttons show, and the turn each
        # checkpoint was taken in
        self.rewind_coder = None
        self.checkpoint_turns = {}

    def for_worker(self, worker):
        """A copy of this io for a parallel builder (loom/workers.py): its own tool cards,
        and every message it sends carries worker, so the browser shows it in the
        builder's lane."""
        from loom.workers import WorkerSession

        io = copy.copy(self)
        io.web = WorkerSession(self.web, worker)
        io.tool_id = None
        io.tool_failed = False
        io.tool_closed = False
        io.run_output = None
        io.pending_ask = None
        return io

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
        mode=None,
        status=None,
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
                mode=mode,
                status=status,
            )
        self.end_tool()
        self.web.end_turn()
        if not self.project_watcher:
            self.project_watcher = ProjectWatcher(self.web, root).start()
        coder = getattr(commands, "coder", None)
        self.remember_files(coder, root, rel_fnames, addable_rel_fnames)
        self.follow_conversation(coder)
        if getattr(coder, "session", None):
            self.checkpoints_changed(coder)
        self.remember_repo(coder)
        self.web.set_mode = lambda mode: self.set_mode(coder, mode)
        self.web.update(**self.session_state(coder, root, rel_fnames, commands))
        inp = self.web.wait_for_input()
        self.add_to_input_history(inp)
        self.user_input(inp)
        return self.web_command(inp)

    def web_command(self, inp):
        """The command loom runs for the browser's input: the web UI's own /phase becomes
        the /project command it stands for."""
        words = inp.split()
        if not words or words[0] != "/phase":
            return inp
        if len(words) == 1:
            return "/project status"
        try:
            phase = get_phase(" ".join(words[1:]))
        except KeyError:
            names = ", ".join(phase.key for phase in PHASES)
            self.tool_error(f"There is no phase {' '.join(words[1:])!r}; use one of: {names}.")
            return ""
        question = (
            f"Go back to {phase.title}? It and the phases after it will be run again, so"
            " their documents are redone."
        )
        if not self.confirm_ask(question, default="n"):
            return ""
        return f"/project back {phase.key}"

    def follow_conversation(self, coder):
        """When the coder's conversation changed, by /resume or /clear, show it instead."""
        session = getattr(coder, "session", None)
        if not session:
            return
        self.sessions_dir = session.directory
        if self.conversation_id is None:
            # The one loom started with, which the chat already shows
            self.conversation_id = session.id
            return
        if session.id == self.conversation_id:
            return
        self.conversation_id = session.id
        title = session.title or get_title(coder.done_messages)
        self.web.start_conversation(
            session.id, title, list(transcript(coder.done_messages, self.web.next_id))
        )

    # Checkpoints, for the rewind buttons (loom/checkpoints.py)

    def checkpoints_changed(self, coder):
        if not getattr(self, "web", None):
            return
        from loom.checkpoints import rewinds_conversation

        self.rewind_coder = coder
        session = coder.session
        items = []
        for num, checkpoint in enumerate(reversed(list(session.checkpoints)), 1):
            if checkpoint["id"] not in self.checkpoint_turns:
                # Seen first now: taken in this turn, or loaded with a resumed session
                self.checkpoint_turns[checkpoint["id"]] = self.web.turn_id
            items.append(
                dict(
                    id=checkpoint["id"],
                    number=num,
                    time=checkpoint.get("time"),
                    prompt=sanitize_for_display(checkpoint.get("prompt") or ""),
                    kind=checkpoint.get("kind", "request"),
                    conversation=rewinds_conversation(checkpoint),
                    turn_id=self.checkpoint_turns[checkpoint["id"]],
                )
            )
        self.web.emit(
            "checkpoints",
            conversation=session.id,
            git=coder.checkpoints.uses_git,
            items=items,
        )

    def conversation_rewound(self, coder):
        # The chat shows the conversation as it is now
        session = coder.session
        title = session.title or get_title(coder.done_messages)
        self.web.start_conversation(
            session.id, title, list(transcript(coder.done_messages, self.web.next_id))
        )

    def checkpoint_changes(self, checkpoint_id):
        """What the rewind dialog shows about a checkpoint: what changed in the files since,
        and whether a rewind can restore them now. None if there's no such checkpoint. Runs
        on the server's thread, with its own index file."""
        from loom.checkpoints import INDEX_FILE, CheckpointError, rewinds_conversation

        coder = self.rewind_coder
        if not coder:
            return None
        session = coder.session
        checkpoint = next(
            (cp for cp in list(session.checkpoints) if cp["id"] == checkpoint_id), None
        )
        if not checkpoint:
            return None
        checkpoints = coder.checkpoints
        res = dict(
            id=checkpoint["id"],
            time=checkpoint.get("time"),
            prompt=sanitize_for_display(checkpoint.get("prompt") or ""),
            kind=checkpoint.get("kind", "request"),
            conversation=rewinds_conversation(checkpoint),
            git=checkpoints.uses_git,
            busy=None,
            error=None,
            changes=None,
        )
        try:
            checkpoints.check_can_restore()
        except CheckpointError as err:
            res["busy"] = str(err)
        try:
            changes = checkpoints.changes(session, checkpoint, index_name=INDEX_FILE + "-web")
            res["changes"] = changes.to_dict()
        except Exception as err:
            res["error"] = str(err)
        return res

    def remember_repo(self, coder):
        repo = getattr(coder, "repo", None)
        if not repo or self.git:
            return
        self.git = repo.repo
        try:
            self.base_commit = repo.get_head_commit_sha()
        except Exception:
            self.base_commit = None

    def remember_files(self, coder, root, rel_fnames, addable_rel_fnames):
        read_only = []
        if coder:
            read_only = [coder.get_rel_fname(f) for f in coder.abs_read_only_fnames]
        self.root = str(root)
        self.files = dict(
            files=sorted(set(rel_fnames) | set(addable_rel_fnames) | set(read_only)),
            chat=sorted(rel_fnames),
            read_only=sorted(read_only),
        )

    def set_mode(self, coder, mode):
        """Switch the agent's permission mode, for the browser's mode pill. Runs on the
        server's thread, like Shift-Tab runs during the terminal's prompt."""
        permissions = getattr(coder, "permissions", None)
        if not permissions or mode not in MODES:
            return False
        permissions.mode = mode
        self.web.update(permission_mode=mode)
        return True

    def permission_mode_changed(self, mode):
        # Like approving a plan: the mode pill shows it right away, not at the next prompt
        if getattr(self, "web", None):
            self.web.update(permission_mode=mode)

    def current_branch(self):
        if not self.git:
            return None
        try:
            return self.git.active_branch.name
        except (TypeError, ValueError):
            # A detached HEAD, or a repo with no commits
            return None

    def session_state(self, coder, root, rel_fnames, commands):
        state = dict(
            version=__version__,
            cwd=str(root),
            files=sorted(rel_fnames),
            commands=list_commands(commands),
            tokens=dict(self.usage),
            cost=self.cost,
            conversation=self.conversation_id,
            branch=self.current_branch(),
            permission_mode=None,
            **project_state(root),
        )
        if coder:
            model = coder.main_model
            state.update(
                model=model.name,
                weak_model=model.weak_model.name if model.weak_model else "",
                edit_format=coder.edit_format,
                read_only_files=sorted(coder.get_rel_fname(f) for f in coder.abs_read_only_fnames),
            )
            permissions = getattr(coder, "permissions", None)
            if permissions:
                state.update(permission_mode=permissions.mode)
        return state

    # Esc comes from the browser, as a cancel message

    @contextmanager
    def esc_interrupts(self):
        yield

    def consume_esc(self):
        return self.web.consume_cancel()

    def poll_cancel(self):
        if self.web.started:
            self.web.check_cancel()

    # Sub-agents' tasks, as Task cards with their tool calls nested in them

    def task_board(self, verbose=False, headers=False):
        if not self.web.started:
            return super().task_board(verbose=verbose, headers=headers)
        from .tasks import WebTaskBoard

        return WebTaskBoard(self, headers=headers)

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
        # The answer is the choice without the parentheses that mark its key
        labels = [(choice, choice.capitalize()) for choice in choice_keys(choices)]
        kind = "checkpoint" if checkpoint else "plan" if plan else "choice"
        if plan:
            plan = dict(plan, text=sanitize_for_display(plan["text"]))
            self.plan_feedback = ""
        with self.asking(kind, question, labels, default or labels[0][0]):
            self.pending_ask["checkpoint"] = checkpoint
            self.pending_ask["plan"] = plan
            if checkpoint and self.project_watcher:
                # The phase is waiting for review now
                self.project_watcher.refresh()
            return super().choice_ask(
                question,
                choices,
                default=default,
                yes_choice=yes_choice,
                no_choice=no_choice,
                checkpoint=checkpoint,
                plan=plan,
            )

    def plan_feedback_ask(self):
        # The plan card sends the feedback with its answer
        if not self.web.started:
            return super().plan_feedback_ask()
        feedback, self.plan_feedback = self.plan_feedback, ""
        return feedback

    def plan_output(self, text, path=None):
        super().plan_output(text, path)
        if self.web.started:
            # Under the plan tool's card, which stays open for its result
            title = f"**Plan** · `{path}`\n\n" if path else "**Plan**\n\n"
            WebMarkdownStream(self.web).update(title + text, final=True)

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

    def usage_output(self, report, sent=0, received=0, cost=0.0):
        # The status line shows the tokens, counted across every agent as they're used
        super().tool_output(report, log_only=True)
        self.usage["sent"] += sent
        self.usage["received"] += received
        self.cost += cost
        self.web.update(tokens=dict(self.usage), cost=self.cost)

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
        if self.tool_closed:
            self.tool_id = None
            self.tool_closed = False
            return
        if result is None and self.run_output is not None:
            result = "".join(self.run_output)
        self.run_output = None
        output = sanitize_for_display(result or "")
        if len(output) > MAX_TOOL_OUTPUT:
            output = output[:MAX_TOOL_OUTPUT] + "\n…"
        failed = error or self.tool_failed
        self.web.emit(
            "tool_end", id=self.tool_id, status="failed" if failed else "done", output=output
        )
        self.tool_id = None

    def command_output(self, text, start=False, exit_code=None):
        """run_cmd's output callback: a command like /run's shows as a card in the chat,
        with its output in the side pane's terminal as it comes."""
        if not self.web.started:
            return
        text = sanitize_for_display(text)
        if exit_code is not None:
            if self.run_output is None:
                return
            if exit_code:
                self.tool_result(f"Exit code: {exit_code}", error=True)
            output = "".join(self.run_output)
            self.web.emit(
                "tool_end",
                id=self.tool_id,
                status="failed" if self.tool_failed else "done",
                output=output[-MAX_TOOL_OUTPUT:],
            )
            # The card stays the current one, so asking to add the output goes on it
            self.run_output = None
            self.tool_closed = True
            return
        if start:
            self.end_tool()
            self.tool_id = self.web.next_id("c")
            self.tool_failed = False
            self.web.emit("tool_start", id=self.tool_id, name="Run", detail=text, args=None)
            self.run_output = []
            self.web.emit("terminal", text=f"$ {text}\n", start=True)
            return
        if self.run_output is not None:
            self.run_output.append(text)
        self.web.emit("terminal", text=text, start=False)

    def print(self, message=""):
        super().print(message)
        message = sanitize_for_display(str(message))
        if "diff --git " in message:
            # /diff: one inline diff per file
            for part in re.split(r"(?m)^(?=diff --git )", message):
                if part.strip():
                    file, lines = parse_diff(part)
                    self.web.emit(
                        "diff", id=self.web.next_id("d"), tool_id=None, file=file, lines=lines
                    )
        else:
            self.emit_system("info", message)

    def edit_document(self, text, path):
        """Let the user edit a /project document in the browser, and return the result."""
        value = self.web.ask("edit", f"Edit {path}", default=text, subject=path)
        return text if value is None else value

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


# Commands only the web UI has, which web_command() turns into loom's own
WEB_COMMANDS = [
    dict(cmd="/phase", desc="Show the project's phases, or go back to one: /phase [PHASE]"),
]


def list_commands(commands):
    """The slash commands, with the first line of their help, for the browser to offer."""
    if not commands:
        return []
    listed = list(WEB_COMMANDS)
    for cmd in sorted(commands.get_commands()):
        method = getattr(commands, "cmd_" + cmd[1:].replace("-", "_"), None)
        doc = (method.__doc__ or "").strip().splitlines() if method else []
        listed.append(dict(cmd=cmd, desc=doc[0] if doc else ""))
    return sorted(listed, key=lambda command: command["cmd"])

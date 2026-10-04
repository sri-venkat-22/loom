import json
import re
import time

from loom import plans, prompts
from loom import tools as agent_tools
from loom.editor import pipe_editor
from loom.tools import ToolError
from loom.utils import format_tokens
from loom.waiting import WaitingSpinner

from .agent_prompts import AgentPrompts
from .base_coder import Coder

# Once a request is done, its long tool results are cut to this size in the chat history.
# The model can run the tool again if it needs the rest.
MAX_OLD_TOOL_RESULT_CHARS = 3000

# Lines of a command's output shown to the user (the model gets all of it)
COMMAND_PREVIEW_LINES = 8
# Lines of an MCP tool's result shown to the user
MCP_PREVIEW_LINES = 6
# Lines of a PostToolUse hook's feedback shown to the user
HOOK_PREVIEW_LINES = 4

# With --checkpoint-steps, a step that uses one of these is checkpointed first
STEP_CHECKPOINT_TOOLS = ("edit_file", "write_file", "bash")

# Compacting: when the conversation passes COMPACT_AT of the model's context window, loom
# shrinks it to about COMPACT_TARGET, keeping the last KEEP_RECENT_STEPS steps as they are
COMPACT_AT = 0.8
COMPACT_TARGET = 0.5
KEEP_RECENT_STEPS = 3
SHRUNK_TOOL_RESULT_CHARS = 1500
# How many times to compact and retry when the model says the messages are too long
MAX_OVERFLOW_RETRIES = 2

THINKING_FIELDS = ("thinking_blocks", "reasoning_details")

# Errors from providers or models that can't take the tools parameter
TOOLS_UNSUPPORTED_RE = re.compile(
    r"(?:tool|function)[\w\s-]{0,30}(?:not (?:be )?supported|unsupported|not enabled)"
    r"|(?:not|n't) support[\w\s-]{0,30}(?:tool|function)"
    r"|support tool use"
    r"|enable-auto-tool-choice"
    r"|does not support parameters: \[[^\]]*'tools'",
    re.IGNORECASE,
)

# A tool call written as text in a reply, in the formats models are trained on, instead of
# made through the API
TEXT_TOOL_CALL_RE = re.compile(
    r"<tool_call>|<\|?tool_calls?_begin\|?>|tool▁calls▁begin|\[TOOL_CALLS\]|<\|python_tag\|>"
    r"|<function=\w"
    r'|\{\s*"(?:name|tool|function)"\s*:\s*"(?:'
    + "|".join(agent_tools.TOOLS)
    + r')"\s*,\s*"(?:arguments|parameters|args|input)"'
)


def tools_unsupported(err):
    """Whether an API error says the model or provider can't do tool calling."""
    return bool(TOOLS_UNSUPPORTED_RE.search(str(err)))


def wrote_tool_call_as_text(content):
    return bool(content and TEXT_TOOL_CALL_RE.search(content))


def shorten(content, limit, why):
    """Keep the start and the end of a long tool result, which usually says how it went,
    and drop the middle."""
    head = limit // 3
    omitted = len(content) - limit
    note = (
        f"\n\n... [{omitted:,} characters dropped from the middle {why}. Don't guess what they"
        " said: run the tool again if you need them.] ...\n\n"
    )
    return content[:head] + note + content[-(limit - head) :]


class AgentCoder(Coder):
    """Explore, edit files and run commands with tools, asking before risky actions."""

    edit_format = "agent"
    gpt_prompts = AgentPrompts()
    max_steps = 100

    in_agent_loop = False
    continue_loop = False
    stop_requested = False
    pending_tool_calls = []
    request_started = None
    request_text = None
    overflow_retries = 0
    # The error, when the provider rejected the tools
    tools_error = None
    nudged_text_tool_call = False
    # The plan the user approved for the current request, which the system prompt carries
    # until the request is done, and its file
    active_plan = None
    active_plan_path = None
    # Set for loom --message: nothing can approve a plan, so it's shown and the agent stops
    one_shot = False
    # Checkpoint before each request, for /rewind (phase agents' runs are checkpointed by
    # the orchestrator instead)
    checkpoint_requests = True

    @property
    def tools(self):
        """The built-in tools, exit_plan_mode in plan mode, then those of the connected MCP
        servers."""
        res = agent_tools.schemas()
        if self.permissions.mode == "plan":
            res += agent_tools.plan_schemas()
        if self.mcp:
            res += self.mcp.tool_schemas()
        return res

    def get_announcements(self):
        lines = super().get_announcements()
        lines.append(f"Permissions: {self.permissions.describe()}")
        if self.mcp and self.mcp.servers:
            lines.append(f"MCP: {self.mcp.summary()}")
        if self.hooks and self.hooks.hooks:
            lines.append(f"Hooks: {self.hooks.summary()}")
        if not self.main_model.info.get("supports_function_calling"):
            lines.append(
                f"Warning: {self.main_model.name} may not support tool calling, which the"
                " agent needs. Use --no-agent if it doesn't work."
            )
        return lines

    def get_prompt_label(self):
        if self.permissions.mode == "ask":
            return self.edit_format
        return f"{self.edit_format} {self.permissions.mode}"

    def get_mode_cycler(self):
        def cycle():
            self.permissions.cycle_mode()
            return self.get_prompt_label()

        return cycle

    def check_for_file_mentions(self, content):
        # The agent finds and reads files itself
        return

    def get_cur_message_text(self):
        # Base the repo map on what the user asked for, not on tool output
        return "".join(
            msg["content"] + "\n"
            for msg in self.cur_messages
            if msg["role"] == "user" and isinstance(msg.get("content"), str)
        )

    def system_prompt_extras(self):
        """Text added to the end of the system prompt."""
        extra = []
        if self.mcp:
            extra.append(self.mcp.instructions())
        if self.permissions.mode == "plan":
            extra.append(self.gpt_prompts.plan_mode_prompt)
        elif self.active_plan:
            # Here, compacting the conversation can't lose it
            plan = plans.brief(self.active_plan, self.active_plan_path)
            extra.append(self.gpt_prompts.approved_plan_prompt.format(plan=plan))
        return extra

    def format_chat_chunks(self):
        chunks = super().format_chat_chunks()
        extra = [text for text in self.system_prompt_extras() if text]
        if extra and chunks.system:
            msg = chunks.system[0]
            content = "\n\n".join([msg["content"]] + extra)
            chunks.system[0] = dict(msg, content=content)
        return chunks

    def format_messages(self):
        chunks = super().format_messages()
        # Also cache up to the newest message, so each step of the loop reads the whole
        # conversation so far from the cache and only pays full price for the new part
        if self.add_cache_headers and chunks.cur:
            last = chunks.cur[-1]
            if isinstance(last.get("content"), str) and last["content"]:
                chunks.cur[-1] = dict(last)
                chunks.add_cache_control(chunks.cur)
        return chunks

    def send_message(self, inp):
        """Send inp, then keep sending tool results back until the model stops calling
        tools, the user denies an action or max_steps is reached. Esc or ^C stops it."""
        if self.checkpoint_requests and not self.num_reflections:
            self.take_checkpoint(inp)
        self.agent_edited = set()
        self.agent_touched = set()
        self.stop_requested = False
        self.in_agent_loop = True
        self.request_started = time.time()
        self.request_text = inp
        self.tools_error = None
        self.nudged_text_tool_call = False
        if self.mcp:
            # When loom started in another chat mode
            self.mcp.start()
        if self.hooks:
            self.hooks.start()
        self.permissions.start()

        message = inp
        try:
            with self.io.esc_interrupts():
                for _step in range(self.max_steps):
                    self.continue_loop = False
                    self.overflow_retries = 0
                    self.compact_if_needed()
                    yield from super().send_message(message)
                    message = None
                    if not self.continue_loop or self.tools_error:
                        break
                else:
                    self.io.tool_warning(
                        f'Stopped after {self.max_steps} steps. Say "continue" to keep going.'
                    )
        except KeyboardInterrupt:
            # Between steps: still commit what was done and keep the history
            self.keyboard_interrupt()
            self.interrupted = True
        finally:
            self.in_agent_loop = False

        self.show_usage_report()
        if self.tools_error:
            self.tools_rejected()
        self.finish_request(inp)

    def send(self, messages, model=None, functions=None):
        try:
            yield from super().send(messages, model, functions)
        except Exception as err:
            # No point retrying
            if not (self.in_agent_loop and tools_unsupported(err)):
                raise
            self.tools_error = err

    def tools_rejected(self):
        """The provider rejected the tools: stop and say how to carry on without them.

        loom doesn't switch to an edit format by itself: the model's edit blocks are applied
        without asking Permissions, so it would ignore plan mode and the user's answers."""
        err = str(self.tools_error).strip().split("\n", 1)[0]
        self.tools_error = None
        # The request got no reply, so it stays out of the chat history
        if all(msg["role"] == "user" for msg in self.cur_messages):
            self.cur_messages = []

        self.io.tool_error(f"{self.main_model.name} can't use the agent's tools: {err}")
        if self.permissions.mode == "plan":
            self.io.tool_output(
                "Use /ask to discuss the code with it instead; it can't change files there."
            )
            return
        edit_format = self.main_model.edit_format
        if edit_format == self.edit_format:
            edit_format = "diff"
        self.io.tool_output(
            f"To have it edit files without tools, switch with /chat-mode {edit_format} and send"
            " the request again (or start loom with --no-agent). Its edits are then applied"
            " without asking you first."
        )

    def get_spinner_text(self):
        def text():
            activity = "Working"
            for todo in self.todos:
                if todo.get("status") == "in_progress":
                    activity = todo.get("active_form") or todo["content"]
                    break
            info = []
            if self.request_started:
                info.append(f"{int(time.time() - self.request_started)}s")
            if self.io.esc_listener:
                info.append("esc to interrupt")
            return f"{activity.rstrip('.…')}… ({' · '.join(info)})" if info else activity + "…"

        return text

    def show_usage_report(self):
        # One report for the whole request, not one per step
        if not self.in_agent_loop:
            super().show_usage_report()

    def add_assistant_reply_to_cur_messages(self):
        tool_calls = self.get_tool_calls()
        self.pending_tool_calls = tool_calls
        if not self.partial_response_content and not tool_calls:
            return

        msg = dict(role="assistant", content=self.partial_response_content or None)
        if tool_calls:
            msg["tool_calls"] = tool_calls
            msg.update(self.get_thinking())
        self.cur_messages.append(msg)

    def reply_completed(self):
        calls = self.pending_tool_calls
        self.pending_tool_calls = []
        if calls:
            self.run_tool_calls(calls)
        elif wrote_tool_call_as_text(self.partial_response_content):
            self.handle_text_tool_call()
        return True

    def handle_text_tool_call(self):
        """The model wrote a tool call in its reply instead of making it: ask it once to use
        tool calling, then suggest an edit format."""
        if self.nudged_text_tool_call:
            self.io.tool_warning(
                f"{self.main_model.name} keeps writing tool calls as text, so it may not support"
                " tool calling. Use /chat-mode diff (or start loom with --no-agent) to have it"
                " edit files without tools."
            )
            return
        self.nudged_text_tool_call = True
        self.io.tool_warning(
            "The model wrote a tool call as text instead of making it; asking it to use tool"
            " calling."
        )
        self.cur_messages.append(dict(role="user", content=self.gpt_prompts.text_tool_call))
        self.continue_loop = True

    def run_tool_calls(self, calls):
        if self.checkpoint_steps:
            self.checkpoint_step(calls)
        # Every call needs a result message, even ones that don't run
        for call in calls:
            if self.stop_requested:
                result = "Not run, because the user stopped an earlier action."
            else:
                try:
                    result = self.run_tool_call(call)
                    self.io.tool_done(result)
                except KeyboardInterrupt:
                    self.keyboard_interrupt()
                    self.stop_requested = True
                    result = "Interrupted by the user. Stop and wait for their instructions."
                    self.io.tool_done(result, error=True)
            self.cur_messages.append(dict(role="tool", tool_call_id=call["id"], content=result))

        self.continue_loop = not self.stop_requested

    def checkpoint_step(self, calls):
        """With --checkpoint-steps: checkpoint before a step that edits files or runs
        commands, so /rewind can undo just that step."""
        shown = []
        for call in calls:
            name = call["function"]["name"]
            if name not in STEP_CHECKPOINT_TOOLS:
                continue
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            detail = " ".join(str(args.get("path") or describe_args(args)).split())[:60]
            shown.append(f"{agent_tools.display_name(name)}({detail})")
        if shown:
            self.take_checkpoint(", ".join(shown), kind="step")

    def run_tool_call(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError as err:
            self.io.tool_call(name)
            self.io.tool_result("Error: the model sent invalid arguments", error=True)
            return f"Error: the arguments were not valid JSON ({err}). Try again with valid JSON."

        try:
            action = agent_tools.prepare(self, name, args)
        except ToolError as err:
            self.io.tool_call(name, describe_args(args), args=args)
            self.io.tool_result(f"Error: {err}", error=True)
            return f"Error: {err}"
        except Exception as err:
            # Never let an unexpected failure escape: the model\'s tool_calls message is
            # already on the way to the chat history, and a missing tool reply would make
            # the next request a 400 forever
            self.io.tool_call(name, describe_args(args), args=args)
            self.io.tool_result(f"Error: {err}", error=True)
            return f"Error preparing {name}: {err.__class__.__name__}: {err}"

        self.io.tool_call(action.name or name, action.detail, args=args)
        refusal = self.refuse_action(name, action)
        if refusal:
            self.io.tool_result(f"Refused: {refusal}", error=True)
            return f"Refused: {refusal}"

        hook_allowed = self.preapproved(action)
        if self.hooks:
            hook = self.hooks.run("PreToolUse", self, agent_tools.hook_name(name), args, action)
            if hook.decision == "block":
                reason = hook.message.strip().split("\n", 1)[0]
                self.io.tool_result(f"Blocked by a hook: {reason}", error=True)
                return (
                    f"Blocked by the user's PreToolUse hook: {hook.message}\nDon't retry the"
                    " same call; do something else or ask the user."
                )
            hook_allowed = hook_allowed or hook.decision == "allow"

        # When the user is asked, the question shows the diff or the command
        asked = self.permissions.decide(action, hook_allowed) == "ask"
        outcome, message = self.permissions.request(action, hook_allowed)
        if outcome == "deny":
            self.io.tool_result("Refused in plan mode", error=True)
            return message
        if outcome == "user-deny":
            self.io.tool_result("Denied", error=True)
            self.stop_requested = True
            return message

        if action.kind == "edit":
            self.before_edit(action)
        try:
            if action.kind in ("bash", "mcp") and self.show_pretty():
                # These can take a while
                with WaitingSpinner(self.get_spinner_text()):
                    result = action.run()
            else:
                result = action.run()
        except (ToolError, OSError) as err:
            self.io.tool_result(f"Error: {err}", error=True)
            return f"Error: {err}"
        except KeyboardInterrupt:
            raise
        except Exception as err:
            # A buggy tool can\'t take the session down: return the error to the model so
            # this call still has a matching tool reply
            self.io.tool_result(f"Error: {err}", error=True)
            return f"Error running {name}: {err.__class__.__name__}: {err}"

        if action.kind == "edit":
            result += self.after_edit(action)
        self.show_tool_result(action, result, diff_shown=asked)

        if self.hooks:
            hook = self.hooks.run(
                "PostToolUse", self, agent_tools.hook_name(name), args, action, result
            )
            if hook.message:
                self.show_hook_feedback(hook.message)
                result += f"\n\nThe user's PostToolUse hook says:\n{hook.message}"
        return result

    def refuse_action(self, name, action):
        """Why this coder won't let the model use tool name for action, or None. Subclasses
        with a limited tool set refuse the rest."""
        return None

    def preapproved(self, action):
        """Whether action runs without asking, like an allow rule. Protected files and plan
        mode still win."""
        return False

    def show_hook_feedback(self, message):
        """Show what a PostToolUse hook told the model, under the tool's result."""
        lines = message.strip().splitlines() or [""]
        lines[0] = "Hook: " + lines[0]
        if len(lines) > HOOK_PREVIEW_LINES:
            more = len(lines) - HOOK_PREVIEW_LINES
            lines = lines[:HOOK_PREVIEW_LINES] + [f"… +{more} lines"]
        self.io.tool_result(lines, styles=["yellow"] * len(lines))

    def show_tool_result(self, action, result, diff_shown=False):
        """Show the outcome of a tool call compactly, under the call."""
        if action.kind == "edit":
            self.io.tool_result(action.summary or result.split("\n", 1)[0])
            if action.preview and not diff_shown and self.io.agent_diffs:
                self.io.diff_output(action.preview, indent="     ")
        elif action.kind == "bash":
            self.show_command_output(result)
        elif action.kind == "todo":
            self.io.todo_output(self.todos)
        elif action.kind == "plan":
            self.io.tool_result(action.summary, error=action.extra.get("error", False))
        elif action.kind == "mcp":
            lines = result.splitlines() or ["(no output)"]
            if len(lines) > MCP_PREVIEW_LINES:
                more = len(lines) - MCP_PREVIEW_LINES
                lines = lines[:MCP_PREVIEW_LINES] + [f"… +{more} lines"]
            self.io.tool_result(lines)
        else:
            self.io.tool_result(action.summary or result.split("\n", 1)[0])

    def before_edit(self, action):
        """Commit the user's own uncommitted changes to a file before the agent first edits
        it, so /undo only reverts the agent's changes. Without git, copy it for /rewind."""
        if not self.repo and action.inside and not self.dry_run:
            self.checkpoints.backup(self.session, action.path)
        if action.path in self.agent_touched:
            return
        self.agent_touched.add(action.path)

        if not (self.repo and self.dirty_commits and not self.dry_run):
            return
        if not action.inside or not action.path.is_file():
            return
        rel_fname = self.get_rel_fname(action.path)
        if self.repo.git_ignored_file(rel_fname):
            return

        self.need_commit_before_edits = set()
        self.check_for_dirty_commit(rel_fname)
        self.dirty_commit()
        self.need_commit_before_edits = set()

    def after_edit(self, action):
        """Track the edit for the commit, and lint the file so the model sees any errors."""
        rel_fname = self.get_rel_fname(action.path)
        if action.inside and not (self.repo and self.repo.git_ignored_file(rel_fname)):
            self.agent_edited.add(rel_fname)

        if not self.auto_lint or self.dry_run:
            return ""
        errors = self.linter.lint(str(action.path))
        if not errors:
            return ""
        self.io.tool_warning(f"The linter found problems in {rel_fname}.")
        return f"\n\nThe linter found problems in {rel_fname}; fix them:\n{errors}"

    def show_command_output(self, result):
        """The end of a command's output, which usually says how it went, and its exit code
        if it failed."""
        status, _, output = result.partition("\n")
        lines = [line.rstrip() for line in output.splitlines()]
        styles = [None] * len(lines)
        if len(lines) > COMMAND_PREVIEW_LINES:
            skipped = len(lines) - COMMAND_PREVIEW_LINES
            lines = [f"… +{skipped} earlier lines"] + lines[-COMMAND_PREVIEW_LINES:]
            styles = ["dim"] + [None] * COMMAND_PREVIEW_LINES
        if status != "Exit code: 0":
            lines.append(status)
            styles.append(self.io.tool_error_color or "red")
        self.io.tool_result(lines or ["(no output)"], styles=styles)

    # Plan mode: presenting the plan (loom/plans.py)

    def present_plan(self, text, action):
        """The exit_plan_mode tool: save the plan, show it and ask the user to approve it,
        edit it or keep planning. Returns the tool's result for the model."""
        try:
            path = plans.save_plan(self.root, text)
        except OSError as err:
            path = None
            self.io.tool_warning(f"Unable to save the plan: {err}")
        shown = plans.rel_path(self.root, path) if path else None

        if self.permissions.approves_plans():
            # The session was in bypass mode before plan mode
            self.io.plan_output(text, shown)
            return self.approve_plan(text, path, "bypass", action, how="automatically (bypass)")

        if self.one_shot or self.io.yes is not None:
            self.io.plan_output(text, shown)
            return self.leave_plan_unapproved(shown, action)

        edited = False
        while True:
            answer = self.io.choice_ask(
                "Approve this plan?",
                plans.CHOICES,
                default=plans.APPROVE_ASK,
                plan=dict(text=text, path=shown),
            )
            if answer in plans.APPROVED_MODES:
                mode = plans.APPROVED_MODES[answer]
                return self.approve_plan(text, path, mode, action, edited=edited)
            if answer == plans.KEEP_PLANNING:
                return self.keep_planning(self.io.plan_feedback_ask().strip(), action)

            new_text = self.edit_plan(text, shown)
            if new_text.strip() and new_text.strip() != text.strip():
                text = new_text
                edited = True
                if path:
                    plans.save_plan(self.root, text, path)
                self.io.tool_output("Saved your edit of the plan.")
            else:
                self.io.tool_output("The plan is unchanged.")

    def edit_plan(self, text, shown):
        """Let the user edit the plan, in the web UI's editor or the terminal's."""
        if self.io.edit_document:
            return self.io.edit_document(text, shown or "plan.md")
        commands = getattr(self, "commands", None)
        return pipe_editor(text, suffix=".md", editor=getattr(commands, "editor", None))

    def approve_plan(self, text, path, mode, action, edited=False, how=""):
        self.permissions.mode = mode
        self.active_plan = text
        self.active_plan_path = plans.rel_path(self.root, path) if path else None
        if self.active_plan_path:
            self.session.plan = self.active_plan_path
        described = {
            "accept-edits": "edits are applied without asking",
            "ask": "each edit and command asks the user first",
            "bypass": "everything runs without asking",
        }[mode]
        action.summary = "Approved" + (f" {how}" if how else "") + f" · {mode} mode"
        result = (
            f"The user approved your plan{' after editing it' if edited else ''}. loom left"
            f" plan mode and is now in {mode} mode: {described}. Carry out the plan now. Start"
            " by writing a to-do list with todo_write from its steps, then work through it,"
            " and verify the result as the plan says."
        )
        if edited:
            result += f"\n\nThe approved plan, as the user edited it:\n\n{text.strip()}"
        return result

    def keep_planning(self, feedback, action):
        if feedback:
            action.summary = "Keep planning: " + " ".join(feedback.split())
            return (
                "The user didn't approve the plan yet; loom is still in plan mode. Their"
                f" feedback:\n\n{feedback}\n\nRevise the plan to address it, investigating"
                " more if you need to, then call exit_plan_mode again with the whole new plan."
            )
        action.summary = "Keep planning"
        self.stop_requested = True
        return (
            "The user didn't approve the plan and wants to keep planning; loom is still in plan"
            " mode. Stop here and wait for them to say what to change."
        )

    def leave_plan_unapproved(self, shown, action):
        """Nobody can approve the plan here, like with --yes-always or --message: stop."""
        action.summary = "Not approved: --yes-always and --message don't approve plans"
        action.extra["error"] = True
        self.stop_requested = True
        where = f" It's saved in {shown}." if shown else ""
        self.io.tool_output(
            f"The plan needs your approval.{where} To carry it out, switch modes with"
            " /permissions accept-edits (or start loom with --permission-mode accept-edits) and"
            " ask the agent to go ahead."
        )
        return (
            "The plan was shown to the user, but loom is running non-interactively, so nobody"
            " can approve it and loom stays in plan mode. Stop here: don't make changes."
        )

    def finish_request(self, inp):
        """Commit the request's edits and move its messages into the chat history."""
        self.active_plan = None
        self.active_plan_path = None
        edited = sorted(self.agent_edited)
        if edited:
            self.loom_edited_files.update(edited)
            self.auto_commit(edited, context=self.get_request_context(inp))

        self.done_messages += self.tidy_tool_messages(self.cur_messages)
        self.cur_messages = []
        self.summarize_start()

        if edited and self.auto_test and not self.reflected_message:
            test_errors = self.commands.cmd_test(self.test_cmd)
            self.test_outcome = not test_errors
            if test_errors and self.io.confirm_ask("Attempt to fix test errors?"):
                self.reflected_message = test_errors

    def get_request_context(self, inp):
        """The request and the agent's final reply, for writing the commit message."""
        reply = ""
        for msg in reversed(self.cur_messages):
            if msg["role"] == "assistant" and isinstance(msg.get("content"), str):
                reply = msg["content"]
                break
        return f"\nUSER: {inp}\nASSISTANT: {reply}\n"

    def tidy_tool_messages(self, messages):
        """Make a finished request's messages safe and cheap to keep: drop tool calls that
        never got a result (after ^C, say), shorten long tool results, and drop the model's
        thinking except where the API still needs it: on the last assistant message, when the
        request stopped at a tool call."""
        answered = {msg["tool_call_id"] for msg in messages if msg["role"] == "tool"}
        called = set()
        assistant = [msg for msg in messages if msg["role"] == "assistant"]
        keep_thinking = assistant[-1] if assistant and assistant[-1].get("tool_calls") else None

        res = []
        for msg in messages:
            if msg["role"] == "assistant" and msg is not keep_thinking:
                msg = {k: v for k, v in msg.items() if k not in THINKING_FIELDS}
            if msg.get("tool_calls"):
                calls = [call for call in msg["tool_calls"] if call["id"] in answered]
                called.update(call["id"] for call in calls)
                msg = dict(msg)
                if calls:
                    msg["tool_calls"] = calls
                else:
                    del msg["tool_calls"]
            elif msg["role"] == "tool":
                if msg["tool_call_id"] not in called:
                    continue
                content = msg["content"]
                if len(content) > MAX_OLD_TOOL_RESULT_CHARS:
                    content = shorten(content, MAX_OLD_TOOL_RESULT_CHARS, "of the history")
                    msg = dict(msg, content=content)

            if msg["role"] == "assistant" and not msg.get("content") and not msg.get("tool_calls"):
                continue
            res.append(msg)
        return res

    # Compacting the conversation when it nears the context window

    def context_window(self):
        return self.main_model.info.get("max_input_tokens") or 0

    def count_tokens(self, messages, with_tools=True):
        """Estimate the tokens in messages, tool calls and (with_tools) tool definitions. It
        errs on the high side, since the tokenizer may not be the model's own."""
        parts = []
        images = 0
        for msg in messages:
            parts.append(msg["role"])
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text") or "")
                    else:
                        images += 1
            elif content:
                parts.append(str(content))
            for call in msg.get("tool_calls") or []:
                parts.append(call["function"]["name"] + " " + call["function"]["arguments"])
        if with_tools and self.tools:
            parts.append(json.dumps(self.tools))

        text = "\n".join(parts)
        tokens = self.main_model.token_count(text)
        if not tokens:
            tokens = len(text) // 3
        return tokens + 4 * len(messages) + 1500 * images

    def context_tokens(self):
        return self.count_tokens(self.format_messages().all_messages())

    def compact_if_needed(self):
        """Compact the conversation before a step if it's close to filling the context
        window, so a long request doesn't fail."""
        window = self.context_window()
        if not (self.auto_compact and window):
            return
        tokens = self.context_tokens()
        limit = int(window * COMPACT_AT)
        if tokens > limit:
            self.compact(int(window * COMPACT_TARGET), limit, tokens)

    def compact_after_overflow(self):
        """The model said the messages are too long: compact harder and try again."""
        if not (self.in_agent_loop and self.auto_compact):
            return False
        if self.overflow_retries >= MAX_OVERFLOW_RETRIES:
            return False
        self.overflow_retries += 1

        tokens = self.context_tokens()
        target = tokens // 2
        if self.context_window():
            target = min(target, int(self.context_window() * COMPACT_TARGET))
        self.io.tool_warning(f"The conversation is too long for {self.main_model.name}.")
        return self.compact(target, target, tokens) < tokens

    def compact(self, target, limit, tokens=None):
        """Shrink the conversation towards target tokens, doing as little as that needs:
        shorten old tool results, summarize the chat history, shorten more tool results,
        then summarize this request's older steps. The results of the latest step, which
        the model hasn't seen yet, are only shortened if it's still over limit. Returns
        the new size."""
        before = tokens or self.context_tokens()
        # (what it does, how many things it did that to)
        steps = [
            ("old tool result", lambda: self.shrink_tool_results(KEEP_RECENT_STEPS)),
            ("earlier message", self.summarize_history),
            ("old tool result", lambda: self.shrink_tool_results(1)),
            ("earlier step", lambda: self.summarize_steps(KEEP_RECENT_STEPS)),
            ("earlier step", lambda: self.summarize_steps(1)),
            ("new tool result", lambda: self.shrink_tool_results(0)),
        ]
        counts = {}
        tokens = before
        for what, step in steps:
            if what == "new tool result" and tokens <= limit:
                break
            num = step()
            if not num:
                continue
            counts[what] = counts.get(what, 0) + num
            tokens = self.context_tokens()
            if tokens <= target:
                break

        if counts:
            verbs = {
                "old tool result": "shortened",
                "new tool result": "shortened",
                "earlier message": "summarized",
                "earlier step": "summarized",
            }
            done = [
                f"{verbs[what]} {agent_tools.plural(num, what)}" for what, num in counts.items()
            ]
            self.io.tool_call("Compacted the conversation")
            self.io.tool_result(
                f"{format_tokens(before)} → {format_tokens(tokens)} tokens: " + ", ".join(done)
            )
        return tokens

    def recent_steps_start(self, keep):
        """The index in cur_messages of the keep-th last step (an assistant message calling
        tools), or len(cur_messages) if keep is 0."""
        if keep <= 0:
            return len(self.cur_messages)
        starts = [
            num
            for num, msg in enumerate(self.cur_messages)
            if msg["role"] == "assistant" and msg.get("tool_calls")
        ]
        if len(starts) < keep:
            return 0
        return starts[-keep]

    def shrink_tool_results(self, keep):
        """Shorten the long tool results of all but the last keep steps. Returns how many
        it shortened."""
        end = self.recent_steps_start(keep)
        num = 0
        for index, msg in enumerate(self.cur_messages[:end]):
            content = msg.get("content")
            if msg["role"] != "tool" or not isinstance(content, str):
                continue
            # Not worth it, which also skips the results shortened before
            if len(content) - SHRUNK_TOOL_RESULT_CHARS < 500:
                continue
            content = shorten(content, SHRUNK_TOOL_RESULT_CHARS, "to save space")
            self.cur_messages[index] = dict(msg, content=content)
            num += 1
        return num

    def summarize_history(self):
        """Summarize the messages of earlier requests."""
        self.summarize_end()
        if not self.done_messages:
            return
        num = len(self.done_messages)
        try:
            summary = self.summarizer.summarize_all(self.done_messages)
        except ValueError as err:
            self.io.tool_warning(str(err))
            return
        self.done_messages = summary + [dict(role="assistant", content="Ok.")]
        return num

    def summarize_steps(self, keep):
        """Replace this request's steps, all but the last keep, with a summary added to the
        request."""
        request = self.cur_messages[0] if self.cur_messages else None
        if not (request and request["role"] == "user" and isinstance(request["content"], str)):
            return
        end = self.recent_steps_start(keep)
        old = self.cur_messages[1:end]
        if not old:
            return

        todos = ""
        if self.todos:
            marks = dict(completed="[x]", in_progress="[~]", pending="[ ]")
            todos = "\n\nYour to-do list:\n" + "\n".join(
                f"- {marks.get(todo['status'], '[ ]')} {todo['content']}" for todo in self.todos
            )
        try:
            summary = self.summarizer.summarize_all(
                [request] + old,
                prompt=prompts.compact_steps,
                prefix=prompts.compact_steps_prefix,
            )[0]["content"]
            summary_tokens = self.count_tokens([dict(role="user", content=summary)], False)
            if summary_tokens >= self.count_tokens(old, False):
                # Not a summary; leave these steps to be shortened instead
                return 0
        except ValueError as err:
            self.io.tool_warning(str(err))
            num_steps = sum(1 for msg in old if msg["role"] == "assistant")
            summary = (
                prompts.compact_steps_prefix
                + f"({num_steps} earlier steps were dropped to save space, and summarizing them"
                " failed. Check the files to see where things stand.)"
            )

        num_steps = sum(1 for msg in old if msg["role"] == "assistant" and msg.get("tool_calls"))
        # The summary covers any earlier summary, which the request carries
        content = (self.request_text or request["content"]) + "\n\n" + summary + todos
        self.cur_messages = [dict(request, content=content)] + self.cur_messages[end:]
        return max(num_steps, 1)


def describe_args(args):
    """A short description of a tool call's arguments, for calls that couldn't be prepared."""
    if not isinstance(args, dict):
        return ""
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""

import inspect

from loom import subagents

from .agent_coder import AgentCoder
from .base_coder import Coder
from .subagent_prompts import SubAgentPrompts

# What Coder.__init__ takes. A parent's own arguments, like a phase agent's phase, are
# left out of the sub-agent's
CODER_ARGS = set(inspect.signature(Coder.__init__).parameters) - {"self", "main_model", "io"}


class SubAgentCoder(AgentCoder):
    """A sub-agent (see loom/subagents.py): the agent loop doing one task for the agent
    that started it, with a fresh conversation and its agent type's tools, ending with a
    report. It doesn't commit or checkpoint; its parent does."""

    gpt_prompts = SubAgentPrompts()
    max_steps = subagents.MAX_STEPS
    checkpoint_requests = False

    parent = None
    task = None
    # The parent's project memory, for recall, when the parent is a phase agent
    shared_memory = None
    # A dollar budget, or None
    budget = None
    # Why it stopped before finishing: "steps" or "budget"
    limit = None
    # Set for its last step, when its tool calls aren't run
    final_step = False
    # How it ended: the provider rejected its tools, the user denied an action, or it
    # stopped without a report
    failed = None
    denied = False
    incomplete = False
    tool_uses = 0
    # Esc stopped it while tasks ran at once, or (stopped) its stop button did
    cancelled = False
    stopped = False
    # Its reply is streaming in
    in_stream = False

    def __init__(self, main_model, io, parent=None, task=None, **kwargs):
        kwargs = {key: value for key, value in kwargs.items() if key in CODER_ARGS}
        self.parent = parent
        self.task = task
        self.shared_memory = getattr(parent, "shared_memory", None)
        # Every message, as the model saw it, for the transcript
        self.messages = []
        # The files it edited, relative to the project root
        self.edited = set()
        super().__init__(main_model, io, **kwargs)
        settings = self.subagent_settings
        self.max_steps = settings.get("max_steps") or subagents.MAX_STEPS
        self.budget = settings.get("budget") or None

    @property
    def agent_type(self):
        return self.task.agent_type

    @property
    def tools(self):
        """The parent's tools that its agent type allows: never task."""
        return [
            schema
            for schema in self.parent.tools
            if self.agent_type.allows(schema["function"]["name"], self)
        ]

    def tool_names(self):
        return [schema["function"]["name"] for schema in self.tools]

    def can_delegate(self):
        return False

    def system_prompt_extras(self):
        names = self.tool_names()
        extra = []
        if self.mcp and any(name.startswith("mcp__") for name in names):
            extra.append(self.mcp.instructions())
        if "save_stitch_screen" in names:
            extra.append(self.stitch_prompt())
        if "web_search" in names or "web_fetch" in names:
            extra.append(self.gpt_prompts.web_tools_prompt)
        if self.permissions.mode == "plan":
            extra.append(self.gpt_prompts.plan_mode_prompt)
        agent_type = self.agent_type
        extra.append(
            self.gpt_prompts.agent_type_prompt.format(
                name=agent_type.name, prompt=agent_type.prompt
            )
        )
        return extra

    # Doing the task

    def do_task(self, prompt):
        """Do the task, and return its report: its final reply. Then the SubagentStop hooks
        run, and one can send it back to work."""
        self.init_before_message()
        report = self.work(prompt)
        for num in range(subagents.MAX_STOP_BLOCKS):
            if self.interrupted or self.failed or self.denied or self.cancelled:
                break
            outcome = self.stop_hooks(report, active=num > 0)
            if outcome.decision != "block":
                break
            reason = outcome.message.strip()
            self.io.tool_warning("SubagentStop hook: " + reason.split("\n", 1)[0])
            report = self.work(reason)
        return report

    def stop_hooks(self, report, active):
        """Run the SubagentStop hooks, with the transcript saved for them to read."""
        from loom.hooks import HookOutcome

        task = self.task
        if not (self.hooks and self.hooks.matching("SubagentStop", task.agent_type.name)):
            return HookOutcome()
        task.report = report
        try:
            path = self.parent.session.save_task(task.record())
        except (OSError, TypeError, ValueError):
            path = None
        return self.hooks.subagent_stop(self, task, path, active)

    def work(self, message):
        """Send message and return the report: its final reply. A sub-agent that ran out of
        steps or budget is told to stop and report, one that ended without a report is asked
        for one, and if it still writes none its last tool results stand in for it."""
        self.incomplete = False
        self.send_request(message)
        if self.interrupted or self.failed or self.denied or self.cancelled:
            return self.final_text()

        limit = self.limit
        if limit:
            self.send_request(subagents.STOP_NOW, final=True)
        elif not self.final_text():
            self.send_request(subagents.ASK_FOR_REPORT, final=True)
        if self.interrupted:
            return ""

        report = self.final_text()
        if report:
            return report
        self.incomplete = True
        why = {
            "steps": f"reached its limit of {self.max_steps} steps",
            "budget": "reached its budget",
        }.get(limit, "stopped")
        return subagents.fallback_report(self.messages, why)

    def send_request(self, message, final=False):
        """One request of the agent loop. final allows a single step, without tools."""
        if self.cancelled:
            return
        self.final_step = final
        self.limit = None
        self.io.user_input(message)
        list(self.send_message(message))

    def final_text(self):
        """Its last reply, if that's text rather than tool calls."""
        for msg in reversed(self.messages):
            if msg["role"] != "assistant":
                continue
            if msg.get("tool_calls"):
                return ""
            return (msg.get("content") or "").strip()
        return ""

    def start_request(self):
        # The parent started the MCP servers and asked about the project's hooks and rules
        pass

    def over_budget(self):
        return bool(self.budget) and self.total_cost >= self.budget

    def context_window(self):
        # Every step resends the whole conversation: keep it small, and compact early
        window = super().context_window()
        return min(window, subagents.CONTEXT_TOKENS) if window else subagents.CONTEXT_TOKENS

    def limit_reached(self, reason):
        self.limit = reason

    def cancel(self):
        """Stop as soon as it can: Esc while tasks run at once, from the main thread."""
        self.cancelled = True
        self.stop_requested = True
        self.interrupted = True

    def stop(self):
        """Stop as soon as it can, for its stop button, without stopping the parent."""
        self.stopped = True
        self.cancelled = True
        self.stop_requested = True

    def show_pretty(self):
        # Its streamed reply goes to the io's hidden Markdown stream rather than straight to
        # the terminal, and it never runs spinners
        return self.in_stream

    def live_incremental_response(self, final):
        pass

    def show_send_output_stream(self, completion):
        def chunks():
            for chunk in completion:
                if self.cancelled:
                    raise KeyboardInterrupt()
                yield chunk

        self.in_stream = True
        try:
            yield from super().show_send_output_stream(chunks())
        finally:
            self.in_stream = False

    def run_tool_calls(self, calls):
        if self.cancelled:
            self.stop_requested = True
        if not self.final_step:
            return super().run_tool_calls(calls)
        for call in calls:
            self.cur_messages.append(
                dict(role="tool", tool_call_id=call["id"], content=subagents.NOT_RUN)
            )
        self.continue_loop = False

    def run_tool_call(self, call):
        self.tool_uses += 1
        return super().run_tool_call(call)

    def action_denied(self):
        super().action_denied()
        self.denied = True

    def refuse_action(self, name, action):
        """Tools its agent type doesn't allow, then whatever its parent would refuse, like
        a phase agent's files it may not write."""
        if name not in self.tool_names():
            allowed = ", ".join(self.tool_names()) or "none"
            return f"the {self.agent_type.name} agent can't use {name}. Its tools are: {allowed}."
        return self.parent.refuse_action(name, action)

    def before_edit(self, action):
        # The parent commits the user's own changes to a file before the first edit, once
        # for all its sub-agents
        with self.parent.task_lock:
            self.parent.before_edit(action)

    def keyboard_interrupt(self):
        # The parent says it was interrupted, once; a stopped one wasn't
        if not self.stopped:
            self.interrupted = True

    def tools_rejected(self):
        err = str(self.tools_error).strip().split("\n", 1)[0]
        self.tools_error = None
        self.failed = f"{self.main_model.name} can't use tools: {err}"

    def finish_request(self, inp):
        """Keep the request's messages; the parent commits the edits."""
        self.edited.update(self.agent_edited)
        self.messages += self.cur_messages
        self.done_messages += self.tidy_tool_messages(self.cur_messages)
        self.cur_messages = []

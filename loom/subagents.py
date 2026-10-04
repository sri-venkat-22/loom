"""
Sub-agents: the agent's task tool hands a focused job, like "explore how auth works" or
"find every caller of X", to a child agent with a fresh context of its own. The child
works with its own tools and returns only a concise report, so the parent's context stays
small.

The child (loom/coders/subagent_coder.py) starts from nothing of the parent's chat: its
system prompt, its agent type's prompt, LOOM.md and the task's prompt. It shares the
parent's permissions (mode, rules and "always" answers), MCP connections, hooks and Esc,
and has its own to-do list, compaction and step limit. It never gets the task tool, so
sub-agents can't start sub-agents.

Agent types:
- explore: read-only. Searches broadly and reports findings with file:line references.
- general: every tool the parent has, except task.

In plan mode only read-only agent types may run.

The child doesn't commit or checkpoint: the files it edits join the parent's, so the
parent's commit at the end of the request and its /rewind checkpoint cover them. What it
spends is added to the parent's tokens and cost.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from loom import tools as agent_tools
from loom.coders.subagent_prompts import EXPLORE_PROMPT, GENERAL_PROMPT
from loom.tools import ToolError, plural, truncate
from loom.utils import format_tokens

DEFAULT_AGENT = "general"
# Steps a sub-agent may take, unless --subagent-max-steps says otherwise
MAX_STEPS = 40
# The longest report the parent gets; longer ones keep their start and end
MAX_REPORT_CHARS = 10_000
# How much of each of its last tool results an unfinished sub-agent returns
MAX_FALLBACK_RESULT_CHARS = 1500
MAX_FALLBACK_RESULTS = 5

# The tools read-only agent types get, besides MCP tools marked read-only
READ_ONLY_TOOLS = ("read_file", "list_dir", "glob", "grep", "web_search", "web_fetch", "recall")
# Never offered to a sub-agent: no sub-agents of sub-agents, and only the main agent
# presents plans
CHILD_EXCLUDED_TOOLS = ("task", "exit_plan_mode")

STOP_NOW = "Stop now. Report what you found and what is unfinished."
ASK_FOR_REPORT = (
    "Write your report now: what you found or did, with file:line references, and what is"
    " unfinished. Your reply is all the main agent will see."
)
NOT_RUN = "Not run: you have no steps left. Write your report now, without tools."


@dataclass
class AgentType:
    """A kind of sub-agent the task tool can start."""

    name: str
    # When to use it, for the task tool's description
    description: str
    # Added to the sub-agent's system prompt
    prompt: str
    # Tool names it may use; None means every tool the parent has except task
    tools: tuple = None
    # Only read-only tools: allowed in plan mode
    read_only: bool = False
    # main, weak or a model name; None means the parent's model
    model: str = None
    # built-in, project or user, and the file it came from
    source: str = "built-in"
    path: Path = None
    # Whether the sub-agent gets the repo map
    repo_map: bool = False

    def allows(self, name, coder=None):
        """Whether a sub-agent of this type may use tool name. coder is the sub-agent, to
        look up MCP tools."""
        if name in CHILD_EXCLUDED_TOOLS:
            return False
        if self.read_only and name not in READ_ONLY_TOOLS and not is_read_only_mcp(coder, name):
            return False
        return self.tools is None or name in self.tools


BUILTIN_TYPES = {
    t.name: t
    for t in [
        AgentType(
            "explore",
            (
                "Read-only: searches the code broadly (files, symbols, callers, config) and"
                " reports findings with file:line references. Use it for questions that need"
                " many files, instead of reading them all yourself."
            ),
            EXPLORE_PROMPT,
            read_only=True,
        ),
        AgentType(
            "general",
            (
                "Every tool you have except task: research, edits and commands. Use it for a"
                " self-contained piece of work with several steps."
            ),
            GENERAL_PROMPT,
        ),
    ]
}


def agent_types(coder=None):
    """{name: AgentType} for the agent types a task can use."""
    return dict(BUILTIN_TYPES)


def is_read_only_mcp(coder, name):
    """Whether name is an MCP tool marked read-only: by its server, or by the user in
    ~/.loom/mcp-readonly.json. Running it still goes through the permissions."""
    mcp = getattr(coder, "mcp", None)
    found = mcp.find(name) if mcp and name.startswith("mcp__") else None
    if not found:
        return False
    from loom.mcp import mget
    from loom.permissions import load_mcp_readonly_rules, mcp_action_matches_rule

    server, tool = found
    if mget(tool.get("annotations"), "readOnlyHint") is True:
        return True
    action = agent_tools.Action(
        "mcp", name[len("mcp__") :], True, "", None, extra=dict(server=server, tool=tool)
    )
    return any(mcp_action_matches_rule(action, rule) for rule in load_mcp_readonly_rules())


def resolve_model(coder, name):
    """The model for a task: main (the parent's), weak, or a model named in the config.
    Raises ToolError for another name."""
    main = coder.main_model
    weak = main.weak_model or main
    name = (name or "").strip()
    if not name or name in ("main", main.name):
        return main
    if name in ("weak", weak.name):
        return weak
    allowed = list(coder.subagent_settings.get("models") or [])
    if name not in allowed:
        choices = ", ".join(["main", "weak"] + allowed)
        raise ToolError(f"tasks can't use the model {name!r}; use one of: {choices}")
    cache = coder.__dict__.setdefault("subagent_models", {})
    if name not in cache:
        from loom.models import Model

        cache[name] = Model(name, weak_model=weak.name)
    return cache[name]


def format_seconds(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def format_cost(cost):
    from loom.orchestrator import format_cost

    return format_cost(cost)


class Task:
    """One task: a sub-agent doing a job for the agent that started it (the parent)."""

    def __init__(self, parent, number, description, prompt, agent_type, model):
        self.parent = parent
        self.number = number
        self.description = description
        self.prompt = prompt
        self.agent_type = agent_type
        self.model = model
        self.child = None
        # running, done, incomplete, denied, interrupted or failed
        self.status = "pending"
        self.started = None
        self.seconds = 0.0
        self.report = ""
        self.error = None

    @property
    def label(self):
        """How questions and output name the sub-agent, like explore: auth flow."""
        return f"{self.agent_type.name}: {self.description}"

    @property
    def tool_uses(self):
        return self.child.tool_uses if self.child else 0

    @property
    def tokens(self):
        if not self.child:
            return 0
        return self.child.total_tokens_sent + self.child.total_tokens_received

    @property
    def cost(self):
        return self.child.total_cost if self.child else 0.0

    def make_child(self, io):
        """The sub-agent: a fresh conversation that shares the parent's permissions, MCP
        connections and hooks."""
        from loom.coders import Coder
        from loom.coders.subagent_coder import SubAgentCoder
        from loom.sessions import Session

        parent = self.parent
        map_tokens = 0
        if self.agent_type.repo_map and parent.repo_map:
            map_tokens = parent.repo_map.max_map_tokens
        return Coder.create(
            main_model=self.model,
            from_coder=parent,
            coder_class=SubAgentCoder,
            edit_format="agent",
            summarize_from_coder=False,
            io=io,
            parent=parent,
            task=self,
            fnames=[],
            read_only_fnames=[],
            done_messages=[],
            cur_messages=[],
            session=Session(),
            map_tokens=map_tokens,
            # Its replies aren't shown, and it never runs spinners or commits
            stream=False,
            total_cost=0.0,
            total_tokens_sent=0,
            total_tokens_received=0,
            summarizer=None,
            file_watcher=None,
            restore_chat_history=False,
            num_cache_warming_pings=0,
            auto_test=False,
            checkpoint_steps=False,
        )

    def run(self, io):
        """Run the task with the sub-agent talking through io. Returns the tool's result
        for the parent: the report and a footer. KeyboardInterrupt (Esc, ^C) goes on up
        once the sub-agent has stopped."""
        self.status = "running"
        self.started = time.time()
        interrupted = False
        try:
            self.child = self.make_child(io)
            self.report = self.child.do_task(self.prompt)
            child = self.child
            if child.interrupted:
                self.status = "interrupted"
                interrupted = True
            elif child.failed:
                self.status = "failed"
                self.error = child.failed
            elif child.denied:
                self.status = "denied"
            elif child.incomplete:
                self.status = "incomplete"
            else:
                self.status = "done"
        except KeyboardInterrupt:
            self.status = "interrupted"
            interrupted = True
        except Exception as err:
            self.status = "failed"
            self.error = f"{err.__class__.__name__}: {err}"
        finally:
            self.seconds = time.time() - self.started
            self.merge()

        if interrupted:
            raise KeyboardInterrupt()
        return self.result()

    def merge(self):
        """Give the parent what the sub-agent spent and the files it edited."""
        child = self.child
        if not child:
            return
        self.parent.add_task_usage(
            self.cost, child.total_tokens_sent, child.total_tokens_received, child.edited
        )

    def stats(self, cost=True):
        """Like 12 tool uses · 31k tokens · $0.04 · 40s."""
        parts = [plural(self.tool_uses, "tool use"), f"{format_tokens(self.tokens)} tokens"]
        if cost and (self.cost or self.model.info.get("input_cost_per_token")):
            parts.append(format_cost(self.cost))
        parts.append(format_seconds(self.seconds))
        return " · ".join(parts)

    def summary(self):
        """The outcome, shown under the Task line."""
        stats = self.stats(cost=False)
        return {
            "done": f"Done ({stats})",
            "incomplete": f"Stopped before finishing ({stats})",
            "denied": f"Stopped: you denied an action ({stats})",
            "interrupted": "Interrupted",
        }.get(self.status, f"Failed: {self.error}")

    def result(self):
        """What the parent's model gets back: the report and one footer line."""
        if self.status == "failed":
            body = f"The sub-agent failed: {self.error}"
            if self.report:
                body += f"\n\nWhat it reported before that:\n{self.report}"
        elif self.status == "denied":
            body = (
                "The user denied one of the sub-agent's actions, so it stopped. Stop too and"
                " wait for the user's instructions."
            )
            if self.report:
                body += f"\n\nWhat it reported:\n{self.report}"
        else:
            body = self.report or "(The sub-agent gave no report.)"
        body = truncate(body.strip(), MAX_REPORT_CHARS)
        return f"{body}\n\n[task: {self.stats()}]"


def fallback_report(messages, why):
    """For a sub-agent that stopped without a report: its last tool results, trimmed and
    marked as incomplete."""
    calls = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            calls[call["id"]] = call["function"]
    results = [msg for msg in messages if msg["role"] == "tool"][-MAX_FALLBACK_RESULTS:]
    parts = [f"(Incomplete: the sub-agent {why} without writing a report.)"]
    if results:
        parts[0] += " Its last tool results:"
    for msg in results:
        function = calls.get(msg["tool_call_id"]) or dict(name="tool", arguments="")
        content = str(msg.get("content") or "")
        if len(content) > MAX_FALLBACK_RESULT_CHARS:
            content = truncate(content, MAX_FALLBACK_RESULT_CHARS)
        shown = " ".join(str(function.get("arguments") or "").split())[:200]
        parts.append(f"## {function.get('name')} {shown}\n{content}")
    return "\n\n".join(parts)

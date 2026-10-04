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
- plan: read-only. Returns a step-by-step implementation plan with the files to change.

Custom agents are Markdown files, in Claude Code's format:

    .loom/agents/reviewer.md      the project's (commit it to share it)
    ~/.loom/agents/reviewer.md    yours, in every project

    ---
    name: reviewer
    description: Reviews a change for bugs. Use it after making a change.
    tools: read_file, grep, glob, bash
    model: main
    ---
    You are a code reviewer...

description says when to use it, which the task tool shows the model. tools is a comma
list of loom's tool names (or Claude Code's: Read, Grep, Glob, LS, Edit, Write, Bash,
WebFetch, WebSearch, TodoWrite) and MCP tools (mcp__SERVER__TOOL, mcp__SERVER for all of a
server's); without it the agent gets every tool except task. model is main (the default),
weak or a model allowed with --subagent-model. The text after the front matter is added
to the agent's system prompt. A project agent overrides a personal one with the same
name, and the built-in agents can't be overridden. The project's agents come with the
repo, so loom asks before first using each one, and remembers "always" in
~/.loom/agents-approvals.json until the file changes.

In plan mode only read-only agent types may run: explore, plan and custom agents whose
tools are all read-only.

When a sub-agent finishes, the SubagentStop hooks run (loom/hooks.py), and one can keep
it going by blocking.

The child doesn't commit or checkpoint: the files it edits join the parent's, so the
parent's commit at the end of the request and its /rewind checkpoint cover them. What it
spends is added to the parent's tokens and cost.
"""

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from loom import tools as agent_tools
from loom.coders.subagent_prompts import EXPLORE_PROMPT, GENERAL_PROMPT, PLAN_PROMPT
from loom.custom_commands import FRONT_MATTER_RE, first_line
from loom.tools import ToolError, plural, truncate
from loom.utils import format_tokens

DEFAULT_AGENT = "general"
# How many tasks of one reply run at once, unless --max-parallel-tasks says otherwise
MAX_PARALLEL = 4
# Steps a sub-agent may take, unless --subagent-max-steps says otherwise
MAX_STEPS = 40
# A sub-agent compacts its conversation as if the model's context window held at most this
# many tokens, so each of its steps stays cheap however large the window is
CONTEXT_TOKENS = 64_000
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

# How many times SubagentStop hooks may send a finished sub-agent back to work
MAX_STOP_BLOCKS = 3

PROJECT_DIR = ".loom/agents"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Claude Code's names for loom's tools, so agent files written for it work
CLAUDE_TOOL_NAMES = dict(
    Read="read_file",
    Write="write_file",
    Edit="edit_file",
    MultiEdit="edit_file",
    Bash="bash",
    Grep="grep",
    Glob="glob",
    LS="list_dir",
    WebFetch="web_fetch",
    WebSearch="web_search",
    TodoWrite="todo_write",
)


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
    # The agent file's sha256, which approving a project agent is for
    hash: str = ""

    def allows(self, name, coder=None):
        """Whether a sub-agent of this type may use tool name. coder is the sub-agent, to
        look up MCP tools."""
        if name in CHILD_EXCLUDED_TOOLS:
            return False
        if self.read_only and name not in READ_ONLY_TOOLS and not is_read_only_mcp(coder, name):
            return False
        if self.tools is None:
            return True
        return any(
            fnmatchcase(name, pattern) or fnmatchcase(name, pattern + "__*")
            for pattern in self.tools
        )

    def where(self, root=None):
        """Where it comes from: built-in, or its file relative to the project or ~."""
        if not self.path:
            return self.source
        path = Path(self.path)
        for base, prefix in ((root, ""), (Path.home(), "~/")):
            if base:
                try:
                    return prefix + path.resolve().relative_to(Path(base).resolve()).as_posix()
                except ValueError:
                    pass
        return str(path)


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
        AgentType(
            "plan",
            (
                "Read-only: investigates and returns a step-by-step implementation plan with"
                " the files to change and how to verify it. Use it to plan a change before"
                " making it."
            ),
            PLAN_PROMPT,
            read_only=True,
        ),
    ]
}


class AgentFileError(Exception):
    pass


def user_dir():
    return Path.home() / ".loom" / "agents"


def agent_dirs(root):
    """The directories holding agent files, later ones overriding earlier ones."""
    dirs = [(user_dir(), "user")]
    if root:
        dirs.append((Path(root) / PROJECT_DIR, "project"))
    return dirs


def agent_files(root):
    """(path, "user" or "project") for every agent file, in the order they override."""
    res = []
    for directory, source in agent_dirs(root):
        if directory.is_dir():
            res += [(path, source) for path in sorted(directory.glob("*.md")) if path.is_file()]
    return res


def parse_tools(value):
    """loom's tool names (or MCP patterns) for an agent file's tools, and the problems."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        raise AgentFileError("tools should be a comma list of tool names")
    tools, problems = [], []
    for item in items:
        item = item.strip()
        if not item:
            continue
        name = CLAUDE_TOOL_NAMES.get(item, item)
        if name in ("task", "Task") or name in CHILD_EXCLUDED_TOOLS:
            problems.append(f"{item} isn't available to sub-agents")
        elif name in agent_tools.ALL_TOOLS or name.startswith("mcp__"):
            if name not in tools:
                tools.append(name)
        else:
            problems.append(f"unknown tool {item!r}")
    return tuple(tools), problems


def load_agent_file(path, source):
    """The AgentType in an agent file, and the problems with it that don't stop it
    loading. Raises AgentFileError for a file that can't be used."""
    path = Path(path)
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise AgentFileError(f"Unable to read {path}: {err}")

    meta = {}
    match = FRONT_MATTER_RE.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as err:
            raise AgentFileError(f"{path} has invalid front matter: {err}")
        if not isinstance(meta, dict):
            raise AgentFileError(f"{path}: the front matter should be key: value lines")
        text = text[match.end() :]
    prompt = text.strip()
    if not prompt:
        raise AgentFileError(f"{path} has no prompt after its front matter")

    name = str(meta.get("name") or path.stem).strip()
    if not NAME_RE.match(name):
        raise AgentFileError(f"{path}: {name!r} isn't a valid agent name (letters, digits, - or _)")
    description = " ".join(str(meta.get("description") or "").split()) or first_line(prompt)

    problems = []
    tools = None
    if meta.get("tools") not in (None, "", "*"):
        tools, problems = parse_tools(meta["tools"])
    model = str(meta.get("model") or "").strip()
    if model in ("", "inherit"):
        model = None

    agent_type = AgentType(
        name,
        description,
        prompt,
        tools=tools,
        read_only=tools is not None and all(tool in READ_ONLY_TOOLS for tool in tools),
        model=model,
        source=source,
        path=path,
        hash=hashlib.sha256(data).hexdigest(),
    )
    return agent_type, [f"{path}: {problem}" for problem in problems]


class AgentTypes:
    """The agent types a coder's tasks can use: the built-in ones and those in agent
    files, reloaded when the files change."""

    def __init__(self, io, root):
        self.io = io
        self.root = root
        self.signature = None
        self.custom = {}
        self.problems = []
        # Project agents approved for this session: {path: hash}
        self.approved = {}

    def signature_now(self):
        res = []
        for path, source in agent_files(self.root):
            try:
                stat = path.stat()
            except OSError:
                continue
            res.append((str(path), source, stat.st_mtime_ns, stat.st_size))
        return tuple(res)

    def refresh(self):
        signature = self.signature_now()
        if signature == self.signature:
            return
        self.signature = signature
        old_problems = self.problems
        self.custom, self.problems = {}, []
        for path, source in agent_files(self.root):
            try:
                agent_type, problems = load_agent_file(path, source)
            except AgentFileError as err:
                self.problems.append(str(err))
                continue
            self.problems += problems
            if agent_type.name in BUILTIN_TYPES:
                self.problems.append(
                    f"{path}: the built-in {agent_type.name} agent can't be overridden; rename it"
                )
                continue
            # A project agent overrides a personal one
            self.custom[agent_type.name] = agent_type
        for problem in self.problems:
            if problem not in old_problems:
                self.io.tool_warning(problem)

    def all(self):
        self.refresh()
        return {**BUILTIN_TYPES, **self.custom}

    def is_approved(self, agent_type):
        if agent_type.source != "project":
            return True
        key = str(Path(agent_type.path).resolve())
        return agent_type.hash in (self.approved.get(key), load_approvals().get(key))

    def approve(self, agent_type, io):
        """Whether a project agent may run: approved before, or now by the user."""
        if self.is_approved(agent_type):
            return True
        where = agent_type.where(self.root)
        tools = ", ".join(agent_type.tools) if agent_type.tools is not None else "all"
        subject = [
            f"description: {agent_type.description}",
            f"tools: {tools}",
            f"model: {agent_type.model or 'main'}",
        ] + agent_type.prompt.splitlines()[:8]
        answer = io.permission_ask(
            f"Use the {agent_type.name} agent from this project's {where}?",
            subject="\n".join(subject),
            always="trust it in this project until it changes",
            explicit_yes_required=True,
        )
        if answer not in ("yes", "always", "bypass"):
            return False
        key = str(Path(agent_type.path).resolve())
        self.approved[key] = agent_type.hash
        if answer == "always":
            save_approval(key, agent_type.hash, io)
        return True


def approvals_file():
    return Path.home() / ".loom" / "agents-approvals.json"


def load_approvals():
    """The project agents the user said to always trust: {path: hash of the file}."""
    try:
        data = json.loads(approvals_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_approval(key, value, io=None):
    data = load_approvals()
    data[key] = value
    try:
        path = approvals_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as err:
        if io:
            io.tool_warning(f"Unable to save the approval to {approvals_file()}: {err}")


def registry(coder):
    """The coder's AgentTypes, made the first time it's needed."""
    res = getattr(coder, "agent_registry", None)
    if res is None:
        res = AgentTypes(coder.io, coder.root)
        coder.agent_registry = res
    return res


def agent_types(coder=None):
    """{name: AgentType} for the agent types a task can use."""
    if coder is None:
        return dict(BUILTIN_TYPES)
    return registry(coder).all()


AGENT_TEMPLATE = """---
name: {name}
description: When the main agent should use this agent, in a sentence or two.
# The tools it may use, like: read_file, list_dir, glob, grep, edit_file, write_file, bash
# (or Claude Code's Read, LS, Glob, Grep, Edit, Write, Bash). Leave it out for every tool.
tools: read_file, list_dir, glob, grep
# main (the default), weak, or a model allowed with --subagent-model
model: main
---
You are the {name} agent. Say what its job is, how it should go about it, and what its
report should contain.
"""


def new_agent_file(root, name):
    """Write a starting .loom/agents/NAME.md. Returns its path. Raises AgentFileError."""
    if not NAME_RE.match(name or ""):
        raise AgentFileError(f"{name!r} isn't a valid agent name: use letters, digits, - or _")
    if name in BUILTIN_TYPES:
        raise AgentFileError(f"{name} is a built-in agent; choose another name")
    path = Path(root) / PROJECT_DIR / f"{name}.md"
    if path.exists():
        raise AgentFileError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AGENT_TEMPLATE.format(name=name), encoding="utf-8")
    return path


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
        # pending, running, done, incomplete, denied, interrupted (Esc), stopped (by its
        # own stop button, in the web UI) or failed
        self.status = "pending"
        # The thread it runs on, whose commands stopping it kills
        self.thread = None
        self.started = None
        self.seconds = 0.0
        self.report = ""
        self.error = None
        # The sub-agent's io, which keeps its transcript
        self.io = None

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
        self.thread = threading.get_ident()
        self.io = io
        self.changed()
        interrupted = False
        try:
            self.child = self.child or self.make_child(io)
            self.report = self.child.do_task(self.prompt)
            child = self.child
            if child.stopped:
                self.status = "stopped"
            elif child.interrupted:
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
            if self.child and self.child.stopped:
                self.status = "stopped"
            else:
                self.status = "interrupted"
                interrupted = True
        except Exception as err:
            self.status = "failed"
            self.error = f"{err.__class__.__name__}: {err}"
        finally:
            self.thread = None
            self.seconds = time.time() - self.started
            self.merge()
            self.changed()

        if interrupted:
            raise KeyboardInterrupt()
        return self.result()

    def changed(self):
        """Show that it started or finished."""
        view = getattr(self.io, "view", None)
        if view is not None:
            view.changed()

    def cancel(self):
        """Esc while tasks run at once: stop the sub-agent as soon as it can, or keep it
        from starting."""
        if self.child:
            self.child.cancel()
        if self.status == "pending":
            self.status = "interrupted"

    def stop(self):
        """Stop just this task, for its stop button: the sub-agent stops as soon as it can,
        its command is killed, and the parent carries on with what it reported. Returns
        whether it was running."""
        if self.status not in ("pending", "running"):
            return False
        if self.child:
            self.child.stop()
        if self.status == "pending":
            self.status = "stopped"
            self.changed()
        thread = self.thread
        if thread is not None:
            agent_tools.kill_running([thread])
        return True

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
            "stopped": f"Stopped by you ({stats})",
            "interrupted": "Interrupted",
        }.get(self.status, f"Failed: {self.error}")

    def record(self):
        """The task as the session saves it: what it was, how it went, its transcript and
        its conversation."""
        child = self.child
        return dict(
            number=self.number,
            description=self.description,
            agent=self.agent_type.name,
            model=self.model.name,
            status=self.status,
            error=self.error,
            prompt=self.prompt,
            report=self.report,
            started=(
                datetime.fromtimestamp(self.started).isoformat(timespec="seconds")
                if self.started
                else None
            ),
            seconds=round(self.seconds, 1),
            tool_uses=self.tool_uses,
            tokens_sent=child.total_tokens_sent if child else 0,
            tokens_received=child.total_tokens_received if child else 0,
            cost=round(self.cost, 6),
            edited=sorted(child.edited) if child else [],
            events=list(self.io.transcript) if self.io else [],
            messages=(child.done_messages + child.cur_messages) if child else [],
        )

    def result(self):
        """What the parent's model gets back: the report and one footer line."""
        if self.status == "failed":
            body = f"The sub-agent failed: {self.error}"
            if self.report:
                body += f"\n\nWhat it reported before that:\n{self.report}"
        elif self.status == "stopped":
            body = (
                "The user stopped this sub-agent before it finished. Carry on without it, and"
                " don't start it again unless they ask."
            )
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


def run_parallel(parent, tasks, workers=MAX_PARALLEL):
    """Run tasks at once, each sub-agent on a thread of its own, at most workers at a time.
    Their questions come here, to the main thread, which answers them one at a time.
    Returns {task number: result}. Esc or ^C stops them all, waits for them to stop, and
    goes on up."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import wait as wait_for

    from loom.subagent_io import BoardAsker, SubAgentIO
    from loom.workers import Asks, stop_workers

    asks = Asks()
    board = parent.io.task_board(verbose=parent.verbose, headers=True)
    ios = {}
    for task in tasks:
        parent.register_task(task)
        ios[task.number] = io = SubAgentIO(parent.io, task, board.view(task), asks=asks)
        # Made here, so only running them happens on the threads
        task.io = io
        task.child = task.make_child(io)
    threads = {}

    def work(task):
        if task.status != "pending":
            # Stopped before it started
            return task.result() if task.status == "stopped" else None
        threads[task.number] = threading.get_ident()
        try:
            return task.run(ios[task.number])
        except KeyboardInterrupt:
            return None

    results = {}
    asker = BoardAsker(parent.io, board)
    board.update()
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="loom-task") as pool:
            futures = {pool.submit(work, task): task for task in tasks}
            try:
                while not all(future.done() for future in futures):
                    asks.serve(asker)
                    # Esc in the web UI, when it couldn't interrupt this thread
                    parent.io.poll_cancel()
            except KeyboardInterrupt:
                for task in tasks:
                    task.cancel()
                stop_workers(asks, [], list(threads.values()))
                wait_for(futures)
                raise
            for future, task in futures.items():
                try:
                    results[task.number] = future.result()
                except Exception as err:
                    task.status = "failed"
                    task.error = f"{err.__class__.__name__}: {err}"
                    results[task.number] = task.result()
    finally:
        board.close()
        for task in tasks:
            parent.save_task(task)
    return results


def describe_record(record):
    """One line about a saved task, like explore · done · 12 tool uses · 31k tokens · 40s."""
    tokens = (record.get("tokens_sent") or 0) + (record.get("tokens_received") or 0)
    parts = [
        record.get("agent") or "?",
        record.get("status") or "?",
        plural(record.get("tool_uses") or 0, "tool use"),
        f"{format_tokens(tokens)} tokens",
    ]
    if record.get("cost"):
        parts.append(format_cost(record["cost"]))
    parts.append(format_seconds(record.get("seconds") or 0))
    if record.get("model"):
        parts.append(record["model"])
    return " · ".join(parts)


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

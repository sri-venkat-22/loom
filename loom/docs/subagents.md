# Sub-agents

The agent can hand a focused job to a **sub-agent**: a separate agent with a fresh
context of its own. The sub-agent works with its own tools, then returns only a short
report, so the main agent's context stays small. Several sub-agents started in one reply
run at once.

```
agent> Use parallel explore tasks to map how loom's permissions, hooks and MCP client fit together, then summarize.
I'll launch three explore tasks in parallel — one on permissions, one on hooks, one on the MCP client — plus a
fourth on how they're wired into the agent's tool-call flow.

● Task(Map permissions system)
  ⎿  Done (23 tool uses · 241k tokens · 41s)
● Task(Map hooks system)
  ⎿  Done (39 tool uses · 244k tokens · 45s)
● Task(Map MCP client system)
  ⎿  Done (12 tool uses · 129k tokens · 27s)
● Task(Map tool-call integration flow)
  ⎿  Done (20 tool uses · 346k tokens · 49s)
The reports agree with each other; the fourth one stopped early, so let me verify the key integration point in
agent_coder.py and the main.py wiring myself before summarizing.
● Read(loom/coders/agent_coder.py)
  ⎿  Read 70 lines (560-629 of 1170)
● Read(loom/main.py)
  ⎿  Read 110 lines (1170-1279 of 1639)
Verified: the flow in agent_coder.py:571-619 and the wiring in main.py:1181-1271 match the reports.
...
Tokens: 993k sent, 18k cache write, 30k cache hit, 18k received.
Cost: $1.20 message, $1.20 session. Including 4 tasks: 945k sent, 15k received.
```

While they run, each task has a block that shows its latest tool call, and how many
others it has made:

```
● Task(Explore permissions subsystem)
  ⎿  Grep("def decide")
     … +11 more tool uses
● Task(Explore hooks subsystem)
  ⎿  Read(loom/hooks.py)
     … +6 more tool uses
```

The agent decides when to delegate. Its system prompt tells it to use the explore agent
for broad searches across many files instead of reading them all itself, to launch
independent tasks in one reply so they run in parallel, to give each task a
self-contained prompt, not to delegate trivial reads or edits, and to check a report's key
claims before relying on them. You can also just ask for it, as above.

## The task tool

The agent starts a sub-agent with the `task` tool:

| Argument | What it is |
|---|---|
| `description` | 3 to 5 words, shown in the Task line. |
| `prompt` | The whole task. The sub-agent sees nothing else of the conversation. |
| `agent` | The agent type, `general` by default. |
| `model` | `main` (the default), `weak`, or a model allowed with `--subagent-model`. |

What comes back is the sub-agent's final reply, its report, cut to 10,000 characters
(keeping the start and the end), with one footer line:

```
[task: 14 tool uses · 38k tokens · $0.04 · 52s]
```

## Agent types

| Type | Tools | Use |
|---|---|---|
| `explore` | read-only: `read_file`, `list_dir`, `glob`, `grep`, `web_search`, `web_fetch`, and MCP tools marked read-only | Searching the code broadly. It reports what it found with `file:line` references, and never edits. |
| `plan` | read-only, like explore | A step-by-step implementation plan with the files to change and how to verify it. |
| `general` | every tool the main agent has, except `task` | A self-contained piece of work with several steps: research, edits and commands. |

`/agents` lists them, with your own.

### Your own agents

An agent is a Markdown file, in Claude Code's format:

- `.loom/agents/NAME.md` in the project (commit it to share it);
- `~/.loom/agents/NAME.md` for every project.

```markdown
---
name: reviewer
description: Reviews a change for bugs and missing tests. Use it after making a change.
tools: Read, Grep, Glob, Bash
model: weak
---
You are a careful code reviewer. Read the diff with `git diff`, then the code around
each change. Report real bugs and missing tests with file:line references, most serious
first. Don't comment on style.
```

- `description` says when to use it. The task tool lists it for the model to choose.
- `tools` is a comma list of loom's tool names (`read_file`, `list_dir`, `glob`, `grep`,
  `edit_file`, `write_file`, `bash`, `todo_write`, `web_search`, `web_fetch`) or Claude
  Code's (`Read`, `LS`, `Glob`, `Grep`, `Edit`, `Write`, `Bash`, `WebFetch`, `WebSearch`,
  `TodoWrite`), and MCP tools: `mcp__SERVER__TOOL`, or `mcp__SERVER` for all of a
  server's. Leave it out for every tool except `task`.
- `model` is `main` (or `inherit`, the default), `weak`, or a model you allow with
  `--subagent-model`.
- The text after the front matter is added to the agent's system prompt.

`/agents new NAME` writes a starting `.loom/agents/NAME.md` for you to edit. A project
agent overrides one of yours with the same name, and the built-in `explore`, `general` and
`plan` can't be overridden: a file with one of their names is ignored, with a warning.

The project's agents come with the repo, so loom asks before using each one for the first
time, showing its description, tools, model and prompt:

```
Use the reviewer agent from this project's .loom/agents/reviewer.md? (Y)es/(N)o/(A)lways: trust it in this project until it changes [Yes]:
```

**Always** remembers it in `~/.loom/agents-approvals.json` until the file changes. **No**
refuses the task, and the agent stops to wait for you. `--yes-always` doesn't approve a
project agent.

## What a sub-agent gets

A sub-agent starts with nothing of the main agent's conversation: its system prompt, its
agent type's prompt, your [LOOM.md](agent.md#project-memory-loommd) and the task's
prompt. It gets no files added to the chat and no repo map.

It **shares** the main agent's [permissions](agent.md#permissions) (the mode, the allow
rules and your "always" answers), its [MCP](mcp.md) connections, its [hooks](hooks.md),
Esc, and the web tools setting. It has its **own** to-do list, compaction and step
limit.

- **No sub-agents of sub-agents**: a sub-agent never gets the `task` tool.
- **Its step limit** is 40 steps (`--subagent-max-steps`). `--subagent-budget DOLLARS`
  stops one once it has spent that much; it's checked between steps, so a step can go
  over it. At either limit, loom tells it: "Stop now. Report what you found and what is
  unfinished." If it still writes no report, its last tool results come back instead,
  marked as incomplete. A sub-agent that ends without a report is asked for one once.
- **Its context** is compacted as if the model's window held 64k tokens, however large it
  really is. Every step resends the whole conversation, so this keeps each step cheap.
- **Edits**: a sub-agent doesn't commit or take checkpoints. The files it edits join the
  main agent's, so loom's commit at the end of the request, `/undo` and the
  [/rewind](sessions.md#rewind) checkpoint before the request cover them.
- **Costs**: what it spends is added to the request's tokens and cost, and the usage line
  says how much of it was the tasks', like `Including 4 tasks: 945k sent, 15k received.`
  in the run above.

## Permissions and safety

A sub-agent's tool calls go through the same permissions and hooks as the main agent's.
Its questions say which sub-agent asks:

```
● Task(Run a check)
  ⎿  Bash(python -m pytest -q)
[general: Run a check] Run this command? (Y)es/(N)o/(A)lways: always allow this command (saved to .loom.permissions.json)/(B)ypass permissions: stop asking for the rest of this session [Yes]:
```

If you deny an action, the sub-agent stops, and so does the main agent, to wait for you.

- **Plan mode**: only read-only agent types run (`explore`, `plan`, and your agents whose
  tools are all read-only). `general` is refused, and a sub-agent's own edits and commands
  are refused as usual.
- **Esc** stops every running sub-agent: their commands are killed, each returns
  "Interrupted by the user", and the main agent stops as it does for any tool.
- **Hooks**: [PreToolUse and PostToolUse hooks](hooks.md) see the task tool as `Task`, with
  Claude Code's `subagent_type` in `tool_input`, so a hook can log or block tasks. A
  sub-agent's own tool calls carry `agent_id` and `agent_type`. **SubagentStop** hooks run
  when a sub-agent has written its report; see
  [hooks.md](hooks.md#subagentstop-when-a-sub-agent-finishes).

## Parallel tasks

The tasks of one reply run at once, each sub-agent on its own thread, at most 4 at a time
(`--max-parallel-tasks`). The reply's other tool calls run in order, and the tasks run
together where the first of them is. Their results go back to the model in the order it
called them.

Their questions come one at a time: a sub-agent waits while another's question is on the
screen, and the block of the sub-agent asking shows above its question. Output that
arrives meanwhile waits until you've answered. When the output isn't a terminal (or with
`--verbose`) every tool call is printed as it comes, labelled with its task, like
`[explore: Map hooks] Read(loom/hooks.py)`.

## Transcripts: /tasks

Each task's transcript is saved with the conversation, in
`.loom.sessions/<session>/tasks/<n>.json`: its prompt, every tool call and result, its
questions and your answers, its report, its numbers and its whole conversation with the
model. `/tasks` lists this conversation's tasks, and `/tasks N` shows one:

```
agent> /tasks
  #  Status       Agent    Tokens   Time  Description
  1  done         explore    241k    40s  Map permissions system
  2  done         explore    244k    45s  Map hooks system
  3  done         explore    129k    27s  Map MCP client system
  4  done         explore    346k    49s  Map tool-call integration flow

Show one's transcript with /tasks N.
agent> /tasks 3
● Task 3(Map MCP client system)
  ⎿  explore · done · 12 tool uses · 129k tokens · $0.15 · 27s · bedrock/global.moonshotai.kimi-k3

> You are exploring a Python CLI codebase called "loom" (an AI coding agent). Your job is to
map the MCP (Model Context Protocol) client so it can be explained to someone. ...
● Read(loom/mcp.py)
  ⎿  Read 955 lines
● Grep("McpManager|McpError|mcp" in loom)
  ⎿  Found 166 matches in 14 files
● Read(loom/tools.py)
  ⎿  Read 113 lines (1540-1652 of 1652)
...
```

`--verbose` shows every tool call of a running task, instead of the latest few.

## What it costs

Delegating keeps the main agent's context small, but the sub-agents' work isn't free:
each one reads the code itself, and every step it takes resends its own conversation. The
run above, against loom's own source with Kimi K3 on Bedrock, next to the same question
asked with `--no-subagents`:

| | With 4 explore tasks | Without sub-agents |
|---|---|---|
| Tokens sent by the main agent | 48k | 415k (368k of them cache hits) |
| Tokens sent in all | 993k | 415k |
| Cost | $1.20 | $0.33 |
| Time | 76s | 46s |

So delegate when the main conversation needs to stay small, like a long task whose context
would otherwise fill up, or work that splits into independent parts. For a one-off
question in a fresh conversation, the agent reading the files itself can be cheaper. Use
`model: weak` for search-heavy agents, `--subagent-budget` to cap what each may spend, or
`--no-subagents` to turn them off.

## In the web UI

In [`loom --web`](web.md), a task is a Task card: its description and agent type, a
spinner while it runs, its latest tool calls as live cards nested in it (with the rest a
click away), and its outcome when it's done. Click the card for the whole transcript: the
prompt, every tool call and the report. **Stop** stops just that task: the main agent
carries on with what it reported. Tasks that run at once show side by side.

## In a project

The [phase agents](project.md) of a `/project` have sub-agents too. Idea Check, Planning,
Design and Testing may start read-only ones (`explore` and `plan`) to research the
project without filling their own context. Building may start any. A sub-agent of a
phase agent gets only tools the phase agent has, and can't write what it may not, like the
acceptance tests test-driven Building locks. What sub-agents spend counts in their phase's
metrics. Parallel builders don't start sub-agents.

## Options

| Option | What it does |
|---|---|
| `--no-subagents` | Take away the task tool. |
| `--subagent-max-steps STEPS` | How many steps a sub-agent may take before it must report (40). |
| `--subagent-budget DOLLARS` | Stop a sub-agent and have it report once it has spent this much. |
| `--subagent-model MODEL` | A model the agent may run a task on, besides the main and weak models (repeatable). |
| `--max-parallel-tasks N` | How many tasks of one reply run at once (4). |

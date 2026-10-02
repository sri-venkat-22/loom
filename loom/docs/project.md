# Projects: from idea to launch

`/project` takes a software idea through six phases, each run by its own agent:

| # | Phase | The agent's tools | It produces |
|---|-------|-------------------|-------------|
| 1 | Idea Check | read, search, write its document | `loom-project/1-idea-report.md`, with a verdict: GO, GO WITH CHANGES or NO-GO |
| 2 | Planning | read, search, write its document | `loom-project/2-prd.md`, the product requirements (PRD) |
| 3 | Design | read, search, write its document | `loom-project/3-architecture.md`, the architecture document |
| 4 | Building | every tool of the [agent](agent.md), MCP servers' included | the code, and `loom-project/4-build-summary.md` |
| 5 | Testing | read, search, run commands, write test files | `loom-project/5-test-report.md`, with a result: PASS or FAIL |
| 6 | Launch | read, search, run commands, write deployment files | deployment files (Dockerfile, CI workflow, ...) and `loom-project/6-deployment.md` |

The Building agent is loom's coding agent with a brief to build what the PRD and the
architecture describe. The other agents have their own prompt and fewer tools: an
agent's tool calls outside its set, and writes outside the files it may write, are
refused. Testing may only write test files (`tests/`, `test_*`, `*_test.*`,
`*.test.*`, `*.spec.*`, ...), so it reports bugs instead of fixing them, and Launch may
only write deployment files (`Dockerfile`, `docker-compose.yml`, `.github/workflows/`,
`Procfile`, `README.md`, ...).

Each agent starts a fresh conversation with the idea and the documents of the earlier
phases it needs. Permissions work as usual: you approve edits and commands (writing the
phase's own document needs no approval, since you review it next), and loom commits each
phase's work to git.

## Running a project

```
agent> /project new A web app where students swap used textbooks
```

The orchestrator runs the phases in order. After each one it names the document to read
and asks you to approve it:

```
Planning is ready for review: loom-project/2-prd.md (84 lines).
Approve the PRD and move on to Design? (Y)es/(N)o [Yes]:
```

Answer no and loom asks what the agent should change, then runs the phase again with
your feedback. Leave the feedback empty to stop; the project waits for you.

Two verdicts change the flow:

- **NO-GO** from the Idea Check agent stops the project unless you tell loom to carry on
  anyway.
- **FAIL** from the Testing agent offers to send the test report back to the Building
  agent, which fixes the code; then Testing runs again. That happens at most 3 times
  in a row before loom stops and asks you.

## Commands

| Command | What it does |
|---------|--------------|
| `/project new IDEA` | Start a project and run its phases |
| `/project run` | Carry on from the phase the project is in |
| `/project` or `/project status` | Show each phase's status |
| `/project approve` | Approve the document waiting for review |
| `/project redo [FEEDBACK]` | Run the current phase again, with feedback |
| `/project back PHASE [FEEDBACK]` | Go back to an earlier phase (like `planning` or `2`); the phases after it run again |
| `/project reset` | Forget the project's progress; its documents and code stay |

`/project status` shows where the project is:

```
Project: A web app where students swap used textbooks
  ✓ 1. Idea Check  idea report       approved, GO
  ✓ 2. Planning    PRD               approved
▶ ◆ 3. Design      architecture doc  waiting for review
  ○ 4. Building    code              pending
  ○ 5. Testing     test report       pending
  ○ 6. Launch      deployment        pending
```

## The state machine

The project's state is saved in `.loom/project.json`, so you can quit loom and carry on
later with `/project run`. Each phase moves through these states:

```
pending ──start──▶ running ──finish──▶ review ──approve──▶ approved
   ▲                  │                   │
   └──────stop────────┘                   │
   └─────────────reject (feedback)────────┘
```

Only the current phase (the first one not approved) can move, so the phases always run
in order. `back` returns the project to an earlier phase and resets the phases after it
to pending. Their documents stay on disk, and when they run again their agents revise
them. The project is complete when all six phases are approved. The file also keeps a
history of every step.

## Tips

- `/project` doesn't work in plan mode, because the agents need to write files. Use
  accept-edits mode (`/permissions accept-edits` or Shift-Tab) to approve the agents'
  edits up front.
- To let the Testing and Launch agents run your test command without asking, add an allow
  rule, like `/permissions allow bash(pytest*)`.
- With `--yes-always` loom approves each document by itself, but it still stops on
  NO-GO and on a failing test report it can't fix. Commands need allow rules, as usual.
- The documents are plain Markdown. You can edit them yourself before you approve them,
  and the later agents work from your version.

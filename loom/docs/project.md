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
architecture describe. Every agent also has two [project memory](#shared-memory) tools:
`recall` and `record_decision`. The other agents have their own prompt and fewer tools: an
agent's tool calls outside its set, and writes outside the files it may write, are
refused. Testing may only write test files (`tests/`, `test_*`, `*_test.*`,
`*.test.*`, `*.spec.*`, ...), so it reports bugs instead of fixing them, and Launch may
only write deployment files (`Dockerfile`, `docker-compose.yml`, `.github/workflows/`,
`Procfile`, `README.md`, ...).

Each agent starts a fresh conversation with the idea and the documents of the earlier
phases it needs. loom only reads documents that are files in the project, up to 1 MB: one
that's a symlink out of the project is ignored, with a warning. Permissions work as usual: you approve edits and commands (writing the
phase's own document needs no approval, since you review it next), and loom commits each
phase's work to git.

## Running a project

```
agent> /project new A web app where students swap used textbooks
```

The orchestrator runs the phases in order, and stops at an approval checkpoint after
each one.

## Approval checkpoints

At each checkpoint loom names the document to read, lists the decisions the agent
recorded, and asks what to do with it:

```
Planning is ready for review: loom-project/2-prd.md (84 lines).
The Planning agent recorded these decisions:
  - Leave payments out of the MVP: students settle in person
Approve the PRD and move on to Design? (A)pprove/(E)dit/(R)eject [Approve]:
```

- **Approve** moves on to the next phase, whose agent works from the document.
- **Edit** opens the document in your editor (`--editor`, or `$VISUAL`/`$EDITOR`). loom
  saves and commits your changes, then asks again. Edits count: the later agents work
  from your version, a changed verdict line (say FAIL to PASS) changes what happens
  next, and the edit is recorded as a decision.
- **Reject** asks what the agent should change, then runs the phase again with your
  feedback. Leave the feedback empty to stop; the project waits for you.

For Building, the output is the code: read the build summary and the commits, edit the
code yourself if you like, and approve, edit the summary, or reject with feedback.

Two verdicts change the question:

- **NO-GO** from the Idea Check agent stops the project unless you choose
  `approve anyway`. The default is to reject.
- **FAIL** from the Testing agent: the default, `send back`, sends the test report back
  to the Building agent, which fixes the code; then Testing runs again. You can also
  edit the report, approve it anyway or reject it. After 3 rounds of fixes in a row the
  default becomes reject, so a project can't loop forever.

Every checkpoint's outcome (approved, edited, rejected with feedback, sent back,
overridden) is recorded in the project's [shared memory](#shared-memory), with who
decided: `founder`, or `loom (--yes-always)` when loom answered for you.

## The test command

The architecture document's Testing approach names the one command that runs the whole
test suite, on a line loom reads:

```
**Test command:** `pytest -q`
```

The Design checkpoint shows the command, or warns when there's no line loom can read;
edit the document to fix it before you approve it. loom runs the command itself when it
needs to know whether the tests pass, the way the agents' bash tool runs commands: from
the project root, without input, with a 10-minute timeout, and only when your permissions
allow it (an allow rule like `/permissions allow bash(pytest*)` saves the question). It
keeps the end of the output, where test runners sum up the failures.

## Test-driven Building

Start a project with `/project new --tdd IDEA` (or from a template with `tdd: true`) and
its Building has two steps:

1. **Acceptance tests.** The Testing agent, in spec mode, turns the PRD's acceptance
   criteria into tests before anything is built, using the interfaces the architecture
   defines. It may only write test files, and it writes
   `loom-project/4a-acceptance-tests.md`, which maps each requirement to its tests. loom
   runs the test command once (the tests should fail: nothing is built yet), then stops
   at a checkpoint: approve the tests, edit the plan (or the tests, in your editor), or
   reject them with feedback for the Testing agent. Once you approve, the tests are
   **locked**: loom commits them and keeps their hashes.
2. **The build loop.** The Building agent builds, then loom runs the test command. While
   tests fail, the end of their output goes back into the same agent conversation, and
   it tries again: up to `--build-retries` more times (3 by default), or until the run
   has cost `--build-budget` dollars.

The Building agent has to make the tests pass, not change them:

- its edits to a locked test are refused;
- after each try, loom checks the locked tests' hashes, and puts back any that changed
  some other way (a command, say), and tells the agent;
- the Building checkpoint shows each try (✗ ✗ ✓), and flags test changes that skip a
  test or mark it as expected to fail, like `@pytest.mark.skip`, `xfail`, `it.skip` or
  `t.Skip()`.

If the tests still fail when the tries run out, Building still goes to its checkpoint,
which says so; the Testing phase afterwards, and sending its failures back to Building,
work as usual. Going back to an earlier phase has the acceptance tests written again.
Without a test command (the template's, or the architecture document's `**Test
command:**` line) or a git repo, Building runs the usual way, with a warning.

## Parallel builders

The architecture document ends with a work plan: how Building splits into work packages,
as a YAML block that loom reads.

```yaml
- id: core
  title: Conversion functions
  owns: ["src/app/convert.py"]
  tests: ["tests/test_convert.py"]
  depends_on: []
  acceptance: [FR-1, FR-2]
  test_command: "python -m pytest -q tests/test_convert.py"
- id: cli
  title: The command line
  owns: ["src/app/cli.py"]
  tests: ["tests/test_cli.py"]
  depends_on: [core]
  acceptance: [FR-3]
```

The Design checkpoint checks the plan and shows it, like `Work plan: 3 packages in 2
waves: core → cli; web`, or what's wrong with it: two packages whose globs could match the
same file, a dependency on a package that isn't there, a cycle of dependencies, or more
packages than three per builder. A plan with problems isn't used.

With a valid plan of more than one package, and more than one builder (`--build-workers`,
3 by default, or `/project workers N` for the project), Building runs as:

1. **The scaffold.** One agent, in the project itself, writes what the packages share:
   the manifests and configuration, the layout, and the interfaces between the packages.
   loom commits it.
2. **Waves of builders.** The packages that depend on nothing go first, then the ones that
   depend on them. Each package's builder works in its own git worktree,
   `.loom/worktrees/ID`, on its own branch, `loom/build/ID`, made from the project's latest
   commit, and up to N build at once. A builder may only write its package's files (its
   `owns` and `tests`) and its notes, `loom-project/packages/ID.md`. Its questions, like
   whether it may run a command, wait for you in the main conversation, marked with its
   package, like `[cli] Run this command?`.
3. **Merging.** Each built package is merged into the project, one at a time. When a merge
   conflicts, the Integrator agent resolves it; if it can't, the merge is undone and
   Building stops.
4. **Integration.** A last agent runs the whole test command, fixes what's broken between
   the packages and writes the build summary.

^C (or Esc in the web UI) stops every builder, and the commands they run. Building keeps
each package's status, branch, cost and attempts, so `/project run` carries on where it
stopped, building only the packages that aren't merged yet. `/project status` lists
them. When the Testing agent's report sends the project back, only the packages that own
the files it names are built again, then the Integration agent runs again. Going back to
an earlier phase starts parallel Building over; `/project reset` and `/project back`
remove the builders' worktrees and branches.

With [test-driven Building](#test-driven-building), the acceptance tests are written and
locked first; a package with its own `test_command` runs the build loop on its tests,
and the Integration agent runs it on the whole suite.

Building runs as one agent, as it always has, when there's no valid plan, the plan has
one package, there's one builder, or loom can't merge safely: no git repo, uncommitted
changes, or `--no-auto-commits`.

## Commands

| Command | What it does |
|---------|--------------|
| `/project new [--template NAME] [--tdd] IDEA` | Start a project, from a [template](#project-templates) if given, [test-driven](#test-driven-building) with `--tdd`, and run its phases |
| `/project templates` | List the project templates |
| `/project workers [N]` | How many [builders](#parallel-builders) build the work packages at once |
| `/project run` | Carry on from the phase the project is in |
| `/project` or `/project status` | Show each phase's status |
| `/project approve` | Approve the document waiting for review |
| `/project edit` | Edit the document waiting for review in your editor |
| `/project reject FEEDBACK` | Reject the document waiting for review, and run its phase again with your feedback |
| `/project redo [FEEDBACK]` | Run the current phase again, with feedback |
| `/project back PHASE [FEEDBACK]` | Go back to an earlier phase (like `planning` or `2`); the phases after it run again |
| `/project decide DECISION` | Record a decision of yours, like `Use PostgreSQL`, for the agents to come |
| `/project decisions` | List every decision: yours, the agents' and the checkpoints' |
| `/project recall QUERY` | Search the project memory, as the agents' `recall` tool does |
| `/project memory` | Show where the memory is and how it searches |
| `/project report [md\|html\|docx\|pdf] [--out FILE] [--summary]` | Write the [project report](#the-project-report) |
| `/project reset` | Forget the project's progress, decisions and memory; its documents and code stay |

`/project status` shows where the project is:

```
Project: A web app where students swap used textbooks
  ✓ 1. Idea Check  idea report       approved, GO        1 run, 1m 12s, $0.03
  ✓ 2. Planning    PRD               approved            2 runs, 4m 40s, $0.11
▶ ◆ 3. Design      architecture doc  waiting for review  1 run, 3m 02s, $0.09
  ○ 4. Building    code              pending
  ○ 5. Testing     test report       pending
  ○ 6. Launch      deployment        pending

Total: 4 runs, 8m 54s, $0.23, 61k tokens sent, 9.8k received, 4 commits
```

loom logs every run of a phase's agent: when it started and finished, what it cost, the
tokens it sent and received, the commit HEAD was at before and after it, how many commits
it made, its verdict, and how it ended (`done`, `stopped` or `failed`). The status adds
the runs up per phase and for the whole project. A model loom has no prices for costs
$0.00.

## Project templates

A template gives a project of a known kind a head start: decisions made up front, a
brief for each phase's agent, a skeleton of files to start the code from, the test
command, and checks that run after the phases. Start a project from one with:

```
agent> /project new --template fastapi-react A web app where students swap used textbooks
```

`/project templates` lists them. loom comes with three:

| Template | What it is | Test command |
|----------|------------|--------------|
| `python-cli` | A Python command-line tool: argparse, src layout, pyproject.toml, pytest | `python -m pytest -q` |
| `fastapi-react` | A FastAPI backend in `api/` and a React + Vite + TypeScript frontend in `web/`, served from one container | `python -m pytest -q api && npm --prefix web test -- --run` |
| `ml-pipeline` | A reproducible scikit-learn pipeline: config-driven load, features, train and evaluate stages | `python -m pytest -q` |

A template is a folder with a `template.yml` and, optionally, a `skeleton/` folder:

```yaml
name: fastapi-react
description: FastAPI backend + React (Vite) frontend
tdd: false
test_command: "python -m pytest -q api && npm --prefix web test -- --run"
decisions: ["Backend is FastAPI on Python 3.11; frontend is React + Vite + TypeScript"]
briefs: {design: "...", building: "...", testing: "...", launch: "..."}
checks: {building: ["npm --prefix web run build"], launch: ["docker build -t app ."]}
writable: {launch: ["fly.toml"]}
```

loom looks for templates in three places, and a later one replaces an earlier one with
the same name: its own (`loom/templates/`), yours (`~/.loom/templates/`) and the
project's (`.loom/templates/`). How a project uses its template:

- **decisions** are recorded in the [shared memory](#shared-memory) from `template`,
  so every agent sees them.
- **briefs** are added to the brief of the phase's agent: `design`, `building` and so on.
- **writable** adds files the phase's agent may write, like `fly.toml` for Launch.
- **test_command** is the project's [test command](#the-test-command), in place of the
  architecture document's.
- **skeleton/** is copied into the project and committed when Building first starts.
  Files the project already has are kept, except that a skeleton's `gitignore` (stored
  without its dot) adds its missing lines to the project's `.gitignore`.
- **checks** run after the phase, like a build or a linter. The checkpoint shows which
  passed, and the results go in the shared memory for the next agents. A failed check
  warns but doesn't stop the project.
- **tdd: true** makes Building test-driven, like `--tdd`.

The checks of a project's own template come with the repo, so they're shell commands
you haven't seen: loom asks before it first runs them, and an "always" answer is kept
with the approved [project hooks](hooks.md#project-hooks-need-your-approval), in
`~/.loom/hooks-approvals.json`, until the checks change. `--yes-always` doesn't approve
them. loom's own templates' checks and yours run without asking.

The project remembers its template's name and a hash of its contents. If the template
changes later, loom says so and uses it as it is now.

## The project report

`/project report` writes everything the project did into one document, to read or to
hand in:

- a cover with the idea, when the project started and finished, the models, the
  template, loom's version, and the totals: runs, time, cost, tokens and commits
- a timeline of the phases: status, verdict, runs, time, cost, and how many rounds of
  fixes a failing test report sent Building
- each phase's document, its headings one level down
- every decision, grouped by phase and kind (decisions, approvals, edits, changes asked
  for, send-backs and overrides)
- the files each phase's runs changed, as `git diff --stat`
- an appendix with the project's history

```
agent> /project report docx
Wrote the project report to loom-project/report.docx
```

The format is `md` (the default), `html`, `docx` or `pdf`, and `--out FILE` writes it
somewhere else (its extension picks the format if you don't name one). `--summary` asks
the weak model for a one-page executive summary, which goes after the cover.

Markdown needs nothing else. The other formats use [pandoc](https://pandoc.org/installing.html):

- **HTML** without pandoc is still written, more plainly.
- **Word** is styled for an academic report (Cambria text, navy headings, each section
  on a new page, A4 with page numbers), from `loom/resources/report-reference.docx`.
  Word fills in the table of contents when it opens the file. Without pandoc, loom
  writes HTML instead and says so.
- **PDF** also needs a PDF engine for pandoc: [typst](https://typst.app) (loom's
  first choice), tectonic, a LaTeX like xelatex, weasyprint or wkhtmltopdf. Without
  one, loom writes Word and HTML instead and says so.

In [loom --web](web.md), `GET /api/project/report?format=docx` downloads the report.

## Shared memory

The project keeps a shared memory in `.loom/memory/`, which git ignores:

- **A database** (`project.db`, SQLite) of the project's state (see
  [the state machine](#the-state-machine)) and its decisions. A decision is one of yours
  (`/project decide`), one an agent recorded with `record_decision` (a technology
  choice, a scope cut, a trade-off, with its reason), or a checkpoint's outcome.
- **A vector store** of the idea, every phase document (split at its headings) and every
  decision, for looking up earlier context. Documents are indexed when their phase
  finishes and again when you edit or approve them.

The agents use it in three ways:

1. Each agent's task message lists the decisions so far, except plain approvals, so
   the Launch agent knows what the founder told the Design agent.
2. The orchestrator looks up passages of earlier documents that aren't in the agent's
   message but matter for its phase. The Launch agent, for one, gets the PRD's
   requirements on hosting, privacy and scale, though not the whole PRD.
3. Every agent can search the memory with `recall`, and record its decisions with
   `record_decision`. Neither touches project files, so they never need approval.

Search uses [ChromaDB](https://www.trychroma.com/) with its default embedding model
(all-MiniLM-L6-v2, an 80 MB download the first time) when the `memory` extra is
installed; `/project memory` offers to install it. Without it, or when the model can't
be downloaded, loom searches a BM25 keyword index in the database instead, so the
memory works with no extra packages. Set `LOOM_MEMORY_STORE=keyword` to always use the
keyword index. The database is the source of truth: the ChromaDB collection is rebuilt
from it when it's missing or out of date.

## The state machine

The project's state is saved in the shared memory's database, so you can quit loom and
carry on later with `/project run`. A project from an older loom, in
`.loom/project.json`, moves into the database the first time loom reads it. Each phase
moves through these states:

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
- The documents are plain Markdown. Besides **Edit** at the checkpoint, you can change
  them in any editor before you approve them; the later agents work from your version.
- Record the constraints you already know before the phases that need them run, like
  `/project decide Deploy on Fly.io; we already pay for it`.

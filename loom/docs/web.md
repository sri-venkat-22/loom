# The web UI

`loom --web` runs loom as usual and lets you chat with it in your browser, in an interface
modeled on Claude Code: your sessions on the left, the chat in the middle with tool calls
as one-line cards and edits as inline diffs, and the code changes on the right. It is the same loom underneath, with the same agent,
permissions, sessions, `/commands` and `/project` orchestrator, and your settings and
`.loom.conf.yml` apply as usual.

![loom --web](images/web-ui.png)

## Starting it

```bash
cd /path/to/your/project
loom --web
```

Loom starts in the terminal, serves the web UI on http://127.0.0.1:8765/ and opens it in
your browser. Press ^C twice in the terminal to stop loom.

- `--port PORT` serves it on another port.
- `--no-browser` doesn't open the browser; open the address yourself.

The server listens on 127.0.0.1 only, so only this computer can reach it. It refuses
pages from other websites and other host names, so a website you visit can't drive loom
through your browser.

The web server comes from loom's `web` extra. If it's missing, `loom --web` offers to
install it.

## Chatting

Type in the input at the bottom. ⌘↵ or ↵ sends, and Shift-↵ starts a new line. A new
conversation offers a few things to ask. Replies stream in as the model writes them,
formatted as Markdown with highlighted code blocks; the model's thinking folds into a
line you can open.

- **Tool calls** show as cards: ● while running, ✓ when done, ✗ when they failed, with the
  result underneath. Click a card for its arguments and full output. Reads and searches in
  a row fold into one **Explored** card.
- **Edits** show their diff on the card, with line numbers and syntax highlighting. When
  loom asks before an edit, **Accept** (`y`) or **Reject** (`n`) it; `a` always accepts
  edits like it, and `b` stops loom asking for the rest of the session. A command waiting
  for approval shows on its card the same way, with **Allow** and **Deny**.
- **Questions** loom asks show inline, with a key for each answer.
- **Esc**, or the stop button, stops loom's current work, like in the terminal.

Under the input:

- **+** opens the file tree, to add files to the chat.
- The **permission mode** (Ask before edits, Accept edits, Plan mode) switches with a
  click or Shift-Tab, like in the terminal. It shows Bypass permissions after you answer
  `b` to an approval.
- The **model** button lists the model aliases to switch the main model to (`/model`), or
  takes any model name, and shows the weak model.

The line under that shows whether loom is working and the tokens used this session. The
header shows the project, its git branch, the `/project` phases and the lines changed
since loom started; click **Changes** to see them.

## Sessions

The sidebar on the left lists this project's saved conversations, newest first, with
the current one marked. Click one to continue it (loom's `/resume`): the chat shows that
conversation, and the agent picks it up where it left off. **New session** starts a new
one (`/clear`); the old one stays saved. ⌘\, or the panel button at the left of the
header, shows and hides the sidebar. Its foot shows the project folder and whether the
page is connected to loom.

## Commands

Type `/` for loom's commands, with arrows to choose, Tab to complete and ↵ to run; ⌘K
opens the list from anywhere. Every loom command works, and the web UI adds:

- `/phase` shows the project's phases, and `/phase PHASE` goes back to one.
- `/dashboard` opens the [project dashboard](#the-project-dashboard).
- `/files`, `/memory [QUERY]` and `/terminal` open the side pane.
- `/theme [dark|light]` switches between the dark and light themes. Dark is the default,
  and the browser remembers your choice.

## Projects

With a `/project` (see [project.md](project.md)), the header shows its phases, idea —
planning — design — building — testing — launch, approved ones ticked and the current one
ringed. It follows the run as it goes. Click a phase to open its card in the
[project dashboard](#the-project-dashboard).

Each phase's checkpoint shows inline, with its verdict and the start of the document;
click the document's name to open it in the side pane:

- **Approve** (`a`) moves on to the next phase.
- **Edit** (`e`) opens the document in the side pane's editor; ⌘S saves it, and loom
  commits your edit and asks again.
- **Request changes** (`r`) asks what the phase's agent should change, and runs it again.
- **Send back** (`s`) and **Approve anyway** show when loom offers them, after failing
  tests or a NO-GO.
- **Abort** (Esc) stops the run; `/project run` picks it up again.

## The side pane

⌘B, or the panel button at the right of the header, shows a pane beside the chat (the
project dashboard first, when there's a project). On a narrow window it hides the
sessions sidebar to make room.

- **Project**: the [project dashboard](#the-project-dashboard).
- **Changes**: every file that differs from the commit loom started at, with its diff and
  how many lines it adds and removes, even though loom commits each change as it goes.
  **Since main** compares with where the branch left `main` (or `master`) instead.
  Files you haven't added to git show as new. Click a file's name to view it.
- **Files**: the project's files. A ticked box marks a file in the chat; click it to
  `/add` or `/drop` the file. Click a name to view the file.
- **Terminal**: the output of `/run` as it runs.
- **Memory**: search the project's shared memory, with ChromaDB when the `memory` extra is
  installed or by keyword otherwise. With no search, it lists the project's decisions.

## The project dashboard

The side pane's **Project** tab shows a `/project` from start to finish, and follows it
as it runs:

- At the top, the idea, the [template](project.md#project-templates) it started from,
  and the project's totals: time, cost, runs and commits, then a bar for each phase's
  cost (its time, when the models have no prices), and the decisions of no phase, like
  the template's. **Download report** downloads the
  [project report](project.md#the-project-report) as Markdown, Word or PDF.
- Then a timeline with a card for each phase: its status (approved, waiting for review,
  running, pending, or to redo after you went back), its verdict, how many times its
  agent ran, for how long and at what cost, the rounds of fixes failing tests sent back
  to Building, the template's checks after its last run, passed or failed, and the
  decisions made in it, which unfold. With [test-driven Building](project.md#test-driven-building),
  Building's card also shows its acceptance tests and each try of the build loop
  (✗ ✗ ✓), and **Tests** opens the acceptance test plan.
- **Document** opens the phase's document in a read-only editor.
- **Diff** shows what a run of the phase changed: the diff of its commits, from where
  HEAD was when the run started to where it ended. Pick another run from the list.
- **Go back here** asks first, then runs `/project back PHASE`: that phase and the ones
  after it run again. It shows for the phases before the current one.

loom reads the dashboard from the project's database, read-only, and pushes it to the
page when it changes. `GET /api/project/timeline` returns the same, and
`GET /api/project/phase/PHASE/diff?run=N` a run's diff.

## Developing the frontend

The frontend is in `loom/web/frontend` (Vite, React, TypeScript, Tailwind and Zustand),
and the server in `loom/web/backend` (FastAPI). `npm run build` puts the frontend in
`loom/web/static`, which loom serves. The build is committed, so installing loom from
GitHub needs no Node.js: after changing the frontend, build it and commit
`loom/web/static` with your change. CI checks that it matches the source.

```bash
cd loom/web/frontend
npm install
npm run build      # or, while working on it:
npm run dev        # http://127.0.0.1:5173/, talking to a `loom --web --no-browser`
```

The messages between the two are described in `loom/web/backend/protocol.py`.

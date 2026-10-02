# The web UI

`loom --web` runs loom as usual and lets you chat with it in your browser, in an interface
modeled on Claude Code: one monospace column, tool calls as one-line cards, edits as
inline diffs, and a status line. It is the same loom underneath, with the same agent,
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

Type in the input at the bottom. ⌘↵ or ↵ sends, and Shift-↵ starts a new line. Replies
stream in as the model writes them; the model's thinking folds into a line you can open.

- **Tool calls** show as cards: ● while running, ✓ when done, ✗ when they failed, with the
  result underneath. Click a card for its arguments and full output.
- **Edits** show their diff on the card, with line numbers and syntax highlighting. When
  loom asks before an edit, press `y` to accept it or `n` to reject it (`a` always accepts
  edits this session). A command waiting for approval shows on its card the same way.
- **Questions** loom asks show inline, with a key for each answer.
- **Esc** stops loom's current work, like in the terminal.

The status line shows whether loom is working, the model, the tokens used this session,
the project folder, and the `/project` phase.

## Commands

Type `/` for loom's commands, with arrows to choose, Tab to complete and ↵ to run; ⌘K
opens the list from anywhere. Every loom command works, and the web UI adds:

- `/phase` shows the project's phases, and `/phase PHASE` goes back to one.
- `/files`, `/memory [QUERY]` and `/terminal` open the side pane.
- `/theme [dark|light]` switches between the dark and light themes. Dark is the default,
  and the browser remembers your choice.

## Projects

With a `/project` (see [project.md](project.md)), the header shows its phases, idea ›
planning › design › building › testing › launch, with the current one in bold. It
follows the run as it goes. Click a phase to go back to it; loom asks first.

Each phase's checkpoint shows inline, with the document and its verdict:

- **Approve** (`a`) moves on to the next phase.
- **Edit** (`e`) opens the document in the side pane's editor; ⌘S saves it, and loom
  commits your edit and asks again.
- **Request changes** (`r`) asks what the phase's agent should change, and runs it again.
- **Send back** (`s`) and **Approve anyway** show when loom offers them, after failing
  tests or a NO-GO.
- **Abort** (Esc) stops the run; `/project run` picks it up again.

## The side pane

⌘B, or ⌘B in the header, shows a pane beside the chat:

- **files**: the project's files. `[x]` marks the files in the chat; click it to `/add` or
  `/drop` a file. Click a name to view the file.
- **memory**: search the project's shared memory, with ChromaDB when the `memory` extra is
  installed or by keyword otherwise. With no search, it lists the project's decisions.
- **terminal**: the output of `/run` as it runs.
- **model**: the main and weak models, and switching them.

## Developing the frontend

The frontend is in `loom/web/frontend` (Vite, React, TypeScript, Tailwind and Zustand),
and the server in `loom/web/backend` (FastAPI). `npm run build` puts the frontend in
`loom/web/static`, which loom serves; until it's built, loom shows a page saying so.

```bash
cd loom/web/frontend
npm install
npm run build      # or, while working on it:
npm run dev        # http://127.0.0.1:5173/, talking to a `loom --web --no-browser`
```

The messages between the two are described in `loom/web/backend/protocol.py`.

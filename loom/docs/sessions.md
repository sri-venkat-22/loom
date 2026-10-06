# Sessions

Loom saves each conversation after every request, so you can close it and pick up where
you left off:

```bash
loom --continue
```

continues the most recent conversation in the project. The model gets the whole
conversation back, tool calls and all; the files that were in the chat are added again,
and the agent's to-do list is restored. Loom shows the end of the conversation to remind
you where it was:

```
Continuing conversation 20260927-101530-3fa2 from 2026-09-27 10:31.

> fix the failing test
● Bash(python -m pytest -q)
● Read(mathutils/stats.py)
● Update(mathutils/stats.py)
The mean divided by len(values) + 1; it now divides by len(values) and the tests pass.
```

## Saved conversations

Conversations are saved as JSON in `.loom.sessions/` in the project root (the root of
the git repo, or the current directory without one). Like loom's other `.loom*` files,
it's git-ignored. Loom keeps the 100 most recent; older ones are deleted as new ones are
saved.

- `/sessions` lists the saved conversations, newest first, with their ids.
- `/resume ID` switches to one of them in the chat, and `loom --resume ID` starts loom
  with it. The start of an id is enough if only one id starts that way.
- `/clear` and `/reset` start a new conversation. The old one stays saved, so
  `/resume` can go back to it.
- `--no-sessions` turns saving off.

The model you run loom with is used, whatever model the conversation was saved with.
If you resume an agent conversation in another chat mode, its tool calls are shown to the
model as plain text.

## Rewind

Before every request the agent handles, loom takes a checkpoint of the project's files
and remembers where the conversation was. `/rewind` goes back to any of them:

```
> /rewind
Checkpoints, newest first:
  1  10:42  add a test for subtract  · 1 changed, 1 created since
  2  10:31  fix the failing test  · 2 changed, 1 created since
Rewind to before which one? (its number, or Enter to cancel): 1
Rewind to before: add a test for subtract
What should go back? (C)ode and conversation/c(O)de only/co(N)versation only/c(A)ncel [Code and conversation]: c
Rewinding the code to before: add a test for subtract
1 file changed, 1 created since:
  M mathutils/stats.py
  - tests/test_subtract.py  (created since: deleted)
Restore these files? (Y)es/(N)o [Yes]: y
Restored 2 files.
Commit 4d1c2e9 Rewind to before: add a test for subtract
Rewound the conversation to before: add a test for subtract
The request is back in the input, to change and send again.
```

- **Code and conversation** puts both back: the files as they were, and the
  conversation and to-do list as they were before that request. The request goes back in
  the input, so you can change it and send it again.
- **Code only** restores the files and keeps the conversation.
- **Conversation only** cuts the conversation back and leaves the files as they are.

`/rewind N` skips the list (1 is the newest checkpoint), and `/rewind N code`,
`conversation` or `both` skips the question too. Pressing **Esc** twice at an empty
prompt opens `/rewind`. Rewinding the conversation also drops the later checkpoints,
whose requests are gone from it.

What a checkpoint covers:

- With git, the whole working tree: the agent's edits, the changes its shell commands
  made (files they created, deleted or renamed), and your own uncommitted changes and
  untracked files. Files `.gitignore` ignores, `.git` and loom's own `.loom*` files are
  never snapshotted or touched, and neither is a file a pattern added since ignores.
- Without git (`--no-git`), only the files the agent edited with `edit_file` and
  `write_file`: loom copies each one into `.loom/checkpoints/` before the agent's first
  edit of it since the checkpoint. Changes shell commands made can't be rewound there.

A snapshot doesn't touch your index, `HEAD`, branches, stash or `git status`: loom
writes it with its own index file into a private ref, `refs/loom/checkpoints/<session>`
(so `git log --all` shows it, but `git log` doesn't). Git stores each version of a file
once, so a snapshot only costs the files that changed, and one of an unchanged tree
reuses the previous one. Files come back byte for byte, line endings included, whatever
`core.autocrlf` or `.gitattributes` say. Restoring shows how many files change, are
created and are deleted, and asks first.

The checkpoints are saved with the conversation, the last 100 of them, so they survive
`--resume`. `/project` takes one before each phase's agents run, and with
`--checkpoint-steps` the agent also takes one before each step that edits files or runs
a command, so `/rewind` can undo a single step (those rewind the code only). A rewind
first checkpoints the files as they were, so another `/rewind` undoes it. `/rewind --gc`
drops the checkpoints of sessions that are no longer saved.

### Rewind and git

loom commits the agent's work after each request, so a code rewind makes a **new**
commit, "Rewind to before: ...", when auto-commits are on. It never resets a branch or
rewrites history. A rewind is refused while git is in the middle of a merge, rebase,
cherry-pick or revert.

`/undo` is different: it takes back loom's last commit (only one loom made in this
session, and not pushed yet), moving `HEAD` back. `/rewind` works from snapshots instead:
it can go back several requests at once, restores what shell commands changed and files
loom never committed, rewinds the conversation, and only ever adds commits.

## When the conversation gets long

Every model has a context window: how much conversation it can take in at once. Loom
keeps the conversation within it in two ways.

**Between requests**, when the earlier messages pass a limit (`--max-chat-history-tokens`,
by default a sixteenth of the window, at most 8k tokens), loom summarizes the older ones
in the background with the weak model (`--weak-model`), keeping the most recent ones as
they were.

**During a request**, the agent's steps can fill the window by themselves. Before each
step, loom checks the size of the conversation and compacts it when it passes 80% of the
window; see [agent.md](agent.md#long-tasks). `--no-auto-compact` turns that off.

`/compact` summarizes the chat history right away, which is useful before starting on
something new that doesn't need all the details of the old conversation. Say what to
keep, like `/compact keep the details of the database schema`.

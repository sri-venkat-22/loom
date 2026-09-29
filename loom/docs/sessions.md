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

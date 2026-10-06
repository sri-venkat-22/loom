# Custom commands

Save a prompt you use often as a Markdown file, and run it as a slash command:

```
.loom/commands/review.md          /review, in this project
~/.loom/commands/explain.md       /explain, in every project
.loom/commands/db/migrate.md      /db:migrate
```

```markdown
---
description: Review the uncommitted changes for bugs
argument-hint: [focus area]
---
Review the output of `git diff HEAD` for bugs. Focus on $ARGUMENTS.
List each problem with its file and line, most serious first. Don't edit any files.
```

```
> /review error handling
● Bash(git diff HEAD)
  ⎿  ...
```

Running a command sends its text as your message, to the agent or whatever chat mode
you're in. The file name, without `.md`, is the command's name, and a subdirectory adds
a prefix with a colon. Names can use letters, digits, `-`, `_` and `.`. Like loom's own
commands, a custom one can be shortened to any unique prefix, and `/help` lists them
under "Custom commands".

## Arguments

- `$ARGUMENTS` is everything you type after the command.
- `$1`, `$2`, ... are its words. Quote a phrase to keep it one word:
  `/rename old_name "new name"`. A missing word is left empty.
- If the text uses neither, what you type is added to the end of it.

## Front matter

An optional YAML block at the top of the file sets:

| Key | What it does |
|---|---|
| `description` | Shown by `/help`. Without it, `/help` shows the first line of the prompt. |
| `argument-hint` | Shown before the description, like `[focus area]`. |
| `chat-mode` | Runs the command in this chat mode, the way `/ask` does: `ask`, `agent`, `code`, `architect` or `context`. |

A command with `chat-mode: ask` answers without editing anything, even when you run it
from the agent:

```markdown
---
description: Explain how a part of the code works
chat-mode: ask
---
Explain how $ARGUMENTS works: where it starts, the main steps, and the files involved.
```

## Sharing commands

Project commands in `.loom/commands/` are meant to be committed, so everyone working
on the repo gets them. A project command overrides one of yours with the same name;
neither can replace loom's own commands such as `/help`.

Loom's `.gitignore` check adds `.loom*` followed by `!.loom/`, which ignores loom's
history and caches but not the `.loom/` directory. If your `.gitignore` has an older
`.loom*` line on its own, add `!.loom/` after it.

The agent can't change files in `.loom/` without asking, even in accept-edits mode:
they are [protected](agent.md#protected-files). Commands are re-read each time they run,
so edits apply right away.

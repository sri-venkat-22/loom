# Hooks

Hooks are shell commands that loom runs before or after the agent uses a tool. Use them
to enforce rules the model can't talk its way around, or to do chores after each edit:

- block commands you never want run, like `git push` or `rm -rf`;
- format each file the agent edits;
- run the linter or a quick test after an edit and tell the model what failed;
- log every command the agent runs.

```
agent> tidy up the imports in app.py
● Update(app.py)
  ⎿  Updated app.py with 2 additions and 3 removals
  ⎿  Hook: app.py:14:80: E501 line too long (88 > 79 characters)
● Update(app.py)
  ⎿  Updated app.py with 1 addition and 1 removal
```

## Configuring hooks

Hooks are configured in JSON, in the same format as Claude Code:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [{"type": "command", "command": "python .loom/check_command.py"}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "edit_file|write_file",
        "hooks": [
          {"type": "command", "command": "ruff format \"$LOOM_FILE_PATH\"", "timeout": 30}
        ]
      }
    ]
  }
}
```

Loom reads `~/.loom/hooks.json`, your hooks for every project, then `.loom/hooks.json`
in the project root, hooks that come with the project. The outer `"hooks"` object is
optional, and an entry can give a `command` directly instead of a list of `hooks`.

- **`PreToolUse`** hooks run after the model asks for a tool and before loom asks you
  about it, so they can block the call, or approve it without asking you.
- **`PostToolUse`** hooks run after a tool ran successfully, and can send the model
  feedback about the result.
- **`matcher`** is a regular expression that must match the whole tool name, ignoring
  case: `bash`, `edit_file`, `write_file`, `read_file`, `list_dir`, `glob`, `grep`,
  `todo_write` or `mcp__<server>__<tool>` for [MCP tools](mcp.md). Leave it out, or use
  `""` or `"*"`, to match every tool.
- **`timeout`** is in seconds, 60 by default. A hook that takes longer is stopped, and
  loom warns and carries on.

`/hooks` lists the hooks and where they came from, and the announcements count them,
like `Hooks: 1 PreToolUse, 1 PostToolUse`. Loom re-reads the files before each request,
so edits apply without restarting. `--no-hooks` turns hooks off.

## What a hook gets

A hook runs in the project root. It gets the tool call as JSON on stdin:

```json
{
  "hook_event_name": "PreToolUse",
  "session_id": "20260927-141502-3f2a",
  "cwd": "/home/me/project",
  "permission_mode": "ask",
  "tool_name": "bash",
  "tool_input": {"command": "git push --force"}
}
```

`PostToolUse` hooks also get `tool_response`, the text the tool returned to the model.
These environment variables are set too:

| Variable | Value |
|---|---|
| `LOOM_PROJECT_DIR` | The project root. |
| `LOOM_HOOK_EVENT` | `PreToolUse` or `PostToolUse`. |
| `LOOM_TOOL_NAME` | The tool's name, like `edit_file`. |
| `LOOM_FILE_PATH` | The absolute path of the file, for `read_file`, `edit_file` and `write_file`. |

## What a hook can do

The exit code decides:

| Exit code | PreToolUse | PostToolUse |
|---|---|---|
| 0 | Carry on as usual. | Nothing more. |
| 2 | Block the tool call. The hook's stderr tells the model why. | stderr is sent to the model with the tool's result. |
| anything else | The hook failed: loom shows its stderr as a warning and carries on. | The same. |

A hook that exits with 0 can instead print JSON:

- `{"decision": "block", "reason": "..."}` blocks the call, like exit code 2.
- `{"decision": "allow"}` from a `PreToolUse` hook approves the call without asking
  you, like an [allow rule](agent.md#allow-rules). Plan mode still refuses edits and
  commands, and [protected files](agent.md#protected-files) still ask.

Claude Code's `hookSpecificOutput` with `permissionDecision` (`allow` or `deny`),
`permissionDecisionReason` and `additionalContext` works too.

When several hooks match, they run in order. For `PreToolUse`, the first one that
blocks wins and the rest don't run.

## Examples

Block pushes and forced resets, with `.loom/check_command.py`:

```python
import json
import re
import sys

command = json.load(sys.stdin)["tool_input"].get("command", "")
if re.search(r"\bgit\s+(push|reset\s+--hard)\b", command):
    print("Don't push or reset; leave that to the user.", file=sys.stderr)
    sys.exit(2)
```

Lint each edited Python file and show the model what's wrong:

```json
{
  "PostToolUse": [
    {
      "matcher": "edit_file|write_file",
      "command": "case \"$LOOM_FILE_PATH\" in *.py) flake8 \"$LOOM_FILE_PATH\" >&2 || exit 2;; esac"
    }
  ]
}
```

Log every command the agent runs:

```json
{"PreToolUse": [{"matcher": "bash", "command": "cat >> ~/.loom/commands.log; echo >> ~/.loom/commands.log"}]}
```

## Project hooks need your approval

Hooks run commands on your machine, and `.loom/hooks.json` comes with the repo. So the
first time loom would run a project's hooks, it shows them and asks:

```
Run the hooks in this project's .loom/hooks.json? (Y)es/(N)o/(A)lways: trust them in this project [No]:
```

"Always" is remembered in `~/.loom/hooks-approvals.json` until the hooks change. If you
say no, they don't run this session. Your own hooks in `~/.loom/hooks.json` never ask.

The agent can't edit hook files without asking, whatever the mode or rules: they are
[protected](agent.md#protected-files).

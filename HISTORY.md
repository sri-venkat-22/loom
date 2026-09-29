# Release history

Loom is a fork of [Aider](https://github.com/Aider-AI/aider), taken from Aider's `main` branch
at version 0.86.3.dev. For changes before the fork, see
[Aider's release history](https://github.com/Aider-AI/aider/blob/main/HISTORY.md).

### Loom v0.88.0

- Loom now works as an agent by default when the model supports tool calling. The model
  reads, searches and edits files and runs commands with tools, calling as many as it
  needs per reply and looping until the job is done, so there's no need to `/add` files.
  `--no-agent` or `/code` gives the classic edit formats; `/agent` switches back.
- Permissions for the agent: edits and shell commands ask first, with `ask`,
  `accept-edits` and `plan` (read-only) modes via `--permission-mode` or `/permissions`,
  and allow rules like `bash(pytest*)` via `--allow`, config, or answering "always".
  Edits to `.git/` and loom's config files always ask. `--yes-always` doesn't approve
  shell commands.
- The agent works with thinking models (`--thinking-tokens`): loom sends Claude's signed
  thinking back with its tool calls, which Anthropic requires. Shift-Tab at the prompt
  cycles the permission mode.
- Prompt caching is on by default for the agent, and each step caches the conversation so
  far. Streamed replies are now costed from the provider's reported usage, so cost reports
  are exact and count cache hits across all of a request's steps.
- NVIDIA build support: `nemotron` (Nemotron 3 Ultra) and `nemotron-super` aliases for
  NVIDIA's free hosted models, set up for the agent, and the default when
  `NVIDIA_NIM_API_KEY` is the only key. The `nemotron` alias used to point at OpenRouter.
- API keys can be kept in `~/.loom/credentials.json` as a JSON object of variables.
- `LOOM.md` project memory: instructions in `~/.loom/LOOM.md` and in `LOOM.md` files from
  the repo root down to the current directory are added to every system prompt.
  `--no-project-memory` turns this off.
- The agent shows each tool call compactly, as `● Tool(what)` with its outcome under it,
  and each edit's diff once, with line numbers. Esc interrupts the agent (the model's
  reply, a running command or an MCP call) without counting towards exiting, and text
  typed while it works is kept for the next prompt.
- A live to-do list: the agent plans multi-step work with a new `todo_write` tool, the
  spinner shows the item in progress, and `/todos` shows the list.
- Conversations are saved to `.loom.sessions/` after every request. `loom --continue`
  resumes the latest one, with its files and to-do list; `--resume ID`, `/sessions` and
  `/resume` pick older ones, and `/clear` starts a new one. `--no-sessions` turns this off.
- Long agent requests no longer overflow the context window: near 80% of it, loom
  shortens old tool results and summarizes older steps, and it compacts and retries if
  the model still rejects the request. `--no-auto-compact` turns this off; `/compact`
  summarizes the chat history on demand.
- MCP client: tools from MCP servers (stdio or streamable HTTP) configured in
  `~/.loom/mcp.json`, the project's `.mcp.json` or `--mcp-config` files are given to the
  agent as `mcp__<server>__<tool>`. They ask before running unless allowed with
  `mcp(SERVER)` rules, and servers from a project's `.mcp.json` need approval to start.
  `/mcp` shows the servers and tools.
- Custom slash commands: Markdown files in `.loom/commands/` (shared with the repo) or
  `~/.loom/commands/` run as `/name`, with `$ARGUMENTS` and `$1`, `$2`... filled in from
  what you type, and front matter for a description, argument hint and chat mode.
- Hooks: shell commands in `~/.loom/hooks.json` or the project's `.loom/hooks.json` run
  before (`PreToolUse`) or after (`PostToolUse`) the agent's tool calls, in Claude Code's
  format. They can block a call, approve it, or send the model feedback like lint errors.
  Project hooks need approval; `/hooks` lists them and `--no-hooks` turns them off.
- The `.gitignore` check now adds `!.loom/` after `.loom*`, so `.loom/` with the custom
  commands and hooks can be committed. Files in `.loom/` are protected from the agent.
- When a provider rejects tool calling, the agent switches to the model's edit format and
  resends the request. A model that writes a tool call as text is asked once to use tool
  calling. `scripts/check_agent_models.py` checks which models work in agent mode.
- A stream that fails part way through (like NVIDIA's "Service temporarily overloaded") is
  now retried instead of ending the request.

### Loom v0.87.0

- Renamed the project from aider to loom: package `loom`, CLI command `loom`, config files
  `.loom*`, environment variables `LOOM_*`, and the `--loomignore` flag.
- Model settings key `aider/extra_params` is now `loom/extra_params`.
- Removed Aider's built-in analytics keys. Analytics now stay off unless you configure your own
  PostHog project with `--analytics-posthog-host` and `--analytics-posthog-project-api-key`.
- Added built-in model aliases `nemotron`, `deepseek-flash` and `gemini-3.1`.

# Loom

Loom is AI pair programming in your terminal. You run `loom` inside a git repository
and describe what you want. Loom works as an agent: the model reads and searches your
code, edits files and runs commands like your tests, asking before each edit and
command, until the job is done. Then loom commits the changes to git.

Loom is a fork of [Aider](https://github.com/Aider-AI/aider), used under the Apache-2.0
license. It is developed at https://github.com/sri-venkat-22/loom.

## Install

Loom is installed from GitHub. It is **not** on PyPI: the `loom` package on PyPI is an
unrelated project, so never run `pip install loom`.

```bash
# latest release
pip install "git+https://github.com/sri-venkat-22/loom.git@v0.87.0"

# or the development version from main
pip install git+https://github.com/sri-venkat-22/loom.git
```

Loom needs Python 3.10 or newer and git. Installing into a virtualenv (or with `pipx` or
`uv tool install`) keeps its dependencies separate from your projects.

## Quick start

```bash
cd /path/to/your/project

# Anthropic Claude Sonnet
loom --model sonnet --api-key anthropic=<key>

# OpenAI
loom --model gpt-4o --api-key openai=<key>

# Google Gemini
loom --model gemini-3.1 --api-key gemini=<key>
```

At the `agent>` prompt:

```
agent> add a --verbose flag that logs each request, with a test
```

Loom finds the code, shows each edit and command for you to approve, runs the tests and
commits the change. Type `/undo` to revert it, or `/help <question>` to ask about using
loom. Put project rules the model should always follow in a `LOOM.md` file.

## More docs

- [agent.md](agent.md): the agent, its tools, permissions, and `LOOM.md` project memory
- [project.md](project.md): `/project` takes an idea through six phase agents, from idea check to launch
- [sessions.md](sessions.md): continuing conversations with `--continue`, and long ones
- [mcp.md](mcp.md): giving the agent tools from MCP servers
- [custom-commands.md](custom-commands.md): your own `/commands` from Markdown files
- [hooks.md](hooks.md): shell commands that run before or after the agent's tools
- [usage.md](usage.md): adding files, chat modes, images, web pages, watch mode, voice
- [commands.md](commands.md): every in-chat `/command`
- [models.md](models.md): choosing a model and setting API keys
- [model-aliases.md](model-aliases.md): short model names like `sonnet` and `gemini`
- [config.md](config.md): config files, `.env` files and environment variables
- [options.md](options.md): every command line option
- [git.md](git.md): auto-commits, undo, commit attribution
- [lint-test.md](lint-test.md): running linters and tests after each change
- [troubleshooting.md](troubleshooting.md): common problems and how to report bugs

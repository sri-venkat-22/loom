# Loom

Loom is AI pair programming in your terminal. You run `loom` inside a git repository,
add the files you want to work on, and describe the change you want. Loom sends your
request, the files, and a map of the rest of the repository to an LLM, applies the edits
it returns to your files, and commits them to git.

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

At the `>` prompt:

```
> /add src/app.py
> add a --verbose flag that logs each request
```

Loom edits `src/app.py`, shows the diff, and commits the change. Type `/undo` to revert
it, or `/help <question>` to ask about using loom.

## More docs

- [usage.md](usage.md): adding files, chat modes, images, web pages, watch mode, voice
- [commands.md](commands.md): every in-chat `/command`
- [models.md](models.md): choosing a model and setting API keys
- [model-aliases.md](model-aliases.md): short model names like `sonnet` and `gemini`
- [config.md](config.md): config files, `.env` files and environment variables
- [options.md](options.md): every command line option
- [git.md](git.md): auto-commits, undo, commit attribution
- [lint-test.md](lint-test.md): running linters and tests after each change
- [troubleshooting.md](troubleshooting.md): common problems and how to report bugs

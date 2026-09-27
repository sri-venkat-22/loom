# Release history

Loom is a fork of [Aider](https://github.com/Aider-AI/aider), taken from Aider's `main` branch
at version 0.86.3.dev. For changes before the fork, see
[Aider's release history](https://github.com/Aider-AI/aider/blob/main/HISTORY.md).

### main branch

- Renamed the project from aider to loom: package `loom`, CLI command `loom`, config files
  `.loom*`, environment variables `LOOM_*`, and the `--loomignore` flag.
- Model settings key `aider/extra_params` is now `loom/extra_params`.
- Removed Aider's built-in analytics keys. Analytics now stay off unless you configure your own
  PostHog project with `--analytics-posthog-host` and `--analytics-posthog-project-api-key`.
- Added built-in model aliases `nemotron`, `deepseek-flash` and `gemini-3.1`.

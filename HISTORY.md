# Release history

### main branch

- First loom release: package `loom`, CLI command `loom`, config files `.loom*`,
  environment variables `LOOM_*`, and the `--loomignore` flag.
- The `loom/extra_params` entry in the model settings file applies to every model.
- No built-in analytics keys. Analytics stay off unless you configure your own
  PostHog project with `--analytics-posthog-host` and `--analytics-posthog-project-api-key`.
- Added built-in model aliases `nemotron`, `deepseek-flash` and `gemini-3.1`.

# Configuration

Every option in [options.md](options.md) can be set three ways. From highest to lowest
priority:

1. **Command line**: `loom --model sonnet --no-auto-commits`
2. **Environment variables**: `LOOM_` plus the option name in capitals with `_` for `-`,
   e.g. `LOOM_MODEL=sonnet`, `LOOM_AUTO_COMMITS=false`. These can also go in a `.env` file.
3. **YAML config file** `.loom.conf.yml`:

   ```yaml
   model: sonnet
   auto-commits: false
   read:
     - CONVENTIONS.md
   lint-cmd:
     - "python: ruff check"
   ```

## Where loom looks for files

- **`.loom.conf.yml`**: home directory, git root, current directory. Values from files
  later in that list override earlier ones. `--config <file>` loads only that file.
- **`.env`**: home directory, git root, current directory, then the file given with
  `--env-file`. Later files override earlier ones. Use it for API keys
  (`ANTHROPIC_API_KEY=...`) and `LOOM_*` settings.
- **`.loomignore`**: gitignore-style patterns for files loom should never see or add,
  in the git root (or `--loomignore <file>`).
- **`.loom.model.settings.yml`** and **`.loom.model.metadata.json`**: settings for
  models loom does not know; see [models.md](models.md).

Run `loom --verbose` to see which config and `.env` files were loaded, and `/settings`
in the chat to print the settings in effect.

## Files loom creates

Loom writes chat and input history to `.loom.chat.history.md` and
`.loom.input.history` in the git root (change with `--chat-history-file` and
`--input-history-file`), and caches the repo map in `.loom.tags.cache.*`. When starting
in a repo, loom offers to add `.loom*` to `.gitignore` so these stay out of git.

Per-user caches (update checks, help index) live in `~/.loom/`.

## Conventions files

To make loom follow your team's coding style, write the rules in a markdown file and add
it read-only on every start:

```yaml
# .loom.conf.yml
read: CONVENTIONS.md
```

## Common settings

| Want | Option |
|---|---|
| Don't commit automatically | `--no-auto-commits` |
| Lint and test after each change | `--lint-cmd ...`, `--test-cmd ... --auto-test` |
| Always start in architect mode | `--architect` |
| Terminal with a dark background | `--dark-mode` |
| Don't stream output | `--no-stream` |
| Vi key bindings | `--vim` |
| Answer in another language | `--chat-language Hindi` |
| Desktop notification when loom needs input | `--notifications` |

# Linting and testing

Loom can lint and test the code after every change it makes, and send any errors back
to the model to fix.

## Linting

Auto-linting is on by default (`--no-auto-lint` turns it off). After each edit loom
lints the files it changed:

- With no lint command configured, loom checks that each file parses (using tree-sitter)
  and, for Python, runs flake8 for fatal errors only (syntax errors, undefined names).
- To use your own linter, give a command per language. Loom appends the file name and
  treats a non-zero exit status as errors to fix:

  ```bash
  loom --lint-cmd "python: ruff check" --lint-cmd "javascript: npx eslint"
  ```

  A command without a language prefix is used for every language.

`/lint` lints and fixes the files in the chat, or all modified files if none are in the
chat.

## Testing

Set a test command with `--test-cmd` and add `--auto-test` to run it after every change:

```bash
loom --test-cmd "pytest -q" --auto-test
```

If the command exits with a non-zero status, loom shows the output to the model and
asks it to fix the failures.

`/test` runs the test command (or any command you give it) once and adds the output to
the chat if it fails. `/run <command>` (or `!<command>`) runs any shell command and
offers to add its output to the chat.

## In a config file

```yaml
# .loom.conf.yml
lint-cmd:
  - "python: ruff check"
test-cmd: pytest -q
auto-test: true
```

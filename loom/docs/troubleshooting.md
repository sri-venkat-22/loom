# Troubleshooting

## Loom can't apply the model's edits

Messages like "SearchReplaceNoExactMatch" or "did not conform to the edit format" mean
the model wrote an edit loom couldn't match against your file. Loom shows the error to
the model and asks it to retry. If it keeps failing:

- Use a stronger model. Small and local models often can't follow edit formats reliably.
- `/drop` files that aren't needed. Less context means fewer confused edits.
- Try another edit format: `--edit-format whole` is the most forgiving (the model
  rewrites whole files) and suits smaller files.
- For reasoning models, try architect mode (`--architect`) with a reliable editor model.

## Context window or token limit errors

The model's context window is full. Check usage with `/tokens`, then `/drop` files,
`/clear` the chat history, or lower `--map-tokens`. For local models, make sure loom
knows the real context size (see [models.md](models.md)). A model whose window is
unknown to loom triggers a warning at startup.

## API key or authentication errors

Check the key is set for the provider of the model you are using: run `loom --verbose`
to see which `.env` and config files were loaded, and see [models.md](models.md) for
the variable names. Pass `--api-key provider=<key>` to rule out file problems.

## Rate limits and timeouts

Loom retries rate-limit and temporary server errors with backoff. For slow models
raise `--timeout` (seconds).

## Loom is slow in a large repository

- Start loom in a subdirectory with `--subtree-only`.
- Exclude generated or vendored files with a `.loomignore` file.
- Lower `--map-tokens`, or set it to 0 to disable the repo map.

## Upgrading

Loom checks GitHub for new releases once a day and offers to install them. To upgrade by
hand:

```bash
pip install --upgrade "git+https://github.com/sri-venkat-22/loom.git@vX.Y.Z"
```

Check your version with `loom --version`. Never run `pip install loom` or
`pip install --upgrade loom`: that package on PyPI is an unrelated project.

## Interactive help

`/help <question>` answers questions about loom from these docs. The first time, it
installs its extra dependencies (a local embedding model) and builds a search index,
which takes a minute.

## Reporting a bug

Use `/report` in the chat to open a GitHub issue with your loom version, Python
version and platform filled in, or file one at
https://github.com/sri-venkat-22/loom/issues. Include the model, the exact command you
ran and the full error output.

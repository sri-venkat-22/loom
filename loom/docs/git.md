# Git integration

Loom works best in a git repository. Every change it makes is committed, so you can
always see what it did and undo it with normal git tools.

## Auto-commits

After each set of edits, loom commits the changed files with a message written by the
weak model (see [models.md](models.md)). Turn this off with `--no-auto-commits`; the
edits stay in your working tree for you to review and commit yourself.

If files you are about to have loom edit already have uncommitted changes, loom commits
those first, as a separate commit, so your work and loom's changes stay apart. Turn that
off with `--no-dirty-commits`.

Loom skips git pre-commit hooks by default (`--no-verify`). Use `--git-commit-verify`
to run them.

## Reviewing and undoing

- `/diff` shows the changes made since your last message.
- `/undo` reverts loom's last commit. It only undoes commits loom made in the current
  chat session, and only if they have not been pushed.
- `/rewind` puts the files (and the conversation, if you like) back to before an
  earlier request, from a snapshot loom takes before each one, shell commands' changes
  included. It commits the result as a new commit, so history is never rewritten. See
  [sessions.md](sessions.md#rewind).
- `/commit [message]` commits changes you made yourself outside the chat.
- `/git <args>` runs any git command, e.g. `/git log --oneline -5`.

## Commit attribution

By default loom adds a trailer to its commit messages:

```
Co-authored-by: loom (<model name>) <sri-venkat-22@users.noreply.github.com>
```

and leaves your author and committer names unchanged. Other options:

| Option | Effect |
|---|---|
| `--no-attribute-co-authored-by` | no trailer |
| `--attribute-author` | author name becomes `Your Name (loom)` |
| `--attribute-committer` | committer name becomes `Your Name (loom)` |
| `--attribute-commit-message-author` | prefix messages with `loom: ` when loom wrote the change |
| `--attribute-commit-message-committer` | prefix every message with `loom: ` |

## Other git options

- `--no-git`: work without git (no commits, no `/undo`).
- `--subtree-only`: only consider files under the current directory. Useful in large
  monorepos.
- `--no-gitignore`: don't offer to add `.loom*` to `.gitignore`.
- `--commit`: commit all pending changes with a generated message, then exit.
- `--commit-prompt`: use your own prompt for generating commit messages.
- `--commit-language`: write commit messages in another language.

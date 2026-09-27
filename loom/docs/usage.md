# Using loom

## Starting loom

Run `loom` from inside your project. You can name files to add to the chat right away:

```bash
loom src/app.py tests/test_app.py
```

If the directory is not a git repository, loom offers to create one. Use `--no-git` to
work without git (you lose auto-commits and `/undo`).

## Adding files to the chat

Loom only edits files that are *in the chat*. Add the files that need to change:

- `/add <files or globs>` adds files loom may edit.
- `/read-only <files>` adds files as reference only; loom will not edit them. Good for
  docs, style guides, or code the change must match.
- `/drop <files>` removes files; `/drop` with no arguments removes all of them.
- `/ls` shows which files are in the chat.

Add only what the change needs. Loom also sends a *repository map*: a compact summary
of the classes, functions and signatures in the rest of the repo, so the model knows
what exists without you adding every file. `/map` prints it and `--map-tokens` sets its
size (0 disables it).

If the model needs to see a file you did not add, loom asks for permission to add it.

## Chat modes

Each message goes to the current mode. Switch with `/chat-mode <mode>`, or send one
message in another mode with `/ask ...`, `/code ...`, `/architect ...`, `/context ...`
or `/help ...`. The same commands with no message switch modes.

- **code** (default): asks for changes and applies them to your files.
- **ask**: questions about your code; no files are changed. A common pattern is to plan
  in `/ask`, then say `/code go ahead` (or `/ok`) to make the change.
- **architect**: an architect model proposes the change and an editor model turns the
  proposal into file edits. Set the editor with `--editor-model`. Useful for reasoning
  models that are strong at planning but weak at precise edit formats.
- **context**: loom works out which files the request will need to touch and adds
  them to the chat.
- **help**: questions about loom itself, answered from these docs.

Start in a mode with `--chat-mode <mode>`, or `--architect` for architect mode.

## Images and web pages

- Add an image file (png, jpg, webp, ...) with `/add` to give the model a screenshot or
  diagram. The model must support images.
- `/paste` pastes an image or text from the clipboard.
- `/web <url>` fetches a page, converts it to markdown and adds it to the chat. Pages
  that need JavaScript use Playwright, which loom offers to install the first time
  (`--disable-playwright` turns this off).

## Working from your editor (watch mode)

Start loom with `--watch-files` and keep editing in your own editor. Write a comment
that ends in `AI!` to ask for a change, or `AI?` to ask a question:

```python
# add retry with exponential backoff to this function AI!
def fetch(url):
    ...
```

When you save the file, loom picks up the comment, uses the surrounding code as
context, and makes the change (or answers the question in the terminal).

## Voice

`/voice` records from your microphone and transcribes it with OpenAI's Whisper API, so
it needs `OPENAI_API_KEY` even when you code with another provider. Choose the audio
format and device with `--voice-format` and `--voice-input-device`.

## Scripting

Run one instruction and exit:

```bash
loom --message "add type hints to utils.py" utils.py
```

`--message-file` reads the instruction from a file, and `--yes-always` answers yes to
every confirmation. Use this carefully, since loom will then run shell commands the model
suggests without asking.

## Tips

- Keep the chat focused: `/drop` files you are done with and `/clear` the history when
  you switch tasks. Big contexts cost more and confuse the model.
- Break large changes into steps and check each one.
- `/tokens` shows what is using the context window.
- `/undo` reverts the last loom commit if a change went wrong.

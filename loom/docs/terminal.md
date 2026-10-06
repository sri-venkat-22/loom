# The terminal

Loom draws for your terminal: a banner at launch, a prompt with a toolbar under it, a
`/` palette, pickers for its questions, an effort slider and a live line while it
works. Its colours, glyphs, spinner and logo are a theme you can change. Everything has
a plain fallback for pipes, dumb terminals and `--no-pretty`, and nothing drawn here
changes what loom does.

## Launch

The LOOM wordmark arrives, then the mark, a shuttle crossing the warp, beside who loom
is talking to and where:

```
  ██      ██████  ██████  ██   ██
  ██      ██  ██  ██  ██  ███ ███
  ██      ██  ██  ██  ██  ███████
  ██      ██  ██  ██  ██  ██ █ ██
  ██████  ██████  ██████  ██   ██
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ ❯

  ┆┆┆┆┆   Loom v0.88.1
  ━━❯━━   kimi-k3 (1M context) · agent · bedrock
  ┆┆┆┆┆   ~/code/shop · git repo, 412 files
          key from AWS_BEARER_TOKEN_BEDROCK (~/.loom/credentials.json)
          weak model deepseek.v3.2 · repo-map 4k tokens · memory LOOM.md
```

The banner always says where the model's key came from (the environment, a `.env`
file, `~/.loom/credentials.json` or `--api-key`) and never shows the key. When a
conversation continues with `--continue`, or the model or chat mode changes, only the
mark and the lines beside it are shown.

The wordmark's entrance is the theme's `intro`: `selvedge` (the default: the letters
appear and their edge draws itself, in under a second), `weave`, `shuttle`,
`threading`, `beatup`, `fill`, or `none` for no wordmark. `--no-animation` shows the
last frame of it.

## The prompt

```
> fix the failing test
───────────────────────────────────────────────────────────────────────────────
  ⏵⏵ ask mode on (shift+tab to cycle) · ! for bash · / for commands     kimi-k3 · $0.42
```

The toolbar under the prompt says what Enter will do with what's typed. Otherwise it
shows the agent's permission mode, each in a colour of its own, so a Shift-Tab that lands
somewhere unexpected is plain to see: ask in gold, accept edits in green, plan in teal
and bypass in red. At its right are the model and what the session has cost. The toolbar
is gone once you press Enter, so the scrollback keeps only what you typed.

### The / palette

Type `/` and the commands that match show under the prompt, each with what it does:

```
> /d
    ❯ /diff     Display the diff of changes since the last message
      /drop     Remove files from the chat session to free up context space
───────────────────────────────────────────────────────────────────────────────
  2 commands · Tab or ↓ to pick · Enter runs it
```

Names match first: `/d` lists the commands that start with d. From two letters, letters
in order match too (`/rwd` finds `/rewind`), and from three a word of a command's
description does, after the names. A plain `/` lists every command in the groups `/help`
uses. When nothing matches, the toolbar says so, and nothing is sent to the model; a
mistyped command that's sent anyway gets the one it was probably meant to be.

## Questions

Loom's questions are pickers, drawn under the conversation and never full screen:

```
● Bash(python -m pytest -q)
  Run this command?
❯ 1. Yes
  2. Always               always allow this command (saved to .loom.permissions.json)
  3. Bypass permissions   stop asking for the rest of this session
  4. No                   and tell loom what to do instead
  ↑/↓ to move · Enter to choose · y a b n · Esc for no
```

Move with the arrow keys (or `j` and `k`) and press Enter, or press an option's number,
or the letter you would have typed before, like `y`. Enter takes the highlighted option,
which starts on the safe one, and Esc picks the safe answer: no, reject or cancel. Once
answered, the picker leaves one line behind:

```
  Run this command?  ✓ yes
```

A `/project` checkpoint is headed by its phase and the document to review, and each of
its answers says what it does, like *continue to Building* or *say what to change, and
Design runs again*.

## While loom works

```
▅ Running the tests… (12s · step 4 · $0.03 · esc to interrupt)
```

The spinner's line says what the agent is doing, from its to-do list, then how long the
request has taken, which step of the loop it's on, what it has cost so far, and that Esc
stops it. On a truecolor terminal a glint of light passes over it. Nothing repaints
faster than the theme's `max_fps`, 15 a second by default.

## /effort

`/effort` trades speed for thoroughness: how long the model may think before it answers.

```
  Effort
  Faster ◀ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ ▶ Smarter
              low         medium         high         xhigh          max
                            ▲
  medium — up to 4k thinking tokens
  ←/→ to adjust · Enter to confirm · Esc to cancel
```

The track carries the theme's gradient, sliding on a truecolor terminal. For models with
a thinking budget the stops are low (2k tokens), medium (4k), high (8k), xhigh (16k)
and max (24k); for models with a reasoning effort they are low, medium and high. Esc
leaves it as it was. `/effort high` sets it without the slider. For a model loom knows no
thinking setting for, use `/think-tokens` or `/reasoning-effort`.

## /cost

`/cost` shows what this session has cost, the main conversation and its sub-agents
apart, and when the project has a `/project`, what every run of each phase has cost,
each with a bar of its share:

```
  the project, every run of each phase
  ◌ Idea Check     $0.08  ██░░░░░░░░░░░░░░░░░░░░░░    9%
  ≡ Planning       $0.05  █░░░░░░░░░░░░░░░░░░░░░░░    6%
  ⊞ Design         $0.44  ████████████░░░░░░░░░░░░   50%
  █ Building       $0.31  ████████░░░░░░░░░░░░░░░░   35%
```

## /project

Each phase has a glyph, and they fill in as the project goes: `◌` Idea Check (nothing on
disk yet), `≡` Planning (requirements as lines), `⊞` Design (a grid), `█` Building
(solid), `▣` Testing (checked) and `▲` Launch. A phase starts under a banner with its
glyph, its number and the documents it works from. `/project status` shows the wordmark
filling with gold as phases are approved, then a row for each phase. `/project back`
without a phase asks which one, saying for each how it stands and what going back to it
redoes.

## Themes

Loom has two themes: loom-dark, the default, a warm charcoal with gold, and loom-light,
for light terminals (`--light-mode`). `--theme NAME` picks one, and `--theme FILE` adds
a theme file. Theme files change any part of a theme, with no code change:

1. `~/.loom/theme.toml`, yours, for every project
2. `.loom/theme.toml` in the project
3. the `--theme FILE`

each on top of the one before.

```toml
# .loom/theme.toml
name = "loom-dark"              # the built-in theme to start from

[color]
accent = "#cf9a4c"              # gold: the prompt, the mark, what's selected
info = "#6f9490"                # teal

[gradient]
effort = ["#6f9490", "#cf9a4c", "#e0b064", "#cf9a4c", "#6f9490"]
effort_256 = [66, 137, 179, 215, 222]
sweep_ms = 5500                 # 0 keeps the track still

[glyph]
selected = "❯"

[glyph.ascii]                   # what dumb terminals get instead
selected = ">"

[spinner]
style = "braille"               # blocks, weave, shuttle, braille or ascii
interval_ms = 100

[logo]
mark = ["┆┆┆┆┆", "━━❯━━", "┆┆┆┆┆"]
intro = "weave"

[limits]
max_fps = 15
```

- `[color]`: `ground`, `inset`, `edge`, `fg`, `dim`, `faint`, `accent`, `accent_dim`,
  `highlight`, `ok`, `fail`, `info` and `selection`, as `"#rrggbb"`, or `"default"` for
  the terminal's own colour. Loom never paints the terminal's background, so its text is
  in the terminal's colour (`fg = "default"`).
- `[glyph]`: the prompt (`prompt`), the picker's marker (`selected`), the mode (`mode`),
  the phases (`idea`, `planning`, `design`, `building`, `testing`, `launch`), the status
  marks (`ok`, `fail`, `hard_fail`, `cached`, `denied`, `queued`, `running`, `review`,
  `unevaluated`), the to-do list's, the slider's and the bars'. `[glyph.ascii]` has an
  ASCII twin for each.
- `[spinner]` and `[spinners]`: the spinner's frames, by `style` or as a list of
  `frames`, its `ascii` frames and its pace.
- `[logo]`: the `mark`, the `wordmark`, their ASCII twins, and the `intro`.
- `code_theme`: the Pygments style of code in the model's replies, like `gruvbox-dark`.

A key loom doesn't know, or a value it can't use, is refused: loom names the file and the
key and uses the built-in theme. `--code-theme` and the `--*-color` options still win
over the theme.

## When the terminal can't

| Terminal | What it gets |
|---|---|
| Truecolor (`COLORTERM=truecolor`) | the sliding gradient, the shimmer, unicode glyphs |
| 256 colours | the gradient's five still stops, unicode glyphs, the spinner |
| `NO_COLOR` or `--no-pretty` | no colour; unicode glyphs, the spinner and the pickers stay |
| `TERM=dumb`, or output to a pipe | the plain lines and typed answers, no escape codes |

`--no-animation` keeps everything still: the intro shows its last frame and the effort
track doesn't slide. `--no-fancy-input` keeps the plain prompt and typed answers.

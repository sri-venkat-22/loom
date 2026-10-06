import contextlib
import io as stdio
import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

from loom.commands import Commands, effort_index, effort_levels
from loom.io import InputOutput
from loom.tui import banner, logo, paint, palette
from loom.tui import theme as tui_theme
from loom.tui import widgets
from loom.tui.banner import (
    BannerInfo,
    banner_lines,
    key_source,
    model_parts,
    note_key_source,
)
from loom.tui.status import Activity, format_elapsed, status_line
from loom.tui.theme import (
    BUILTIN,
    DARK,
    PLAIN,
    Capabilities,
    Theme,
    ThemeError,
    load_theme,
    parse_toml_subset,
)

COLOR = Capabilities(color="truecolor", unicode=True, animate=False, interactive=True)
NO_COLOR = Capabilities(color="none", unicode=True, animate=False, interactive=True)


def dark(caps=COLOR):
    return Theme(BUILTIN["loom-dark"]).degrade(caps)


def plain(renderables):
    """What rich renderables print, without colour."""
    console = Console(file=stdio.StringIO(), width=120, color_system=None)
    for renderable in renderables:
        console.print(renderable)
    return console.file.getvalue()


class TestTheme(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.root = Path(self.tmp.name) / "project"
        (self.home / ".loom").mkdir(parents=True)
        (self.root / ".loom").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builtin_themes(self):
        theme = load_theme(self.root, home=self.home)
        self.assertEqual(theme.name, "loom-dark")
        self.assertEqual(theme.code_theme, "gruvbox-dark")
        light = load_theme(self.root, name="loom-light", home=self.home)
        self.assertEqual(light.name, "loom-light")
        self.assertNotEqual(light.raw_color("accent"), theme.raw_color("accent"))
        # The light theme only changes what it names; the glyphs are dark's
        self.assertEqual(light.data["glyph"], DARK["glyph"])
        with self.assertRaises(ThemeError):
            load_theme(self.root, name="loom-neon", home=self.home)

    def test_files_change_the_theme_in_order(self):
        (self.home / ".loom" / "theme.toml").write_text(
            '[color]\naccent = "#112233"\ninfo = "#445566"\n\n[spinner]\nstyle = "braille"\n'
        )
        (self.root / ".loom" / "theme.toml").write_text(
            '# the project\'s\n[color]\naccent = "#abcdef"\n\n[logo]\nintro = "weave"\n'
        )
        theme = load_theme(self.root, home=self.home).degrade(COLOR)
        self.assertEqual(theme.color("accent"), "#abcdef")
        self.assertEqual(theme.color("info"), "#445566")
        self.assertEqual(theme.spinner_frames, tuple(DARK["spinners"]["braille"]))
        self.assertEqual(theme.intro, "weave")
        self.assertEqual(len(theme.sources), 2)

        # --theme FILE goes on top, and a file can name the built-in to start from
        extra = Path(self.tmp.name) / "mine.toml"
        extra.write_text('name = "loom-light"\n[glyph]\nselected = ">"\n')
        theme = load_theme(self.root, path=extra, home=self.home)
        self.assertEqual(theme.name, "loom-light")
        self.assertEqual(theme.glyph("selected"), ">")
        self.assertEqual(theme.raw_color("accent"), "#abcdef")

    def test_unknown_keys_and_bad_values_are_refused_loudly(self):
        theme_file = self.root / ".loom" / "theme.toml"
        for text, words in [
            ('[color]\nsparkle = "#cf9a4c"\n', "color.sparkle"),
            ('[colour]\naccent = "#cf9a4c"\n', "colour"),
            ('[color]\naccent = "gold"\n', "color.accent"),
            ('[logo]\nintro = "fireworks"\n', "logo.intro"),
            ("[limits]\nmax_fps = 120\n", "limits.max_fps"),
            ('[glyph]\ntrack = ["<"]\n', "glyph.track"),
            ("[gradient]\neffort_256 = [66, 999]\n", "gradient.effort_256"),
            ('code_theme = "no-such-style"\n', "code_theme"),
            ("[color\n", "TOML"),
        ]:
            theme_file.write_text(text)
            with self.assertRaises(ThemeError) as raised:
                load_theme(self.root, home=self.home)
            self.assertIn(str(theme_file), str(raised.exception))
            self.assertIn(words, str(raised.exception))
        with self.assertRaises(ThemeError):
            load_theme(self.root, path=self.root / "missing.toml", home=self.home)

    def test_degrade(self):
        theme = Theme(BUILTIN["loom-dark"])
        full = theme.degrade(Capabilities(color="truecolor", unicode=True, animate=True))
        self.assertEqual(full.glyph("selected"), "❯")
        self.assertEqual(full.color("accent"), "#cf9a4c")
        self.assertEqual(full.sweep_ms, 5500)
        self.assertEqual(full.gradient(), DARK["gradient"]["effort"])

        # 256 colours: the theme's five still stops
        stops = theme.degrade(Capabilities(color="256")).gradient()
        self.assertEqual(stops, [tui_theme.xterm_hex(n) for n in DARK["gradient"]["effort_256"]])
        self.assertEqual(theme.degrade(Capabilities(color="256")).sweep_ms, 0)

        # NO_COLOR keeps the glyphs and the attributes, not the colours
        no_color = theme.degrade(NO_COLOR)
        self.assertIsNone(no_color.color("accent"))
        self.assertEqual(no_color.style("accent", bold=True), "bold")
        self.assertEqual(no_color.glyph("selected"), "❯")
        self.assertEqual(no_color.spinner_frames, tuple(DARK["spinners"]["blocks"]))

        # A dumb terminal or a pipe gets the ASCII twins
        ascii = theme.degrade(PLAIN)
        self.assertEqual(ascii.glyph("selected"), ">")
        self.assertEqual(ascii.glyph("ok"), "[ok]")
        self.assertEqual(ascii.spinner_frames, ("|", "/", "-", "\\"))
        self.assertTrue(all(ord(char) < 128 for row in ascii.wordmark + ascii.mark for char in row))
        # Every glyph has an ASCII twin
        glyphs = set(DARK["glyph"]) - {"ascii"}
        self.assertEqual(glyphs, set(DARK["glyph"]["ascii"]))
        # "default" is the terminal's own colour
        self.assertIsNone(full.color("fg"))
        self.assertEqual(full.style("fg"), "none")

    def test_capabilities(self):
        tty = MagicMock()
        tty.isatty.return_value = True
        tty.encoding = "utf-8"
        pipe = MagicMock()
        pipe.isatty.return_value = False

        detect = Capabilities.detect
        caps = detect(tty, environ=dict(TERM="xterm-256color", COLORTERM="truecolor"))
        self.assertEqual(caps, Capabilities("truecolor", True, True, True))
        self.assertEqual(detect(tty, environ=dict(TERM="xterm-256color")).color, "256")
        self.assertEqual(detect(tty, environ=dict(TERM="xterm")).color, "16")
        no_color = detect(tty, environ=dict(TERM="xterm-256color", NO_COLOR="1"))
        self.assertEqual(no_color, Capabilities("none", True, True, True))
        self.assertFalse(detect(tty, animation=False, environ=dict(TERM="xterm")).animate)
        self.assertEqual(detect(tty, environ=dict(TERM="dumb")), PLAIN)
        self.assertEqual(detect(pipe, environ=dict(TERM="xterm-256color")), PLAIN)
        tty.encoding = "ascii"
        self.assertFalse(detect(tty, environ=dict(TERM="xterm")).unicode)


class TestToml(unittest.TestCase):
    def test_subset_parser_reads_what_tomllib_reads(self):
        text = """
# a comment
name = "loom-dark"   # trailing comment
code_theme = 'monokai'

[color]
accent = "#cf9a4c"     # a "quoted # hash" stays
fg = "default"

[gradient]
effort = [
    "#6f9490",  # teal
    "#cf9a4c",
]
effort_256 = [66, 137]
sweep_ms = 5_500

[glyph.ascii]
mode = ">>"
escaped = "a\\"b\\u00e9"

[flags]
on = true
off = false
ratio = 0.5
"""
        expected = {
            "name": "loom-dark",
            "code_theme": "monokai",
            "color": {"accent": "#cf9a4c", "fg": "default"},
            "gradient": {
                "effort": ["#6f9490", "#cf9a4c"],
                "effort_256": [66, 137],
                "sweep_ms": 5500,
            },
            "glyph": {"ascii": {"mode": ">>", "escaped": 'a"bé'}},
            "flags": {"on": True, "off": False, "ratio": 0.5},
        }
        self.assertEqual(parse_toml_subset(text), expected)
        try:
            import tomllib
        except ImportError:
            return
        self.assertEqual(tomllib.loads(text), expected)

    def test_subset_parser_errors(self):
        for text in ["[color\n", "accent\n", 'a = "open\n', "a = [1, 2\n", "a = nope\n"]:
            with self.assertRaises(ValueError):
                parse_toml_subset(text)


class TestPaint(unittest.TestCase):
    def test_gradients(self):
        stops = ["#000000", "#ffffff"]
        self.assertEqual(paint.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(paint.gradient_at(stops, 0), "#000000")
        self.assertEqual(paint.gradient_at(stops, 1), "#ffffff")
        track = paint.sweep(DARK["gradient"]["effort"], 9)
        self.assertEqual(len(track), 9)
        self.assertEqual(track[0], "#6f9490")
        # The sweep slides: half a pass later the track starts where it ended
        self.assertNotEqual(paint.sweep(DARK["gradient"]["effort"], 9, 0.25), track)
        self.assertEqual(paint.steps(["#1", "#2", "#3"], 6), ["#1", "#1", "#2", "#2", "#3", "#3"])

    def test_shimmer_and_ansi(self):
        text = paint.shimmer("Working", "#cf9a4c", "#e0b064", 0.5)
        self.assertEqual(text.plain, "Working")
        self.assertIn("\x1b[38;2;", paint.ansi(text, "truecolor"))
        self.assertEqual(paint.ansi(text, "none"), "Working")
        # rich keeps the codes of a style it has drawn, so 256 colours get colours of their own
        self.assertIn("\x1b[38;5;", paint.ansi(Text("x", style="#123457"), "256"))


class TestPalette(unittest.TestCase):
    def setUp(self):
        self.entries = palette.entries(Commands(MagicMock(), None))

    def names(self, query):
        return [entry.name for entry in palette.matches(query, self.entries)]

    def test_matching_ranks_names_first(self):
        # One letter: only the names that start with it
        self.assertEqual(self.names("d"), ["/diff", "/drop"])
        # From three letters, descriptions match too ("different" for /architect), after
        self.assertEqual(self.names("dif"), ["/diff", "/architect"])
        # Letters in order
        self.assertEqual(self.names("rwd"), ["/rewind"])
        # A word of the description, after the names
        found = self.names("design")
        self.assertIn("/project", found)
        self.assertEqual(self.names("zz"), [])
        # Everything, grouped as /help groups it
        everything = self.names("")
        self.assertEqual(len(everything), len(self.entries))
        self.assertEqual(everything[0], "/agent")
        self.assertIn("/effort", everything)
        self.assertIn("/cost", everything)

    def test_descriptions_and_suggestions(self):
        descriptions = {entry.name: entry.description for entry in self.entries}
        self.assertEqual(
            descriptions["/diff"], "Display the diff of changes since the last message"
        )
        # Usage after a colon is left out, and long ones are cut
        self.assertNotIn("/project new", descriptions["/project"])
        self.assertTrue(all(len(text) <= palette.MAX_DESCRIPTION for text in descriptions.values()))
        self.assertEqual(palette.suggest("/cmmit", list(descriptions)), "/commit")
        self.assertIsNone(palette.suggest("/xyzzy", list(descriptions)))

    def test_help_is_rendered_from_the_same_registry(self):
        text = plain(palette.help_lines(dark(NO_COLOR), self.entries, 100))
        for entry in self.entries:
            self.assertIn(entry.name, text)
        self.assertIn("── agent", text)
        self.assertIn("── settings", text)


class TestBanner(unittest.TestCase):
    def setUp(self):
        # main notes where it loaded each key from; start each test knowing none
        sources = patch.dict(banner.KEY_SOURCES, clear=True)
        sources.start()
        self.addCleanup(sources.stop)

    def test_banner_says_where_the_key_comes_from_never_the_key(self):
        model = MagicMock()
        model.name = "openai/gpt-4o"
        model.info = dict(max_input_tokens=128000)
        model.keys_in_environment = ["OPENAI_API_KEY"]
        model.missing_keys = []
        environ = dict(OPENAI_API_KEY="sk-very-secret-123")
        self.assertEqual(key_source(model, environ), "key from OPENAI_API_KEY (env)")
        note_key_source(["OPENAI_API_KEY"], "~/.loom/credentials.json")
        key = key_source(model, environ)
        self.assertEqual(key, "key from OPENAI_API_KEY (~/.loom/credentials.json)")
        self.assertEqual(model_parts(model), ("gpt-4o", "openai"))

        info = BannerInfo(
            version="1.2.3",
            model="gpt-4o",
            provider="openai",
            context=128000,
            mode="agent",
            cwd=str(Path.home() / "code" / "shop"),
            repo="git repo, 12 files",
            key=key,
            details=["weak model gpt-4o-mini", "repo-map 4k tokens"],
            warnings=["A large repo"],
        )
        text = plain(banner_lines(dark(), info))
        for words in [
            "Loom v1.2.3",
            "gpt-4o (128k context) · agent · openai",
            "~/code/shop · git repo, 12 files",
            "key from OPENAI_API_KEY",
            "weak model gpt-4o-mini · repo-map 4k tokens",
            "A large repo",
            "━━❯━━",
        ]:
            self.assertIn(words, text)
        self.assertNotIn("sk-very-secret-123", text)

    def test_missing_and_local_keys(self):
        model = MagicMock()
        model.name = "ollama/qwen2.5-coder"
        model.info = {}
        model.keys_in_environment = False
        model.missing_keys = []
        self.assertEqual(key_source(model, {}), "runs locally, no key needed")
        model.name = "anthropic/claude-sonnet-4-6"
        model.missing_keys = ["ANTHROPIC_API_KEY"]
        self.assertEqual(key_source(model, {}), "no key: set ANTHROPIC_API_KEY")
        model.name = "bedrock/global.moonshotai.kimi-k3"
        model.keys_in_environment = True
        model.missing_keys = []
        self.assertEqual(
            key_source(model, dict(AWS_BEARER_TOKEN_BEDROCK="x")),
            "key from AWS_BEARER_TOKEN_BEDROCK (env)",
        )


class TestLogo(unittest.TestCase):
    def test_every_intro_ends_with_the_wordmark(self):
        theme = dark()
        for intro in tui_theme.INTROS:
            frames = logo.intro_frames(theme, intro)
            if intro == "none":
                self.assertEqual(frames, [])
                continue
            self.assertGreater(len(frames), 3, intro)
            last = plain(frames[-1])
            self.assertIn(theme.wordmark[0], last, intro)
            self.assertIn(theme.wordmark[-1], last, intro)
        # Selvedge ends with the shuttle parked at the end of the edge
        self.assertTrue(plain(logo.intro_frames(theme, "selvedge")[-1]).rstrip().endswith("━ ❯"))

    def test_without_animation_the_last_frame_shows(self):
        theme = dark(PLAIN)
        console = Console(file=stdio.StringIO(), width=100, color_system=None)
        self.assertTrue(logo.play_intro(console, theme))
        self.assertIn("##      ######", console.file.getvalue())
        # Too narrow for the wordmark: none
        narrow = Console(file=stdio.StringIO(), width=30, color_system=None)
        self.assertFalse(logo.play_intro(narrow, theme))
        self.assertTrue(plain(logo.wordmark_fill(theme, 0.5)).startswith("  ##      ######"))


class TestStatus(unittest.TestCase):
    def test_activity_and_line(self):
        activity = Activity("Running the tests", ("12s", "step 4", "esc to interrupt"))
        self.assertEqual(str(activity), "Running the tests… (12s · step 4 · esc to interrupt)")
        self.assertEqual(str(Activity("Working")), "Working…")
        line = status_line(dark(NO_COLOR), "▅", activity, now=1.0)
        self.assertEqual(line.plain, "▅ Running the tests… (12s · step 4 · esc to interrupt)")
        # A plain string works too
        self.assertEqual(status_line(dark(PLAIN), "|", "Waiting", now=1.0).plain, "| Waiting...")
        self.assertEqual(format_elapsed(9), "9s")
        self.assertEqual(format_elapsed(65), "1m 05s")
        self.assertEqual(format_elapsed(3720), "1h 02m")


class TestWidgets(unittest.TestCase):
    def select(self, keys, options=None, **kwargs):
        options = options or [
            widgets.Option("Yes", "y"),
            widgets.Option("Always", "a", "always allow it"),
            widgets.Option("No", "n", "and tell loom what to do instead"),
        ]
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return widgets.select(
                dark(NO_COLOR),
                "Run this command?",
                options,
                input=pipe,
                output=DummyOutput(),
                **kwargs,
            )

    def test_select(self):
        self.assertEqual(self.select("\r"), 0)
        self.assertEqual(self.select("\r", default=2), 2)
        self.assertEqual(self.select("j\r"), 1)
        self.assertEqual(self.select("\x1b[B\x1b[B\r"), 2)
        self.assertEqual(self.select("\x1b[A\r"), 2)  # up from the first goes round
        self.assertEqual(self.select("a"), 1)  # the letters of typed answers still pick
        self.assertEqual(self.select("3"), 2)
        self.assertEqual(self.select("\x1b", cancel=2), 2)
        with self.assertRaises(KeyboardInterrupt):
            self.select("\x03")
        with self.assertRaises(EOFError):
            self.select("\x04")

    def test_slider(self):
        stops = [widgets.Stop(name) for name in ("low", "medium", "high", "xhigh", "max")]

        def slide(keys, index=1):
            with create_pipe_input() as pipe:
                pipe.send_text(keys)
                return widgets.slider(
                    dark(NO_COLOR), "Effort", stops, index, input=pipe, output=DummyOutput()
                )

        self.assertEqual(slide("\r"), 1)
        self.assertEqual(slide("\x1b[C\x1b[C\r"), 3)
        self.assertEqual(slide("\x1b[C" * 9 + "\r"), 4)  # stops at the top
        self.assertEqual(slide("\x1b[D\x1b[D\x1b[D\r"), 0)
        self.assertEqual(slide("5\r"), 4)
        self.assertIsNone(slide("\x1b[C\x1b"))

    def test_slider_frames_draw_the_track(self):
        theme = dark(replace(COLOR, animate=True))
        canvas = widgets.Canvas(3, 20)
        widgets.draw_track(canvas, theme, 2, 10, 100.0, False, 1.0)
        track = [canvas.cells[1][col] for col in range(2, 12)]
        self.assertTrue(all(char == "━" for _style, char in track))
        self.assertGreater(len({style for style, _char in track}), 2)  # a gradient
        self.assertEqual(widgets.stop_columns(5, 10, 51), [13, 24, 35, 46, 57])


def interactive(io, pipe):
    """io drawing pickers, as in a terminal, reading keys from pipe."""
    io.theme = io.theme.degrade(NO_COLOR)
    io.input = pipe
    io.output = DummyOutput()
    io.console = Console(file=stdio.StringIO(), width=100, color_system=None)
    return io


class TestPickersInTheTerminal(unittest.TestCase):
    def test_questions_use_pickers_and_leave_one_line(self):
        with create_pipe_input() as pipe:
            io = interactive(InputOutput(fancy_input=False), pipe)
            pipe.send_text("n")
            self.assertFalse(io.confirm_ask("Add the output to the chat?"))
            pipe.send_text("\r")
            self.assertTrue(io.confirm_ask("Create calc.py?", subject="calc.py"))
            pipe.send_text("a")
            self.assertEqual(
                io.permission_ask("Run this command?", always="always allow it", bypass="no"),
                "always",
            )
            pipe.send_text("\x1b")
            self.assertEqual(io.permission_ask("Run this command?"), "no")
            pipe.send_text("j\r")
            checkpoint = dict(
                phase="design",
                title="Design",
                document="loom-project/3-architecture.md",
                document_title="architecture doc",
                next_title="Building",
            )
            self.assertEqual(
                io.choice_ask("Approve it?", ["approve", "edit", "reject"], checkpoint=checkpoint),
                "edit",
            )
            shown = io.console.file.getvalue()
        self.assertIn("Add the output to the chat?  · no", shown)
        self.assertIn("Create calc.py?  ✓ yes", shown)
        self.assertIn("Run this command?  ⊗ no", shown)
        self.assertIn("⊞  Design checkpoint", shown)
        self.assertIn("Approve it?  ▸ edit", shown)
        self.assertEqual(widgets.Option("x").detail, "")

    def test_toolbar(self):
        io = InputOutput(fancy_input=False)
        io.theme = dark(NO_COLOR)
        completer = MagicMock()
        completer.palette_entries.return_value = palette.entries(Commands(MagicMock(), None))

        def hint(text, mode=None, label="agent"):
            fragments = io.toolbar_hint(text, label, mode, completer)
            return "".join(text for _style, text in fragments)

        self.assertIn("ask mode on (shift+tab to cycle)", hint("", lambda: "ask"))
        self.assertIn("plan mode on", hint("", lambda: "plan"))
        self.assertIn("bypass permissions on", hint("", lambda: "bypass"))
        self.assertIn("ask mode · questions only", hint("", None, "ask"))
        self.assertIn("2 commands", hint("/d"))
        self.assertIn("no command matches /zz · nothing is sent to the model", hint("/zz"))
        self.assertIn("runs this in the shell", hint("!ls"))
        styles = [style for style, _text in io.toolbar_hint("", "agent", lambda: "plan")]
        self.assertIn("class:toolbar.mode.plan", styles)

    def test_pipes_get_plain_lines_without_escape_codes(self):
        out = stdio.StringIO()
        with contextlib.redirect_stdout(out):
            io = InputOutput(pretty=True, fancy_input=False)
            io.console = Console(file=out, force_terminal=False)
            self.assertFalse(io.fancy)
            io.show_banner(MagicMock(), ["Loom v1", "Model: x"], intro=True)
            io.phase_banner(3, 6, "design", "Design", "→ 3-architecture.md", "Phase 3/6: Design.")
            io.cost_report([("this session", [("tool", "this conversation", 0.5)])])
        text = out.getvalue()
        self.assertIn("Loom v1", text)
        self.assertIn("Phase 3/6: Design.", text)
        self.assertIn("this conversation", text)
        self.assertNotIn("\x1b", text)


class TestCommands(unittest.TestCase):
    def model(self, accepts):
        model = MagicMock()
        model.name = "anthropic/claude-sonnet-4-6"
        model.accepts_settings = accepts
        model.get_raw_thinking_tokens.return_value = None
        model.get_reasoning_effort.return_value = None
        model.parse_token_value = lambda value: int(value[:-1]) * 1024
        return model

    def test_effort_levels(self):
        thinking = self.model(["thinking_tokens"])
        levels = effort_levels(thinking)
        self.assertEqual([level[0] for level in levels], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(effort_index(thinking, levels), 1)
        thinking.get_raw_thinking_tokens.return_value = 15000
        self.assertEqual(effort_index(thinking, levels), 3)
        reasoning = self.model(["reasoning_effort"])
        self.assertEqual(len(effort_levels(reasoning)), 3)
        reasoning.get_reasoning_effort.return_value = "high"
        self.assertEqual(effort_index(reasoning, effort_levels(reasoning)), 2)
        self.assertEqual(effort_levels(self.model([])), [])

    def test_effort_command(self):
        io = InputOutput(fancy_input=False, yes=True)
        coder = MagicMock()
        coder.main_model = self.model(["thinking_tokens"])
        commands = Commands(io, coder)
        with patch.object(io, "tool_output") as output:
            commands.cmd_effort("max")
        coder.main_model.set_thinking_tokens.assert_called_once_with("24k")
        self.assertIn("Effort set to max", output.call_args[0][0])
        with patch.object(io, "tool_error") as error:
            commands.cmd_effort("turbo")
        self.assertIn("low, medium, high, xhigh, max", error.call_args[0][0])

    def test_unknown_commands_say_what_was_meant(self):
        io = InputOutput(fancy_input=False, yes=True)
        commands = Commands(io, MagicMock())
        with patch.object(io, "tool_error") as error:
            commands.run("/cmmit")
        self.assertEqual(
            error.call_args[0][0],
            "Invalid command: /cmmit, did you mean /commit? Nothing was sent to the model.",
        )

    def test_cost(self):
        io = InputOutput(fancy_input=False, yes=True)
        coder = MagicMock()
        coder.total_cost = 0.75
        coder.main_model.name = "gpt-4o"
        task = MagicMock()
        task.cost = 0.25
        coder.session.tasks = [task]
        coder.root = tempfile.mkdtemp()
        commands = Commands(io, coder)
        sections = commands.cost_sections()
        self.assertEqual(
            sections,
            [
                (
                    "this session with gpt-4o",
                    [("tool", "this conversation", 0.5), ("running", "1 sub-agent task", 0.25)],
                )
            ],
        )
        with patch.object(io, "tool_output") as output:
            commands.cmd_cost("")
        lines = [call[0][0] for call in output.call_args_list]
        self.assertIn("This session with gpt-4o:", lines)
        self.assertTrue(
            any(re.search(r"this conversation\s+\$0\.50\s+67%", line) for line in lines)
        )
        self.assertTrue(any(re.search(r"total\s+\$0\.75", line) for line in lines))


class TestCompletions(unittest.TestCase):
    def test_slash_completions_carry_descriptions(self):
        from loom.io import AutoCompleter

        commands = Commands(MagicMock(), None)
        completer = AutoCompleter("", [], [], commands, "utf-8", theme=dark())
        completions = list(
            completer.get_command_completions(Document("/dif"), None, "/dif", ["/dif"])
        )
        self.assertEqual([c.text for c in completions], ["/diff", "/architect"])
        self.assertEqual(
            completions[0].display_meta_text, palette.first_sentence(commands.cmd_diff.__doc__)
        )
        self.assertIn("❯", completions[0].display_text)


if __name__ == "__main__":
    os.environ.setdefault("LOOM_ANALYTICS", "false")
    unittest.main()

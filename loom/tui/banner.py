"""
The launch banner: the wordmark arriving (logo.py), then the mark beside who loom is
talking to, where it is and where its key came from:

      ┆┆┆┆┆   Loom v0.88.1
      ━━❯━━   kimi-k3 (1M context) · agent · bedrock
      ┆┆┆┆┆   ~/code/shop · git repo, 412 files
              key from AWS_BEARER_TOKEN_BEDROCK (~/.loom/credentials.json)

The key's source is always shown and the key never is. The banner is a pure function of
a BannerInfo, which the coder fills in (Coder.get_banner_info).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from loom.tui.logo import mark_lines

# Where loom got each environment variable it set: {name: "~/.loom/credentials.json"}.
# main fills it in as it loads .env files, credentials.json and --api-key.
KEY_SOURCES = {}

# The variables that hold each provider's key, the first one set being the one used
PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "azure": ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY"),
    "bedrock": ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "AWS_ACCESS_KEY_ID"),
    "bedrock_converse": ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "AWS_ACCESS_KEY_ID"),
    "cohere": ("COHERE_API_KEY",),
    "cohere_chat": ("COHERE_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "fireworks_ai": ("FIREWORKS_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "nvidia_nim": ("NVIDIA_NIM_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "together_ai": ("TOGETHERAI_API_KEY", "TOGETHER_API_KEY"),
    "vertex_ai": ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_PROJECT"),
    "xai": ("XAI_API_KEY",),
}
# Providers that run on this machine and need no key
LOCAL_PROVIDERS = ("ollama", "ollama_chat", "lm_studio", "llamafile")

# What to ask for, by edit format, under the banner at launch
INVITATIONS = {
    "agent": (
        "Describe what you want built or changed, and loom will find the code, make the edit"
        " and run the checks."
    ),
    "ask": "Ask about the code. Nothing is changed in this mode.",
    "architect": "Describe a change: an architect model plans it and an editor model makes it.",
    "context": "Describe a change, and loom will find the files it needs.",
    "help": "Ask how to use loom.",
}
DEFAULT_INVITATION = "Describe a change, and loom will edit the files in the chat."


def note_key_source(names, source):
    """Remember that source set the environment variables names."""
    for name in names:
        KEY_SOURCES[name] = source


@dataclass
class BannerInfo:
    version: str
    model: str
    provider: str = None
    # The model's context window, in tokens
    context: int = None
    # The edit format, like agent
    mode: str = None
    # Like "reasoning high" or "8k think tokens"
    effort: str = None
    cwd: str = ""
    # Like "git repo, 412 files"
    repo: str = None
    # Like "key from OPENAI_API_KEY (.env)"
    key: str = None
    # Short facts shown faint under the identity, like "weak model deepseek-chat"
    details: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def model_parts(model):
    """(the model's name without its provider, the provider) for a models.Model."""
    first, sep, rest = model.name.partition("/")
    if sep:
        # Like bedrock in bedrock/global.moonshotai.kimi-k3
        return rest, first
    return model.name, (model.info or {}).get("litellm_provider")


def format_context(tokens):
    if not tokens:
        return None
    tokens = int(tokens)
    if tokens >= 1_000_000:
        value = tokens / 1_000_000
        return f"{value:g}M" if value == int(value) else f"{value:.1f}M"
    return f"{round(tokens / 1000)}k"


def short_path(path):
    """path with the home directory as ~."""
    path = str(path)
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def key_source(model, environ=None):
    """Where model's credential comes from, as "key from NAME (source)", or None. The key
    itself is never part of it."""
    environ = os.environ if environ is None else environ
    _, provider = model_parts(model)
    if provider in LOCAL_PROVIDERS:
        return "runs locally, no key needed"
    names = model.keys_in_environment if isinstance(model.keys_in_environment, list) else []
    names = list(names) + list(PROVIDER_KEYS.get(provider or "", ()))
    for name in names:
        if environ.get(name):
            source = KEY_SOURCES.get(name, "env")
            what = "credentials" if name.startswith("AWS_") and "TOKEN" not in name else "key"
            return f"{what} from {name} ({source})"
    if model.missing_keys:
        return f"no key: set {' or '.join(model.missing_keys)}"
    return None


def model_provider(name):
    """The provider of a model name, like openai for gpt-4o, or None."""
    if "/" in name:
        return name.split("/", 1)[0]
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return None


def key_available(name, environ=None):
    """Whether this machine has a key for the model called name."""
    environ = os.environ if environ is None else environ
    provider = model_provider(name)
    if provider in LOCAL_PROVIDERS:
        return True
    return any(environ.get(var) for var in PROVIDER_KEYS.get(provider or "", ()))


def banner_lines(theme, info, width=96):
    """The banner's rows, as rich Text: the mark beside the identity, then the details."""
    mark = mark_lines(theme)
    gap = "   "
    pad = " " * (len("  ") + len(theme.mark[0]) + len(gap))

    model = Text()
    model.append(info.model, style=theme.style("accent"))
    context = format_context(info.context)
    if context:
        model.append(f" ({context} context)", style=theme.style("dim"))
    for part, style in ((info.mode, "fg"), (info.effort, "dim"), (info.provider, "dim")):
        if part:
            model.append(f" {theme.glyph('dot')} ", style=theme.style("faint"))
            model.append(part, style=theme.style(style))

    where = Text()
    where.append(short_path(info.cwd), style=theme.style("dim"))
    if info.repo:
        where.append(f" {theme.glyph('dot')} ", style=theme.style("faint"))
        where.append(info.repo, style=theme.style("dim"))

    title = Text()
    title.append(f"Loom v{info.version}", style=theme.style("fg", bold=True))
    identity = [title, model, where]
    if info.key:
        key = Text()
        key.append(
            info.key, style=theme.style("fail" if info.key.startswith("no key") else "faint")
        )
        identity.append(key)

    rows = []
    for num, line in enumerate(identity):
        row = Text("  ")
        if num < len(mark):
            row += mark[num]
            row.append(gap)
        else:
            row = Text(pad)
        rows.append(row + line)

    for line in join_details(info.details, width - len(pad), theme.glyph("dot")):
        rows.append(Text(pad) + Text(line, style=theme.style("faint")))
    for warning in info.warnings:
        row = Text(pad)
        row.append(theme.glyph("hard_fail") + " ", style=theme.style("accent"))
        row.append(warning, style=theme.style("accent"))
        rows.append(row)
    return rows


def join_details(details, width, dot):
    """details joined with dots into lines no wider than width, where they fit."""
    lines = []
    line = ""
    for detail in details:
        joined = f"{line} {dot} {detail}" if line else detail
        if line and len(joined) > width:
            lines.append(line)
            line = detail
        else:
            line = joined
    if line:
        lines.append(line)
    return lines


def invitation_lines(theme, edit_format, can_cycle=False):
    """What to type, under the banner at launch: lines to show indented."""
    first = Text()
    first.append(INVITATIONS.get(edit_format, DEFAULT_INVITATION), style=theme.style("dim"))
    second = Text()
    second.append("Type ", style=theme.style("dim"))
    second.append("/", style=theme.style("accent", bold=True))
    second.append(" for commands, ", style=theme.style("dim"))
    second.append("!", style=theme.style("accent", bold=True))
    second.append(" to run a shell command", style=theme.style("dim"))
    if can_cycle:
        second.append(", ", style=theme.style("dim"))
        second.append("shift+tab", style=theme.style("accent"))
        second.append(" to change what runs without asking", style=theme.style("dim"))
    second.append(".", style=theme.style("dim"))
    return [first, second]

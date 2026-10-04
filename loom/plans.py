"""
Plans the agent presents in plan mode, with its exit_plan_mode tool.

In plan mode the agent can only read. When it knows what to do, it calls exit_plan_mode
with a plan in markdown. loom saves the plan to .loom/plans/<time>-<slug>.md (a folder
with its own .gitignore, like .loom/memory/), shows it and asks the user to approve it,
edit it or keep planning. Approving it switches to accept-edits or ask mode, and the agent
carries the plan out in the same request, with the plan in its system prompt until the
request is done.
"""

import re
from datetime import datetime
from pathlib import Path

PLANS_DIR = ".loom/plans"

# The answers to "Approve this plan?", whose first letters differ
APPROVE_AUTO = "approve and auto-accept edits"
APPROVE_ASK = "yes, approve and ask for each edit"
KEEP_PLANNING = "keep planning"
EDIT_PLAN = "edit plan"
CHOICES = [APPROVE_AUTO, APPROVE_ASK, KEEP_PLANNING, EDIT_PLAN]
# The permission mode each approval switches to
APPROVED_MODES = {APPROVE_AUTO: "accept-edits", APPROVE_ASK: "ask"}

# The most of a plan the system prompt carries; the rest is a read_file away
PROMPT_CHARS = 6000
MAX_SLUG = 40


def plan_title(text):
    """The plan's first heading, or its first line, without the markdown."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in lines:
        if line.startswith("#"):
            return line.lstrip("#").strip()
    first = lines[0] if lines else ""
    first = re.sub(r"^([-*+]|\d+[.)])\s+", "", first)
    return first.strip("*_` ")


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:MAX_SLUG].rstrip("-") or "plan"


def plans_dir(root):
    """The folder plans are saved in, made with its .gitignore: plans are notes for this
    checkout, like the chat history."""
    folder = Path(root) / PLANS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists() and not ignore.is_symlink():
        ignore.write_bytes(b"*\n")
    return folder


def save_plan(root, text, path=None):
    """Write the plan to path, or to a new file in .loom/plans. Returns the path."""
    if path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = plans_dir(root)
        path = folder / f"{stamp}-{slugify(plan_title(text))}.md"
        num = 2
        while path.exists():
            path = folder / f"{stamp}-{slugify(plan_title(text))}-{num}.md"
            num += 1
    path = Path(path)
    data = text if text.endswith("\n") else text + "\n"
    path.write_bytes(data.encode("utf-8"))
    return path


def read_plan(path):
    return Path(path).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def rel_path(root, path):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def brief(text, shown_path=None):
    """The plan as the system prompt carries it: all of it, or its start and where the
    rest is."""
    text = text.strip()
    if len(text) <= PROMPT_CHARS:
        return text
    where = f" Read {shown_path} for the rest." if shown_path else ""
    omitted = len(text) - PROMPT_CHARS
    return text[:PROMPT_CHARS] + f"\n\n... [{omitted:,} more characters of the plan.{where}]"

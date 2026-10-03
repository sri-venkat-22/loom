"""
The six phases of a loom project, in order: Idea Check → Planning → Design → Building →
Testing → Launch. Each phase is run by its own agent, with its own brief, a limited set
of tools and one document it produces. The orchestrator (loom/orchestrator.py) runs them.

The Building agent is the coding agent itself (every tool, MCP included) with a brief;
the others write a document, and Testing and Launch may also write test and deployment
files. Every phase agent can search the project's shared memory and record decisions in it.
"""

import re
from dataclasses import dataclass

from loom.coders import phase_prompts

DOCS_DIR = "loom-project"

READ_TOOLS = ("read_file", "list_dir", "glob", "grep")
WRITE_TOOLS = ("write_file", "edit_file")
# The project's shared memory (loom/memory.py)
MEMORY_TOOLS = ("recall", "record_decision")

TEST_FILES = (
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "**/spec/**",
    "**/test_*",
    "**/*_test.*",
    "**/*.test.*",
    "**/*.spec.*",
    "**/conftest.py",
)

DEPLOYMENT_FILES = (
    "**/Dockerfile*",
    "**/*.dockerfile",
    "**/.dockerignore",
    "**/docker-compose*.y*ml",
    "**/compose*.y*ml",
    ".github/workflows/**",
    "**/Procfile",
    "deploy/**",
    "deployment/**",
    "infra/**",
    "k8s/**",
    "helm/**",
    "**/*.tf",
    "fly.toml",
    "render.yaml",
    "vercel.json",
    "netlify.toml",
    "app.yaml",
    "Makefile",
    "README.md",
)


@dataclass(frozen=True)
class Phase:
    number: int
    key: str
    title: str
    # The file it writes, relative to the project root, and what to call it
    document: str
    document_title: str
    # What the phase produces, as listed in /project
    produces: str
    brief: str
    # Tool names it may use; None means every tool, MCP servers' included
    tools: tuple = None
    # Globs of the files it may write besides its document; None means any file
    writable: tuple = ()
    # The keys of the earlier phases whose documents it gets
    inputs: tuple = ()
    # A line like "**Verdict:** GO" that loom reads from the document
    verdict_label: str = None
    verdicts: tuple = ()
    # What the orchestrator looks up in the earlier documents not in the agent's message
    recall: str = ""
    # A step of a phase that isn't its own phase, like test-driven Building's acceptance
    # tests ("spec"), and the agent's name when it isn't the phase's
    mode: str = None
    agent_name: str = None

    @property
    def agent(self):
        return self.agent_name or f"{self.title} agent"


PHASES = [
    Phase(
        1,
        "idea",
        "Idea Check",
        f"{DOCS_DIR}/1-idea-report.md",
        "idea report",
        "idea report",
        phase_prompts.IDEA,
        tools=READ_TOOLS + WRITE_TOOLS + MEMORY_TOOLS + ("todo_write",),
        verdict_label="Verdict",
        verdicts=("GO WITH CHANGES", "NO-GO", "GO"),
    ),
    Phase(
        2,
        "planning",
        "Planning",
        f"{DOCS_DIR}/2-prd.md",
        "PRD",
        "PRD",
        phase_prompts.PLANNING,
        tools=READ_TOOLS + WRITE_TOOLS + MEMORY_TOOLS + ("todo_write",),
        inputs=("idea",),
        recall="users problem scope MVP assumptions risks",
    ),
    Phase(
        3,
        "design",
        "Design",
        f"{DOCS_DIR}/3-architecture.md",
        "architecture document",
        "architecture doc",
        phase_prompts.DESIGN,
        tools=READ_TOOLS + WRITE_TOOLS + MEMORY_TOOLS + ("todo_write",),
        inputs=("idea", "planning"),
        recall="constraints performance security scale platforms risks",
    ),
    Phase(
        4,
        "building",
        "Building",
        f"{DOCS_DIR}/4-build-summary.md",
        "build summary",
        "code",
        phase_prompts.BUILDING,
        tools=None,
        writable=None,
        inputs=("planning", "design"),
        recall="constraints technical risks assumptions out of scope",
    ),
    Phase(
        5,
        "testing",
        "Testing",
        f"{DOCS_DIR}/5-test-report.md",
        "test report",
        "test report",
        phase_prompts.TESTING,
        tools=READ_TOOLS + WRITE_TOOLS + MEMORY_TOOLS + ("bash", "todo_write"),
        writable=TEST_FILES,
        inputs=("planning", "design", "building"),
        verdict_label="Result",
        verdicts=("PASS", "FAIL"),
        recall="acceptance criteria edge cases errors performance security",
    ),
    Phase(
        6,
        "launch",
        "Launch",
        f"{DOCS_DIR}/6-deployment.md",
        "deployment document",
        "deployment",
        phase_prompts.LAUNCH,
        tools=READ_TOOLS + WRITE_TOOLS + MEMORY_TOOLS + ("bash", "todo_write"),
        writable=DEPLOYMENT_FILES,
        inputs=("design", "building", "testing"),
        recall="hosting deployment users scale performance security privacy configuration",
    ),
]

PHASES_BY_KEY = {phase.key: phase for phase in PHASES}

# Test-driven Building's first step (loom/tdd.py): the Testing agent, in spec mode, writes
# acceptance tests from the PRD before anything is built. It's part of Building.
SPEC = Phase(
    4,
    "building",
    "Acceptance tests",
    f"{DOCS_DIR}/4a-acceptance-tests.md",
    "acceptance test plan",
    "acceptance tests",
    phase_prompts.SPEC,
    tools=READ_TOOLS + WRITE_TOOLS + MEMORY_TOOLS + ("bash", "todo_write"),
    writable=TEST_FILES,
    inputs=("planning", "design"),
    recall="acceptance criteria requirements interfaces edge cases errors",
    mode="spec",
    agent_name="Testing agent (spec mode)",
)


def get_phase(name):
    """A phase by key, number or title, like "design", "3" or "Idea Check"."""
    name = str(name).strip().lower()
    for phase in PHASES:
        if name in (phase.key, str(phase.number), phase.title.lower()):
            return phase
    raise KeyError(name)


def next_phase(phase):
    return PHASES[phase.number] if phase.number < len(PHASES) else None


def normalize_verdict(text):
    return re.sub(r"[\s-]+", " ", text).strip().upper()


FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Values that mean the document gives no test command
NO_COMMAND = ("none", "n/a", "na", "-", "tbd")


def read_verdict(phase, text):
    """The verdict a phase's document gives on its verdict line, like GO or FAIL, or None
    if it has none (or still has the template's placeholder)."""
    if not phase.verdict_label or not text:
        return None
    pattern = rf"^[\s>*_#-]*{phase.verdict_label}[\s*_]*:[\s*_]*(.*)$"
    for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
        value = match.group(1)
        if "<" in value or "|" in value:
            continue
        value = normalize_verdict(re.sub(r"[*_`]", " ", value))
        # The longest first, so GO WITH CHANGES isn't read as GO
        for verdict in phase.verdicts:
            if value.startswith(normalize_verdict(verdict)):
                return verdict
    return None


def read_test_command(text):
    """The command a document's "**Test command:** `pytest -q`" line gives, or None if it
    has none (or still has the template's placeholder). The command can also be in a code
    block right under the line."""
    if not text:
        return None
    pattern = r"^[ \t>*_#-]*Test command[ \t*_]*:[ \t*_]*(.*)$"
    for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
        value = match.group(1).strip()
        if not value:
            value = first_fenced_line(text[match.end() :])
        command = clean_command(value)
        if command:
            return command
    return None


def first_fenced_line(text):
    """The first line of the code block that text starts with, after blank lines."""
    lines = text.lstrip("\n").splitlines()
    if not lines or not FENCE_RE.match(lines[0]):
        return ""
    for line in lines[1:]:
        if FENCE_RE.match(line):
            break
        if line.strip() and not line.strip().startswith("#"):
            return line
    return ""


def clean_command(value):
    """A command from a document line: the code span if it has one, without Markdown's
    bold markers or a shell prompt."""
    value = value.strip()
    code = re.search(r"(`+)\s*(.+?)\s*\1", value)
    if code:
        value = code.group(2)
    else:
        value = re.sub(r"(\*\*|__)\s*$", "", value).strip()
    if value.startswith("$ "):
        value = value[2:].strip()
    if not value or value.lower().rstrip(".") in NO_COMMAND:
        return None
    if value.startswith("<") and value.endswith(">"):
        # The template's placeholder
        return None
    return value

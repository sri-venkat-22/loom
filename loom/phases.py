"""
The six phases of a loom project, in order: Idea Check → Planning → Design → Building →
Testing → Launch. Each phase is run by its own agent, with its own brief, a limited set
of tools and one document it produces. The orchestrator (loom/orchestrator.py) runs them.

The Building agent is the coding agent itself (every tool, MCP included) with a brief;
the others write a document, and Testing and Launch may also write test and deployment
files.
"""

import re
from dataclasses import dataclass

from loom.coders import phase_prompts

DOCS_DIR = "loom-project"

READ_TOOLS = ("read_file", "list_dir", "glob", "grep")
WRITE_TOOLS = ("write_file", "edit_file")

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

    @property
    def agent(self):
        return f"{self.title} agent"


PHASES = [
    Phase(
        1,
        "idea",
        "Idea Check",
        f"{DOCS_DIR}/1-idea-report.md",
        "idea report",
        "idea report",
        phase_prompts.IDEA,
        tools=READ_TOOLS + WRITE_TOOLS + ("todo_write",),
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
        tools=READ_TOOLS + WRITE_TOOLS + ("todo_write",),
        inputs=("idea",),
    ),
    Phase(
        3,
        "design",
        "Design",
        f"{DOCS_DIR}/3-architecture.md",
        "architecture document",
        "architecture doc",
        phase_prompts.DESIGN,
        tools=READ_TOOLS + WRITE_TOOLS + ("todo_write",),
        inputs=("idea", "planning"),
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
    ),
    Phase(
        5,
        "testing",
        "Testing",
        f"{DOCS_DIR}/5-test-report.md",
        "test report",
        "test report",
        phase_prompts.TESTING,
        tools=READ_TOOLS + WRITE_TOOLS + ("bash", "todo_write"),
        writable=TEST_FILES,
        inputs=("planning", "design", "building"),
        verdict_label="Result",
        verdicts=("PASS", "FAIL"),
    ),
    Phase(
        6,
        "launch",
        "Launch",
        f"{DOCS_DIR}/6-deployment.md",
        "deployment document",
        "deployment",
        phase_prompts.LAUNCH,
        tools=READ_TOOLS + WRITE_TOOLS + ("bash", "todo_write"),
        writable=DEPLOYMENT_FILES,
        inputs=("design", "building", "testing"),
    ),
]

PHASES_BY_KEY = {phase.key: phase for phase in PHASES}


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

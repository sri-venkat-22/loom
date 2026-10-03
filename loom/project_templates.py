"""
Project templates: a head start for a /project of a known kind, like a FastAPI + React app
or a Python command-line tool.

A template is a folder with a template.yml and, optionally, a skeleton/ of files to start
the code from:

    name: python-cli
    description: A Python command-line tool, packaged with pyproject.toml
    tdd: false
    test_command: "python -m pytest -q"
    decisions: ["The CLI uses argparse from the standard library"]
    briefs: {design: "...", building: "...", testing: "...", launch: "..."}
    checks: {building: ["python -m compileall -q src"], launch: ["docker build -t app ."]}
    writable: {launch: ["fly.toml"]}

loom finds templates in three places, and a later one replaces an earlier one with the
same name: loom's own (loom/templates/), yours (~/.loom/templates/) and the project's
(.loom/templates/).

How a project uses its template (see the orchestrator):
- Its decisions go in the project memory, from the "template", for every agent to see.
- Each phase agent gets the template's brief for its phase, and may also write the
  template's writable files for its phase.
- Its test command is the project's, before the architecture document's.
- The skeleton is copied in and committed when Building first starts.
- After a phase, its checks run. They warn when they fail, but don't block.

A project's own templates come with the repo, so their checks (shell commands) only run
once the user approves them, which loom remembers like approved project hooks.
"""

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from loom import hooks
from loom.display import sanitize_for_display
from loom.phases import PHASES_BY_KEY

TEMPLATE_FILE = "template.yml"
SKELETON_DIR = "skeleton"
PROJECT_TEMPLATES = ".loom/templates"
# Where each kind of template is, from the first to the last that counts
SOURCES = ("built-in", "user", "project")
# A skeleton's gitignore, stored without its dot so git doesn't apply it to loom's repo
RENAMED = {"gitignore": ".gitignore"}
FIELDS = ("name", "description", "tdd", "test_command", "decisions", "briefs", "checks", "writable")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Bigger skeleton files aren't copied
MAX_SKELETON_FILE = 1_000_000


class TemplateError(Exception):
    pass


def builtin_dir():
    return Path(__file__).resolve().parent / "templates"


def template_dirs(root):
    """(source, folder) of each place templates may be, in order."""
    return [
        ("built-in", builtin_dir()),
        ("user", Path.home() / ".loom" / "templates"),
        ("project", Path(root) / PROJECT_TEMPLATES),
    ]


@dataclass
class Template:
    name: str
    description: str
    path: Path
    source: str
    tdd: bool = False
    test_command: str = None
    decisions: list = field(default_factory=list)
    briefs: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    writable: dict = field(default_factory=dict)

    @property
    def skeleton(self):
        """The skeleton folder, or None if the template has none."""
        path = self.path / SKELETON_DIR
        return path if path.is_dir() else None

    @property
    def trusted(self):
        """Whether its checks run without asking: loom's own templates and the user's."""
        return self.source != "project"

    def brief(self, key):
        return (self.briefs.get(key) or "").strip()

    def checks_for(self, key):
        return list(self.checks.get(key) or [])

    def writable_for(self, key):
        return list(self.writable.get(key) or [])

    def skeleton_files(self):
        """(relative posix path in the project, file) of each skeleton file, sorted."""
        skeleton = self.skeleton
        if not skeleton:
            return []
        found = []
        for path in sorted(skeleton.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(skeleton)
            if any(part in ("__pycache__", ".git") for part in rel.parts):
                continue
            name = RENAMED.get(rel.name, rel.name)
            found.append(((rel.parent / name).as_posix(), path))
        return sorted(found)

    def hash(self):
        """A hash of the template's contents: its template.yml and its skeleton."""
        digest = hashlib.sha256()
        digest.update((self.path / TEMPLATE_FILE).read_bytes())
        for rel, path in self.skeleton_files():
            digest.update(rel.encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
        return digest.hexdigest()

    def checks_hash(self):
        return hashlib.sha256(json.dumps(self.checks, sort_keys=True).encode()).hexdigest()


def string_list(value, what):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TemplateError(f"{what} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def by_phase(value, what, lists=True):
    """A {phase key: value} mapping from template.yml, checking the phase keys."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TemplateError(f"{what} must map phases to values")
    res = {}
    for key, item in value.items():
        if key not in PHASES_BY_KEY:
            phases = ", ".join(PHASES_BY_KEY)
            raise TemplateError(f"{what} names an unknown phase {key!r}; use one of: {phases}")
        if lists:
            res[key] = string_list(item, f"{what}.{key}")
        elif isinstance(item, str):
            res[key] = item
        else:
            raise TemplateError(f"{what}.{key} must be text")
    return res


def parse(folder, source):
    """The template in folder. Raises TemplateError if it isn't a valid one."""
    folder = Path(folder)
    path = folder / TEMPLATE_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise TemplateError(f"Unable to read {path}: {err}")
    if not isinstance(data, dict):
        raise TemplateError(f"{path} must be a YAML mapping")
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise TemplateError(f"{path} has unknown fields: {', '.join(unknown)}")

    name = data.get("name", folder.name)
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise TemplateError(f"{path}: name must be lowercase letters, digits, ., _ or -")
    description = data.get("description") or ""
    test_command = data.get("test_command")
    if not isinstance(description, str) or not (
        test_command is None or isinstance(test_command, str)
    ):
        raise TemplateError(f"{path}: description and test_command must be text")
    tdd = data.get("tdd", False)
    if not isinstance(tdd, bool):
        raise TemplateError(f"{path}: tdd must be true or false")
    try:
        return Template(
            name=name,
            description=" ".join(description.split()),
            path=folder,
            source=source,
            tdd=tdd,
            test_command=(test_command or "").strip() or None,
            decisions=string_list(data.get("decisions"), "decisions"),
            briefs=by_phase(data.get("briefs"), "briefs", lists=False),
            checks=by_phase(data.get("checks"), "checks"),
            writable=by_phase(data.get("writable"), "writable"),
        )
    except TemplateError as err:
        raise TemplateError(f"{path}: {err}")


def find_templates(root, warn=None):
    """Every template, {name: Template}, the later places' replacing the earlier ones'.
    Invalid ones are skipped, and warn(message) says why."""
    found = {}
    for source, folder in template_dirs(root):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not (path / TEMPLATE_FILE).is_file():
                continue
            try:
                template = parse(path, source)
            except TemplateError as err:
                if warn:
                    warn(str(err))
                continue
            found[template.name] = template
    return found


def load_template(root, name, warn=None):
    """The template called name. Raises TemplateError if there's none."""
    templates = find_templates(root, warn)
    if name not in templates:
        names = ", ".join(sorted(templates)) or "none"
        raise TemplateError(f"There is no template {name!r}. Templates: {names}.")
    return templates[name]


# Using a template


def copy_skeleton(template, root):
    """Copy the template's skeleton into root, without replacing the project's own files.
    A skeleton's .gitignore adds its missing lines to the project's. Returns (the files
    written, the files skipped because they exist), as posix paths."""
    root = Path(root)
    written, skipped = [], []
    for rel, source in template.skeleton_files():
        target = root / rel
        if source.stat().st_size > MAX_SKELETON_FILE:
            skipped.append(rel)
            continue
        if target.exists() or target.is_symlink():
            if rel == ".gitignore" and merge_lines(target, source):
                written.append(rel)
            else:
                skipped.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(rel)
    return written, skipped


def merge_lines(target, source):
    """Add source's lines that target doesn't have to the end of target. Returns whether
    it added any."""
    have = target.read_bytes().decode("utf-8", errors="replace")
    lines = set(have.splitlines())
    missing = [
        line
        for line in source.read_bytes().decode("utf-8", errors="replace").splitlines()
        if line.strip() and line not in lines
    ]
    if not missing:
        return False
    text = have if not have or have.endswith("\n") else have + "\n"
    target.write_bytes((text + "\n".join(missing) + "\n").encode("utf-8"))
    return True


# Approving a project template's checks, which reuses the project hooks' approvals


def approval_key(template, root):
    return f"template-checks:{Path(root).resolve()}:{template.name}"


def checks_approved(template, root):
    return hooks.load_approvals().get(approval_key(template, root)) == template.checks_hash()


def approve_checks(template, root, io):
    hooks.save_approval(approval_key(template, root), template.checks_hash(), io)


def describe_checks(template):
    lines = []
    for key, commands in template.checks.items():
        for command in commands:
            lines.append(f"{key}: {sanitize_for_display(command, show_escapes=True)}")
    return "\n".join(lines)

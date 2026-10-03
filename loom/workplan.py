"""
The work plan: how the Design agent splits Building into work packages that builders can
work on at once, each in its own git worktree (see loom/parallel.py).

The architecture document has a "## Work packages" section with a YAML block:

    ```yaml
    - id: core
      title: Conversion functions
      owns: ["src/app/convert.py"]
      tests: ["tests/test_convert.py"]
      depends_on: []
      acceptance: [FR-1, FR-2]
      test_command: "python -m pytest -q tests/test_convert.py"
    - id: cli
      title: The command line
      owns: ["src/app/cli.py"]
      tests: ["tests/test_cli.py"]
      depends_on: [core]
      acceptance: [FR-3]
    ```

A package's builder may only write the files its owns and tests globs match, so no two
packages may own the same file. A package starts once the packages it depends on are
built and merged: the packages fall into waves, each built at once. read_workplan() finds
the section and checks it; a plan with problems isn't used, and Building runs as one agent.
"""

import itertools
import re
from dataclasses import dataclass, field

import yaml

from loom.tools import glob_match

SECTION = "Work packages"
FIELDS = ("id", "title", "owns", "tests", "depends_on", "acceptance", "test_command")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
WILDCARDS = "*?[{"
# How many paths of a glob loom tries against another's when checking for overlap
MAX_SAMPLES = 32


@dataclass(frozen=True)
class Package:
    id: str
    title: str
    owns: tuple
    tests: tuple = ()
    depends_on: tuple = ()
    acceptance: tuple = ()
    # The command that runs just this package's tests, if the plan gives one
    test_command: str = None

    @property
    def writable(self):
        """The globs of the files the package's builder may write."""
        return self.owns + self.tests

    def owns_path(self, path):
        return any(glob_match(pattern, path) for pattern in self.writable)


@dataclass
class WorkPlan:
    packages: list = field(default_factory=list)
    # The ids of the packages that can be built at once, in order
    waves: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def valid(self):
        return bool(self.packages) and not self.problems

    @property
    def ids(self):
        return [package.id for package in self.packages]

    def package(self, package_id):
        return next(package for package in self.packages if package.id == package_id)

    def owner(self, path):
        """The package that owns path, or None."""
        return next((package for package in self.packages if package.owns_path(path)), None)

    def describe(self):
        """The plan in a line, like: 3 packages in 2 waves: core, then cli and web."""
        waves = ["; ".join(wave) for wave in self.waves]
        return (
            f"{len(self.packages)} package{'s' if len(self.packages) != 1 else ''} in"
            f" {len(self.waves)} wave{'s' if len(self.waves) != 1 else ''}: "
            + " → ".join(waves)
        )


def section(text):
    """The text of the document's Work packages section, or None if it has none."""
    lines = text.splitlines()
    fenced = False
    start = level = None
    for num, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING_RE.match(line)
        if not match:
            continue
        depth = len(match.group(1))
        if start is None:
            if match.group(2).strip().lower() == SECTION.lower():
                start, level = num + 1, depth
        elif depth <= level:
            return "\n".join(lines[start:num])
    return "\n".join(lines[start:]) if start is not None else None


def yaml_block(text):
    """The first fenced code block in text, or None."""
    lines = text.splitlines()
    for num, line in enumerate(lines):
        if FENCE_RE.match(line):
            body = []
            for inner in lines[num + 1 :]:
                if FENCE_RE.match(inner):
                    return "\n".join(body)
                body.append(inner)
            return "\n".join(body)
    return None


def string_list(value):
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, (str, int)) for item in value):
        return None
    return tuple(str(item).strip() for item in value if str(item).strip())


def read_workplan(text, max_packages=None, files=()):
    """The work plan in an architecture document, or None if it has no Work packages
    section. files are the project's files, to check that no two packages own one.
    max_packages, if given, is the most the plan may have."""
    found = section(text or "")
    if found is None:
        return None
    plan = WorkPlan()
    block = yaml_block(found)
    if block is None:
        plan.problems.append("The Work packages section has no YAML block.")
        return plan
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as err:
        first = str(err).strip().splitlines()[0]
        plan.problems.append(f"The work packages aren't valid YAML: {first}")
        return plan
    if isinstance(data, dict) and isinstance(data.get("packages"), list):
        data = data["packages"]
    if not isinstance(data, list) or not data:
        plan.problems.append("The work packages must be a YAML list of packages.")
        return plan

    for num, item in enumerate(data, 1):
        package = parse_package(item, num, plan.problems)
        if package:
            if package.id in plan.ids:
                plan.problems.append(f"Two packages are called {package.id}.")
            else:
                plan.packages.append(package)

    if max_packages and len(plan.packages) > max_packages:
        plan.problems.append(
            f"{len(plan.packages)} packages are more than the {max_packages} the builders can"
            " take (three per --build-workers)."
        )
    check_overlaps(plan, files)
    check_dependencies(plan)
    if not plan.problems:
        plan.waves = waves(plan.packages)
    return plan


def parse_package(item, num, problems):
    """The num-th package of the plan, or None, with what's wrong added to problems."""
    if not isinstance(item, dict):
        problems.append(f"Package {num} isn't a mapping of fields.")
        return None
    name = item.get("id")
    where = f"Package {name}" if isinstance(name, str) else f"Package {num}"
    unknown = sorted(set(item) - set(FIELDS))
    if unknown:
        problems.append(f"{where} has unknown fields: {', '.join(map(str, unknown))}.")
    if not isinstance(name, str) or not ID_RE.match(name):
        problems.append(
            f"{where} needs an id of up to 24 lowercase letters, digits, - or _, like core."
        )
        return None
    lists = {key: string_list(item.get(key)) for key in FIELDS[2:6]}
    for key, value in lists.items():
        if value is None:
            problems.append(f"{where}: {key} must be a list of strings.")
            return None
    if not lists["owns"]:
        problems.append(f"{where} owns no files: give it at least one glob in owns.")
        return None
    test_command = item.get("test_command")
    if test_command is not None and not isinstance(test_command, str):
        problems.append(f"{where}: test_command must be text.")
        test_command = None
    return Package(
        id=name,
        title=str(item.get("title") or name),
        owns=lists["owns"],
        tests=lists["tests"],
        depends_on=lists["depends_on"],
        acceptance=lists["acceptance"],
        test_command=(test_command or "").strip() or None,
    )


# Overlapping globs


def has_wildcards(glob):
    return any(char in glob for char in WILDCARDS)


def samples(glob):
    """Some paths glob matches, its wildcards filled in a few ways."""
    parts = re.split(r"(\*\*/|\*\*|\*|\?|\[[^\]]*\]|\{[^}]*\})", glob)
    options = []
    for part in parts:
        if part == "**/":
            options.append(["", "x/"])
        elif part == "**":
            options.append(["x", "x/y"])
        elif part in ("*", "?"):
            options.append(["x"])
        elif part.startswith("[") and part.endswith("]"):
            inner = part[1:-1]
            options.append([inner[0] if inner and inner[0] not in "!^" else "x"])
        elif part.startswith("{") and part.endswith("}"):
            options.append(part[1:-1].split(","))
        else:
            options.append([part])
    return ["".join(combo) for combo in itertools.islice(itertools.product(*options), MAX_SAMPLES)]


def literal_prefix(glob):
    """The directory a glob starts with before its first wildcard, like src/ for
    src/**/*.py."""
    cut = min((glob.index(char) for char in WILDCARDS if char in glob), default=len(glob))
    return glob[: glob.rfind("/", 0, cut) + 1]


def literal_suffix(glob):
    """The end of a glob's last segment after its last wildcard, like .py for *.py."""
    tail = glob.rsplit("/", 1)[-1]
    cut = max((tail.rindex(char) for char in "*?]}" if char in tail), default=-1)
    return tail[cut + 1 :]


def may_overlap(a, b):
    """Whether some path could match both globs. It errs on the side of yes."""
    if not has_wildcards(a) or not has_wildcards(b):
        if not has_wildcards(a) and not has_wildcards(b):
            return a == b
        literal, pattern = (a, b) if not has_wildcards(a) else (b, a)
        return glob_match(pattern, literal)
    if any(glob_match(b, path) for path in samples(a)):
        return True
    if any(glob_match(a, path) for path in samples(b)):
        return True
    prefix_a, prefix_b = literal_prefix(a), literal_prefix(b)
    if not (prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)):
        return False
    shorter = a if len(prefix_a) <= len(prefix_b) else b
    if "**" not in shorter:
        return False
    suffix_a, suffix_b = literal_suffix(a), literal_suffix(b)
    if suffix_a and suffix_b and not (suffix_a.endswith(suffix_b) or suffix_b.endswith(suffix_a)):
        return False
    return True


def check_overlaps(plan, files):
    for first, second in itertools.combinations(plan.packages, 2):
        clash = next(
            ((a, b) for a in first.writable for b in second.writable if may_overlap(a, b)),
            None,
        )
        if clash:
            plan.problems.append(
                f"Packages {first.id} and {second.id} could both own the same files:"
                f" {clash[0]} and {clash[1]}."
            )
            continue
        shared = [path for path in files if first.owns_path(path) and second.owns_path(path)]
        if shared:
            plan.problems.append(
                f"Packages {first.id} and {second.id} both own {', '.join(shared[:3])}."
            )


# Dependencies


def check_dependencies(plan):
    ids = set(plan.ids)
    for package in plan.packages:
        for dep in package.depends_on:
            if dep == package.id:
                plan.problems.append(f"Package {package.id} depends on itself.")
            elif dep not in ids:
                plan.problems.append(f"Package {package.id} depends on {dep}, which isn't one.")
    cycle = find_cycle(plan.packages)
    if cycle:
        plan.problems.append(f"The packages depend on each other in a cycle: {' → '.join(cycle)}.")


def find_cycle(packages):
    """A dependency cycle, as the ids around it (the first one again at the end), or None."""
    deps = {package.id: [d for d in package.depends_on if d != package.id] for package in packages}
    state = {}

    def visit(node, path):
        state[node] = "visiting"
        for dep in deps.get(node, []):
            if dep not in deps:
                continue
            if state.get(dep) == "visiting":
                return path[path.index(dep) :] + [dep]
            if dep not in state:
                found = visit(dep, path + [dep])
                if found:
                    return found
        state[node] = "done"
        return None

    for node in deps:
        if node not in state:
            found = visit(node, [node])
            if found:
                return found
    return None


def waves(packages):
    """The packages' ids in waves: each wave's packages depend only on earlier waves'."""
    remaining = {package.id: set(package.depends_on) for package in packages}
    done = set()
    res = []
    while remaining:
        ready = [pid for pid, deps in remaining.items() if deps <= done]
        if not ready:
            break
        res.append(ready)
        done.update(ready)
        for pid in ready:
            del remaining[pid]
    return res

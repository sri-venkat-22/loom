"""
Test-driven Building, for projects started with /project new --tdd (or from a template
with tdd: true).

Building then has two steps:

1. Acceptance tests. The Testing agent, in spec mode (it may only write test files),
   turns the PRD's acceptance criteria into tests, which fail since nothing is built yet,
   and writes loom-project/4a-acceptance-tests.md, which maps the requirements to the
   tests. The founder approves the tests at their own checkpoint, edits or rejects them.
   Once approved, the tests are locked: loom keeps their paths and hashes.

2. The build loop. The Building agent builds, then loom runs the test command. While the
   tests fail, the end of their output goes back into the same agent conversation for
   another try: up to --build-retries more times, or until the run has cost
   --build-budget dollars.

So the agent can't make the tests pass by changing them, it can't edit a locked test,
loom puts back any locked test that changed some other way (a bash command, say), and the
Building checkpoint flags test changes that skip tests or expect them to fail.

The Testing phase afterwards, and sending its failures back to Building, work as usual.
Without a test command or a git repo, Building runs as usual, with a warning.
"""

import hashlib
import itertools
import re
import time
from pathlib import Path

from loom.coders import phase_prompts
from loom.orchestrator import keep_end, now
from loom.phases import SPEC, TEST_FILES
from loom.repo import ANY_GIT_ERROR, EMPTY_TREE
from loom.tools import glob_match, plural

# How much of the failing tests' output goes back to the agent
RETRY_OUTPUT_CHARS = 5000

# Lines that skip a test or expect it to fail, in the test frameworks agents use
SKIP_RE = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail|importorskip)\("
    r"|unittest\.(?:skip\w*|expectedFailure)\b|@(?:skip\w*|expectedFailure)\b"
    r"|\.skipTest\(|\b(?:it|test|describe|context)\.(?:skip|todo|fixme)\b"
    r"|\bx(?:it|describe|test)\(|\bt\.Skip(?:Now|f)?\(|#\[ignore\]|@Disabled\b|@Ignore\b"
)
MAX_SKIPS = 20

# How each build loop ended
RESULTS = dict(
    passed="the acceptance tests pass",
    failed="the acceptance tests still fail",
    budget="the build budget ran out",
    not_run="the tests weren't run",
    stopped="the agent stopped",
)


def is_test(path):
    return any(glob_match(pattern, path) for pattern in TEST_FILES)


def file_hash(path):
    """A hash of the file's text, the same whether its lines end in \r\n or \n: on Windows
    the agents write \r\n, and git may give back \n."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class TestDrivenBuilding:
    """Runs a test-driven project's Building phase for its orchestrator."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.io = orchestrator.io
        self.state = orchestrator.state
        self.root = orchestrator.root
        self.command = None

    @property
    def spec(self):
        """The acceptance tests' state, in the Building phase's: their status (pending,
        review or approved), the locked tests' hashes and the commit they're locked at."""
        return self.state.phase_data("building").setdefault("spec", dict(status="pending"))

    @property
    def git(self):
        return self.orchestrator.coder.repo.repo

    def ready(self):
        """Whether Building can be test-driven. If not, says why, and Building runs as
        usual."""
        if not self.orchestrator.coder.repo:
            self.io.tool_warning(
                "Test-driven Building needs a git repo, to lock the acceptance tests. Building"
                " the usual way instead."
            )
            return False
        self.command = self.orchestrator.test_command()
        if not self.command:
            self.io.tool_warning(
                "Test-driven Building needs a test command: the template's, or a **Test"
                " command:** line in the architecture document's Testing approach. Building"
                " the usual way instead."
            )
            return False
        return True

    def run(self, phase):
        """Write and approve the acceptance tests, unless they are already, then build until
        they pass. Returns whether Building finished, like Orchestrator.run_phase."""
        if self.spec.get("status") != "approved" and not self.acceptance_tests():
            return False
        return self.build(phase)

    # Step 1: the acceptance tests

    def acceptance_tests(self):
        """Have the Testing agent write the acceptance tests and the founder approve them.
        Returns whether they're approved and locked."""
        spec = self.spec
        while True:
            if spec.get("status") != "review" and not self.write_tests():
                return False
            choice = self.review()
            if choice == "approve":
                self.lock()
                return True
            feedback = self.ask_feedback()
            if not feedback:
                self.state.stop("building", "the acceptance tests are waiting for review")
                self.state.save()
                self.io.tool_output(
                    "Stopped at the acceptance tests. Edit them, or give feedback with"
                    " /project redo FEEDBACK, then /project run."
                )
                return False
            spec.update(status="pending", feedback=feedback, redo=True)
            self.state.save()
            self.orchestrator.decide(
                SPEC, "rejected", f"Asked for changes to the acceptance tests: {feedback}"
            )

    def write_tests(self):
        """Run the Testing agent in spec mode. Returns whether it wrote the plan."""
        orchestrator = self.orchestrator
        spec = self.spec
        self.io.tool_output(
            (
                "Test-driven Building, step 1 of 2: the Testing agent writes acceptance tests from"
                " the PRD."
            ),
            bold=True,
        )
        agent = orchestrator.make_agent(SPEC)
        meter = orchestrator.start_run(agent)
        extra = dict(step="acceptance tests")
        message = orchestrator.task_message(SPEC, spec)
        orchestrator.guarded(
            SPEC, agent, meter, lambda: orchestrator.do_task(agent, SPEC, message), extra
        )

        reason, outcome = None, "failed"
        if agent.failed:
            reason = agent.failed
        elif orchestrator.stopped(agent):
            reason, outcome = "stopped by the user", "stopped"
        elif not orchestrator.document_text(SPEC):
            reason = f"the agent didn't write {SPEC.document}"
        if reason:
            orchestrator.log_run(SPEC, agent, meter, outcome, extra=extra)
            self.state.stop("building", reason)
            self.state.save()
            self.io.tool_error(f"Building stopped: {reason}. Continue with /project run.")
            return False

        orchestrator.log_run(SPEC, agent, meter, "done", extra=extra)
        for name in ("feedback", "redo", "stale"):
            spec.pop(name, None)
        spec.update(status="review", base=meter["base"], tests=self.changed_tests(meter["base"]))
        self.state.log("building", "tests written", ", ".join(spec["tests"]))
        self.state.save()
        self.remember()

        # Nothing is built, so they should fail
        passed, output = orchestrator.run_tests(self.command)
        spec["first_run"] = dict(passed=passed, status=output.split("\n", 1)[0])
        self.state.save()
        return True

    def remember(self):
        text = self.orchestrator.document_text(SPEC)
        if text:
            self.orchestrator.memory.index_text(
                "doc:spec", "document", "building", SPEC.document, text
            )

    def changed_tests(self, base):
        """The test files that differ from the commit base, or are new."""
        try:
            names = self.git.git.diff("--name-only", base or EMPTY_TREE, "--").splitlines()
            names += self.git.git.ls_files("--others", "--exclude-standard").splitlines()
        except ANY_GIT_ERROR as err:
            self.io.tool_warning(f"Unable to find the acceptance tests in git: {err}")
            return []
        found = {name for name in names if is_test(name) and name != SPEC.document}
        return sorted(name for name in found if (self.root / name).is_file())

    def review(self):
        """The acceptance tests' checkpoint. Returns approve or reject."""
        spec = self.spec
        info = dict(
            phase="building",
            title=SPEC.title,
            document=SPEC.document,
            document_title=SPEC.document_title,
            verdict=None,
            next="building",
            next_title="Building",
        )
        while True:
            text = self.orchestrator.document_text(SPEC)
            tests = spec.get("tests") or []
            self.io.tool_output()
            self.io.tool_output(
                (
                    f"The acceptance tests are ready for review: {SPEC.document}"
                    f" ({len(text.splitlines())} lines) and {plural(len(tests), 'test file')}."
                ),
                bold=True,
            )
            for path in tests:
                self.io.tool_output(f"  {path}")
            first = spec.get("first_run") or {}
            if first.get("passed") is True:
                self.io.tool_warning(
                    "The acceptance tests pass already, so they may not test what Building has"
                    " to build."
                )
            elif first.get("passed") is False:
                self.io.tool_output("They fail now, as they should before Building.")
            if not tests:
                self.io.tool_warning("The Testing agent wrote no test files, so none get locked.")
            choice = self.io.choice_ask(
                "Approve the acceptance tests and lock them for Building?",
                ["approve", "edit", "reject"],
                checkpoint=info,
            )
            if choice != "edit":
                return choice
            if self.orchestrator.edit_document(SPEC):
                self.remember()

    def ask_feedback(self):
        if self.io.yes is not None:
            return ""
        return self.io.prompt_ask(
            "What should the Testing agent change in the acceptance tests? (leave empty to stop"
            " here)"
        ).strip()

    def lock(self):
        """Lock the acceptance tests: keep their hashes, and the commit they're in."""
        orchestrator = self.orchestrator
        spec = self.spec
        # The founder may have changed them at the checkpoint
        tests = self.changed_tests(spec.get("base"))
        orchestrator.commit([self.root / path for path in tests], "Lock the acceptance tests")
        spec.update(
            status="approved",
            approved=now(),
            tests=tests,
            locked={path: file_hash(self.root / path) for path in tests},
            commit=orchestrator.head(),
        )
        self.state.log("building", "tests locked", ", ".join(tests))
        self.state.save()
        orchestrator.decide(
            SPEC,
            "approved",
            f"Approved the acceptance tests and locked {plural(len(tests), 'test file')}",
            source=orchestrator.checkpoint_source(),
        )

    # Step 2: building until the tests pass

    def build(self, phase):
        orchestrator = self.orchestrator
        self.io.tool_output(
            (
                "Test-driven Building, step 2 of 2: the Building agent builds until the acceptance"
                " tests pass."
            ),
            bold=True,
        )
        agent = orchestrator.make_agent(phase, locked=self.spec.get("locked") or {})
        meter = orchestrator.start_run(agent)
        attempts = []
        extra = dict(step="build", attempts=attempts)
        orchestrator.guarded(
            phase, agent, meter, lambda: self.loop(phase, agent, meter, extra), extra
        )
        extra["skips"] = self.skips()
        return orchestrator.end_run(phase, agent, meter, extra)

    def loop(self, phase, agent, meter, extra, message=None, command=None):
        """Run agent, then the tests (command, by default the project's), and again with
        their failures in the same conversation, until they pass or the retries or budget
        run out. It runs where agent works, maybe a parallel builder's worktree, and only
        changes extra, so it can run on the builder's thread."""
        orchestrator = self.orchestrator
        io = agent.io
        command = command or self.command
        retries = orchestrator.settings.get("build_retries", 3)
        budget = orchestrator.settings.get("build_budget")
        locked = sorted(self.spec.get("locked") or {})
        if message is None:
            message = orchestrator.task_message(phase)
        message += "\n\n" + phase_prompts.tdd_build.format(
            tests=", ".join(locked) or "none", command=command, retries=retries
        )
        attempts = extra["attempts"]
        for attempt in itertools.count(1):
            started = time.monotonic()
            agent.run(with_message=message, preproc=False)
            if agent.failed or orchestrator.stopped(agent):
                extra["result"] = "stopped"
                return
            restored = self.restore_locked(agent)
            passed, output = orchestrator.run_tests(command, coder=agent)
            cost = orchestrator.spent(agent, meter)["cost"]
            attempts.append(
                dict(
                    attempt=attempt,
                    passed=passed,
                    seconds=round(time.monotonic() - started, 1),
                    cost=round(cost, 6),
                    restored=restored,
                )
            )
            if passed:
                extra["result"] = "passed"
                break
            if passed is None:
                extra["result"] = "not_run"
                break
            if attempt > retries:
                extra["result"] = "failed"
                io.tool_warning(f"The tests still fail after {plural(attempt, 'attempt')}.")
                break
            if budget is not None and cost >= budget:
                extra["result"] = "budget"
                io.tool_warning(
                    f"Building has cost ${cost:.2f}, which is the --build-budget of"
                    f" ${budget:.2f}, so it stops trying."
                )
                break
            io.tool_output(
                "The tests fail; sending the failures back to the agent"
                f" (attempt {attempt + 1} of {retries + 1})."
            )
            message = phase_prompts.tdd_retry.format(
                command=command,
                attempt=attempt,
                output=keep_end(output, RETRY_OUTPUT_CHARS),
                restored=(
                    phase_prompts.tdd_restored.format(files=", ".join(restored)) if restored else ""
                ),
            )
        orchestrator.nudge_for_document(agent, phase)

    def restore_locked(self, agent):
        """Put back the locked tests that changed where agent works, from the commit
        they're locked at. Returns their paths."""
        spec = self.spec
        root = Path(agent.root)
        git = agent.repo.repo
        changed = []
        for path, digest in (spec.get("locked") or {}).items():
            full = root / path
            if not full.is_file() or file_hash(full) != digest:
                changed.append(path)
        for path in changed:
            try:
                blob = git.commit(spec["commit"]).tree / path
                data = blob.data_stream.read()
            except (KeyError, ValueError) + ANY_GIT_ERROR as err:
                agent.io.tool_warning(f"Unable to put back {path}: {err}")
                continue
            full = root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
        if changed:
            agent.io.tool_warning(
                f"Put back {', '.join(changed)}: the acceptance tests are locked, and"
                f" {'it' if len(changed) == 1 else 'they'} changed."
            )
            if agent.auto_commits and not agent.dry_run:
                try:
                    agent.repo.commit(
                        fnames=[str(root / path) for path in changed],
                        message="Put back the locked acceptance tests",
                        coder=agent,
                    )
                except Exception as err:
                    agent.io.tool_warning(f"Unable to commit the put back tests: {err}")
        return changed

    def skips(self):
        """Lines added to test files since the tests were locked that skip a test or
        expect it to fail, as "path: line"."""
        commit = self.spec.get("commit")
        if not commit:
            return []
        found = []
        try:
            diff = self.git.git.diff("-U0", "--no-color", commit, "--")
            untracked = self.git.git.ls_files("--others", "--exclude-standard").splitlines()
        except ANY_GIT_ERROR:
            return []
        path = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("+") and path and is_test(path) and SKIP_RE.search(line):
                found.append(f"{path}: {line[1:].strip()}")
        for path in untracked:
            if not is_test(path):
                continue
            try:
                text = (self.root / path).read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            found += [
                f"{path}: {line.strip()}" for line in text.splitlines() if SKIP_RE.search(line)
            ]
        return found[:MAX_SKIPS]


def describe_build(entry):
    """What the Building checkpoint says about a test-driven build run: [(line, warn)]."""
    if not entry or entry.get("step") not in ("build", "integration"):
        return []
    lines = []
    attempts = entry.get("attempts") or []
    result = entry.get("result")
    if attempts:
        marks = " ".join("✓" if a["passed"] else "✗" for a in attempts)
        summary = f"Test-driven Building: {plural(len(attempts), 'attempt')} ({marks})"
        if result in RESULTS:
            summary += f"; {RESULTS[result]}"
        lines.append((summary + ".", result not in ("passed", None)))
    restored = sorted({path for a in attempts for path in a.get("restored") or []})
    if restored:
        lines.append((f"loom put back locked tests that changed: {', '.join(restored)}", True))
    skips = entry.get("skips") or []
    if skips:
        lines.append(("These test changes skip tests or expect them to fail; check them:", True))
        lines += [(f"  {skip}", True) for skip in skips]
    return lines

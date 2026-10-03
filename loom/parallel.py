"""
Parallel Building: when the architecture document has a valid work plan of more than one
package (loom/workplan.py) and more than one builder may work at once (--build-workers,
or /project workers N), Building runs as:

1. The scaffold. One agent, in the project itself, writes what the packages share: the
   manifests, layout and configuration, and the interfaces between the packages. loom
   commits it.
2. Waves of builders. Each package's builder works in its own git worktree, on its own
   branch, from the project's latest commit (loom/worktrees.py), on a thread of its own,
   up to the number of builders at once. A builder may only write its package's files.
   Its questions go to the main thread (loom/workers.py). With test-driven Building, a
   package with its own test command runs the build loop on its tests.
3. Merging. Each built package's branch is merged into the project, one at a time. When
   a merge conflicts, the Integrator agent resolves it.
4. Integration. A last agent runs the whole test command, fixes what's broken between
   the packages, and writes the build summary: the Building phase's document.

The Building phase keeps each package's status, branch, commit, cost and attempts, so
/project run picks up where Building stopped and builds only the unfinished packages.
When the Testing agent's report sends the project back to Building, only the packages
that own the failing files are built again. With no valid plan, one package or one
builder, Building runs as one agent, as it always has.
"""

import dataclasses
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for_all
from pathlib import Path

from loom import worktrees
from loom.coders import phase_prompts
from loom.orchestrator import now
from loom.phases import DOCS_DIR, PHASES_BY_KEY
from loom.repo import ANY_GIT_ERROR, GitRepo
from loom.tools import plural
from loom.workers import Asks, WorkerIO, stop_workers
from loom.worktrees import WorktreeError

PACKAGES_DIR = f"{DOCS_DIR}/packages"
SCAFFOLD_DOCUMENT = f"{DOCS_DIR}/4-scaffold.md"
# A package's statuses: built is built but not merged yet
PACKAGE_STATUSES = ("pending", "running", "built", "merged", "failed", "stopped")
# Paths in a test report, like src/app/cli.py:42
PATH_RE = re.compile(r"(?<![\w/.-])[\w.-]+(?:/[\w.-]+)*\.[A-Za-z]\w*")
CONFLICT_RE = re.compile(r"^(?:<<<<<<<|>>>>>>>) ", re.MULTILINE)


def plan_hash(plan):
    """A hash of a work plan's packages: when it changes, Building starts over."""
    packages = [dataclasses.asdict(package) for package in plan.packages]
    return hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()[:16]


def has_conflict_markers(path):
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return False
    return bool(CONFLICT_RE.search(text))


class ParallelBuilding:
    """Runs the Building phase of its orchestrator's project as parallel builders."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.io = orchestrator.io
        self.state = orchestrator.state
        self.root = orchestrator.root
        self.plan = None
        # Each builder's thread, by package id, so its commands can be killed
        self.threads = {}

    @property
    def git(self):
        return self.orchestrator.coder.repo.repo

    @property
    def data(self):
        return self.state.phase_data("building")

    @property
    def packages(self):
        """Each package's state, by id: its status, branch, commit, cost and attempts."""
        return self.data.setdefault("packages", {})

    def ready(self):
        """Whether Building runs in parallel. If the plan can't be used, says why."""
        orchestrator = self.orchestrator
        coder = orchestrator.coder
        if orchestrator.workers <= 1 or not coder.repo:
            return False
        plan = orchestrator.work_plan()
        if not plan or (plan.valid and len(plan.packages) < 2):
            return False
        if not plan.valid:
            self.io.tool_warning(
                "The architecture document's work plan has problems, so Building runs as one"
                " agent: " + " ".join(plan.problems)
            )
            return False
        if not coder.auto_commits or coder.dry_run:
            self.io.tool_warning(
                "Parallel builders need loom to commit their work (--auto-commits), so"
                " Building runs as one agent."
            )
            return False
        if self.dirty():
            self.io.tool_warning(
                "The project has uncommitted changes, which merging the builders' work would"
                " clash with, so Building runs as one agent. Commit them to build in parallel."
            )
            return False
        self.plan = plan
        return True

    def dirty(self):
        try:
            return self.git.is_dirty(untracked_files=False)
        except ANY_GIT_ERROR:
            return True

    def run(self, phase):
        """Run Building in parallel. Returns whether it finished, like run_phase."""
        orchestrator = self.orchestrator
        self.io.tool_output(
            f"Parallel Building: {self.plan.describe()}, with up to"
            f" {plural(orchestrator.workers, 'builder')} at once.",
            bold=True,
        )
        tdd = self.test_driven()
        if tdd is False:
            return False
        self.prepare()
        if not self.scaffold(phase):
            return False
        for wave in self.plan.waves:
            todo = [pid for pid in wave if self.packages[pid]["status"] != "merged"]
            if not todo:
                continue
            if not self.build_wave(todo, tdd):
                return False
            if not self.merge_wave(todo):
                return False
        return self.integrate(phase, tdd)

    def test_driven(self):
        """With test-driven Building, its acceptance tests come first, written and locked in
        the project before the scaffold. Returns its TestDrivenBuilding, None when Building
        isn't test-driven, or False when it stopped at the acceptance tests."""
        if not self.state.tdd:
            return None
        from loom.tdd import TestDrivenBuilding

        tdd = TestDrivenBuilding(self.orchestrator)
        if not tdd.ready():
            return None
        if tdd.spec.get("status") != "approved" and not tdd.acceptance_tests():
            return False
        return tdd

    # The packages' state

    def prepare(self):
        """Bring the packages' state in line with the plan: a new plan starts over, an
        unfinished package starts again, and a test report that sent the project back
        has the packages that own its failing files built again."""
        parallel = self.data.setdefault("parallel", {})
        digest = plan_hash(self.plan)
        if parallel.get("plan") != digest:
            parallel.clear()
            parallel["plan"] = digest
            self.data["packages"] = {}
        packages = self.packages
        for num, wave in enumerate(self.plan.waves, 1):
            for pid in wave:
                record = packages.setdefault(pid, dict(status="pending", cost=0))
                record.update(title=self.plan.package(pid).title, wave=num)
                if record["status"] in ("running", "failed", "stopped"):
                    record["status"] = "pending"
                if record["status"] == "built" and not self.branch_exists(record.get("branch")):
                    record["status"] = "pending"
        backs = sum(
            1
            for entry in self.state.history
            if entry.get("event") == "back" and entry.get("phase") == "building"
        )
        if backs > parallel.get("backs", 0):
            parallel["backs"] = backs
            self.rebuild()
        self.state.save()

    def branch_exists(self, branch):
        if not branch:
            return False
        try:
            self.git.git.rev_parse("--verify", "--quiet", f"refs/heads/{branch}")
            return True
        except ANY_GIT_ERROR:
            return False

    def rebuild(self):
        """The project came back to Building: build again the packages that own the files
        the test report says fail, or every package when the founder asked for it."""
        packages = self.packages
        built = [pid for pid in self.plan.ids if packages[pid]["status"] == "merged"]
        if not built:
            return
        if "testing" in (self.data.get("attach") or []):
            report = self.orchestrator.document_text(PHASES_BY_KEY["testing"])
            again = self.failing_packages(report)
            if again:
                self.io.tool_output(
                    f"Building again the packages that own the failing files: {', '.join(again)}."
                )
            else:
                self.io.tool_output(
                    "No package owns the files the test report names, so the Integration agent"
                    " fixes them."
                )
        else:
            again = built
        for pid in again:
            packages[pid].update(status="pending", feedback=self.data.get("feedback") or "")

    def failing_packages(self, report):
        """The packages that own the files a test report names, in the plan's order."""
        found = set()
        for match in PATH_RE.findall(report or ""):
            path = match[2:] if match.startswith("./") else match
            owner = self.plan.owner(path)
            if owner:
                found.add(owner.id)
        return [pid for pid in self.plan.ids if pid in found]

    def stop(self, reason):
        """Stop Building for reason. Returns False, for run's callers."""
        self.state.stop("building", reason)
        self.state.save()
        self.io.tool_error(f"Building stopped: {reason}. Continue with /project run.")
        return False

    def end_step(self, step, agent, meter, extra):
        """After agent's run of step (the scaffold, say): stop Building if it failed, was
        stopped or didn't write its document. Either way, log the run. Returns whether it
        did its step."""
        orchestrator = self.orchestrator
        reason, outcome = None, "failed"
        if agent.failed:
            reason = agent.failed
        elif orchestrator.stopped(agent):
            reason, outcome = "stopped by the user", "stopped"
        elif not orchestrator.document_text(step):
            reason = f"the {step.agent} didn't write {step.document}"
        if reason:
            orchestrator.log_run(step, agent, meter, outcome, extra=extra)
            return self.stop(reason)
        orchestrator.log_run(step, agent, meter, "done", extra=extra)
        self.state.save()
        return True

    # 1. The scaffold

    def scaffold(self, phase):
        orchestrator = self.orchestrator
        parallel = self.data["parallel"]
        if parallel.get("scaffold"):
            return True
        step = dataclasses.replace(
            phase,
            title="Scaffold",
            document=SCAFFOLD_DOCUMENT,
            document_title="scaffold notes",
            brief=phase_prompts.SCAFFOLD,
            mode="scaffold",
            agent_name="Scaffold agent",
        )
        self.io.tool_output(
            "Step 1: the Scaffold agent writes what the work packages share.", bold=True
        )
        agent = orchestrator.make_agent(step)
        meter = orchestrator.start_run(agent)
        extra = dict(step="scaffold")
        orchestrator.guarded(step, agent, meter, lambda: orchestrator.do_task(agent, step), extra)
        if not self.end_step(step, agent, meter, extra):
            return False
        parallel["scaffold"] = orchestrator.head()
        self.state.log("building", "scaffold", parallel["scaffold"][:7])
        self.state.save()
        return True

    # 2. Waves of builders

    def package_phase(self, package):
        """The Phase a package's builder works in: Building, writing only its files."""
        document = f"{PACKAGES_DIR}/{package.id}.md"
        deps = ", ".join(package.depends_on)
        brief = phase_prompts.PACKAGE.format(
            id=package.id,
            title=package.title,
            dependencies=f", and the packages it depends on: {deps}" if deps else "",
            globs=", ".join(package.writable),
            document=document,
            acceptance=", ".join(package.acceptance) or "its part of the PRD",
            test_command=f", with `{package.test_command}`" if package.test_command else "",
        )
        return dataclasses.replace(
            PHASES_BY_KEY["building"],
            title=f"Building {package.id}",
            document=document,
            document_title=f"{package.id} package notes",
            brief=brief,
            writable=package.writable,
            mode="package",
            agent_name=f"builder of {package.id}",
        )

    def make_builder(self, package, path, io, locked=None):
        """The agent of a package's builder, working in the worktree at path."""
        from loom.coders import Coder
        from loom.coders.phase_coder import PhaseCoder
        from loom.sessions import Session

        orchestrator = self.orchestrator
        main = orchestrator.coder
        repo = GitRepo(
            io,
            [],
            str(path),
            models=main.repo.models,
            attribute_author=main.repo.attribute_author,
            attribute_committer=main.repo.attribute_committer,
            attribute_commit_message_author=main.repo.attribute_commit_message_author,
            attribute_commit_message_committer=main.repo.attribute_commit_message_committer,
            commit_prompt=main.repo.commit_prompt,
            git_commit_verify=main.repo.git_commit_verify,
            attribute_co_authored_by=main.repo.attribute_co_authored_by,
        )
        return Coder.create(
            from_coder=main,
            coder_class=PhaseCoder,
            edit_format="agent",
            summarize_from_coder=False,
            io=io,
            repo=repo,
            # No repo map, live streaming, MCP servers, hooks or file watching on a thread
            map_tokens=0,
            stream=False,
            mcp=None,
            hooks=None,
            file_watcher=None,
            permissions=main.permissions.copy_for(io),
            phase=self.package_phase(package),
            shared_memory=orchestrator.memory,
            template=orchestrator.template,
            locked=locked,
            fnames=[],
            read_only_fnames=[],
            done_messages=[],
            cur_messages=[],
            session=Session(),
        )

    def build_wave(self, ids, tdd):
        """Build the packages ids at once, each in its own worktree. Returns whether they
        were all built."""
        orchestrator = self.orchestrator
        base = orchestrator.head()
        asks = Asks()
        lock = threading.Lock()
        locked = tdd.spec.get("locked") if tdd else None
        builders = []
        for pid in ids:
            record = self.packages[pid]
            if record["status"] == "built":
                continue
            package = self.plan.package(pid)
            try:
                path = worktrees.create(self.git, self.root, pid, base)
            except WorktreeError as err:
                return self.stop(str(err))
            agent = self.make_builder(package, path, WorkerIO(self.io, pid, asks, lock), locked)
            builders.append((package, agent, orchestrator.start_run(agent)))
            record.update(
                status="running",
                branch=worktrees.branch_name(pid),
                base=base,
                started=now(),
            )
        self.state.save()
        if not builders:
            return True

        names = ", ".join(package.id for package, _, _ in builders)
        self.io.tool_output(f"Building {names}, each in its own worktree.", bold=True)
        results = self.run_builders(builders, asks, tdd)
        return self.record(builders, results)

    def run_builders(self, builders, asks, tdd):
        """Run the builders on threads, answering their questions until they're done.
        Returns {package id: result}. ^C stops them all, records them and goes on up."""
        agents = [agent for _, agent, _ in builders]
        workers = self.orchestrator.workers
        results = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="loom-builder") as pool:
            futures = {
                pool.submit(self.build_package, package, agent, meter, tdd): package.id
                for package, agent, meter in builders
            }
            try:
                while not all(future.done() for future in futures):
                    asks.serve(self.io)
            except KeyboardInterrupt:
                self.io.tool_warning("Stopping the builders…")
                stop_workers(asks, agents, list(self.threads.values()))
                wait_for_all(futures)
                for future, pid in futures.items():
                    results[pid] = self.result_of(future)
                # Stopping the phase is up to whoever handles the interrupt
                self.record(builders, results, interrupted=True)
                raise
            for future, pid in futures.items():
                results[pid] = self.result_of(future)
        return results

    def result_of(self, future):
        try:
            return future.result()
        except BaseException as err:
            return dict(outcome="failed", error=f"{err.__class__.__name__}: {err}", extra=None)

    def build_package(self, package, agent, meter, tdd):
        """On a builder's thread: build the package. It doesn't change the project's state,
        which the main thread records from the result: {outcome, error, extra}."""
        self.threads[package.id] = threading.get_ident()
        orchestrator = self.orchestrator
        phase = agent.phase
        extra = dict(step="package", package=package.id, attempts=[])
        result = dict(outcome="done", error=None, extra=extra)
        try:
            message = orchestrator.task_message(phase, self.packages[package.id])
            if tdd and package.test_command:
                tdd.loop(phase, agent, meter, extra, message=message, command=package.test_command)
            else:
                orchestrator.do_task(agent, phase, message)
        except KeyboardInterrupt:
            result["outcome"] = "stopped"
        except Exception as err:
            result.update(outcome="failed", error=f"{err.__class__.__name__}: {err}")
        finally:
            orchestrator.collect(agent, meter)
        if result["outcome"] == "done":
            if agent.failed:
                result.update(outcome="failed", error=agent.failed)
            elif orchestrator.stopped(agent):
                result["outcome"] = "stopped"
            elif not orchestrator.document_text(phase, agent.root):
                result.update(outcome="failed", error=f"it didn't write {phase.document}")
        return result

    def record(self, builders, results, interrupted=False):
        """Record how each builder of a wave did. Returns whether they all built their
        package; if not, Building stops, unless it was interrupted."""
        orchestrator = self.orchestrator
        failed = []
        for package, agent, meter in builders:
            record = self.packages[package.id]
            result = results.get(package.id) or dict(outcome="stopped", error=None, extra=None)
            extra = result["extra"] or dict(step="package", package=package.id, attempts=[])
            orchestrator.log_run(agent.phase, agent, meter, result["outcome"], extra=extra)
            spent = orchestrator.spent(agent, meter)
            record["cost"] = round((record.get("cost") or 0) + spent["cost"], 6)
            record["runs"] = (record.get("runs") or 0) + 1
            if extra.get("attempts"):
                record["attempts"] = len(extra["attempts"])
                record["result"] = extra.get("result")
            if result["outcome"] == "done":
                record.update(status="built", commit=orchestrator.head(agent), finished=now())
                record.pop("error", None)
                record.pop("feedback", None)
            else:
                record.update(status=result["outcome"], error=result.get("error"))
                failed.append(package.id)
        self.state.save()
        if not failed or interrupted:
            return not failed
        why = "; ".join(
            f"{pid} {self.packages[pid]['status']}"
            + (f" ({self.packages[pid]['error']})" if self.packages[pid].get("error") else "")
            for pid in failed
        )
        return self.stop(f"not every package was built: {why}")

    # 3. Merging

    def merge_wave(self, ids):
        """Merge the wave's built packages into the project, one at a time, and remove
        their worktrees. Returns whether they all merged."""
        orchestrator = self.orchestrator
        for pid in ids:
            record = self.packages[pid]
            if record["status"] != "built":
                continue
            self.io.tool_output(f"Merging the {pid} package into the project.")
            try:
                conflicts = worktrees.merge(
                    self.git, record["branch"], f"Merge the {pid} work package"
                )
            except WorktreeError as err:
                return self.stop(str(err))
            if conflicts and not self.resolve(pid, conflicts):
                return False
            record.update(status="merged", merged=orchestrator.head())
            worktrees.remove(self.git, self.root, pid)
            self.state.log("building", "merged", pid)
            self.state.save()
        return True

    def resolve(self, pid, conflicts):
        """Have the Integrator agent resolve a merge's conflicts, then finish the merge.
        Returns whether it did; if not, the merge is undone and Building stops."""
        orchestrator = self.orchestrator
        self.io.tool_warning(
            f"Merging {pid} conflicts in {', '.join(conflicts)}: the Integrator agent resolves"
            " it."
        )
        step = dataclasses.replace(
            PHASES_BY_KEY["building"],
            title="Integrator",
            mode="merge",
            agent_name="Integrator agent",
        )
        agent = orchestrator.make_agent(step)
        # loom finishes the merge itself: a commit of some files can't, mid-merge
        agent.auto_commits = False
        agent.dirty_commits = False
        meter = orchestrator.start_run(agent)
        extra = dict(step="merge", package=pid, conflicts=conflicts)
        message = phase_prompts.merge_conflicts.format(id=pid, files=", ".join(conflicts))
        orchestrator.guarded(
            step,
            agent,
            meter,
            lambda: agent.run(with_message=message, preproc=False),
            extra,
        )
        left = [path for path in conflicts if has_conflict_markers(self.root / path)]
        stopped = agent.failed or orchestrator.stopped(agent)
        orchestrator.log_run(step, agent, meter, "done" if not (left or stopped) else "failed", extra=extra)
        if left or stopped:
            worktrees.abort_merge(self.git)
            why = f"still conflicts in {', '.join(left)}" if left else "the Integrator agent stopped"
            return self.stop(f"merging {pid} {why}")
        try:
            worktrees.finish_merge(self.git, conflicts)
        except WorktreeError as err:
            worktrees.abort_merge(self.git)
            return self.stop(str(err))
        return True

    # 4. Integration

    def integrate(self, phase, tdd):
        """The last step: the Integration agent makes the merged packages work together and
        writes the build summary. Returns whether Building finished."""
        orchestrator = self.orchestrator
        self.io.tool_output(
            "Last step: the Integration agent brings the packages together.", bold=True
        )
        command = orchestrator.test_command() or "the project's tests"
        step = dataclasses.replace(
            phase,
            title="Integration",
            brief=phase_prompts.INTEGRATION.format(
                packages=", ".join(self.plan.ids), command=command
            ),
            mode="integration",
            agent_name="Integration agent",
        )
        agent = orchestrator.make_agent(step, locked=tdd.spec.get("locked") if tdd else None)
        meter = orchestrator.start_run(agent)
        if tdd:
            extra = dict(step="integration", attempts=[])
            work = lambda: tdd.loop(step, agent, meter, extra)  # noqa: E731
        else:
            extra = dict(step="integration")
            work = lambda: orchestrator.do_task(agent, step)  # noqa: E731
        orchestrator.guarded(step, agent, meter, work, extra)
        if tdd:
            extra["skips"] = tdd.skips()
        done = orchestrator.end_run(step, agent, meter, extra)
        if done:
            self.data["parallel"]["integrated"] = now()
            self.state.save()
        return done


def describe_packages(state):
    """The lines /project status shows for a project's packages, or []."""
    packages = state.phase_data("building").get("packages")
    if not packages:
        return []
    from loom.orchestrator import format_cost

    width = max(len(pid) for pid in packages)
    lines = []
    for pid, record in packages.items():
        line = f"    {pid:<{width}}  {record.get('status', 'pending'):<8}"
        if record.get("cost"):
            line += f"  {format_cost(record['cost'])}"
        if record.get("attempts"):
            line += f"  {plural(record['attempts'], 'attempt')}"
        if record.get("error"):
            line += f"  {record['error']}"
        lines.append(line.rstrip())
    return lines

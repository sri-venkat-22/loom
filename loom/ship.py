"""
Shipping a /project: Launch's second step. The Launch agent prepares the deployment (its
files and document); /project ship [fly] [--pr|--release] ships it:

1. loom checks what's needed: the provider's command and its login (and gh's, for GitHub).
   When something is missing, it says what to install or how to log in, and stops. loom
   never stores, asks for or writes tokens.
2. It runs the local checks: the template's Launch checks, docker build when there's a
   Dockerfile, and the test command.
3. It shows the plan (the provider, app, region, account and GitHub steps) and asks you to
   type the app's name. It never ships on its own under --yes-always or in bypass mode;
   a run with nobody to ask needs --allow-deploy.
4. It deploys, then smoke tests the app: GET /health, then /.
5. With --pr it pushes the loom/launch branch and opens a pull request; with --release it
   tags the commit and makes a GitHub release. With no GitHub remote, it asks before
   making a private repository.
6. It saves the URL in the project's state and in the deployment document, and records
   each thing it did outside the project as a decision of yours.

/project rollback deploys the release before the current one, after the same checks.
"""

import re

from loom import deploy
from loom.deploy import DEFAULT_PROVIDER, DeployError, environment_names, smoke_test
from loom.deploy.github import LAUNCH_BRANCH, GitHub, project_version
from loom.memory import FOUNDER
from loom.orchestrator import TransitionError, now
from loom.phases import PHASES_BY_KEY

LAUNCH = PHASES_BY_KEY["launch"]
# Who did it when nobody was asked
ALLOWED_SOURCE = "loom (--allow-deploy)"
MAX_DEPLOYMENTS = 20


class Ship:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.io = orchestrator.io
        self.state = orchestrator.state
        self.root = orchestrator.root
        self.coder = orchestrator.coder

    @property
    def git(self):
        return self.coder.repo.repo if self.coder.repo else None

    # Who may ship

    @property
    def unattended(self):
        """Whether loom answers its own questions: --yes-always, or bypass mode."""
        permissions = getattr(self.coder, "permissions", None)
        bypass = getattr(self.io, "bypass_permissions", False) or (
            permissions is not None and permissions.mode == "bypass"
        )
        return self.io.yes is True or bool(bypass)

    def may_ship(self, what="ship"):
        if self.unattended and not self.orchestrator.settings.get("allow_deploy"):
            self.io.tool_error(
                f"loom doesn't {what} on its own under --yes-always or bypass permissions."
                f" Run /project {what} where you can answer, or allow it with --allow-deploy."
            )
            return False
        return True

    @property
    def source(self):
        return ALLOWED_SOURCE if self.unattended else FOUNDER

    def confirm(self, word, question):
        """Have the founder type word to go ahead. --allow-deploy goes ahead unattended."""
        if self.unattended:
            return True
        answer = self.io.prompt_ask(
            f"{question} Type {word} to go ahead, or anything else to cancel:"
        )
        if answer.strip() != word:
            self.io.tool_output("Cancelled: nothing was shipped.")
            return False
        return True

    def blocked(self, lines):
        self.io.tool_error("loom can't ship yet:")
        for line in lines:
            self.io.tool_error(f"  {line}")
        return False

    # /project ship

    def ship(self, provider_name=None, pr=False, release=False):
        """Ship the project. Deploys to provider_name (the default provider unless only a
        GitHub step is asked for), then opens a pull request or makes a release. Returns
        whether everything went."""
        if self.state.status(LAUNCH.key) not in ("review", "approved"):
            raise TransitionError(
                "Ship once Launch has prepared the deployment: /project run gets there."
            )
        if pr and release:
            raise TransitionError("Ship with --pr or --release, not both.")
        deploying = provider_name is not None or not (pr or release)
        provider = deploy.get_provider(provider_name or DEFAULT_PROVIDER) if deploying else None
        github = GitHub(self.git, self.root) if (pr or release) else None
        if github and not self.git:
            raise TransitionError("GitHub needs the project in a git repo.")

        if not self.may_ship():
            return False
        missing = (provider.missing() if provider else []) + (github.missing() if github else [])
        if missing:
            return self.blocked(missing)
        plan = provider.plan(self.root) if provider else None
        if plan and plan.problems:
            return self.blocked(plan.problems)

        if not self.local_checks(plan):
            return False
        self.show_plan(provider, plan, github, pr, release)
        word = plan.app if plan else (LAUNCH_BRANCH if pr else "release")
        if not self.confirm(word, "Ship it?"):
            return False

        record = dict(time=now(), source=self.source)
        if provider and not self.deploy(provider, plan, record):
            return False
        if github and not self.github(github, pr, release, record):
            self.save(record)
            return False
        self.save(record)
        return record.get("healthy", True)

    def show_plan(self, provider, plan, github, pr, release):
        self.io.tool_output("The plan:", bold=True)
        if provider:
            account = provider.whoami()
            region = f", region {plan.region}" if plan.region else ""
            self.io.tool_output(
                f"  Deploy to {provider.title} as {plan.app}{region}, from {plan.config},"
                f" as {account}."
            )
            names = environment_names(provider)
            if names:
                self.io.tool_output(f"  {provider.cli} also reads {', '.join(names)}.")
        if github:
            remote = github.remote() or "a new private repository"
            if pr:
                self.io.tool_output(f"  Push {LAUNCH_BRANCH} to {remote} and open a pull request.")
            if release:
                self.io.tool_output(f"  Tag {self.tag()} and make a GitHub release of it.")

    # The local checks

    def local_checks(self, plan):
        """The template's Launch checks, docker build, and the test command. Returns
        whether they all pass."""
        orchestrator = self.orchestrator
        template = orchestrator.template
        commands = []
        if template and template.checks_for("launch") and orchestrator.checks_allowed(template):
            commands += template.checks_for("launch")
        if (self.root / "Dockerfile").is_file() and not any("docker build" in c for c in commands):
            if deploy.find_cli("docker"):
                commands.append(f"docker build -t {(plan.app if plan else None) or 'app'} .")
            else:
                self.io.tool_output(
                    "Docker isn't installed, so loom can't check that the image builds."
                )
        self.io.tool_output("Local checks before shipping:", bold=True)
        failed = []
        for command in commands:
            passed, _ = orchestrator.run_tests(command)
            if not passed:
                failed.append(command)
        if orchestrator.test_command():
            passed, _ = orchestrator.run_tests()
            if not passed:
                failed.append(orchestrator.test_command())
        if failed:
            self.io.tool_error(
                f"loom won't ship: {', '.join(failed)} didn't pass. Fix them, then /project"
                " ship again."
            )
            return False
        return True

    # Deploying

    def deploy(self, provider, plan, record):
        self.io.tool_output(f"Deploying to {provider.title}…", bold=True)
        record.update(
            provider=provider.name, title=provider.title, app=plan.app, region=plan.region
        )
        try:
            url = provider.deploy(self.root)
        except DeployError as err:
            self.io.tool_error(str(err))
            self.decide(f"Tried to deploy to {provider.title} as {plan.app}, which failed: {err}")
            return False
        record["url"] = url
        healthy, smoke = smoke_test(url)
        record.update(healthy=healthy, smoke=smoke, status=provider.status(url))
        self.decide(f"Deployed to {provider.title} as {plan.app} at {url}")
        if healthy:
            self.io.tool_output(f"Shipped: {url} ({smoke}).", bold=True)
        else:
            self.io.tool_error(
                f"Deployed to {url}, but the smoke test failed: {smoke}. Look at the app's"
                f" logs ({provider.cli} logs), and go back to the release before with /project"
                " rollback."
            )
        return True

    # GitHub

    def tag(self):
        return f"v{project_version(self.root) or '0.1.0'}"

    def github(self, github, pr, release, record):
        if not github.remote():
            name = re.sub(r"[^A-Za-z0-9._-]+", "-", self.root.name).strip("-") or "project"
            if not self.io.confirm_ask(
                (
                    f"The project has no GitHub remote. Make the private repository {name} on"
                    " GitHub with gh?"
                ),
                default="n",
                explicit_yes_required=True,
            ):
                self.io.tool_output("No repository was made; add a remote, then ship again.")
                return False
            try:
                url = github.create_repo(name)
            except DeployError as err:
                self.io.tool_error(str(err))
                return False
            record["repo"] = url
            self.decide(f"Made the private GitHub repository {name} ({url})")
        title = f"Launch: {self.state.idea.splitlines()[0][:70]}"
        try:
            if pr:
                record["pr"] = github.open_pr(title, self.summary(record))
                self.decide(f"Pushed {LAUNCH_BRANCH} and opened the pull request {record['pr']}")
                self.io.tool_output(f"Pull request: {record['pr']}", bold=True)
            if release:
                tag = self.tag()
                notes = self.root / LAUNCH.document
                record["release"] = github.release(tag, title, notes if notes.is_file() else None)
                record["tag"] = tag
                self.decide(f"Tagged {tag} and made the GitHub release {record['release']}")
                self.io.tool_output(f"Release: {record['release']}", bold=True)
        except DeployError as err:
            self.io.tool_error(str(err))
            return False
        return True

    def summary(self, record):
        lines = ["Shipped with loom's /project ship.", ""]
        if record.get("url"):
            lines.append(f"Deployed to {record['title']} at {record['url']}.")
        lines.append(f"See {LAUNCH.document} for how to deploy, configure and roll back.")
        return "\n".join(lines)

    # Recording it

    def decide(self, text):
        self.orchestrator.decide(LAUNCH, "decision", text, source=self.source)

    def save(self, record):
        """Keep the deployment in the project's state and its deployment document."""
        self.state.data["deployment"] = record
        history = self.state.data.setdefault("deployments", [])
        history.append(record)
        del history[:-MAX_DEPLOYMENTS]
        self.state.log(LAUNCH.key, "shipped", record.get("url") or record.get("pr") or "")
        self.state.save()
        self.write_down(self.describe(record))

    def describe(self, record):
        """The deployment, as a line of the deployment document's Shipped section."""
        parts = []
        if record.get("url"):
            region = f", region {record['region']}" if record.get("region") else ""
            smoke = "passed" if record.get("healthy") else "failed"
            parts.append(
                f"deployed to {record['title']} as `{record['app']}`{region}, at"
                f" {record['url']}; smoke test {smoke} ({record.get('smoke')})"
            )
        if record.get("pr"):
            parts.append(f"pull request {record['pr']}")
        if record.get("release"):
            parts.append(f"release {record.get('tag')}: {record['release']}")
        return f"- **{record['time']}**: " + "; ".join(parts) + "."

    def write_down(self, line):
        """Add line to the deployment document's Shipped section, and commit it."""
        path = self.root / LAUNCH.document
        text = path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else ""
        text = text.replace("\r\n", "\n").rstrip("\n")
        if "\n## Shipped\n" not in f"\n{text}\n":
            text += "\n\n## Shipped\n"
        text += f"\n{line}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        self.orchestrator.remember_document(LAUNCH)
        self.orchestrator.commit(path, "Record the launch in the deployment document")

    # /project rollback

    def rollback(self):
        """Go back to the release before the current deployment. Returns whether it did."""
        record = self.state.data.get("deployment")
        if not record or not record.get("provider"):
            raise TransitionError("Nothing has been deployed yet: /project ship deploys it.")
        provider = deploy.get_provider(record["provider"], app=record.get("app"))
        if not self.may_ship("rollback"):
            return False
        missing = provider.missing()
        if missing:
            return self.blocked(missing)
        self.io.tool_output(
            (
                f"Roll back {provider.title} app {record['app']} to its release before the current"
                " one."
            ),
            bold=True,
        )
        if not self.confirm(record["app"], "Roll back?"):
            return False
        try:
            what = provider.rollback(self.root)
        except DeployError as err:
            self.io.tool_error(str(err))
            return False
        healthy, smoke = smoke_test(record["url"]) if record.get("url") else (None, "")
        self.decide(f"Rolled back {provider.title} app {record['app']} to {what}")
        record.update(rolled_back=now(), rolled_back_to=what, healthy=healthy, smoke=smoke)
        self.state.log(LAUNCH.key, "rolled back", what)
        self.state.save()
        self.write_down(
            f"- **{record['rolled_back']}**: rolled back to {what}; smoke test {smoke}."
        )
        self.io.tool_output(f"Rolled back to {what}.", bold=True)
        return True


def parse_ship(args):
    """(provider name or None, pr, release) from /project ship's arguments."""
    provider, pr, release = None, False, False
    for word in args.split():
        if word == "--pr":
            pr = True
        elif word == "--release":
            release = True
        elif word.lower() in deploy.PROVIDERS and not provider:
            provider = word.lower()
        else:
            raise TransitionError(
                f"Unexpected {word!r}: /project ship [{'|'.join(deploy.PROVIDERS)}]"
                " [--pr|--release]"
            )
    return provider, pr, release

"""GitHub, through gh (https://cli.github.com): the project's repository, a pull request
from the loom/launch branch, or a release.

git pushes with the user's own credentials, and gh logs in on its own: loom never handles
tokens."""

import json
import os
import re
import tempfile
from pathlib import Path

from loom.repo import ANY_GIT_ERROR

from . import DeployError, find_cli, run_cli

LAUNCH_BRANCH = "loom/launch"
URL_RE = re.compile(r"https://\S+")


def last_url(output):
    urls = URL_RE.findall(output or "")
    return urls[-1].rstrip(".") if urls else None


def project_version(root):
    """The project's version, from pyproject.toml or package.json, or None."""
    root = Path(root)
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_bytes().decode("utf-8", errors="replace")
        match = re.search(r"""^version\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
        if match:
            return match.group(1)
    package = root / "package.json"
    if package.is_file():
        try:
            version = json.loads(package.read_bytes()).get("version")
        except (ValueError, AttributeError):
            version = None
        if isinstance(version, str):
            return version
    return None


class GitHub:
    cli = "gh"
    title = "GitHub"
    install_hint = "https://cli.github.com"
    logins = ("gh auth login",)
    env_vars = ("GH_TOKEN", "GITHUB_TOKEN")

    def __init__(self, git, root):
        self.git = git
        self.root = Path(root)

    def run(self, *args):
        return run_cli(["gh"] + list(args), cwd=self.root)

    def available(self):
        return bool(find_cli("gh"))

    def whoami(self):
        code, output = self.run("api", "user", "--jq", ".login")
        lines = output.strip().splitlines()
        return lines[-1].strip() if not code and lines else None

    def missing(self):
        """What the user has to do before loom can use GitHub, as lines, or []."""
        if not self.available():
            return [f"Install gh: {self.install_hint}"]
        if self.whoami():
            return []
        return [
            f"gh isn't logged in. Log in with: {' or '.join(self.logins)}",
            (
                f"or set {' or '.join(self.env_vars)} in the environment loom runs in. loom doesn't"
                " store, ask for or write tokens."
            ),
        ]

    def remote(self):
        """The URL of the origin remote, or None."""
        try:
            return self.git.git.remote("get-url", "origin").strip() or None
        except ANY_GIT_ERROR:
            return None

    def create_repo(self, name):
        """Make a private GitHub repository for the project, as its origin. Returns its URL."""
        code, output = self.run(
            "repo", "create", name, "--private", "--source", str(self.root), "--remote", "origin"
        )
        if code:
            raise DeployError(f"gh repo create failed (exit code {code}): {output.strip()}")
        return last_url(output) or self.remote()

    def push_launch_branch(self):
        """Point loom/launch at the project's commit and push it."""
        try:
            self.git.git.branch("-f", LAUNCH_BRANCH, "HEAD")
            self.git.git.push("--force", "-u", "origin", LAUNCH_BRANCH)
        except ANY_GIT_ERROR as err:
            raise DeployError(f"Unable to push {LAUNCH_BRANCH}: {err}")

    def default_branch(self):
        code, output = self.run(
            "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"
        )
        lines = output.strip().splitlines()
        return lines[-1].strip() if not code and lines else "main"

    def open_pr(self, title, body):
        """Open a pull request from loom/launch, or find the open one. Returns its URL."""
        self.push_launch_branch()
        base = self.default_branch()
        # In a file: a body of several lines doesn't survive every shell's arguments
        handle, name = tempfile.mkstemp(suffix=".md", prefix="loom-pr-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(body.encode("utf-8"))
            code, output = self.run(
                "pr",
                "create",
                "--head",
                LAUNCH_BRANCH,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                name,
            )
        finally:
            os.unlink(name)
        url = last_url(output)
        if code and not (url and "already exists" in output):
            raise DeployError(f"gh pr create failed (exit code {code}): {output.strip()}")
        return url

    def release(self, tag, title, notes_file):
        """Tag the project's commit and make a GitHub release of it. Returns its URL."""
        try:
            if tag in [t.name for t in self.git.tags]:
                raise DeployError(
                    f"The tag {tag} already exists: change the project's version first."
                )
            self.git.git.tag("-a", tag, "-m", title)
            self.git.git.push("origin", tag)
        except ANY_GIT_ERROR as err:
            raise DeployError(f"Unable to tag and push {tag}: {err}")
        args = ["release", "create", tag, "--title", title]
        args += ["--notes-file", str(notes_file)] if notes_file else ["--generate-notes"]
        code, output = self.run(*args)
        if code:
            raise DeployError(f"gh release create failed (exit code {code}): {output.strip()}")
        return last_url(output)

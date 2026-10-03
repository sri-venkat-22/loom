"""Fly.io, through flyctl (https://fly.io/docs/flyctl/)."""

import json
import re
from pathlib import Path

from . import DeployError, Plan, Provider, find_cli, run_cli

CONFIG = "fly.toml"
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# Lines of a failed command's output loom shows
ERROR_LINES = 15


def toml_value(text, key):
    """The string value of a top-level key in a TOML file, like app in fly.toml."""
    match = re.search(rf"""^\s*{key}\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    return match.group(1) if match else None


def tail(output, lines=ERROR_LINES):
    return "\n".join(output.strip().splitlines()[-lines:])


class FlyProvider(Provider):
    name = "fly"
    title = "Fly.io"
    cli = "flyctl"
    install_hint = "https://fly.io/docs/flyctl/install/"
    logins = ("flyctl auth login",)
    env_vars = ("FLY_API_TOKEN", "FLY_ACCESS_TOKEN")

    def __init__(self, app=None):
        self.app = app

    def command(self):
        """flyctl, or fly, which its installers also make."""
        return "flyctl" if find_cli("flyctl") else "fly"

    def available(self):
        return bool(find_cli("flyctl") or find_cli("fly"))

    def run(self, *args, cwd=None):
        return run_cli([self.command()] + list(args), cwd=cwd)

    def whoami(self):
        code, output = self.run("auth", "whoami")
        lines = output.strip().splitlines()
        if code or not lines:
            return None
        return lines[-1].strip()

    def plan(self, root):
        plan = Plan(self.name, config=CONFIG)
        path = Path(root) / CONFIG
        if not path.is_file():
            plan.problems.append(
                "There's no fly.toml. The Launch agent writes one when Fly.io is the target,"
                " or make one with flyctl launch --no-deploy."
            )
            return plan
        text = path.read_bytes().decode("utf-8", errors="replace")
        self.app = self.app or toml_value(text, "app")
        plan.app = self.app
        plan.region = toml_value(text, "primary_region")
        if not plan.app:
            plan.problems.append('fly.toml has no app name: add app = "NAME".')
        return plan

    def deploy(self, root):
        code, output = self.run("deploy", "--remote-only", "--app", self.app, cwd=root)
        if code:
            raise DeployError(f"flyctl deploy failed (exit code {code}):\n{tail(output)}")
        urls = URL_RE.findall(output)
        return urls[-1].rstrip("/.") if urls else f"https://{self.app}.fly.dev"

    def status(self, url):
        code, output = self.run("status", "--app", self.app, "--json")
        if code:
            return f"flyctl status failed (exit code {code})"
        try:
            machines = json.loads(output).get("Machines") or []
        except (ValueError, AttributeError):
            return "flyctl status said something loom can't read"
        states = [machine.get("state") or "unknown" for machine in machines]
        return f"{len(machines)} machine{'s' if len(machines) != 1 else ''}: {', '.join(states)}"

    def releases(self):
        """The app's releases, newest first."""
        code, output = self.run("releases", "--app", self.app, "--image", "--json")
        if code:
            raise DeployError(f"flyctl releases failed (exit code {code}):\n{tail(output)}")
        try:
            releases = json.loads(output)
        except ValueError:
            raise DeployError("flyctl releases said something loom can't read")
        return sorted(releases, key=lambda release: release.get("Version") or 0, reverse=True)

    def rollback(self, root=None):
        """Deploy the image of the release before the current one. Returns which."""
        good = [
            release
            for release in self.releases()
            if (release.get("Status") or "complete").lower() in ("complete", "succeeded")
        ]
        if len(good) < 2:
            raise DeployError("There's no earlier release of the app to go back to.")
        previous = good[1]
        image = previous.get("ImageRef") or previous.get("Image")
        if not image:
            raise DeployError(
                f"flyctl doesn't say which image release {previous.get('Version')} ran."
            )
        code, output = self.run("deploy", "--app", self.app, "--image", image, cwd=root)
        if code:
            raise DeployError(f"flyctl deploy --image failed (exit code {code}):\n{tail(output)}")
        return f"version {previous.get('Version')} ({image})"

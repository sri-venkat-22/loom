"""
Deploying a /project (see loom/ship.py): the providers loom can ship to, each through its
own command-line tool.

A provider is an adapter with:

  name, title    like fly and Fly.io
  cli            the command it runs, like flyctl
  install_hint   where to get the command
  logins         how to log in, like "flyctl auth login"
  env_vars       the NAMES of environment variables the command reads instead
  available()    whether its command is installed
  whoami()       the account it's logged in as, or None
  plan(root)     what it would deploy: Plan(app, region, config, problems)
  deploy(root)   deploys, and returns the app's URL
  status(url)    a line on how the app is doing
  rollback()     goes back to the release before, and says which

loom never stores, asks for or writes tokens: the provider's command logs in on its own,
or reads its environment variables. When something is missing, loom says what to install
or how to log in, and stops.

Fly.io (loom/deploy/fly.py) is the first provider; Render and Vercel can be added the same
way, in PROVIDERS.
"""

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# How long a provider's command may run, in seconds: a deploy builds an image
CLI_TIMEOUT = 900
# The smoke test: the paths it tries, how often, and how long it waits in between
SMOKE_PATHS = ("/health", "/")
SMOKE_ATTEMPTS = 5
SMOKE_DELAY = 3
SMOKE_TIMEOUT = 10


class DeployError(Exception):
    pass


@dataclass
class Plan:
    """What a provider would deploy."""

    provider: str
    app: str = None
    region: str = None
    config: str = None
    # What stops it deploying, like a missing config file
    problems: list = field(default_factory=list)


def find_cli(name):
    """The path of command name, or None if it isn't installed."""
    return shutil.which(name)


def run_cli(args, cwd=None, timeout=CLI_TIMEOUT):
    """Run a provider's command, args, without stdin. Returns (exit code, output). Its
    environment is loom's own, so it can read the tokens the user set for it."""
    path = find_cli(args[0])
    if not path:
        raise DeployError(f"{args[0]} isn't installed")
    try:
        proc = subprocess.run(
            [path] + list(args[1:]),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DeployError(f"{' '.join(args[:3])} took more than {timeout} seconds")
    except OSError as err:
        raise DeployError(f"Unable to run {args[0]}: {err}")
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


class Provider:
    """A deploy provider. Subclasses fill in the class attributes and the methods."""

    name = None
    title = None
    cli = None
    install_hint = None
    logins = ()
    env_vars = ()

    def available(self):
        return bool(find_cli(self.cli))

    def whoami(self):
        raise NotImplementedError

    def plan(self, root):
        raise NotImplementedError

    def deploy(self, root):
        raise NotImplementedError

    def status(self, url):
        raise NotImplementedError

    def rollback(self):
        raise NotImplementedError

    def missing(self):
        """What the user has to do before loom can deploy, as lines, or [] when ready."""
        if not self.available():
            return [f"Install {self.cli}: {self.install_hint}"]
        if self.whoami():
            return []
        lines = [f"{self.cli} isn't logged in. Log in with: {' or '.join(self.logins)}"]
        if self.env_vars:
            lines.append(
                f"or set {' or '.join(self.env_vars)} in the environment loom runs in."
                " loom doesn't store, ask for or write tokens."
            )
        return lines


def providers():
    """The providers, by name."""
    from .fly import FlyProvider

    return {provider.name: provider for provider in (FlyProvider,)}


PROVIDERS = ("fly",)
DEFAULT_PROVIDER = "fly"


def get_provider(name, app=None):
    try:
        provider = providers()[name]
    except KeyError:
        raise DeployError(
            f"loom can't deploy to {name!r}; it can deploy to: {', '.join(PROVIDERS)}"
        )
    return provider(app=app)


def smoke_test(url, paths=SMOKE_PATHS, attempts=None, delay=None):
    """Check that the app at url answers: GET each of paths until one succeeds, trying
    again for a while, since a new deploy can take a moment to start. Returns (ok, what
    happened)."""
    attempts = SMOKE_ATTEMPTS if attempts is None else attempts
    delay = SMOKE_DELAY if delay is None else delay
    last = "no answer"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(delay)
        for path in paths:
            target = url.rstrip("/") + path
            try:
                request = urllib.request.Request(target, headers={"User-Agent": "loom-smoke-test"})
                with urllib.request.urlopen(request, timeout=SMOKE_TIMEOUT) as response:
                    return True, f"GET {path} answered {response.status}"
            except urllib.error.HTTPError as err:
                last = f"GET {path} answered {err.code}"
            except (urllib.error.URLError, OSError, ValueError) as err:
                reason = getattr(err, "reason", err)
                last = f"GET {path} failed: {reason}"
    return False, last


def environment_names(provider):
    """The provider's environment variables that are set, by name only."""
    return [name for name in provider.env_vars if os.environ.get(name)]

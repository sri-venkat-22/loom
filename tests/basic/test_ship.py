import json
import os
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import git

from loom import deploy
from loom.coders import Coder
from loom.commands import Commands
from loom.io import InputOutput
from loom.models import Model
from loom.orchestrator import Orchestrator, ProjectState
from loom.permissions import Permissions
from loom.phases import PHASES
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory

from .test_agent import make_repo
from .test_orchestrator import IDEA, PRD

PYTHON = sys.executable
PYTEST = f"{PYTHON} -B -m pytest -q -p no:cacheprovider tests"
ARCHITECTURE = f"# Architecture: adder\n## Testing approach\n**Test command:** `{PYTEST}`\n"
DEPLOYMENT = "# Deployment: adder\n## Target and why\nFly.io.\n"
FLY_TOML = 'app = "adder-app"\nprimary_region = "iad"\n'

LOG = """
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([NAME] + args) + "\\n")
"""

FLYCTL = """
if args[:2] == ["auth", "whoami"]:
    user = os.environ.get("FAKE_FLY_USER")
    if not user:
        print("Error: No access token available. Please login with 'flyctl auth login'")
        sys.exit(1)
    print(user)
elif args[:1] == ["deploy"] and "--image" in args:
    print("Updating machines with image " + args[args.index("--image") + 1])
elif args[:1] == ["deploy"]:
    if os.environ.get("FAKE_FLY_DEPLOY_FAIL"):
        print("Error: failed to build the image")
        sys.exit(1)
    print("==> Building image\\n--> Pushing image done")
    print("Visit your newly deployed app at " + os.environ["FAKE_FLY_URL"] + "/")
elif args[:1] == ["status"]:
    print(json.dumps({"Machines": [{"state": "started"}, {"state": "started"}]}))
elif args[:1] == ["releases"]:
    print(json.dumps([
        {"Version": 1, "Status": "complete", "ImageRef": "registry.fly.io/adder-app:deployment-1"},
        {"Version": 2, "Status": "complete", "ImageRef": "registry.fly.io/adder-app:deployment-2"},
    ]))
else:
    sys.exit(2)
"""

GH = """
if args[:2] == ["api", "user"]:
    user = os.environ.get("FAKE_GH_USER")
    if not user:
        print("To get started with GitHub CLI, please run:  gh auth login")
        sys.exit(4)
    print(user)
elif args[:2] == ["repo", "create"]:
    subprocess.run(["git", "remote", "add", "origin", os.environ["FAKE_GH_REMOTE"]], check=True)
    print("https://github.com/me/" + args[2])
elif args[:2] == ["repo", "view"]:
    print("main")
elif args[:2] == ["pr", "create"]:
    print("https://github.com/me/adder/pull/7")
elif args[:2] == ["release", "create"]:
    print("https://github.com/me/adder/releases/tag/" + args[2])
else:
    sys.exit(2)
"""

DOCKER = """
sys.exit(int(os.environ.get("FAKE_DOCKER_EXIT", "0")))
"""


def make_cli(folder, name, body):
    """A fake command on PATH that logs its arguments, then runs body."""
    code = LOG.replace("NAME", repr(name)) + body
    if os.name == "nt":
        (folder / f"{name}.py").write_bytes(code.encode())
        (folder / f"{name}.cmd").write_bytes(f'@"{PYTHON}" "%~dp0{name}.py" %*\r\n'.encode())
    else:
        path = folder / name
        path.write_bytes(f"#!{PYTHON}\n{code}".encode())
        path.chmod(0o755)


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer looks up the host's name, which can take many seconds
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = self.server.status
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok" if status == 200 else b"broken")

    def log_message(self, *args):
        pass


class ShipTestCase(unittest.TestCase):
    def setUp(self):
        self.tools = IgnorantTemporaryDirectory()
        folder = Path(self.tools.name)
        make_cli(folder, "flyctl", FLYCTL)
        make_cli(folder, "gh", GH)
        make_cli(folder, "docker", DOCKER)
        self.log = folder / "calls.log"
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.env = patch.dict(
            os.environ,
            {
                "PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_LOG": str(self.log),
                "FAKE_FLY_USER": "founder@example.com",
                "FAKE_FLY_URL": self.url,
                "FAKE_GH_USER": "founder",
                "LOOM_MEMORY_STORE": "keyword",
            },
        )
        self.env.start()
        for name in ("FAKE_FLY_DEPLOY_FAIL", "FAKE_DOCKER_EXIT"):
            os.environ.pop(name, None)
        self.smoke = patch.multiple(deploy, SMOKE_ATTEMPTS=1, SMOKE_DELAY=0)
        self.smoke.start()

    def tearDown(self):
        self.smoke.stop()
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tools.cleanup()

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def project(self, io=None, settings=None, launch=True):
        """A project whose Launch has prepared the deployment, in a git repo."""
        if io is None:
            io = InputOutput(yes=None)
            io.prompt_ask = MagicMock(return_value="adder-app")
            io.confirm_ask = MagicMock(return_value=True)
        self.io = io
        make_repo()
        Path("tests").mkdir()
        Path("tests/test_ok.py").write_bytes(b"def test_ok():\n    assert True\n")
        Path("Dockerfile").write_bytes(b"FROM python:3.12-slim\n")
        Path("fly.toml").write_bytes(FLY_TOML.encode())
        coder = Coder.create(
            Model("gpt-4o-mini"),
            "agent",
            io=io,
            map_tokens=0,
            permissions=Permissions(
                io, mode="accept-edits", allow=[f"bash({PYTHON}*)", "bash(docker build*)"]
            ),
            project_settings=settings or {},
        )
        orchestrator = Orchestrator(coder)
        orchestrator.new_project(IDEA)
        state = orchestrator.state
        texts = ["# Idea report\n**Verdict:** GO\n", PRD, ARCHITECTURE, "# Build\n", "# Test\n"]
        texts.append(DEPLOYMENT)
        for phase, text in zip(PHASES, texts):
            Path(phase.document).parent.mkdir(exist_ok=True)
            Path(phase.document).write_bytes(text.encode())
            if phase.key == "launch" and not launch:
                break
            state.start(phase.key)
            state.finish(phase.key)
            if phase.key != "launch":
                state.approve(phase.key)
        state.save()
        repo = git.Repo(".")
        repo.git.add(".")
        repo.git.commit("-m", "The project")
        self.commands = Commands(io, coder)
        return coder, orchestrator


class TestShip(ShipTestCase):
    def test_ship_to_fly(self):
        with GitTemporaryDirectory():
            coder, orchestrator = self.project()
            self.io.tool_output = MagicMock()
            self.commands.cmd_project("ship")

            calls = self.calls()
            self.assertIn(["flyctl", "auth", "whoami"], calls)
            self.assertIn(["docker", "build", "-t", "adder-app", "."], calls)
            self.assertIn(["flyctl", "deploy", "--remote-only", "--app", "adder-app"], calls)
            # The checks ran before the deploy
            self.assertLess(
                calls.index(["docker", "build", "-t", "adder-app", "."]),
                calls.index(["flyctl", "deploy", "--remote-only", "--app", "adder-app"]),
            )
            question = self.io.prompt_ask.call_args[0][0]
            self.assertIn("Type adder-app to go ahead", question)
            shown = [c[0][0] for c in self.io.tool_output.call_args_list if c[0]]
            self.assertIn(
                (
                    "  Deploy to Fly.io as adder-app, region iad, from fly.toml, as"
                    " founder@example.com."
                ),
                shown,
            )

            state = ProjectState.load(".")
            record = state.data["deployment"]
            self.assertEqual(record["url"], self.url)
            self.assertTrue(record["healthy"])
            self.assertEqual(record["smoke"], "GET /health answered 200")
            self.assertEqual(record["status"], "2 machines: started, started")
            self.assertEqual(record["source"], "founder")
            self.assertEqual(len(state.data["deployments"]), 1)

            # Written down in the deployment document, and committed
            doc = Path("loom-project/6-deployment.md").read_text()
            self.assertIn("## Shipped", doc)
            self.assertIn(f"deployed to Fly.io as `adder-app`, region iad, at {self.url}", doc)
            self.assertFalse(git.Repo(".").is_dirty())

            from loom.project_report import ProjectReport

            # /project ship ran on an orchestrator of its own
            report = ProjectReport.from_orchestrator(Orchestrator(coder)).markdown()
            self.assertIn(
                f"- **Deployed:** {self.url} (Fly.io, adder-app; smoke test passed)", report
            )

            decision = orchestrator.memory.decisions()[-1]
            self.assertEqual(decision["phase"], "launch")
            self.assertEqual(decision["source"], "founder")
            self.assertEqual(decision["text"], f"Deployed to Fly.io as adder-app at {self.url}")

    def test_not_logged_in(self):
        with GitTemporaryDirectory():
            os.environ.pop("FAKE_FLY_USER")
            self.project()
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship fly")
            errors = "\n".join(c[0][0] for c in self.io.tool_error.call_args_list)
            self.assertIn("flyctl isn't logged in. Log in with: flyctl auth login", errors)
            self.assertIn("FLY_API_TOKEN or FLY_ACCESS_TOKEN", errors)
            self.assertNotIn("deploy", [c[1] for c in self.calls() if len(c) > 1])
            self.io.prompt_ask.assert_not_called()

    def test_not_installed_and_no_config(self):
        with GitTemporaryDirectory():
            self.project()
            self.io.tool_error = MagicMock()
            real = deploy.find_cli
            with patch.object(
                deploy, "find_cli", lambda name: None if "fly" in name else real(name)
            ):
                with patch("loom.deploy.fly.find_cli", lambda name: None):
                    self.commands.cmd_project("ship")
            errors = "\n".join(c[0][0] for c in self.io.tool_error.call_args_list)
            self.assertIn("Install flyctl: https://fly.io/docs/flyctl/install/", errors)

            Path("fly.toml").unlink()
            self.commands.cmd_project("ship")
            self.assertIn("There's no fly.toml", self.io.tool_error.call_args[0][0])
            self.assertEqual([c for c in self.calls() if c[:2] == ["flyctl", "deploy"]], [])

    def test_failed_smoke_test(self):
        with GitTemporaryDirectory():
            self.server.status = 500
            self.project()
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship")
            record = ProjectState.load(".").data["deployment"]
            self.assertFalse(record["healthy"])
            self.assertEqual(record["smoke"], "GET / answered 500")
            error = self.io.tool_error.call_args[0][0]
            self.assertIn("the smoke test failed: GET / answered 500", error)
            self.assertIn("/project rollback", error)
            self.assertIn("smoke test failed", Path("loom-project/6-deployment.md").read_text())

    def test_failed_deploy(self):
        with GitTemporaryDirectory():
            os.environ["FAKE_FLY_DEPLOY_FAIL"] = "1"
            coder, orchestrator = self.project()
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship")
            self.assertIn("flyctl deploy failed (exit code 1)", self.io.tool_error.call_args[0][0])
            self.assertNotIn("deployment", ProjectState.load(".").data)
            self.assertIn("which failed", orchestrator.memory.decisions()[-1]["text"])

    def test_local_checks_must_pass(self):
        with GitTemporaryDirectory():
            os.environ["FAKE_DOCKER_EXIT"] = "1"
            self.project()
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship")
            self.assertIn("loom won't ship: docker build", self.io.tool_error.call_args[0][0])
            self.assertEqual([c for c in self.calls() if c[:2] == ["flyctl", "deploy"]], [])
            self.io.prompt_ask.assert_not_called()

    def test_rollback(self):
        with GitTemporaryDirectory():
            coder, orchestrator = self.project()
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("rollback")
            self.assertIn("Nothing has been deployed", self.io.tool_error.call_args[0][0])
            self.commands.cmd_project("ship")
            self.commands.cmd_project("rollback")
            calls = self.calls()
            self.assertIn(["flyctl", "releases", "--app", "adder-app", "--image", "--json"], calls)
            self.assertIn(
                [
                    "flyctl",
                    "deploy",
                    "--app",
                    "adder-app",
                    "--image",
                    "registry.fly.io/adder-app:deployment-1",
                ],
                calls,
            )
            record = ProjectState.load(".").data["deployment"]
            self.assertEqual(
                record["rolled_back_to"], "version 1 (registry.fly.io/adder-app:deployment-1)"
            )
            self.assertEqual(
                orchestrator.memory.decisions()[-1]["text"],
                (
                    "Rolled back Fly.io app adder-app to version 1"
                    " (registry.fly.io/adder-app:deployment-1)"
                ),
            )
            self.assertIn(
                "rolled back to version 1", Path("loom-project/6-deployment.md").read_text()
            )


class TestConfirmation(ShipTestCase):
    def test_wrong_name_cancels(self):
        with GitTemporaryDirectory():
            self.project()
            self.io.prompt_ask = MagicMock(return_value="adder")
            self.commands.cmd_project("ship")
            self.assertEqual([c for c in self.calls() if c[:2] == ["flyctl", "deploy"]], [])
            self.assertNotIn("deployment", ProjectState.load(".").data)

    def test_yes_always_never_ships_on_its_own(self):
        with GitTemporaryDirectory():
            io = InputOutput(yes=True)
            io.tool_error = MagicMock()
            io.prompt_ask = MagicMock()
            self.project(io)
            self.commands.cmd_project("ship")
            self.assertIn("loom doesn't ship on its own", io.tool_error.call_args[0][0])
            self.assertIn("--allow-deploy", io.tool_error.call_args[0][0])
            self.assertEqual(self.calls(), [])

    def test_bypass_mode_never_ships_on_its_own(self):
        with GitTemporaryDirectory():
            coder, orchestrator = self.project()
            coder.permissions.mode = "bypass"
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship")
            self.assertIn("loom doesn't ship on its own", self.io.tool_error.call_args[0][0])
            self.assertEqual(self.calls(), [])

    def test_allow_deploy_ships_unattended(self):
        with GitTemporaryDirectory():
            io = InputOutput(yes=True)
            io.prompt_ask = MagicMock()
            coder, orchestrator = self.project(io, settings=dict(allow_deploy=True))
            self.commands.cmd_project("ship")
            io.prompt_ask.assert_not_called()
            self.assertIn(["flyctl", "deploy", "--remote-only", "--app", "adder-app"], self.calls())
            record = ProjectState.load(".").data["deployment"]
            self.assertEqual(record["source"], "loom (--allow-deploy)")
            self.assertEqual(orchestrator.memory.decisions()[-1]["source"], "loom (--allow-deploy)")

    def test_ship_after_launch_is_prepared(self):
        with GitTemporaryDirectory():
            self.project(launch=False)
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship")
            self.assertIn("once Launch has prepared", self.io.tool_error.call_args[0][0])
            self.commands.cmd_project("ship heroku")
            self.assertIn("Unexpected 'heroku'", self.io.tool_error.call_args[0][0])


class TestGitHub(ShipTestCase):
    def bare_remote(self):
        self.remote = IgnorantTemporaryDirectory()
        git.Repo.init(self.remote.name, bare=True)
        self.addCleanup(self.remote.cleanup)
        return self.remote.name

    def test_pull_request(self):
        with GitTemporaryDirectory():
            remote = self.bare_remote()
            coder, orchestrator = self.project()
            repo = git.Repo(".")
            repo.create_remote("origin", remote)
            self.io.prompt_ask = MagicMock(return_value="loom/launch")
            self.commands.cmd_project("ship --pr")
            calls = self.calls()
            # Only GitHub: no deploy
            self.assertEqual([c for c in calls if c[0] == "flyctl"], [])
            pr = next(c for c in calls if c[1:3] == ["pr", "create"])
            self.assertEqual(pr[3:7], ["--head", "loom/launch", "--base", "main"])
            self.assertEqual(pr[pr.index("--title") + 1], f"Launch: {IDEA}")
            remote_repo = git.Repo(remote)
            # The branch is the project as shipped; recording the launch came after
            self.assertEqual(
                remote_repo.commit("loom/launch").hexsha, repo.head.commit.parents[0].hexsha
            )
            self.assertEqual(
                repo.head.commit.message.strip(), "Record the launch in the deployment document"
            )
            record = ProjectState.load(".").data["deployment"]
            self.assertEqual(record["pr"], "https://github.com/me/adder/pull/7")
            self.assertIn(
                "Pushed loom/launch and opened the pull request https://github.com/me/adder/pull/7",
                [d["text"] for d in orchestrator.memory.decisions()],
            )

    def test_release_with_a_deploy(self):
        with GitTemporaryDirectory():
            remote = self.bare_remote()
            coder, orchestrator = self.project()
            Path("pyproject.toml").write_bytes(b'[project]\nname = "adder"\nversion = "1.2.0"\n')
            repo = git.Repo(".")
            repo.git.add("pyproject.toml")
            repo.git.commit("-m", "Version")
            repo.create_remote("origin", remote)
            self.commands.cmd_project("ship fly --release")
            calls = self.calls()
            self.assertIn(["flyctl", "deploy", "--remote-only", "--app", "adder-app"], calls)
            release = next(c for c in calls if c[1:3] == ["release", "create"])
            self.assertEqual(release[3], "v1.2.0")
            self.assertIn("--notes-file", release)
            self.assertIn("v1.2.0", [tag.name for tag in git.Repo(remote).tags])
            record = ProjectState.load(".").data["deployment"]
            self.assertEqual(record["release"], "https://github.com/me/adder/releases/tag/v1.2.0")
            self.assertEqual(record["tag"], "v1.2.0")

    def test_no_remote_asks_before_making_a_repo(self):
        with GitTemporaryDirectory():
            remote = self.bare_remote()
            os.environ["FAKE_GH_REMOTE"] = remote
            coder, orchestrator = self.project()
            self.io.prompt_ask = MagicMock(return_value="loom/launch")
            self.io.confirm_ask = MagicMock(return_value=False)
            self.commands.cmd_project("ship --pr")
            question = self.io.confirm_ask.call_args
            self.assertIn("Make the private repository", question[0][0])
            self.assertTrue(question[1]["explicit_yes_required"])
            self.assertEqual([c for c in self.calls() if c[1:3] == ["repo", "create"]], [])

            self.io.confirm_ask = MagicMock(return_value=True)
            self.commands.cmd_project("ship --pr")
            create = next(c for c in self.calls() if c[1:3] == ["repo", "create"])
            self.assertIn("--private", create)
            self.assertEqual(git.Repo(".").remotes.origin.url, remote)
            texts = [d["text"] for d in orchestrator.memory.decisions()]
            self.assertTrue(any(t.startswith("Made the private GitHub repository") for t in texts))

    def test_gh_not_logged_in(self):
        with GitTemporaryDirectory():
            os.environ.pop("FAKE_GH_USER")
            self.project()
            self.io.tool_error = MagicMock()
            self.commands.cmd_project("ship --release")
            errors = "\n".join(c[0][0] for c in self.io.tool_error.call_args_list)
            self.assertIn("gh isn't logged in. Log in with: gh auth login", errors)
            self.assertIn("GH_TOKEN or GITHUB_TOKEN", errors)


if __name__ == "__main__":
    unittest.main()

"""
MCP client: connect to Model Context Protocol servers and give their tools to the agent.

Servers are configured the way Claude Code and other MCP clients do it:

    {
      "mcpServers": {
        "github": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"],
          "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
        },
        "docs": {
          "type": "http",
          "url": "https://example.com/mcp",
          "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}
        }
      }
    }

loom reads ~/.loom/mcp.json, the project's .mcp.json and any --mcp-config files, later
ones overriding earlier ones. The project's .mcp.json comes with the repo, so loom asks
before starting each of its servers, and remembers the answer "always" in
~/.loom/mcp-approvals.json until the server's config changes.

Servers run over stdio (a command) or streamable HTTP (a url). Each tool is offered to
the model as mcp__<server>__<tool>, and calls go through Permissions like the built-in
tools do.
"""

import hashlib
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from loom import __version__

PROTOCOL_VERSION = "2025-06-18"
CONNECT_TIMEOUT = 30
CALL_TIMEOUT = 300
PROJECT_CONFIG = ".mcp.json"
MAX_TOOL_NAME = 64
MAX_DESCRIPTION = 2000
MAX_INSTRUCTIONS = 4000

ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class McpError(Exception):
    pass


def user_config_file():
    return Path.home() / ".loom" / "mcp.json"


def approvals_file():
    return Path.home() / ".loom" / "mcp-approvals.json"


def expand_env(value, env=None):
    """Replace ${VAR} and ${VAR:-default} in the strings of a server's config, so secrets
    can stay in the environment."""
    env = os.environ if env is None else env
    if isinstance(value, str):

        def replace(match):
            name, default = match.groups()
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise McpError(f"the environment variable {name} isn't set")

        return ENV_RE.sub(replace, value)
    if isinstance(value, list):
        return [expand_env(item, env) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item, env) for key, item in value.items()}
    return value


def load_config_file(path):
    """{server name: config} from an MCP config file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise McpError(f"Unable to read {path}: {err}")
    if not isinstance(data, dict):
        raise McpError(f"{path} should hold a JSON object with an mcpServers object")
    # VS Code calls it "servers"
    servers = data.get("mcpServers", data.get("servers"))
    if not isinstance(servers, dict):
        raise McpError(f"{path} has no mcpServers object")

    res = {}
    for name, config in servers.items():
        if not isinstance(config, dict):
            raise McpError(f"{path}: the config for {name!r} should be an object")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise McpError(f"{path}: server names can only use letters, digits, _ . and -")
        if not (config.get("command") or config.get("url")):
            raise McpError(f"{path}: {name!r} needs a command to run or a url")
        if config.get("disabled"):
            continue
        res[name] = config
    return res


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def tool_name(server, tool):
    """The name the model sees: mcp__server__tool, within the 64 characters APIs allow."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    if len(name) > MAX_TOOL_NAME:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = name[: MAX_TOOL_NAME - 9] + "_" + digest
    return name


def tool_parameters(tool):
    """A tool's inputSchema as function parameters."""
    schema = tool.get("inputSchema")
    schema = dict(schema) if isinstance(schema, dict) else {}
    schema.pop("$schema", None)
    schema["type"] = "object"
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def format_result(result):
    """(text for the model, whether the tool reported an error) from a tools/call result."""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({item.get('mimeType', 'unknown type')}) not shown]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            if "text" in resource:
                parts.append(f"[resource {resource.get('uri', '')}]\n{resource['text']}")
            else:
                parts.append(f"[binary resource {resource.get('uri', '')} not shown]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {item.get('uri', '')} {item.get('name', '')}]".strip())
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], indent=2))
    return "\n".join(parts).strip() or "(no output)", bool(result.get("isError"))


# Connections: JSON-RPC over stdio or HTTP


class Connection:
    """Sends JSON-RPC requests and matches up the responses. Subclasses send messages."""

    def __init__(self, on_notification=None):
        self.on_notification = on_notification or (lambda msg: None)
        self.pending = {}
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.closed = False

    def send(self, msg):
        raise NotImplementedError

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        msg_id = next(self.ids)
        answer = queue.Queue(1)
        with self.lock:
            self.pending[msg_id] = answer
        msg = dict(jsonrpc="2.0", id=msg_id, method=method)
        if params is not None:
            msg["params"] = params
        try:
            self.send(msg)
            response = answer.get(timeout=timeout)
        except queue.Empty:
            self.cancel(msg_id, "timed out")
            raise McpError(f"{method} timed out after {timeout} seconds")
        except KeyboardInterrupt:
            self.cancel(msg_id, "the user interrupted it")
            raise
        finally:
            with self.lock:
                self.pending.pop(msg_id, None)

        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {}
            raise McpError(error.get("message") or str(response["error"]))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method, params=None):
        msg = dict(jsonrpc="2.0", method=method)
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def cancel(self, msg_id, reason):
        try:
            self.notify("notifications/cancelled", dict(requestId=msg_id, reason=reason))
        except McpError:
            pass

    def handle(self, msg):
        """Deal with a message from the server."""
        if isinstance(msg, list):
            for item in msg:
                self.handle(item)
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:
                self.handle_request(msg)
            else:
                self.on_notification(msg)
            return
        with self.lock:
            answer = self.pending.get(msg.get("id"))
        if answer and answer.empty():
            answer.put(msg)

    def handle_request(self, msg):
        """Answer a request from the server. loom only offers ping."""
        if msg["method"] == "ping":
            reply = dict(jsonrpc="2.0", id=msg["id"], result={})
        else:
            error = dict(code=-32601, message=f"loom doesn't support {msg['method']}")
            reply = dict(jsonrpc="2.0", id=msg["id"], error=error)
        try:
            self.send(reply)
        except McpError:
            pass

    def fail_pending(self, message):
        with self.lock:
            answers = list(self.pending.items())
        for msg_id, answer in answers:
            if answer.empty():
                answer.put(dict(id=msg_id, error=dict(message=message)))

    def close(self):
        self.closed = True


class StdioConnection(Connection):
    """A server run as a subprocess, sending one JSON-RPC message per line."""

    def __init__(self, command, args=None, env=None, cwd=None, on_notification=None):
        super().__init__(on_notification)
        self.command = command
        self.args = [str(arg) for arg in args or []]
        self.env = dict(os.environ, **{k: str(v) for k, v in (env or {}).items()})
        self.cwd = cwd
        self.proc = None
        self.stderr = deque(maxlen=20)
        self.write_lock = threading.Lock()

    def start(self):
        executable = shutil.which(self.command, path=self.env.get("PATH")) or self.command
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Its own session, so ^C in the terminal doesn't kill the server
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                [executable] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                **kwargs,
            )
        except OSError as err:
            raise McpError(f"Unable to run {self.command}: {err}")
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()

    def read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.stderr.append(line.decode(errors="replace")[:500])
                continue
            self.handle(msg)
        self.closed = True
        self.fail_pending(self.exit_message())

    def read_stderr(self):
        for line in self.proc.stderr:
            self.stderr.append(line.decode(errors="replace").rstrip()[:500])

    def exit_message(self):
        message = "the server exited"
        if self.proc and self.proc.poll() is not None:
            message += f" with code {self.proc.returncode}"
        # Give the stderr reader a moment to catch the last words
        time.sleep(0.05)
        lines = [line for line in self.stderr if line.strip()]
        if lines:
            message += ": " + "\n".join(lines[-5:])
        return message

    def send(self, msg):
        if self.closed or not self.proc:
            raise McpError(self.exit_message())
        data = (json.dumps(msg) + "\n").encode()
        try:
            with self.write_lock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError(self.exit_message())

    def close(self):
        super().close()
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        # Close its input and give it a moment, then terminate it, then kill it
        try:
            proc.stdin.close()
        except OSError:
            pass
        for sig in (None, signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            if sig is not None:
                try:
                    if os.name == "nt":
                        proc.kill()
                    else:
                        os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                proc.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                continue


class HttpConnection(Connection):
    """A server at a URL, using MCP's streamable HTTP transport: each message is POSTed,
    and a request's response comes back as JSON or as a stream of server-sent events."""

    def __init__(self, url, headers=None, timeout=CALL_TIMEOUT, on_notification=None):
        super().__init__(on_notification)
        self.url = url
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.session_id = None
        self.protocol_version = None
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT), follow_redirects=True
        )

    def get_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"loom/{__version__}",
        }
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def send(self, msg):
        if self.closed:
            raise McpError("the connection is closed")
        is_request = "method" in msg and "id" in msg
        try:
            with self.client.stream(
                "POST", self.url, json=msg, headers=self.get_headers()
            ) as response:
                self.check_status(response)
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self.session_id = session_id
                if not is_request:
                    return
                content_type = response.headers.get("content-type", "")
                if content_type.startswith("text/event-stream"):
                    self.read_events(response, msg["id"])
                else:
                    body = response.read()
                    if body.strip():
                        try:
                            self.handle(json.loads(body))
                        except ValueError:
                            raise McpError(
                                f"the server sent a response that isn't JSON: {body[:200]!r}"
                            )
        except httpx.HTTPError as err:
            raise McpError(f"{type(err).__name__}: {err}")

    def check_status(self, response):
        status = response.status_code
        if status < 400:
            return
        if status in (401, 403):
            raise McpError(
                f"HTTP {status}: the server needs authorization. loom doesn't do OAuth; set an"
                " Authorization header in the server's config."
            )
        if status == 404 and self.session_id:
            raise McpError("HTTP 404: the server ended the session; reconnect with /mcp connect")
        body = response.read().decode(errors="replace").strip()
        raise McpError(f"HTTP {status}: {body[:300]}")

    def read_events(self, response, msg_id):
        """Read server-sent events until the response to msg_id arrives."""
        data = []
        for line in response.iter_lines():
            if not line:
                if data:
                    self.handle_event("\n".join(data))
                    data = []
                    if self.is_answered(msg_id):
                        return
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value[1:] if value.startswith(" ") else value)
        if data:
            self.handle_event("\n".join(data))

    def handle_event(self, data):
        try:
            self.handle(json.loads(data))
        except ValueError:
            pass

    def is_answered(self, msg_id):
        with self.lock:
            answer = self.pending.get(msg_id)
        return answer is None or not answer.empty()

    def close(self):
        if self.session_id and not self.closed:
            try:
                self.client.delete(self.url, headers=self.get_headers(), timeout=5)
            except httpx.HTTPError:
                pass
        super().close()
        self.client.close()


# Servers


class McpServer:
    def __init__(self, name, config, source, root=None):
        self.name = name
        self.config = config
        self.source = source  # the config file it came from
        self.root = root
        self.status = "not started"  # connecting, connected, failed, not approved
        self.error = ""
        self.tools = []
        self.instructions = ""
        self.server_info = {}
        self.connection = None
        self.tools_changed = False
        self.lock = threading.Lock()

    @property
    def is_project_server(self):
        return (
            Path(self.source).name == PROJECT_CONFIG
            and self.root is not None
            and (Path(self.source).resolve().parent == Path(self.root).resolve())
        )

    @property
    def timeout(self):
        try:
            return float(self.config.get("timeout") or CALL_TIMEOUT)
        except (TypeError, ValueError):
            return CALL_TIMEOUT

    def describe(self):
        """The command line or URL."""
        if self.config.get("url"):
            return str(self.config["url"])
        return " ".join(
            [str(self.config["command"])] + [str(a) for a in self.config.get("args") or []]
        )

    def on_notification(self, msg):
        if msg.get("method") == "notifications/tools/list_changed":
            self.tools_changed = True

    def connect(self, timeout=CONNECT_TIMEOUT):
        with self.lock:
            self.close()
            self.status = "connecting"
            self.error = ""
            try:
                self._connect(timeout)
                self.status = "connected"
            except Exception as err:
                # This runs in a thread, where anything uncaught would be lost
                self.status = "failed"
                self.error = str(err) or type(err).__name__
                self.close()

    def _connect(self, timeout):
        config = expand_env(self.config)
        if config.get("url"):
            kind = config.get("type") or "http"
            if kind == "sse":
                raise McpError(
                    "the old HTTP+SSE transport isn't supported; use the server's streamable"
                    " HTTP endpoint (type http)"
                )
            self.connection = HttpConnection(
                config["url"], config.get("headers"), self.timeout, self.on_notification
            )
        else:
            self.connection = StdioConnection(
                config["command"],
                config.get("args"),
                config.get("env"),
                config.get("cwd") or self.root,
                self.on_notification,
            )
            self.connection.start()

        result = self.connection.request(
            "initialize",
            dict(
                protocolVersion=PROTOCOL_VERSION,
                capabilities={},
                clientInfo=dict(name="loom", version=__version__),
            ),
            timeout,
        )
        if isinstance(self.connection, HttpConnection):
            self.connection.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo") or {}
        instructions = result.get("instructions") or ""
        self.instructions = instructions[:MAX_INSTRUCTIONS] if isinstance(instructions, str) else ""
        self.connection.notify("notifications/initialized")
        if "tools" in (result.get("capabilities") or {}):
            self.tools = self.list_tools(timeout)

    def list_tools(self, timeout=CONNECT_TIMEOUT):
        tools = []
        cursor = None
        for _page in range(100):
            params = dict(cursor=cursor) if cursor else {}
            result = self.connection.request("tools/list", params, timeout)
            tools += [
                tool
                for tool in result.get("tools") or []
                if isinstance(tool, dict) and isinstance(tool.get("name"), str)
            ]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def refresh_tools(self):
        """Fetch the tool list again after the server said it changed."""
        if not self.tools_changed or self.status != "connected":
            return
        self.tools_changed = False
        try:
            self.tools = self.list_tools()
        except McpError as err:
            self.error = f"Unable to refresh the tools: {err}"

    def call_tool(self, name, arguments):
        if self.status != "connected" or not self.connection:
            raise McpError(
                f"the {self.name} MCP server isn't connected: {self.error or self.status}"
            )
        if self.connection.closed:
            self.status = "failed"
            self.error = "the connection closed"
            raise McpError(f"the {self.name} MCP server has stopped")
        return self.connection.request(
            "tools/call", dict(name=name, arguments=arguments), self.timeout
        )

    def close(self):
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None


class McpManager:
    """The configured MCP servers, and the tools they give the agent."""

    def __init__(self, io, servers=None, root=None):
        self.io = io
        self.root = root
        self.servers = {server.name: server for server in servers or []}
        self.started = False

    @classmethod
    def from_config(cls, io, root, config_files=(), use_default_files=True):
        """Read the user's and the project's config files, then config_files. A broken
        default file is skipped with a warning; a broken config_file raises McpError."""
        configs = {}
        sources = []
        if use_default_files:
            sources.append((user_config_file(), False))
            if root:
                sources.append((Path(root) / PROJECT_CONFIG, False))
        sources += [(Path(fname), True) for fname in config_files or []]

        for path, explicit in sources:
            if not explicit and not path.is_file():
                continue
            try:
                servers = load_config_file(path)
            except McpError as err:
                if explicit:
                    raise
                io.tool_warning(str(err))
                continue
            for name, config in servers.items():
                configs[name] = (config, str(path))

        servers = [
            McpServer(name, config, source, root) for name, (config, source) in configs.items()
        ]
        return cls(io, servers, root)

    # Approving the project's servers

    def load_approvals(self):
        try:
            data = json.loads(approvals_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def is_approved(self, server):
        if not server.is_project_server:
            return True
        approved = self.load_approvals().get(str(Path(self.root).resolve()), {})
        return approved.get(server.name) == config_hash(server.config)

    def save_approval(self, server):
        data = self.load_approvals()
        data.setdefault(str(Path(self.root).resolve()), {})[server.name] = config_hash(
            server.config
        )
        try:
            path = approvals_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as err:
            self.io.tool_warning(f"Unable to save the approval to {approvals_file()}: {err}")

    def approve(self, server):
        """Ask before starting a server from the project's .mcp.json."""
        if self.is_approved(server):
            return True
        answer = self.io.permission_ask(
            f"Start the MCP server {server.name!r} from this project's {PROJECT_CONFIG}?",
            subject=server.describe(),
            always="trust it in this project",
            explicit_yes_required=True,
        )
        if answer == "always":
            self.save_approval(server)
        if answer in ("yes", "always"):
            return True
        server.status = "not approved"
        server.error = f"use /mcp connect {server.name} to start it"
        return False

    # Connecting

    def start(self, wait=CONNECT_TIMEOUT):
        """Connect to every server, in parallel, waiting up to wait seconds. Servers still
        connecting after that carry on in the background."""
        if self.started:
            return
        self.started = True
        servers = [server for server in self.servers.values() if self.approve(server)]
        self.connect(servers, wait)

    def connect(self, servers, wait=CONNECT_TIMEOUT):
        threads = []
        for server in servers:
            server.status = "connecting"
            thread = threading.Thread(target=server.connect, name=f"mcp-{server.name}", daemon=True)
            thread.start()
            threads.append(thread)
        if not threads:
            return

        from loom.waiting import WaitingSpinner

        deadline = time.time() + wait
        names = ", ".join(server.name for server in servers)
        with WaitingSpinner(f"Connecting to MCP servers: {names}"):
            for thread in threads:
                thread.join(max(0, deadline - time.time()))
        for server in servers:
            if server.status == "failed":
                self.io.tool_warning(f"MCP server {server.name} failed to start: {server.error}")

    def reconnect(self, name):
        """Connect to a server again, or for the first time if it wasn't approved."""
        server = self.servers.get(name)
        if not server:
            raise McpError(f"There's no MCP server named {name!r}")
        if self.approve(server):
            self.connect([server])
        return server

    def close(self):
        for server in self.servers.values():
            server.close()

    # Tools

    def connected(self):
        return [server for server in self.servers.values() if server.status == "connected"]

    def tool_map(self):
        """{name the model sees: (server, tool)} for every connected server's tools."""
        res = {}
        for server in self.connected():
            server.refresh_tools()
            for tool in server.tools:
                name = tool_name(server.name, tool["name"])
                num = 2
                while name in res:
                    name = tool_name(server.name, f"{tool['name']}_{num}")
                    num += 1
                res[name] = (server, tool)
        return res

    def tool_schemas(self):
        schemas = []
        for name, (server, tool) in self.tool_map().items():
            description = tool.get("description") or tool.get("title") or tool["name"]
            description = f"[{server.name} MCP server] {description}"[:MAX_DESCRIPTION]
            schemas.append(
                dict(
                    type="function",
                    function=dict(
                        name=name, description=description, parameters=tool_parameters(tool)
                    ),
                )
            )
        return schemas

    def find(self, name):
        """(server, tool) for a tool name the model used, or None."""
        if not name.startswith("mcp__"):
            return None
        return self.tool_map().get(name)

    def instructions(self):
        """What the connected servers told loom about using them, for the system prompt."""
        sections = [
            f"## {server.name}\n{server.instructions.strip()}"
            for server in self.connected()
            if server.instructions.strip()
        ]
        if not sections:
            return ""
        return (
            "# MCP servers\nInstructions from the MCP servers whose tools (named"
            " mcp__<server>__<tool>) you can use:\n\n"
            + "\n\n".join(sections)
        )

    def summary(self):
        """One line about the servers, for the announcements."""
        parts = []
        for server in self.servers.values():
            if server.status == "connected":
                parts.append(f"{server.name} ({len(server.tools)} tools)")
            else:
                parts.append(f"{server.name} ({server.status})")
        return ", ".join(parts)

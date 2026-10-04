"""
Google Stitch (https://stitch.withgoogle.com): designs UI screens from a description and
gives back each screen's HTML (Tailwind CSS) and a screenshot, over MCP.

With STITCH_API_KEY set, loom adds Stitch's MCP server as the built-in "stitch" server
(loom/mcp.py), so the agent can design pages with it. Any MCP server offering Stitch's
tools counts, whatever it's called, so a proxy configured in mcp.json works too.

This module finds that server, fetches a screen's HTML for the save_stitch_screen tool
(loom/tools.py), and holds the configuration of the built-in server.
"""

import json
import re

import httpx

URL = "https://stitch.googleapis.com/mcp"
API_KEY_ENV = "STITCH_API_KEY"

# The built-in server, added when STITCH_API_KEY is set and no config file names a
# "stitch" server (or disables it)
SERVER_NAME = "stitch"
SERVER_CONFIG = dict(
    type="http",
    url=URL,
    headers={"X-Goog-Api-Key": "${" + API_KEY_ENV + "}"},
    # Generating a screen takes a few minutes
    timeout=600,
)

# The tools that make a server Stitch
SIGNATURE_TOOLS = {"generate_screen_from_text", "get_screen"}

MAX_HTML_BYTES = 5_000_000
DOWNLOAD_TIMEOUT = 60

SCREEN_RE = re.compile(r"^projects/([^/\s]+)/screens/([^/\s]+)$")


class StitchError(Exception):
    pass


def is_stitch(server):
    """Whether a connected MCP server offers Stitch's tools."""
    names = {tool.get("name") for tool in server.tools}
    return SIGNATURE_TOOLS <= names


def find_server(mcp):
    """The connected Stitch server of an McpManager, or None."""
    if not mcp:
        return None
    for server in mcp.connected():
        if is_stitch(server):
            return server
    return None


def parse_screen(screen, project_id=None):
    """(project id, screen id) from projects/P/screens/S, or a bare screen id and the
    project id."""
    screen = (screen or "").strip()
    match = SCREEN_RE.match(screen)
    if match:
        return match.group(1), match.group(2)
    project_id = (project_id or "").strip().removeprefix("projects/")
    screen_id = screen.removeprefix("screens/")
    if not project_id or not screen_id or "/" in screen_id or "/" in project_id:
        raise StitchError(
            "screen must be the screen's resource name, projects/PROJECT_ID/screens/SCREEN_ID,"
            " as Stitch's tools return it (or a screen id with project_id)"
        )
    return project_id, screen_id


def payload(result):
    """The data of a Stitch tool result: its structuredContent, else the JSON in its
    text."""
    from loom.mcp import format_result, mget

    text, is_error = format_result(result)
    if is_error:
        raise StitchError(text)
    structured = mget(result, "structuredContent")
    if isinstance(structured, dict):
        return structured
    try:
        data = json.loads(text)
    except ValueError:
        raise StitchError(f"Stitch's get_screen didn't return the screen: {text[:500]}")
    if not isinstance(data, dict):
        raise StitchError(f"Stitch's get_screen returned {type(data).__name__}, not a screen")
    return data


def download_url(data, key):
    value = data.get(key)
    url = value.get("downloadUrl") if isinstance(value, dict) else None
    return url if isinstance(url, str) and url.strip() else None


def get_screen_arguments(server):
    """The argument names the server's get_screen takes, or None if it doesn't say."""
    for tool in server.tools:
        if tool.get("name") == "get_screen":
            schema = tool.get("inputSchema")
            properties = schema.get("properties") if isinstance(schema, dict) else None
            return set(properties) if isinstance(properties, dict) and properties else None
    return None


def get_screen(server, project_id, screen_id):
    """The screen's details: title, htmlCode.downloadUrl, screenshot.downloadUrl."""
    from loom.mcp import McpError

    # Stitch takes the resource name, and refuses arguments its schema doesn't list; older
    # versions and some proxies take the ids instead
    args = dict(
        name=f"projects/{project_id}/screens/{screen_id}", projectId=project_id, screenId=screen_id
    )
    accepted = get_screen_arguments(server)
    if accepted:
        args = {key: value for key, value in args.items() if key in accepted} or args
    try:
        return payload(server.call_tool("get_screen", args))
    except McpError as err:
        raise StitchError(str(err))


def download(url, transport=None):
    """The text at a screen's download URL. Stitch serves the files from Google's
    storage, over https."""
    if not url.lower().startswith("https://"):
        raise StitchError(f"refusing to download the screen from a non-https URL: {url}")
    try:
        with httpx.Client(
            transport=transport, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise StitchError(
                        f"downloading the screen failed: HTTP {response.status_code} (the link"
                        " may have expired; call save_stitch_screen again for a new one)"
                    )
                data = b""
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > MAX_HTML_BYTES:
                        raise StitchError("the screen's HTML is over 5 MB")
                encoding = response.encoding or "utf-8"
    except httpx.HTTPError as err:
        raise StitchError(f"downloading the screen failed: {err.__class__.__name__}: {err}")
    return data.decode(encoding, errors="replace").replace("\r\n", "\n")


def fetch_screen(server, project_id, screen_id, transport=None):
    """(the screen's details, its HTML)."""
    data = get_screen(server, project_id, screen_id)
    url = download_url(data, "htmlCode")
    if not url:
        raise StitchError(
            f"Stitch has no HTML for projects/{project_id}/screens/{screen_id} yet. If it's"
            " still being generated, wait and try again."
        )
    return data, download(url, transport)

"""A fake Google Stitch MCP server over stdio, with the tools and answers of the real one
(https://stitch.googleapis.com/mcp), for testing loom's Stitch support."""

import json
import sys

SCREENS = {
    "p-1/s-home": dict(
        name="projects/p-1/screens/s-home",
        title="Home",
        htmlCode=dict(downloadUrl="https://files.example/s-home.html"),
        screenshot=dict(downloadUrl="https://files.example/s-home.png"),
    ),
    # Still being generated
    "p-1/s-wip": dict(name="projects/p-1/screens/s-wip", title="Pricing"),
}


def schema(**properties):
    return {"type": "object", "properties": {k: {"type": "string"} for k in properties}}


TOOLS = [
    dict(
        name="create_project",
        description="Creates a new Stitch project.",
        inputSchema=schema(title=1),
    ),
    dict(name="list_screens", description="Lists all screens.", inputSchema=schema(projectId=1)),
    dict(name="get_screen", description="Retrieves a screen.", inputSchema=schema(name=1)),
    dict(
        name="generate_screen_from_text",
        description="Generates a new screen within a project from a text prompt.",
        inputSchema=schema(projectId=1, prompt=1, deviceType=1, designSystem=1),
    ),
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def text(value, is_error=False):
    if not isinstance(value, str):
        value = json.dumps(value)
    return dict(content=[dict(type="text", text=value)], isError=is_error)


def call_tool(name, args):
    if name == "create_project":
        return text(dict(name="projects/p-1", title=args.get("title", "")))
    if name == "list_screens":
        return text(dict(screens=list(SCREENS.values())))
    if name == "get_screen":
        # Like the real server, refuse arguments the schema doesn't list
        if set(args) != {"name"}:
            return text("Request contains an invalid argument.", is_error=True)
        project, screen = args["name"].split("/")[1], args["name"].split("/")[3]
        found = SCREENS.get(f"{project}/{screen}")
        if not found:
            return text(f"Screen {args['name']} not found", is_error=True)
        return text(found)
    if name == "generate_screen_from_text":
        return dict(
            content=[],
            structuredContent=dict(
                outputComponents=[
                    dict(designSystem=dict(name="assets/ds-1")),
                    dict(design=dict(screens=[SCREENS["p-1/s-home"]])),
                ],
                projectId=args["projectId"],
            ),
        )
    raise KeyError(name)


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = dict(
                protocolVersion=msg["params"]["protocolVersion"],
                capabilities=dict(tools={}),
                serverInfo=dict(name="stitch", version="1.0"),
            )
        elif method == "tools/list":
            result = dict(tools=TOOLS)
        elif method == "tools/call":
            params = msg["params"]
            result = call_tool(params["name"], params.get("arguments") or {})
        else:
            send(dict(jsonrpc="2.0", id=msg["id"], error=dict(code=-32601, message="no method")))
            continue
        send(dict(jsonrpc="2.0", id=msg["id"], result=result))


if __name__ == "__main__":
    main()

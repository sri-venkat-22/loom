"""A small MCP server over stdio, for testing loom's MCP client."""

import json
import sys
import time

TOOLS = [
    dict(
        name="add",
        description="Add two numbers.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        annotations=dict(readOnlyHint=True),
    ),
    dict(
        name="echo",
        description="Echo the text back, after pinging the client.",
        inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
    ),
    dict(name="fail", description="Always fails.", inputSchema={"type": "object"}),
    dict(name="slow", description="Sleeps.", inputSchema={"type": "object"}),
    dict(name="grow", description="Adds a tool.", inputSchema={"type": "object"}),
    dict(name="crash", description="Exits.", inputSchema={"type": "object"}),
]
LATE_TOOL = dict(name="late", description="Added later.", inputSchema={"type": "object"})

tools = list(TOOLS)
next_id = 1000


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def text(value, is_error=False):
    return dict(content=[dict(type="text", text=str(value))], isError=is_error)


def call_tool(name, args):
    global next_id
    if name == "add":
        return dict(
            content=[dict(type="text", text=str(args["a"] + args["b"]))],
            structuredContent=dict(sum=args["a"] + args["b"]),
        )
    if name == "echo":
        # Ask the client something first, as servers may
        next_id += 1
        send(dict(jsonrpc="2.0", id=next_id, method="ping"))
        reply = read()
        assert reply.get("id") == next_id and reply.get("result") == {}, reply
        return text(args.get("text", ""))
    if name == "fail":
        return text("it broke", is_error=True)
    if name == "slow":
        time.sleep(args.get("seconds", 30))
        return text("done")
    if name == "grow":
        tools.append(LATE_TOOL)
        send(dict(jsonrpc="2.0", method="notifications/tools/list_changed"))
        return text("grown")
    if name == "crash":
        print("crashing on purpose", file=sys.stderr, flush=True)
        sys.exit(3)
    if name == "late":
        return text("late tool")
    raise KeyError(name)


def main():
    print("this server writes some noise to stdout first")
    print("and to stderr", file=sys.stderr, flush=True)
    sys.stdout.flush()
    while True:
        msg = read()
        method = msg.get("method")
        if "id" not in msg:
            continue  # notifications
        if method == "initialize":
            result = dict(
                protocolVersion=msg["params"]["protocolVersion"],
                capabilities=dict(tools=dict(listChanged=True)),
                serverInfo=dict(name="test-server", version="1.0"),
                instructions="Use add for arithmetic.",
            )
        elif method == "tools/list":
            # Two pages
            if msg.get("params", {}).get("cursor") == "page2":
                result = dict(tools=tools[3:])
            else:
                result = dict(tools=tools[:3], nextCursor="page2")
        elif method == "tools/call":
            params = msg["params"]
            try:
                result = call_tool(params["name"], params.get("arguments") or {})
            except KeyError:
                send(
                    dict(
                        jsonrpc="2.0",
                        id=msg["id"],
                        error=dict(code=-32602, message=f"unknown tool {params['name']}"),
                    )
                )
                continue
        else:
            send(
                dict(jsonrpc="2.0", id=msg["id"], error=dict(code=-32601, message="no such method"))
            )
            continue
        send(dict(jsonrpc="2.0", id=msg["id"], result=result))


if __name__ == "__main__":
    main()

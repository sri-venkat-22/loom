"""A saved conversation as the web UI's messages, so switching to it in the sessions
sidebar (or with /resume) shows it like it was had in the browser."""

import json
import re

from loom import prompts, tools
from loom.display import sanitize_for_display

from .protocol import MAX_TOOL_OUTPUT

# Tool results the model got back that mean the call didn't do its job, like a denied
# edit or a command that exited with an error
FAILED_RESULT = re.compile(
    r"(Error|Refused|Denied|Interrupted|Not run|Blocked|The user denied|Exit code: (?!0\b))"
)


def text_of(content):
    """The text of a message's content, which can be a list of parts with images."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("text")
        )
    return ""


def call_detail(arguments):
    """The one-line detail a tool call's card shows, like the terminal's recap does."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return {}, ""
    if not isinstance(args, dict):
        return {}, ""
    detail = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    first, _, rest = detail.strip().partition("\n")
    return args, " ".join(first.split()) + (" …" if rest.strip() else "")


def transcript(messages, next_id):
    """The (type, payload) messages that show messages, a coder's done_messages. next_id
    makes ids, like WebSession.next_id."""
    results = {
        msg.get("tool_call_id"): text_of(msg.get("content"))
        for msg in messages
        if msg.get("role") == "tool"
    }
    for msg in messages:
        role = msg.get("role")
        content = text_of(msg.get("content"))
        if role == "user":
            if content.startswith(prompts.summary_prefix):
                yield "system", dict(level="info", text="(The earlier conversation, summarized.)")
            elif content.strip():
                yield "user", dict(text=content)
        elif role == "assistant":
            if content.strip() and not (content.strip() == "Ok." and len(messages) > 1):
                reply = next_id("m")
                yield "assistant_delta", dict(
                    id=reply, text=content.strip(), reasoning="", replace=True
                )
                yield "assistant_end", dict(id=reply)
            for call in msg.get("tool_calls") or []:
                function = call.get("function") or {}
                args, detail = call_detail(function.get("arguments"))
                card = next_id("c")
                yield "tool_start", dict(
                    id=card,
                    name=tools.display_name(function.get("name", "")),
                    detail=sanitize_for_display(detail),
                    args=json.dumps(args, indent=2, ensure_ascii=False) if args else None,
                )
                output = sanitize_for_display(results.get(call.get("id"), ""))
                failed = bool(FAILED_RESULT.match(output.lstrip()))
                yield "tool_end", dict(
                    id=card,
                    status="failed" if failed else "done",
                    output=output[:MAX_TOOL_OUTPUT],
                )

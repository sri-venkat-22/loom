#!/usr/bin/env python
"""
Check which models can drive loom's agent, and print a table of the results.

    python scripts/check_agent_models.py nemotron deepseek-flash gemini-3.1

It finds API keys the way loom does (the environment, .env files, ~/.loom/credentials.json)
and runs two checks on each model:

1. probe: one request offering the agent's tools and asking for two file reads. It passes if
   the model calls read_file through the API, not as text, with valid JSON arguments. Both
   calls in one reply means it makes parallel calls.
2. task: a real agent run in a temporary git repo: find and fix a bug in calc.py, then run
   check_calc.py with bash. It passes if the file is fixed, the check ran and printed OK, and
   the agent finished with a reply to the user.

A model that fails the probe should use an edit format (--no-agent, or /chat-mode diff);
loom also switches to one when a provider rejects tool calling.
"""

import argparse
import contextlib
import io as stringio
import json
import os
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path

import git

from loom import models
from loom import tools as agent_tools
from loom.coders import Coder
from loom.coders.agent_coder import tools_unsupported, wrote_tool_call_as_text
from loom.io import InputOutput
from loom.main import load_dotenv_files, register_litellm_models, register_models
from loom.permissions import Permissions

PROBE_PROMPT = (
    "Read the files README.md and setup.py with the read_file tool. Make both calls now, in"
    " this reply. Don't answer in text."
)

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
CHECK = (
    "from calc import add, mul\n\n"
    "assert add(2, 3) == 5, f'add(2, 3) returned {add(2, 3)}'\n"
    "assert mul(2, 3) == 6\n"
    "print('OK')\n"
)
TASK_PROMPT = (
    "check_calc.py fails. Find the bug in the code it checks and fix it, then run"
    " `python check_calc.py` to confirm it prints OK."
)


class TaskTimeout(BaseException):
    """Not an Exception, so loom's handlers for API and tool errors let it through."""


def probe(model):
    """Ask for two tool calls in one request. Returns a dict of findings."""
    messages = [
        dict(role="system", content="You are a coding agent. Use the tools you are given."),
        dict(role="user", content=PROBE_PROMPT),
    ]
    started = time.time()
    try:
        _, completion = model.send_completion(
            messages, None, stream=False, tools=agent_tools.schemas()
        )
    except Exception as err:
        return dict(
            ok=False,
            error=first_line(err),
            # Otherwise the model couldn't be reached, which says nothing about its tools
            rejected_tools=tools_unsupported(err),
            seconds=time.time() - started,
        )

    seconds = time.time() - started
    message = completion.choices[0].message
    calls = message.tool_calls or []
    res = dict(seconds=seconds, calls=len(calls), text=first_line(message.content or ""))
    names = []
    valid = True
    for call in calls:
        names.append(call.function.name)
        try:
            args = json.loads(call.function.arguments or "{}")
            valid = valid and isinstance(args, dict) and "path" in args
        except ValueError:
            valid = False
    res["names"] = names
    res["valid_args"] = valid
    res["as_text"] = not calls and wrote_tool_call_as_text(message.content or "")
    res["ok"] = bool(calls) and valid and set(names) == {"read_file"}
    res["parallel"] = len(calls) >= 2
    return res


def make_repo(root):
    repo = git.Repo.init(root)
    with repo.config_writer() as config:
        config.set_value("user", "name", "loom check")
        config.set_value("user", "email", "check@example.com")
    (root / "calc.py").write_text(CALC)
    (root / "check_calc.py").write_text(CHECK)
    (root / ".gitignore").write_text(".loom*\n")
    repo.git.add(".")
    repo.git.commit("-m", "initial")
    return repo


def run_task(model, timeout, log_file, stream):
    """Run the agent on the bug-fixing task. Returns a dict of findings."""
    res = dict(ok=False)
    # ignore_cleanup_errors: on Windows git can still hold files open when the task ends
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp).resolve()
        make_repo(root).close()
        output = stringio.StringIO()
        started = time.time()

        def alarm(signum, frame):
            raise TaskTimeout()

        # Windows has no SIGALRM, so the timeout only applies elsewhere
        use_alarm = hasattr(signal, "SIGALRM")
        if use_alarm:
            old_handler = signal.signal(signal.SIGALRM, alarm)
            signal.alarm(timeout)
        coder = None
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with contextlib.redirect_stdout(output):
                io = InputOutput(pretty=False, yes=True, fancy_input=False)
                coder = Coder.create(
                    main_model=model,
                    edit_format="agent",
                    io=io,
                    map_tokens=1024,
                    stream=stream,
                    permissions=Permissions(io, mode="accept-edits", allow=["bash"]),
                )
                coder.run(with_message=TASK_PROMPT)
        except TaskTimeout:
            res["error"] = f"timed out after {timeout}s"
        except Exception as err:
            res["error"] = first_line(err)
            output.write(traceback.format_exc())
        finally:
            if use_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
            os.chdir(cwd)

        res["seconds"] = time.time() - started
        res["fixed"] = "a + b" in (root / "calc.py").read_text().replace("b + a", "a + b")
        if log_file:
            log_file.write_text(output.getvalue())
        if coder:
            res.update(analyze(coder.done_messages + coder.cur_messages))
            res["cost"] = coder.total_cost

    res["ok"] = bool(res["fixed"] and res.get("checked") and res.get("replied"))
    return res


def analyze(messages):
    """What the agent did, from its messages."""
    steps = 0
    tools = []
    json_errors = 0
    tool_errors = 0
    checked = False
    reply = ""
    for msg in messages:
        if msg["role"] == "assistant":
            if msg.get("tool_calls"):
                steps += 1
                tools += [call["function"]["name"] for call in msg["tool_calls"]]
            if isinstance(msg.get("content"), str) and msg["content"].strip():
                reply = msg["content"]
        elif msg["role"] == "tool":
            content = msg["content"]
            if content.startswith("Error: the arguments were not valid JSON"):
                json_errors += 1
            elif content.startswith("Error:"):
                tool_errors += 1
            if content.startswith("Exit code: 0") and "OK" in content:
                checked = True

    last = messages[-1] if messages else {}
    return dict(
        steps=steps,
        tools=tools,
        json_errors=json_errors,
        tool_errors=tool_errors,
        checked=checked,
        replied=bool(reply) and last.get("role") == "assistant" and not last.get("tool_calls"),
        as_text=wrote_tool_call_as_text(reply),
    )


def first_line(value, limit=160):
    text = str(value).strip().split("\n", 1)[0]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def verdict(result):
    probe_res, task = result["probe"], result.get("task") or {}
    if probe_res.get("error") and not probe_res.get("rejected_tools"):
        return "not tested: the model couldn't be reached"
    if not probe_res.get("ok"):
        return "no: use an edit format"
    if task.get("ok"):
        return "yes"
    return "partly: tool calls work, the task failed"


def describe_probe(res):
    if "error" in res:
        return f"error: {res['error']}"
    if res.get("as_text"):
        return "wrote the calls as text"
    if not res.get("calls"):
        return f"no tool calls ({res.get('text') or 'empty reply'})"
    if not res.get("valid_args"):
        return "invalid arguments"
    note = "2 parallel calls" if res.get("parallel") else f"{res['calls']} call"
    return f"ok, {note}, {res['seconds']:.0f}s"


def describe_task(res):
    if not res:
        return "skipped"
    parts = []
    if res.get("error"):
        parts.append(res["error"])
    parts.append("fixed" if res.get("fixed") else "not fixed")
    parts.append("checked" if res.get("checked") else "not checked")
    if not res.get("replied"):
        parts.append("no final reply")
    if res.get("as_text"):
        parts.append("tool calls as text")
    parts.append(f"{res.get('steps', 0)} steps")
    errors = res.get("json_errors", 0) + res.get("tool_errors", 0)
    if errors:
        parts.append(f"{errors} tool errors")
    parts.append(f"{res.get('seconds', 0):.0f}s")
    return ", ".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("models", nargs="+", help="Model names or aliases")
    parser.add_argument(
        "--timeout", type=int, default=300, help="Seconds per agent task (not enforced on Windows)"
    )
    parser.add_argument("--request-timeout", type=int, default=120, help="Seconds per API request")
    parser.add_argument("--no-task", action="store_true", help="Only run the probe")
    parser.add_argument("--no-stream", action="store_true", help="Don't stream the task")
    parser.add_argument("--log-dir", help="Save each task's transcript here")
    parser.add_argument("--json", help="Also write the results to this JSON file")
    args = parser.parse_args()

    os.environ.setdefault("LOOM_ANALYTICS", "false")
    load_dotenv_files(None, None)
    quiet = InputOutput(pretty=False, yes=True, fancy_input=False)
    register_models(None, None, quiet)
    register_litellm_models(None, None, quiet)
    models.request_timeout = args.request_timeout
    # So the agent's `python check_calc.py` runs this Python
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    log_dir = Path(args.log_dir) if args.log_dir else None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for name in args.models:
        model = models.Model(name)
        result = dict(alias=name, model=model.name)
        missing = model.missing_keys
        if missing:
            result["probe"] = dict(ok=False, error=f"no API key: set {' or '.join(missing)}")
        else:
            print(f"{name}: probing {model.name}...", flush=True)
            result["probe"] = probe(model)
        if result["probe"].get("ok") and not args.no_task:
            print(f"{name}: running the agent task...", flush=True)
            log_file = log_dir / f"{name}.log" if log_dir else None
            result["task"] = run_task(model, args.timeout, log_file, not args.no_stream)
        result["function_calling"] = model.info.get("supports_function_calling")
        result["verdict"] = verdict(result)
        results.append(result)
        print(f"{name}: {result['verdict']}", flush=True)

    print()
    print("| Model | Tool-call probe | Agent task | Works in agent mode |")
    print("|---|---|---|---|")
    for res in results:
        print(
            f"| `{res['alias']}` ({res['model']}) | {describe_probe(res['probe'])} |"
            f" {describe_task(res.get('task'))} | {res['verdict']} |"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2, default=str) + "\n")
    return 0 if all(res["verdict"] == "yes" for res in results) else 1


if __name__ == "__main__":
    sys.exit(main())

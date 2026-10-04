"""
What parallel builders (loom/parallel.py) need to run on threads beside the main one.

Each builder talks through a WorkerIO: its output goes to the main io with [id] in front
of it (or, in the web UI, to the builder's own lane), and its questions, like a permission
to run a command, go into an Asks queue. Builders never prompt themselves: the main
thread answers their questions one at a time with the main io, while it waits for them.

^C (or the web UI's stop) reaches the main thread. stop() then asks every builder to
stop, kills the commands they're running, and makes their waiting questions fail.
"""

import contextlib
import functools
import queue
import threading

from loom import tools

ASKS = ("confirm_ask", "permission_ask", "choice_ask", "prompt_ask")
# The io methods whose output gets the builder's id in front, in the terminal
PREFIXED = ("tool_output", "tool_warning", "tool_error", "tool_call", "tool_result")
# The io methods that write, which run one builder at a time
OUTPUTS = PREFIXED + (
    "tool_done",
    "assistant_output",
    "todo_output",
    "diff_output",
    "print",
    "append_chat_history",
)
# How often waits wake up to see whether to stop
POLL_SECONDS = 0.1


class Stopped(KeyboardInterrupt):
    """The builder was told to stop while it waited for an answer."""


class Asks:
    """Builders' questions, which the main thread answers one at a time."""

    def __init__(self):
        self.queue = queue.Queue()
        self.stopped = threading.Event()

    def ask(self, worker, name, args, kwargs):
        """From a builder's thread: ask the main thread question name(*args, **kwargs) and
        wait for its answer."""
        if self.stopped.is_set():
            raise Stopped()
        request = dict(
            worker=worker,
            name=name,
            args=args,
            kwargs=kwargs,
            done=threading.Event(),
            answer=None,
            error=None,
        )
        self.queue.put(request)
        while not request["done"].wait(POLL_SECONDS):
            if self.stopped.is_set():
                raise Stopped()
        if request["error"] is not None:
            raise request["error"]
        return request["answer"]

    def serve(self, io, timeout=POLL_SECONDS):
        """On the main thread: answer the next question with io, waiting up to timeout for
        one. Returns whether there was one."""
        try:
            request = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return False
        args = list(request["args"])
        if request["worker"] is not None and args and isinstance(args[0], str):
            # Say which builder asks; a sub-agent says it itself
            args[0] = f"[{request['worker']}] {args[0]}"
        try:
            request["answer"] = getattr(io, request["name"])(*args, **request["kwargs"])
        except KeyboardInterrupt as err:
            request["error"] = Stopped()
            raise err
        except Exception as err:
            request["error"] = err
        finally:
            request["done"].set()
        return True

    def stop(self):
        """Fail the questions waiting now and from now on."""
        self.stopped.set()
        while True:
            try:
                request = self.queue.get_nowait()
            except queue.Empty:
                return
            request["error"] = Stopped()
            request["done"].set()


class WorkerSession:
    """The web UI's session for a builder: every message it sends says which builder it's
    from, so the browser shows it in the builder's lane."""

    def __init__(self, session, worker):
        self.session = session
        self.worker = worker

    def emit(self, type, **payload):
        return self.session.emit(type, worker=self.worker, **payload)

    def __getattr__(self, name):
        return getattr(self.session, name)


class WorkerIO:
    """The io of the builder of work package worker. It writes through the main io one
    builder at a time (lock), and asks through asks."""

    def __init__(self, io, worker, asks, lock):
        target = io.for_worker(worker) if hasattr(io, "for_worker") else io
        own = vars(self)
        own.update(
            main=io,
            target=target,
            worker=worker,
            asks=asks,
            lock=lock,
            # The web UI shows the builder in its own lane; the terminal by a prefix
            prefixed=target is io,
            # No spinners or live Markdown from a thread
            pretty=False,
        )

    def __getattr__(self, name):
        if name in ASKS:
            return functools.partial(self.ask, name)
        value = getattr(self.target, name)
        if name in OUTPUTS and callable(value):
            return functools.partial(self.write, name, value)
        return value

    def __setattr__(self, name, value):
        # Never on the main io, like the permission mode's bypass flag
        vars(self)[name] = value

    def ask(self, name, *args, **kwargs):
        return self.asks.ask(self.worker, name, args, kwargs)

    def write(self, name, method, *args, **kwargs):
        if self.prefixed and name in PREFIXED:
            args = self.prefix(name, args)
        with self.lock:
            return method(*args, **kwargs)

    def prefix(self, name, args):
        tag = f"[{self.worker}]"
        if not args:
            return args
        first, rest = args[0], args[1:]
        if name == "tool_result":
            lines = first.splitlines() if isinstance(first, str) else list(first)
            return ([f"{tag} {line}" for line in lines],) + rest
        return (f"{tag} {first}",) + rest

    def usage_output(self, report, sent=0, received=0, cost=0.0):
        with self.lock:
            report = f"[{self.worker}] {report}" if self.prefixed else report
            return self.main.usage_output(report, sent=sent, received=received, cost=cost)

    def rule(self, *args, **kwargs):
        # Rules between requests would cut through the other builders' output
        pass

    @contextlib.contextmanager
    def esc_interrupts(self):
        # Esc and ^C reach the main thread, which stops the builders
        yield


def stop_workers(asks, agents, threads):
    """Stop the builders: tell their agents to stop, fail their waiting questions and kill
    the commands their threads are running."""
    asks.stop()
    for agent in agents:
        agent.stop_requested = True
    return tools.kill_running(threads)

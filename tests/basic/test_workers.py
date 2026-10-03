import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loom import tools
from loom.io import InputOutput
from loom.memory import ProjectDB, ProjectMemory
from loom.permissions import Permissions, Rule
from loom.utils import GitTemporaryDirectory, IgnorantTemporaryDirectory
from loom.workers import Asks, Stopped, WorkerIO, stop_workers


def in_thread(work):
    """Run work() on a thread; returns (thread, results), with its result or error."""
    results = {}

    def run():
        try:
            results["value"] = work()
        except BaseException as err:
            results["error"] = err

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results


class TestWorkerIO(unittest.TestCase):
    def test_output_is_prefixed(self):
        io = InputOutput(pretty=False, yes=True)
        for name in ("tool_output", "tool_warning", "tool_error", "tool_call", "tool_result"):
            setattr(io, name, MagicMock())
        io.usage_output = MagicMock()
        worker = WorkerIO(io, "core", Asks(), threading.Lock())

        worker.tool_output("Built it", bold=True)
        io.tool_output.assert_called_with("[core] Built it", bold=True)
        worker.tool_warning("Careful")
        io.tool_warning.assert_called_with("[core] Careful")
        worker.tool_call("Bash", "pytest")
        io.tool_call.assert_called_with("[core] Bash", "pytest")
        worker.tool_result("one\ntwo", error=True)
        io.tool_result.assert_called_with(["[core] one", "[core] two"], error=True)
        worker.tool_result(["three"])
        io.tool_result.assert_called_with(["[core] three"])
        worker.usage_output("Tokens: 1k sent", sent=1000, cost=0.5)
        io.usage_output.assert_called_with(
            "[core] Tokens: 1k sent", sent=1000, received=0, cost=0.5
        )

        # Its settings are its own
        self.assertFalse(worker.pretty)
        worker.bypass_permissions = True
        self.assertFalse(io.bypass_permissions)
        self.assertEqual(worker.yes, True)
        with worker.esc_interrupts():
            pass
        worker.rule()

    def test_questions_go_to_the_main_thread(self):
        io = InputOutput(pretty=False, yes=None)
        io.permission_ask = MagicMock(return_value="yes")
        asks = Asks()
        worker = WorkerIO(io, "cli", asks, threading.Lock())

        thread, results = in_thread(
            lambda: worker.permission_ask("Run this command?", subject="ls")
        )
        # The builder waits for the main thread
        for _ in range(100):
            if asks.serve(io):
                break
        thread.join(5)
        self.assertEqual(results["value"], "yes")
        io.permission_ask.assert_called_once_with("[cli] Run this command?", subject="ls")
        self.assertFalse(asks.serve(io, timeout=0))

    def test_stop_fails_waiting_questions(self):
        io = InputOutput(pretty=False, yes=None)
        asks = Asks()
        worker = WorkerIO(io, "cli", asks, threading.Lock())
        thread, results = in_thread(lambda: worker.confirm_ask("Go on?"))
        time.sleep(0.2)
        agent = MagicMock(stop_requested=False)
        stop_workers(asks, [agent], [thread.ident])
        thread.join(5)
        self.assertIsInstance(results["error"], Stopped)
        self.assertTrue(agent.stop_requested)
        # And any asked after
        with self.assertRaises(Stopped):
            worker.confirm_ask("Again?")

    def test_a_main_thread_interrupt_stops_the_builder_too(self):
        io = InputOutput(pretty=False, yes=None)
        io.confirm_ask = MagicMock(side_effect=KeyboardInterrupt)
        asks = Asks()
        worker = WorkerIO(io, "cli", asks, threading.Lock())
        thread, results = in_thread(lambda: worker.confirm_ask("Go on?"))
        with self.assertRaises(KeyboardInterrupt):
            for _ in range(100):
                asks.serve(io)
        thread.join(5)
        self.assertIsInstance(results["error"], Stopped)

    def test_web_builders_get_their_own_lane(self):
        from loom.web.backend.session import WebSession
        from loom.web.backend.webio import WebIO

        session = WebSession(interrupt=lambda: None)
        session.started = True
        io = WebIO(pretty=False, session=session)
        worker = WorkerIO(io, "web", Asks(), threading.Lock())
        io.tool_call("Read", "a.py")
        worker.tool_call("Bash", "npm test")
        worker.tool_result("passed")
        worker.tool_done("passed")
        io.tool_done("1 line")

        starts = [m for m in session.history if m["type"] == "tool_start"]
        self.assertEqual([m.get("worker") for m in starts], [None, "web"])
        # No prefix: the lane says whose it is
        self.assertEqual(starts[1]["name"], "Bash")
        outputs = [m for m in session.history if m["type"] == "tool_output"]
        self.assertEqual(outputs[0]["id"], starts[1]["id"])
        self.assertEqual(outputs[0]["worker"], "web")
        ends = [(m["id"], m.get("worker")) for m in session.history if m["type"] == "tool_end"]
        self.assertEqual(ends, [(starts[1]["id"], "web"), (starts[0]["id"], None)])


class TestRunningCommands(unittest.TestCase):
    def test_killing_a_threads_command(self):
        with IgnorantTemporaryDirectory() as root:
            Path(root, "sleep.py").write_bytes(b"import time\ntime.sleep(30)\n")
            started = time.monotonic()
            thread, results = in_thread(
                lambda: tools.run_command(f"{sys.executable} sleep.py", root, 60)
            )
            for _ in range(100):
                if thread.ident in tools.RUNNING:
                    break
                time.sleep(0.05)
            self.assertEqual(tools.kill_running([thread.ident]), 1)
            thread.join(10)
            self.assertLess(time.monotonic() - started, 20)
            code, _ = results["value"]
            self.assertNotEqual(code, 0)
            self.assertNotIn(thread.ident, tools.RUNNING)


class TestSharedState(unittest.TestCase):
    def test_permissions_copy_shares_rules(self):
        io = InputOutput(pretty=False, yes=None)
        main = Permissions(io, mode="accept-edits", allow=["bash(pytest*)"])
        worker_io = WorkerIO(io, "core", Asks(), threading.Lock())
        copy = main.copy_for(worker_io)
        self.assertEqual(copy.mode, "accept-edits")
        self.assertIs(copy.io, worker_io)
        copy.add_rule(Rule.parse("bash(ruff*)"), "session")
        self.assertIn("bash(ruff*)", [str(rule) for rule, _ in main.rules])
        main.mode = "bypass"
        self.assertEqual(copy.mode, "accept-edits")

    def test_the_database_takes_writes_from_threads(self):
        with patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"}), GitTemporaryDirectory():
            memory = ProjectMemory(".")
            memory.record_decision(None, "First")
            with ProjectDB(".").connect() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")

            def record(worker):
                for num in range(15):
                    memory.record_decision("building", f"{worker} decision {num}", source=worker)

            threads = [threading.Thread(target=record, args=(f"w{n}",)) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(len(memory.decisions()), 61)

    def test_costs_add_up_across_threads(self):
        from .test_orchestrator import make_coder

        with patch.dict(os.environ, {"LOOM_MEMORY_STORE": "keyword"}), GitTemporaryDirectory():
            from loom.orchestrator import Orchestrator

            from .test_agent import make_repo

            make_repo()
            coder = make_coder()
            orchestrator = Orchestrator(coder)

            def spend():
                for _ in range(200):
                    agent = MagicMock(total_cost=1.0, total_tokens_sent=10, total_tokens_received=1)
                    agent.loom_commit_hashes = set()
                    meter = dict(usage=dict(cost=0.5, tokens_sent=5, tokens_received=0))
                    orchestrator.collect(agent, meter)

            threads = [threading.Thread(target=spend) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertAlmostEqual(coder.total_cost, 400.0)
            self.assertEqual(coder.total_tokens_sent, 4000)


if __name__ == "__main__":
    unittest.main()

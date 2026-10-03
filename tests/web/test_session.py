import asyncio
import threading
import unittest

from loom.web.backend.protocol import event
from loom.web.backend.session import WebSession


class Interrupts:
    """Counts the interrupts a session sends, instead of signalling the test's thread."""

    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return True


def run_in_thread(func, *args):
    """Run func on a thread, and return a function that waits for its result."""
    result = {}

    def target():
        try:
            result["value"] = func(*args)
        except BaseException as err:
            result["error"] = err

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def join():
        thread.join(5)
        if thread.is_alive():
            raise AssertionError(f"{func.__name__} didn't return")
        if "error" in result:
            raise result["error"]
        return result["value"]

    return join


class TestEvents(unittest.TestCase):
    def test_unknown_event_type(self):
        with self.assertRaises(ValueError):
            event("nonsense")

    def test_backlog_starts_with_the_latest_snapshot(self):
        session = WebSession()
        session.emit("system", level="info", text="one")
        session.update(model="kimi-k3")
        session.emit("system", level="info", text="two")
        session.update(cwd="/repo")

        loop = asyncio.new_event_loop()
        try:
            backlog = session.connect(asyncio.Queue(), loop)
        finally:
            loop.close()

        self.assertEqual([msg["type"] for msg in backlog], ["session", "system", "system"])
        self.assertEqual(backlog[0]["model"], "kimi-k3")
        self.assertEqual(backlog[0]["cwd"], "/repo")
        self.assertEqual([msg.get("text") for msg in backlog[1:]], ["one", "two"])

    def test_connected_browsers_get_new_messages_in_order(self):
        session = WebSession()
        loop = asyncio.new_event_loop()
        client = asyncio.Queue()
        try:
            session.connect(client, loop)
            for num in range(5):
                session.emit("system", level="info", text=str(num))
            loop.run_until_complete(asyncio.sleep(0))
            texts = [client.get_nowait()["text"] for _ in range(client.qsize())]
            self.assertEqual(texts, ["0", "1", "2", "3", "4"])

            session.disconnect(client)
            session.emit("system", level="info", text="after")
            loop.run_until_complete(asyncio.sleep(0))
            self.assertTrue(client.empty())
        finally:
            loop.close()


class TestBrowserMessages(unittest.TestCase):
    def test_invalid_messages_are_refused(self):
        session = WebSession()
        self.assertFalse(session.handle("input"))
        self.assertFalse(session.handle(dict(type="input", text="   ")))
        self.assertFalse(session.handle(dict(type="input", text=3)))
        self.assertFalse(session.handle(dict(type="answer", ask_id="a1", value="y")))
        self.assertFalse(session.handle(dict(type="shutdown")))
        self.assertTrue(session.inputs.empty())

    def test_input_starts_a_turn(self):
        session = WebSession()
        self.assertTrue(session.handle(dict(type="input", text="hello")))
        self.assertEqual(session.wait_for_input(), "hello")
        self.assertTrue(session.busy)
        self.assertTrue(session.snapshot["busy"])
        self.assertEqual([msg["type"] for msg in session.history], ["user", "turn_start"])

        session.handle(dict(type="input", text="next"))
        self.assertEqual(session.wait_for_input(), "next")
        types = [msg["type"] for msg in session.history]
        self.assertEqual(types, ["user", "turn_start", "turn_end", "user", "turn_start"])
        self.assertEqual(session.history[2]["status"], "done")
        self.assertEqual(session.history[2]["turn_id"], session.history[1]["turn_id"])

    def test_ask_waits_for_the_answer(self):
        session = WebSession()
        join = run_in_thread(session.ask, "confirm", "Add file?", [("y", "Yes"), ("n", "No")], "y")

        for _ in range(50):
            asks = [msg for msg in session.history if msg["type"] == "ask"]
            if asks:
                break
            threading.Event().wait(0.02)
        ask = asks[0]
        self.assertEqual(ask["question"], "Add file?")
        self.assertEqual(
            ask["choices"], [dict(value="y", label="Yes"), dict(value="n", label="No")]
        )

        self.assertFalse(session.handle(dict(type="answer", ask_id="nope", value="n")))
        self.assertTrue(session.handle(dict(type="answer", ask_id=ask["ask_id"], value="n")))
        self.assertEqual(join(), "n")
        self.assertEqual(
            session.history[-1], event("ask_resolved", ask_id=ask["ask_id"], value="n")
        )
        # Answering twice does nothing
        self.assertFalse(session.handle(dict(type="answer", ask_id=ask["ask_id"], value="y")))

    def test_closing_ends_waits(self):
        session = WebSession()
        join = run_in_thread(session.ask, "prompt", "Feedback?")
        session.close()
        with self.assertRaises(EOFError):
            join()
        self.assertEqual(session.history[-1]["type"], "ask_resolved")
        self.assertIsNone(session.history[-1]["value"])

        with self.assertRaises(EOFError):
            session.wait_for_input()


class TestMode(unittest.TestCase):
    def test_mode_messages_go_to_set_mode(self):
        session = WebSession(interrupt=Interrupts())
        # Before loom has a coder there's nothing to switch
        self.assertFalse(session.handle(dict(type="mode", mode="plan")))

        modes = []
        session.set_mode = lambda mode: modes.append(mode) or True
        self.assertTrue(session.handle(dict(type="mode", mode="plan")))
        self.assertFalse(session.handle(dict(type="mode", mode=3)))
        self.assertEqual(modes, ["plan"])


class TestCancel(unittest.TestCase):
    def test_cancel_interrupts_only_a_busy_session(self):
        interrupts = Interrupts()
        session = WebSession(interrupt=interrupts)
        self.assertFalse(session.handle(dict(type="cancel")))
        self.assertEqual(interrupts.count, 0)

        session.handle(dict(type="input", text="work"))
        session.wait_for_input()
        self.assertTrue(session.handle(dict(type="cancel")))
        # Pressing Esc again before the Coder notices doesn't interrupt twice
        self.assertFalse(session.handle(dict(type="cancel")))
        self.assertEqual(interrupts.count, 1)

        self.assertTrue(session.consume_cancel())
        self.assertFalse(session.consume_cancel())

        session.handle(dict(type="input", text="again"))
        session.wait_for_input()
        turn_ends = [msg for msg in session.history if msg["type"] == "turn_end"]
        self.assertEqual(turn_ends[0]["status"], "cancelled")

        # The next turn starts afresh
        session.end_turn()
        turn_ends = [msg for msg in session.history if msg["type"] == "turn_end"]
        self.assertEqual(turn_ends[1]["status"], "done")

    def test_cancel_without_signals(self):
        # Like loom started with SIGINT ignored: the waiting question interrupts itself
        session = WebSession(interrupt=lambda: False)
        session.handle(dict(type="input", text="work"))
        session.wait_for_input()
        join = run_in_thread(session.ask, "confirm", "Run pytest?", [("y", "Yes")])
        for _ in range(50):
            if any(msg["type"] == "ask" for msg in session.history):
                break
            threading.Event().wait(0.02)
        self.assertTrue(session.handle(dict(type="cancel")))
        with self.assertRaises(KeyboardInterrupt):
            join()
        self.assertIsNone(session.history[-1]["value"])
        # The Coder still learns the interrupt was Esc, and it only fires once
        self.assertTrue(session.consume_cancel())
        session.check_cancel()

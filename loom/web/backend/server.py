"""Start the web server for `loom --web`, on a background thread.

The Coder keeps the main thread, so ^C and the browser's Esc interrupt it the same way
they do in the terminal.
"""

import socket
import threading
import time
import webbrowser

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
START_TIMEOUT = 10


class ServerError(Exception):
    pass


def check_port(port, host=HOST):
    """Raise ServerError if port can't be listened on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as err:
            raise ServerError(f"Can't listen on {host}:{port}: {err.strerror or err}")


def start_server(session, port=DEFAULT_PORT, open_browser=True, host=HOST, io=None):
    """Serve the web UI for session, and io (the WebIO) on host:port, and return its URL
    once it's listening. Only this computer can connect."""
    import uvicorn

    from .app import create_app

    check_port(port, host)
    config = uvicorn.Config(
        create_app(session, io=io),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="loom-web", daemon=True)
    thread.start()

    deadline = time.monotonic() + START_TIMEOUT
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise ServerError(f"The web server didn't start on {host}:{port}.")
        time.sleep(0.05)

    session.started = True
    url = f"http://{host}:{port}/"
    if open_browser:
        webbrowser.open(url)
    return url

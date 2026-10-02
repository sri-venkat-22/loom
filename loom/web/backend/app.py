"""The FastAPI app behind `loom --web`: the /ws WebSocket and the built frontend."""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from loom import __version__

from .api import make_router
from .protocol import PROTOCOL_VERSION

# Where `npm run build` in loom/web/frontend puts the frontend
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

NOT_BUILT = """<!doctype html>
<meta charset="utf-8">
<title>loom</title>
<body style="background:#1a1a1a;color:#e5e5e5;font:14px ui-monospace,monospace;padding:40px">
<p>loom's web frontend hasn't been built. In a loom checkout, run:</p>
<pre>cd loom/web/frontend
npm install
npm run build</pre>
<p>and reload this page.</p>
</body>
"""


def local_origin(origin):
    """Whether a browser page at origin may use the WebSocket: only pages served from this
    computer can, so a website can't drive loom from the user's browser."""
    if not origin:
        # Not a browser
        return True
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def create_app(session, static_dir=STATIC_DIR, allowed_hosts=LOCAL_HOSTS, io=None):
    """The app for session. io is the WebIO, which the side pane's /api routes read the
    project through."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # Refuse other Host names, so a rebound DNS name can't reach the server
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.include_router(make_router(io))

    @app.get("/api/health")
    def health():
        return dict(ok=True, version=__version__, protocol=PROTOCOL_VERSION)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if not local_origin(socket.headers.get("origin")):
            await socket.close(code=1008)
            return
        await socket.accept()

        client = asyncio.Queue()
        backlog = session.connect(client, asyncio.get_running_loop())

        async def relay():
            for message in backlog:
                await socket.send_json(message)
            while True:
                await socket.send_json(await client.get())

        sender = asyncio.create_task(relay())
        try:
            while True:
                try:
                    message = await socket.receive_json()
                except ValueError:
                    continue
                session.handle(message)
        except WebSocketDisconnect:
            pass
        finally:
            session.disconnect(client)
            sender.cancel()

    static_dir = Path(static_dir)
    if (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    else:

        @app.get("/")
        def not_built():
            return HTMLResponse(NOT_BUILT)

    return app

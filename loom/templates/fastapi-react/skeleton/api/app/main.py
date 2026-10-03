"""The API: JSON routes under /api, and the built frontend (web/dist) at /."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="app")

# The frontend, once built with npm --prefix web run build. In development, Vite's dev
# server serves it instead and proxies /api here.
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


@app.get("/api/health")
def health():
    return {"status": "ok"}


if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")

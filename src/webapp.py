"""FastAPI supervision platform for the paper-trading bot.

Serves a single-page live dashboard, a control REST API and a WebSocket that
pushes the bot's state a few times per second. The bot itself runs continuously
in a background thread (see :class:`~src.bot_runner.BotRunner`).

PAPER ONLY: every control action only moves *simulated* money inside the paper
system. There is no endpoint that talks to Polymarket for writing, no wallet, no
keys, no order signing. The server binds to localhost by default.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .bot_runner import BotRunner
from .logger import get_logger
from .runtime_config import EDITABLE_FIELDS

log = get_logger("webapp")

_WEB_DIR = Path(__file__).resolve().parent / "web"
PUSH_SECONDS = 2.0


class ConfigUpdate(BaseModel):
    values: dict[str, Any]


class CloseRequest(BaseModel):
    match: str


def create_app(runner: BotRunner, push_seconds: float = PUSH_SECONDS) -> FastAPI:
    app = FastAPI(title="Polymarket Paper Bot — Supervisión", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((_WEB_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    def state() -> JSONResponse:
        return JSONResponse(runner.snapshot())

    @app.get("/api/config")
    def get_config() -> JSONResponse:
        fields = [
            {"name": n, "label": EDITABLE_FIELDS[n][1], "type": EDITABLE_FIELDS[n][0].__name__}
            for n in EDITABLE_FIELDS
        ]
        return JSONResponse({"fields": fields, "values": runner.config_values()})

    @app.post("/api/config")
    def post_config(update: ConfigUpdate) -> JSONResponse:
        runner.update_config(update.values)
        return JSONResponse({"ok": True})

    @app.post("/api/control/pause")
    def pause() -> JSONResponse:
        runner.pause()
        return JSONResponse({"ok": True, "status": "paused"})

    @app.post("/api/control/resume")
    def resume() -> JSONResponse:
        runner.resume()
        return JSONResponse({"ok": True, "status": "running"})

    @app.post("/api/control/reset")
    def reset() -> JSONResponse:
        runner.reset()
        return JSONResponse({"ok": True})

    @app.post("/api/control/close")
    def close(req: CloseRequest) -> JSONResponse:
        runner.close_position(req.match)
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(runner.snapshot())
                await asyncio.sleep(push_seconds)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("WebSocket closed: %s", exc)
            with contextlib.suppress(Exception):
                await websocket.close()

    return app

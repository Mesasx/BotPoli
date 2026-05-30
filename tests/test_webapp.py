"""Tests for the FastAPI supervision platform and the state snapshot builder."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.bot_runner import build_state
from src.models import BUY, EquitySnapshot, utcnow
from src.runtime_config import editable_values
from src.storage import Storage
from src.webapp import create_app
from tests.conftest import make_order, make_snapshot


class FakeRunner:
    """Minimal stand-in exposing the surface the web app uses."""

    def __init__(self, config):
        self.config = config
        self.calls: list[tuple] = []
        self._snap = {"status": "running", "kpis": {}, "positions": [],
                      "closed": [], "best": [], "worst": [], "markets": [],
                      "alerts": [], "equity_curve": [], "logs": []}

    def snapshot(self):
        return dict(self._snap)

    def config_values(self):
        return editable_values(self.config)

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))

    def reset(self):
        self.calls.append(("reset",))

    def close_position(self, match):
        self.calls.append(("close", match))

    def update_config(self, values):
        self.calls.append(("config", values))


def _client(config):
    runner = FakeRunner(config)
    return TestClient(create_app(runner)), runner


def test_index_served(config):
    client, _ = _client(config)
    r = client.get("/")
    assert r.status_code == 200
    assert "Polymarket Paper Bot" in r.text
    assert "PAPER" in r.text


def test_state_endpoint(config):
    client, _ = _client(config)
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_config_endpoints(config):
    client, runner = _client(config)
    r = client.get("/api/config")
    body = r.json()
    assert any(f["name"] == "initial_balance" for f in body["fields"])
    assert body["values"]["initial_balance"] == 1000.0

    r2 = client.post("/api/config", json={"values": {"initial_balance": 2500}})
    assert r2.status_code == 200
    assert ("config", {"initial_balance": 2500}) in runner.calls


def test_control_endpoints(config):
    client, runner = _client(config)
    assert client.post("/api/control/pause").json()["status"] == "paused"
    assert client.post("/api/control/resume").json()["status"] == "running"
    client.post("/api/control/reset")
    client.post("/api/control/close", json={"match": "will-x"})
    names = [c[0] for c in runner.calls]
    assert names == ["pause", "resume", "reset", "close"]
    assert ("close", "will-x") in runner.calls


def test_websocket_pushes_state(config):
    runner = FakeRunner(config)
    app = create_app(runner, push_seconds=0.01)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        assert data["status"] == "running"


def test_build_state_reports_kpis(config):
    storage = Storage(config)
    try:
        # Open a position and record an equity point.
        storage.save_order(make_order(side=BUY, fill_price=0.40, shares=10.0))
        storage.save_market_snapshots([make_snapshot(midpoint=0.50)])
        storage.save_equity_snapshot(EquitySnapshot(
            timestamp=utcnow(), cash=996.0, positions_value=5.0, equity=1001.0,
            realized_pnl=0.0, unrealized_pnl=1.0, open_positions=1,
            exposure=5.0, drawdown=0.0,
        ))
        state = build_state(storage, config, runner=None)
        assert state["kpis"]["initial_balance"] == 1000.0
        assert state["kpis"]["open_positions"] == 1
        assert len(state["positions"]) == 1
        assert state["positions"][0]["entry_price"] == 0.40
        assert state["status"] == "stopped"  # no runner
        assert isinstance(state["equity_curve"], list)
    finally:
        storage.close()

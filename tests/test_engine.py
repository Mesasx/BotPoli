"""End-to-end engine cycle: scan -> signal -> risk -> fill -> persist."""

from __future__ import annotations

from src.main import PaperEngine
from src.storage import Storage

from .conftest import FakeClient, sample_gamma_market


def test_run_cycle_opens_position_on_cheap_yes(config):
    client = FakeClient(
        markets=[sample_gamma_market()],
        quotes={
            "tok_yes": {"best_bid": 0.39, "best_ask": 0.40, "midpoint": 0.395, "spread": 0.01},
            "tok_no": {"best_bid": 0.59, "best_ask": 0.60, "midpoint": 0.595, "spread": 0.01},
        },
    )
    storage = Storage(config)
    try:
        engine = PaperEngine(config, storage, client)
        summary = engine.run_cycle()

        assert summary["buys"] == 1          # YES bought; NO blocked by ALLOW_NO=false
        assert summary["open_positions"] == 1
        assert summary["equity"] <= 1000.0   # cash spent + (small) slippage cost

        pf = storage.load_portfolio()
        pos = pf.get_position("tok_yes")
        assert pos is not None
        assert pos.outcome == "Yes"
        # Roughly max_trade_size USDC deployed at ~0.40.
        assert 4.9 < pos.cost_basis < 5.1
    finally:
        storage.close()


def test_run_cycle_exits_on_take_profit(config):
    market = sample_gamma_market()
    # First cycle: buy at ~0.40
    buy_quotes = {
        "tok_yes": {"best_bid": 0.39, "best_ask": 0.40, "midpoint": 0.395, "spread": 0.01},
        "tok_no": {"best_bid": 0.59, "best_ask": 0.60, "midpoint": 0.595, "spread": 0.01},
    }
    storage = Storage(config)
    try:
        engine = PaperEngine(config, storage, FakeClient([market], buy_quotes))
        engine.run_cycle()
        assert storage.load_portfolio().open_position_count() == 1

        # Second cycle: price jumps to 0.55 (+~37%) -> take profit sell
        tp_quotes = {
            "tok_yes": {"best_bid": 0.55, "best_ask": 0.56, "midpoint": 0.555, "spread": 0.01},
            "tok_no": {"best_bid": 0.44, "best_ask": 0.45, "midpoint": 0.445, "spread": 0.01},
        }
        engine2 = PaperEngine(config, storage, FakeClient([market], tp_quotes))
        summary = engine2.run_cycle()
        assert summary["sells"] == 1

        pf = storage.load_portfolio()
        assert pf.open_position_count() == 0
        assert pf.realized_pnl > 0          # profitable exit
        assert pf.win_rate() == 1.0
    finally:
        storage.close()

"""Portfolio accounting: PnL, equity, exposure, win rate, avg-cost."""

from __future__ import annotations

from src.models import BUY, SELL
from src.portfolio import Portfolio

from .conftest import make_order


def test_buy_updates_cash_and_position(config):
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(side=BUY, fill_price=0.40, shares=10))  # cost 4.0
    assert pf.cash == 996.0
    pos = pf.get_position("tok_yes")
    assert pos is not None
    assert pos.shares == 10
    assert pos.avg_price == 0.40
    assert pf.open_position_count() == 1


def test_weighted_average_price_on_second_buy():
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(fill_price=0.40, shares=10))  # 4.0
    pf.apply_order(make_order(fill_price=0.50, shares=10))  # 5.0
    pos = pf.get_position("tok_yes")
    assert pos.shares == 20
    assert abs(pos.avg_price - 0.45) < 1e-9


def test_sell_realizes_pnl_and_closes_position():
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(side=BUY, fill_price=0.40, shares=10))
    realized = pf.apply_order(make_order(side=SELL, fill_price=0.50, shares=10))
    assert abs(realized - 1.0) < 1e-9            # (0.50-0.40)*10
    assert abs(pf.realized_pnl - 1.0) < 1e-9
    assert pf.get_position("tok_yes") is None     # closed
    assert len(pf.closed_trades) == 1
    assert pf.win_rate() == 1.0
    # cash back: 996 + 5.0 = 1001
    assert abs(pf.cash - 1001.0) < 1e-9


def test_equity_and_unrealized_pnl_with_mark():
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(side=BUY, fill_price=0.40, shares=10))
    pf.mark_to_market({"tok_yes": 0.60})
    assert abs(pf.unrealized_pnl() - 2.0) < 1e-9   # (0.60-0.40)*10
    assert abs(pf.positions_value() - 6.0) < 1e-9
    assert abs(pf.equity() - (996.0 + 6.0)) < 1e-9


def test_exposure_aggregation_by_market():
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(token_id="a", market_id="m1", fill_price=0.40, shares=10))
    pf.apply_order(make_order(token_id="b", market_id="m1", fill_price=0.20, shares=10))
    pf.apply_order(make_order(token_id="c", market_id="m2", fill_price=0.30, shares=10))
    pf.mark_to_market({"a": 0.40, "b": 0.20, "c": 0.30})
    assert abs(pf.market_exposure("m1") - 6.0) < 1e-9   # 4 + 2
    assert abs(pf.market_exposure("m2") - 3.0) < 1e-9
    assert abs(pf.total_exposure() - 9.0) < 1e-9


def test_win_rate_mixed():
    pf = Portfolio(1000.0)
    pf.apply_order(make_order(token_id="a", side=BUY, fill_price=0.40, shares=10))
    pf.apply_order(make_order(token_id="a", side=SELL, fill_price=0.50, shares=10))  # win
    pf.apply_order(make_order(token_id="b", side=BUY, fill_price=0.40, shares=10))
    pf.apply_order(make_order(token_id="b", side=SELL, fill_price=0.30, shares=10))  # loss
    assert pf.win_rate() == 0.5


def test_replay_from_orders_is_deterministic():
    orders = [
        make_order(side=BUY, fill_price=0.40, shares=10),
        make_order(side=SELL, fill_price=0.50, shares=10),
    ]
    pf = Portfolio.from_orders(1000.0, orders)
    assert abs(pf.realized_pnl - 1.0) < 1e-9
    assert pf.open_position_count() == 0

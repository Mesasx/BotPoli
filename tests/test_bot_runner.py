"""Tests for the background bot runner's control handling (no network)."""

from __future__ import annotations

from src.bot_runner import BotRunner


class _Stub:
    """Records calls for reset/close; stands in for storage/engine/client."""

    def __init__(self):
        self.reset_called = 0
        self.closed = []

    def reset_paper(self):
        self.reset_called += 1

    def force_close(self, match):
        self.closed.append(match)
        return 1

    def close(self):
        pass


def _runner(config):
    return BotRunner(base_config=config)


def test_pause_and_resume_change_status(config):
    r = _runner(config)
    storage = engine = client = _Stub()
    r.pause()
    r._drain_commands(engine, storage, client)
    assert r.status == "paused"
    r.resume()
    r._drain_commands(engine, storage, client)
    assert r.status == "running"


def test_reset_and_close_dispatch_to_engine(config):
    r = _runner(config)
    stub = _Stub()
    r.reset()
    r.close_position("will-x-happen")
    r._drain_commands(engine=stub, storage=stub, client=stub)
    assert stub.reset_called == 1
    assert stub.closed == ["will-x-happen"]


def test_command_failure_does_not_raise(config):
    r = _runner(config)

    class Boom(_Stub):
        def force_close(self, match):
            raise RuntimeError("boom")

    r.close_position("x")
    # Should swallow the error and record it, not raise.
    r._drain_commands(engine=Boom(), storage=Boom(), client=Boom())
    assert r._last_error and "boom" in r._last_error


def test_config_values_exposed(config):
    r = _runner(config)
    vals = r.config_values()
    assert vals["initial_balance"] == config.initial_balance

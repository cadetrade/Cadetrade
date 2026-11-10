from datetime import date

import numpy as np
import pandas as pd

from cadetrade.research.backtest import run_backtest
from cadetrade.research.rconfig import Config, RiskCfg
from cadetrade.research.risk import RiskManager


def synthetic(n=3000, seed=1):
    rng = np.random.default_rng(seed)
    close = 30000 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    open_ = np.r_[close[0], close[:-1]]
    spread = close * 0.004
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread,
                         "low": np.minimum(open_, close) - spread, "close": close, "volume": 1.0}, index=idx)


def test_position_size_respects_risk_and_cap():
    r = RiskManager(RiskCfg(risk_per_trade=0.01, max_position_pct=0.25))
    qty = r.position_size(1000, entry=100, stop=98)   # rischio 10$ / 2$ = 5, cap 2.5
    assert qty == 2.5
    assert r.position_size(1000, entry=100, stop=60) == 0.25   # 10$/40$


def test_kill_switch_on_drawdown():
    r = RiskManager(RiskCfg(max_drawdown=0.15, max_daily_loss=0.5))
    r.update(1000, date(2024, 1, 1))
    r.update(840, date(2024, 1, 2))
    assert r.halted_reason == "max_drawdown" and r.position_size(840, 100, 90) == 0


def test_daily_loss_resets_next_day():
    r = RiskManager(RiskCfg(max_daily_loss=0.03, max_drawdown=0.5))
    r.update(1000, date(2024, 1, 1))
    r.update(960, date(2024, 1, 1))
    assert r.halted_reason == "daily_loss"
    r.update(960, date(2024, 1, 2))
    assert r.can_open


def test_backtest_runs_and_limits_drawdown():
    res = run_backtest(synthetic(), Config())
    assert res["trades"] > 0
    assert res["max_drawdown_pct"] > -20

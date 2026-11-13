import numpy as np
import pandas as pd

from cadetrade.research import intraday as it


def _day(date, prices, vol=1000):
    idx = pd.date_range(f"{date} 09:30", periods=len(prices), freq="5min", tz="America/New_York")
    p = np.array(prices, float)
    return pd.DataFrame({"open": p, "high": p * 1.0005, "low": p * 0.9995, "close": p, "volume": vol}, index=idx)


def test_breakout_long_closes_at_end_of_day():
    prices = [100] * 6 + [100.2] + list(np.linspace(100.3, 102, 71))
    t = it.s_apertura30(_day("2026-01-05", prices), 100, 0.0)
    assert len(t) == 1 and t[0] > 0.01


def test_gap_fade_hits_prior_close():
    prices = list(np.linspace(101, 99.5, 10)) + [99.5] * 68
    t = it.s_gap(_day("2026-01-05", prices), 100, 0.0)
    assert t and abs(t[0] - (-(100 / 101 - 1))) < 1e-9


def test_costs_lower_results_and_no_overnight():
    bars = pd.concat([_day("2026-01-05", list(np.linspace(100, 103, 78))),
                      _day("2026-01-06", [102] + list(np.linspace(102, 99, 77)))])
    d0, t0 = it.run(bars, it.s_ultima, 0.0)
    d1, t1 = it.run(bars, it.s_ultima, 0.001)
    assert len(t0) == len(t1) and sum(t1) < sum(t0)
    assert len(d0) == 2


def test_vwap_and_rsi_run_without_errors():
    rng = np.random.default_rng(1)
    prices = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 78)))
    day = _day("2026-01-05", prices)
    for t in it.s_vwap(day, 100, 0.0002) + it.s_rsi2(day, 100, 0.0002):
        assert -0.2 < t < 0.2

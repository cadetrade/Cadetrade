import numpy as np
import pandas as pd

from cadetrade.research import scanner as sc


def _day(date, prices, vol=1000.0):
    idx = pd.date_range(f"{date} 09:30", periods=len(prices), freq="5min", tz="America/New_York")
    p = np.array(prices, float)
    v = np.full(len(p), vol)
    return pd.DataFrame({"open": p, "high": p * 1.001, "low": p * 0.999, "close": p, "volume": v}, index=idx)


def test_follow_first_bar_long_wins_on_up_day():
    d = _day("2026-01-05", [100] + list(np.linspace(100.5, 103, 77)))
    d.iloc[0, d.columns.get_loc("close")] = 100.4
    r = sc.t_segui(d, 99)
    assert r is not None and r > 0.02
    assert sc.t_contro(d, 99) < 0


def test_pick_needs_unusual_first_bar_volume():
    days = {}
    for i, dt in enumerate(pd.bdate_range("2026-01-01", periods=16)):
        a = _day(dt.date(), [50] * 30, vol=1000.0)
        b = _day(dt.date(), [50] * 30, vol=1000.0)
        if i == 15:
            a.iloc[0, a.columns.get_loc("volume")] = 5000.0
        days[dt.date()] = {"AAA": a, "BBB": b}
    data = {s: pd.concat([days[d][s] for d in sorted(days)]) for s in ("AAA", "BBB")}
    per_day, fv, pc = sc.build_days(data)
    last = sorted(per_day)[-1]
    assert sc.pick(last, per_day[last], fv, pc) == ["AAA"]

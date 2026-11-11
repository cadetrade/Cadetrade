import numpy as np
import pandas as pd

from cadetrade import backtest


def _df(trend, n=200, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(trend, 0.01, n)))
    idx = pd.date_range("2026-03-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)


def test_uptrend_makes_money_downtrend_short_makes_money():
    data = {"SPY": _df(0.004), "QQQ": _df(-0.004, seed=2)}
    start = pd.Timestamp("2026-08-01")
    res = backtest.run(data, start)
    assert res["trade"] >= 2
    sides = {(t["symbol"], t["lato"]) for t in res["trades"]}
    assert ("SPY", "long") in sides and ("QQQ", "short") in sides
    assert res["profitto_usd"] > 0
    longonly = backtest.run(data, start, allow_short=False)
    assert all(t["lato"] == "long" for t in longonly["trades"])
    assert "SPY" in res["compra_e_tieni_%"] and backtest.report(res, "x")


def test_learning_only_uses_past_and_logs_lessons():
    data = {"SPY": _df(0.0, n=400, seed=3), "QQQ": _df(0.0, n=400, seed=4), "GLD": _df(0.0, n=400, seed=5)}
    res = backtest.run(data, pd.Timestamp("2026-06-01"), learn=True)
    assert isinstance(res["lezioni"], list) and res["curva"]
    # cambiare il futuro non cambia le decisioni prese prima
    cut = pd.Timestamp("2026-10-01")
    short = {k: v[v.index < cut.tz_localize("UTC")] for k, v in data.items()}
    a = [t for t in backtest.run(short, pd.Timestamp("2026-06-01"), learn=True)["trades"] if t["fuori"] < "2026-09-25"]
    b = [t for t in res["trades"] if t["fuori"] < "2026-09-25"]
    assert a == b


def test_compare_pieces_run():
    data = {"SPY": _df(0.002, n=400, seed=3), "QQQ": _df(0.001, n=400, seed=4), "GLD": _df(0.0, n=400, seed=5)}
    start = pd.Timestamp("2026-06-01")
    bh = backtest.buy_hold(data, {"SPY": 0.5, "QQQ": 0.3, "GLD": 0.2}, start, 100_000)
    assert bh and abs(bh[0][1] - 100_000) < 500
    assert backtest.rotation(data, start, 100_000, top=2)
    assert backtest.run(data, start, slow=True, learn=True)["curva"]


def test_leverage_doubles_moves_and_charges_interest():
    d = pd.date_range("2026-01-01", periods=3, freq="D")
    curve = [(d[0], 100_000.0), (d[1], 110_000.0), (d[2], 99_000.0)]
    out = backtest.levered(curve, 2.0, rate=0.0)
    assert round(out[1][1]) == 120_000 and round(out[2][1]) == 96_000
    assert backtest.levered(curve, 2.0, rate=0.1)[1][1] < 120_000

import numpy as np
import pandas as pd

from cadetrade.research import optimize


def _data():
    out = {}
    idx = pd.bdate_range("2021-01-04", periods=900, tz="UTC")
    for i, s in enumerate(("SPY", "QQQ", "GLD", "BTC/USD", "MSFT", "AAPL", "NVDA", "AMZN")):
        rng = np.random.default_rng(i)
        c = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, len(idx))))
        out[s] = pd.DataFrame({"close": c}, index=idx)
    return out


def test_simulate_basic_and_no_lookahead():
    px = optimize.closes(_data())
    a = optimize.simulate(px, optimize.BASES["SPY/QQQ/oro"], ("fissa", 1.0), "tiene", 0.0, False)
    assert abs(a.iloc[0] - 1) < 1e-9 and a.notna().all()
    # cambiare il futuro non cambia il passato
    px2 = px.copy()
    px2.iloc[-50:] *= 1.5
    for args in [(("volat", 0.15), "short", 0.5, True), (("toro/orso", (2.0, 0.5)), "oro", 0.3, False)]:
        x = optimize.simulate(px, optimize.BASES["tutto il nucleo"], *args)
        y = optimize.simulate(px2, optimize.BASES["tutto il nucleo"], *args)
        assert np.allclose(x.iloc[:-51], y.iloc[:-51])


def test_leverage_two_x_roughly_doubles_daily_moves():
    px = optimize.closes(_data())
    one = optimize.simulate(px, {"SPY": 1.0}, ("fissa", 1.0), "tiene", 0.0, False).pct_change().dropna()
    two = optimize.simulate(px, {"SPY": 1.0}, ("fissa", 2.0), "tiene", 0.0, False).pct_change().dropna()
    assert abs((two.iloc[10:] / one.iloc[10:]).median() - 2) < 0.05


def test_cash_etf_and_tilt():
    d = _data()
    d["BIL"] = d["MSFT"]
    px = optimize.closes(d)
    w = optimize.targets(px, {"QQQ": 0.6, "GLD": 0.4}, ("fissa", 0.5), "tiene", 0.0, False, cash_sym="BIL", tilt=0.5)
    assert abs((w[["QQQ", "GLD", "BIL"]].sum(axis=1) - 1).iloc[-1]) < 1e-9
    assert abs(w["QQQ"].iloc[-1] + w["GLD"].iloc[-1] - 0.5) < 1e-9 and w["QQQ"].iloc[-1] != 0.3

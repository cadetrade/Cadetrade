import numpy as np
import pandas as pd

from cadetrade.research import armageddon as ar


def test_call_price_sane():
    assert ar.call_price(100, 105, 30, 0.5) > 0
    assert ar.call_price(100, 105, 0, 0.5) == 0
    assert ar.call_price(120, 105, 0, 0.5) == 15
    assert ar.call_price(100, 105, 30, 0.8) > ar.call_price(100, 105, 30, 0.4)


def test_run_finds_breakouts_and_caps_shots():
    idx = pd.bdate_range("2021-01-04", periods=800, tz="UTC")
    rng = np.random.default_rng(0)
    data = {}
    for i, s in enumerate(("AAA", "BBB", "CCC")):
        c = 50 * np.exp(np.cumsum(rng.normal(0.002, 0.025, len(idx))))
        v = rng.uniform(0.5, 3.0, len(idx)) * 1e6
        data[s] = pd.DataFrame({"close": c, "volume": v}, index=idx)
    spy = pd.Series(np.linspace(100, 300, len(idx)), index=idx.tz_convert("America/New_York").normalize().tz_localize(None))
    shots = ar.run(data, spy)
    assert shots
    for y in {x["anno"] for x in shots}:
        assert sum(x["anno"] == y for x in shots) <= ar.SHOTS
    assert all(x["moltiplica"] >= 0 for x in shots)
    lev = ar.run(data, spy, lever5=True)
    assert all(x["moltiplica"] >= 0 for x in lev)

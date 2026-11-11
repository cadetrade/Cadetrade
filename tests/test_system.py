import pandas as pd

from cadetrade import system
from conftest import make_ctx


def test_plan_orders_band_sells_first_and_cleans_extra():
    pos = {"QQQ": {"qty": 100, "price": 500.0}, "BTC/USD": {"qty": 0.5, "price": 90000.0},
           "MSFT": {"qty": 10, "price": 400.0}}
    out = system.plan_orders({"QQQ": 0.6, "GLD": 0.5}, 100_000, pos, {"QQQ": 500.0, "GLD": 300.0, "MSFT": 400.0})
    sides = [(o["symbol"], o["side"], o["qty"]) for o in out]
    assert ("BTC/USD", "sell", 0.5) in sides and ("MSFT", "sell", 10) in sides
    assert ("GLD", "buy", 166) in sides
    assert ("QQQ", "buy", 20) in sides                    # 100 -> 120 azioni = 10.000$ > 3%: va comprato
    assert out[-1]["side"] == "buy" and out[0]["side"] == "sell"


def test_plan_orders_inside_band_does_nothing():
    pos = {"QQQ": {"qty": 120, "price": 500.0}, "GLD": {"qty": 130, "price": 300.0}}
    assert system.plan_orders({"QQQ": 0.61, "GLD": 0.4}, 100_000, pos, {"QQQ": 500.0, "GLD": 300.0}) == []


def test_run_system_once_a_day(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path / "s")
    monkeypatch.setattr(system, "target_weights", lambda b, p: ({"QQQ": 0.9, "GLD": 0.6}, {"leva": 1.5, "giorno_dati": "x"}))
    t = pd.Timestamp("2026-11-11 21:40", tz="Europe/Rome").to_pydatetime()
    res = system.run_system(ctx, "bilanciato", t)
    assert {o["symbol"] for o in res["orders"] if o["side"] == "buy"} >= {"QQQ", "GLD"}
    assert system.run_system(ctx, "bilanciato", t) == {"skipped": "già fatto oggi"}
    assert any("Sistema bilanciato" in m for m in ctx.notifier.sent)

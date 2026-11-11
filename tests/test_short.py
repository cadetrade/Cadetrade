"""Short veri: solo su titoli liquidi in trend ribassista, stop sopra il prezzo sul broker, chiusura con cover."""
from conftest import at, make_ctx

from cadetrade.brain import FakeBrain
from cadetrade.cage import review
from cadetrade.stops import forced_stops, initial_stop, new_record, trail
from cadetrade.wakeup import guard, load_risk, run_wakeup

ACC = {"equity": 100_000.0, "cash": 100_000.0, "last_equity": 100_000.0}


def brain(*orders):
    return FakeBrain({"orders": list(orders), "reasoning": "r", "planNext": "p"})


def rv(orders, **kw):
    args = dict(slot_hhmm="1540", account=ACC, positions={}, prices={"SPY": 600, "BTC/USD": 95000, "PLTR": 150},
                stock_market_open=True, peak_equity=100_000, stopped_today=set(), trend={"SPY": False}, atr={"SPY": 6.0})
    args.update(kw)
    return review(orders, **args)


def test_short_stop_is_above_and_sized_by_risk():
    assert initial_stop(100, 2.0, d=-1) == 104.0
    v = rv([{"symbol": "SPY", "side": "short", "qty": 30, "why": "rimbalzo sotto EMA50", "stop": 630}], atr={"SPY": 10.0})
    o = v.accepted[0]
    assert o["stop"] == 630.0 and o["qty"] == 16            # 500 $ di rischio / 30 $ a pezzo


def test_short_only_on_allowed_symbols_in_downtrend_at_entry_wakeups():
    reasons = {r["symbol"]: r["reason"] for r in rv([
        {"symbol": "BTC/USD", "side": "short", "qty": 0.1, "why": "x"},
        {"symbol": "PLTR", "side": "short", "qty": 5, "why": "x"}], picks={"PLTR"}).rejected}
    assert "solo su" in reasons["BTC/USD"] and "solo su" in reasons["PLTR"]
    assert "trend ribassista" in rv([{"symbol": "SPY", "side": "short", "qty": 5, "why": "x"}], trend={"SPY": True}).rejected[0]["reason"]
    assert "si entra solo" in rv([{"symbol": "SPY", "side": "short", "qty": 5, "why": "x"}], slot_hhmm="1830").rejected[0]["reason"]


def test_total_short_exposure_is_capped():
    pos = {"QQQ": {"qty": -50, "avg_entry": 520, "price": 520, "market_value": -26_000, "plpc": 0.0}}
    v = rv([{"symbol": "SPY", "side": "short", "qty": 10, "why": "x"}], positions=pos,
           prices={"SPY": 600, "QQQ": 520}, trend={"SPY": False, "QQQ": False}, atr={"SPY": 30.0})
    assert "short oltre il 30%" in v.rejected[0]["reason"]


def test_short_trail_moves_stop_down_only():
    rec = new_record(100.0, 104.0, 2.0, -1)
    assert trail(rec, 96.0) and rec["stop"] == 100.0       # +1R a favore: pareggio
    assert trail(rec, 90.0) and rec["stop"] == 95.0        # segue a 2,5 ATR dal minimo
    assert not trail(rec, 94.0) and rec["stop"] == 95.0    # non risale mai
    pos = {"SPY": {"qty": -10, "avg_entry": 100, "price": 95.5, "market_value": -955, "plpc": 0.045}}
    assert forced_stops(pos, {"SPY": rec})[0]["side"] == "cover"


def test_short_round_trip_with_broker_stop(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain({"symbol": "SPY", "side": "short", "qty": 20, "why": "sotto EMA50", "stop": 615}))
    ctx.use_signals = False
    # senza segnali il trend è sconosciuto: per lo short serve la conferma, quindi la forniamo nel broker finto
    import cadetrade.wakeup as w
    orig = w.signals_mod.compute
    w.signals_mod.compute = lambda b, syms: {"SPY": {"prezzo": 600, "sopra_ema50": False, "atr14": 6.0}}
    ctx.use_signals = True
    try:
        baton = run_wakeup(ctx, "1540", at("1540"))
    finally:
        w.signals_mod.compute = orig
    assert baton["orders"][0]["side"] == "short" and baton["orders"][0]["outcome"] == "inviato"
    assert ctx.broker.positions()["SPY"]["qty"] == -20
    stop = ctx.broker.open_stops()[0]
    assert stop["side"] == "buy" and stop["stop_price"] == 615.0
    assert load_risk(ctx)["stops"]["SPY"]["dir"] == -1
    ctx.broker.set_price("SPY", 620)                       # sale oltre lo stop: il broker ricopre
    assert "SPY" not in ctx.broker.positions()
    ctx.use_signals = False
    guard(ctx, at("1601"))
    trades = (ctx.state_dir / "journal.jsonl").read_text()
    assert '"short": true' in trades and '"pnl_usd": -400.0' in trades


def test_cover_closes_short_and_cancels_stop(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain())
    ctx.broker.set_price("QQQ", 520)
    ctx.broker.submit("QQQ", "sell", 10, "setup-QQQ")         # short aperto a mano
    ctx.broker.set_price("QQQ", 500)
    run_wakeup(ctx, "1300", at("1300"))                        # crea il record e lo stop sul broker
    assert ctx.broker.open_stops()[0]["side"] == "buy"
    ctx.brain = brain({"symbol": "QQQ", "side": "cover", "qty": 10, "why": "obiettivo raggiunto"})
    baton = run_wakeup(ctx, "1830", at("1830"))
    assert baton["orders"][0]["outcome"] == "inviato"
    assert "QQQ" not in ctx.broker.positions() and ctx.broker.open_stops() == []
    assert '"pnl_usd": 200.0' in (ctx.state_dir / "journal.jsonl").read_text()

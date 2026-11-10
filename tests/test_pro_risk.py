"""Gestione del rischio da professionista: stop sulla volatilità, quantità dal rischio, stop che salgono,
stop veri sul broker, tetto unico QQQ+SPY, entrate solo due volte al giorno, notizie nel contesto."""
from conftest import at, buy_position, make_ctx

from cadetrade.brain import FakeBrain
from cadetrade.cage import review
from cadetrade.stops import initial_stop, new_record, trail
from cadetrade.wakeup import guard, load_risk, run_wakeup

ACC = {"equity": 100_000.0, "cash": 100_000.0, "last_equity": 100_000.0}


def brain(*orders):
    return FakeBrain({"orders": list(orders), "reasoning": "r", "planNext": "p"})


def test_stop_follows_volatility_and_stays_within_bounds():
    assert initial_stop(100, 2.0) == 96.0                  # 2 ATR
    assert initial_stop(100, 2.0, proposed=99.5) == 98.0   # troppo vicino: almeno 1 ATR
    assert initial_stop(100, 2.0, proposed=80) == 93.0     # troppo lontano: al massimo 3,5 ATR
    assert initial_stop(100, 2.0, proposed=97) == 97.0     # il torto del cervello, se ragionevole
    assert initial_stop(100, None) == 97.0                 # senza ATR: riserva -3%
    assert initial_stop(100, None, proposed=96) == 96.0    # ...o il torto del cervello se tra -1% e -6%


def test_quantity_is_cut_to_the_risk_budget():
    v = review([{"symbol": "SPY", "side": "buy", "qty": 30, "why": "trend"}], slot_hhmm="1540", account=ACC,
               positions={}, prices={"SPY": 600}, stock_market_open=True, peak_equity=100_000, stopped_today=set(),
               atr={"SPY": 20.0})
    o = v.accepted[0]
    assert o["stop"] == 560.0 and o["qty"] == 18          # 750 $ di rischio / 40 $ a pezzo
    assert "ridotta da 30 a 18" in o["why"]


def test_qqq_and_spy_share_one_cap():
    pos = {"SPY": {"qty": 33, "avg_entry": 600, "price": 600, "market_value": 19_800, "plpc": 0.0}}
    v = review([{"symbol": "QQQ", "side": "buy", "qty": 25, "why": "x"}], slot_hhmm="1540", account=ACC,
               positions=pos, prices={"QQQ": 520, "SPY": 600}, stock_market_open=True, peak_equity=100_000,
               stopped_today=set())
    assert not v.accepted and "indici USA" in v.rejected[0]["reason"]


def test_buys_only_at_entry_wakeups_but_sells_anytime():
    pos = {"GLD": {"qty": 10, "avg_entry": 300, "price": 305, "market_value": 3050, "plpc": 0.016}}
    v = review([{"symbol": "GLD", "side": "sell", "qty": 10, "why": "tesi esaurita"},
                {"symbol": "SPY", "side": "buy", "qty": 1, "why": "x"}], slot_hhmm="1830", account=ACC,
               positions=pos, prices={"GLD": 305, "SPY": 600}, stock_market_open=True, peak_equity=100_000,
               stopped_today=set())
    assert [o["symbol"] for o in v.accepted] == ["GLD"]
    assert "si entra solo" in v.rejected[0]["reason"]


def test_stop_goes_to_breakeven_then_trails_and_never_drops():
    rec = new_record(100.0, 96.0, 2.0)
    assert not trail(rec, 103.0) and rec["stop"] == 96.0
    assert trail(rec, 104.0) and rec["stop"] == 100.0      # +1R: pareggio
    assert trail(rec, 110.0) and rec["stop"] == 105.0      # segue a 2,5 ATR dal massimo
    assert not trail(rec, 106.0) and rec["stop"] == 105.0  # non scende mai


def test_buy_places_a_real_stop_on_the_broker(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain({"symbol": "SPY", "side": "buy", "qty": 20, "why": "trend", "stop": 585}))
    run_wakeup(ctx, "1540", at("1540"))
    stops = ctx.broker.open_stops()
    assert [(o["symbol"], o["qty"], o["stop_price"]) for o in stops] == [("SPY", 20, 585.0)]
    assert load_risk(ctx)["stops"]["SPY"]["stop"] == 585.0


def test_broker_stop_fill_is_journaled_and_blocks_rebuy(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain({"symbol": "SPY", "side": "buy", "qty": 20, "why": "trend", "stop": 585}))
    run_wakeup(ctx, "1540", at("1540"))
    ctx.broker.set_price("SPY", 570)           # gap sotto lo stop: lo esegue il broker, non il bot
    assert "SPY" not in ctx.broker.positions()
    guard(ctx, at("1601"))
    assert any("Stop eseguito dal broker" in m for m in ctx.notifier.sent)
    trades = (ctx.state_dir / "journal.jsonl").read_text()
    assert '"symbol": "SPY"' in trades and '"stop": true' in trades
    baton = run_wakeup(ctx, "2140", at("2140"))
    assert "chiuso dallo stop oggi" in baton["rejected"][0]["reason"]


def test_selling_cancels_the_broker_stop_first(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain({"symbol": "SPY", "side": "buy", "qty": 20, "why": "trend", "stop": 585}))
    run_wakeup(ctx, "1540", at("1540"))
    ctx.brain = brain({"symbol": "SPY", "side": "sell", "qty": 20, "why": "tesi esaurita"})
    baton = run_wakeup(ctx, "1830", at("1830"))
    assert baton["orders"][0]["outcome"] == "inviato"
    assert ctx.broker.open_stops() == [] and "SPY" not in ctx.broker.positions()


def test_trailing_stop_moves_the_broker_order(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain({"symbol": "GLD", "side": "buy", "qty": 10, "why": "oro", "stop": 291}))
    run_wakeup(ctx, "1540", at("1540"))          # entrata 300, stop 291 (R = 9)
    ctx.broker.set_price("GLD", 312)               # oltre +1R: lo stop sale almeno a pareggio
    guard(ctx, at("1601"))
    stop = ctx.broker.open_stops()[0]["stop_price"]
    assert stop >= 300.0
    ctx.broker.set_price("GLD", 305)
    guard(ctx, at("1602"))
    assert ctx.broker.open_stops()[0]["stop_price"] == stop   # non scende


def test_manual_position_gets_a_fallback_stop(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain())
    buy_position(ctx, "QQQ", 5, 520, 521)
    run_wakeup(ctx, "1830", at("1830"))
    assert load_risk(ctx)["stops"]["QQQ"]["stop"] == 504.4
    assert ctx.broker.open_stops()[0]["symbol"] == "QQQ"


def test_news_reach_the_brain(tmp_path):
    ctx = make_ctx(tmp_path / "s", brain())
    ctx.broker.set(news=[{"ora": "2026-11-10T13:00:00Z", "titolo": "Fed: tassi fermi", "simboli": ["SPY"], "sintesi": ""}])
    run_wakeup(ctx, "1540", at("1540"))
    assert ctx.brain.contexts[0]["notizie"][0]["titolo"] == "Fed: tassi fermi"
    assert ctx.brain.contexts[0]["sveglia"]["si_puo_entrare"] is True


def test_big_tech_have_lower_cap_and_share_the_tech_group():
    v = review([{"symbol": "NVDA", "side": "buy", "qty": 70, "why": "x"}], slot_hhmm="1540", account=ACC, positions={},
               prices={"NVDA": 180}, stock_market_open=True, peak_equity=100_000, stopped_today=set(), atr={"NVDA": 2.0})
    assert "oltre il 12%" in v.rejected[0]["reason"]               # 12.600 $ = 12,6%
    pos = {s: {"qty": 1, "avg_entry": 10_000, "price": 10_000, "market_value": 10_000, "plpc": 0.0}
           for s in ("SPY", "MSFT", "AAPL", "AMZN")}
    acc = {"equity": 100_000.0, "cash": 60_000.0, "last_equity": 100_000.0}
    v = review([{"symbol": "NVDA", "side": "buy", "qty": 60, "why": "x"}], slot_hhmm="1540", account=acc, positions=pos,
               prices={"NVDA": 180, **{s: 10_000 for s in pos}}, stock_market_open=True, peak_equity=100_000,
               stopped_today=set(), atr={"NVDA": 2.0})
    assert "tecnologia USA" in v.rejected[0]["reason"]             # 40% + 10,8% > 50%


def test_trend_filter_blocks_buys_below_ema50():
    v = review([{"symbol": "QQQ", "side": "buy", "qty": 5, "why": "x"}], slot_hhmm="1540", account=ACC, positions={},
               prices={"QQQ": 520}, stock_market_open=True, peak_equity=100_000, stopped_today=set(), trend={"QQQ": False})
    assert "media a 50 giorni" in v.rejected[0]["reason"]


def test_total_open_risk_is_capped():
    # già aperte: 4 posizioni con 900 $ di rischio ciascuna = 3.600 $; spazio rimasto 400 $
    pos = {s: {"qty": 100, "avg_entry": 100, "price": 100, "market_value": 10_000, "plpc": 0.0} for s in ("GLD", "MSFT", "AAPL", "AMZN")}
    stops = {s: {"entry": 100, "stop": 91, "initial_stop": 91, "atr": 4.5, "high": 100} for s in pos}
    acc = {"equity": 100_000.0, "cash": 60_000.0, "last_equity": 100_000.0}
    v = review([{"symbol": "SPY", "side": "buy", "qty": 30, "why": "x"}], slot_hhmm="1540", account=acc, positions=pos,
               prices={"SPY": 600, **{s: 100 for s in pos}}, stock_market_open=True, peak_equity=100_000,
               stopped_today=set(), stops=stops, atr={"SPY": 6.0})
    assert v.accepted[0]["qty"] == 33 or v.accepted[0]["qty"] == 30
    assert (600 - v.accepted[0]["stop"]) * v.accepted[0]["qty"] <= 400 + 1e-6


def test_spike_day_pick_is_watched_then_buyable(tmp_path):
    from datetime import timedelta
    from conftest import DAY
    ctx = make_ctx(tmp_path / "s", brain({"symbol": "PLTR", "side": "buy", "qty": 5, "why": "fomo"}))
    ctx.broker.set(hot={"PLTR": [4.0, 0.12]})
    baton = run_wakeup(ctx, "1540", at("1540"))            # PLTR +12% oggi: giorno dell'esplosione, si osserva
    assert any(r["symbol"] == "PLTR" and "esplosione" in r["reason"] for r in baton["rejected"])
    ctx.broker.set(hot={}, screener=[])                     # il giorno dopo non è più caldo, ma resta in osservazione
    baton = run_wakeup(ctx, "1540", at("1540", DAY + timedelta(days=1)))
    assert [o["symbol"] for o in baton["orders"]] == ["PLTR"]


def test_alarm_after_three_brain_failures(tmp_path):
    ctx = make_ctx(tmp_path / "s", FakeBrain("niente json"))
    for hhmm in ("1300", "1540", "1830"):
        run_wakeup(ctx, hhmm, at(hhmm))
    assert any("da 3 sveglie" in m for m in ctx.notifier.sent)


def test_review_compares_with_buy_and_hold(tmp_path):
    from cadetrade.learning import Journal
    j = Journal(tmp_path)
    j.mark(at("1540"), 100_000, 600, {"SPY": 600, "GLD": 300})
    j.mark(at("1830"), 95_000, 600, {"SPY": 600, "GLD": 300})
    j.mark(at("2140"), 101_000, 606, {"SPY": 606, "GLD": 303})
    perf = j.performance(at("0900"))
    assert perf["compra_e_tieni_spy_%"] == 1.0 and perf["compra_e_tieni_nucleo_%"] == 1.0
    assert perf["peggior_calo_%"] == -5.0

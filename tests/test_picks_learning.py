"""Scelta giornaliera dei titoli e apprendimento dagli errori."""
from datetime import timedelta

from cadetrade.brain import BrainError, FakeBrain, validate_review
from cadetrade.cage import review
from cadetrade.learning import Journal, run_review, stats
from cadetrade.picker import PickStore, pick
from cadetrade.wakeup import run_wakeup
from conftest import DAY, at, buy_position, make_ctx

import pytest


# --- scelta del giorno ---
def test_picker_filters_and_ranks(ctx):
    picks = pick(ctx.broker)
    syms = [p["symbol"] for p in picks]
    assert syms == ["PLTR", "DOGE/USD"]            # i due con più "fomo"
    assert "TQQQ" not in syms                       # ETF a leva: vietato
    assert "PENNY" not in syms                      # sotto 5 $ e +30% in un giorno
    assert "TSLA" not in syms                       # volume nella norma
    assert "volume 4.0x" in picks[0]["why"]


def test_picks_are_fixed_for_the_day(ctx):
    run_wakeup(ctx, "0900", at("0900"))
    first = PickStore(ctx.state_dir).today(DAY)
    ctx.broker.set(screener=["NVDA"])
    run_wakeup(ctx, "1540", at("1540"))
    assert PickStore(ctx.state_dir).today(DAY) == first
    assert PickStore(ctx.state_dir).today(DAY + timedelta(days=1)) is None


def test_pick_can_be_bought_with_lower_cap(tmp_path):
    brain = FakeBrain({"orders": [{"symbol": "PLTR", "side": "buy", "qty": 60, "why": "fomo"},     # 9%
                                  {"symbol": "DOGE/USD", "side": "buy", "qty": 30000, "why": "fomo"},  # 12%
                                  {"symbol": "TSLA", "side": "buy", "qty": 5, "why": "non scelto"}],
                       "reasoning": "r", "planNext": "p"})
    ctx = make_ctx(tmp_path / "s", brain)
    baton = run_wakeup(ctx, "1540", at("1540"))
    assert [o["symbol"] for o in baton["orders"]] == ["PLTR"]
    reasons = {r["symbol"]: r["reason"] for r in baton["rejected"]}
    assert "oltre il 10%" in reasons["DOGE/USD"]
    assert reasons["TSLA"] == "fuori universo"
    assert {p["symbol"] for p in brain.contexts[0]["scelti_oggi"]} == {"PLTR", "DOGE/USD"}


def test_yesterdays_pick_can_be_sold_not_bought():
    acc = {"equity": 100_000, "cash": 90_000, "last_equity": 100_000}
    pos = {"PLTR": {"qty": 50, "avg_entry": 150, "price": 155, "market_value": 7750, "plpc": 0.033}}
    orders = [{"symbol": "PLTR", "side": "buy", "qty": 5, "why": ""}, {"symbol": "PLTR", "side": "sell", "qty": 50, "why": ""}]
    v = review(orders, slot_hhmm="1540", account=acc, positions=pos, prices={"PLTR": 155},
               stock_market_open=True, peak_equity=100_000, stopped_today=set(), picks=set())
    assert [o["side"] for o in v.accepted] == ["sell"]
    assert "si può solo vendere" in v.rejected[0]["reason"]


# --- diario ---
def test_journal_records_thesis_and_outcome(tmp_path):
    ctx = make_ctx(tmp_path / "s", FakeBrain({"orders": [{"symbol": "QQQ", "side": "buy", "qty": 10, "why": "trend; torto sotto 505"}],
                                               "reasoning": "r", "planNext": "p"}))
    run_wakeup(ctx, "1540", at("1540"))
    ctx.broker.set_price("QQQ", 500.0)          # -3.8%: lo stop vende
    ctx.brain = FakeBrain({"orders": [], "reasoning": "r", "planNext": "p"})
    run_wakeup(ctx, "1830", at("1830"))
    trades = Journal(ctx.state_dir).closed_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["thesis"] == "trend; torto sotto 505" and t["stop"] and t["pnl_%"] == -3.85
    assert stats(trades)["chiusi_dallo_stop"] == 1


def test_journal_survives_truncated_line(tmp_path):
    j = Journal(tmp_path)
    j.trades.write_text('{"ts": "2026-11-10", "symbol": "SPY", "pnl_usd": 1, "pnl_%": 1, "kind": "nucleo", "stop": false}\n{"ts": "2026')
    assert len(j.closed_trades()) == 1


# --- pagella e lezioni ---
def test_lessons_reach_the_next_decision(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    run_wakeup(ctx, "1540", at("1540"))
    run_review(ctx, at("1600"))
    run_wakeup(ctx, "1830", at("1830"))
    seen = ctx.brain.contexts[-1]
    assert "Lo stop si rispetta sempre." in seen["lezioni_imparate"]
    assert "ritracciamento" in seen["esperimento_in_corso"]["text"]
    assert any(m.startswith("📒 Pagella") for m in ctx.notifier.sent)


def test_review_sees_trades_rejections_and_previous_experiment(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    buy_position(ctx, "GLD", 10, 300, 288)
    run_wakeup(ctx, "1540", at("1540"))          # stop su GLD + rifiuti della gabbia (SPY ok, ecc.)
    run_review(ctx, at("1600"))
    run_review(ctx, at("1700"))
    ctx_seen = ctx.brain.contexts[-1]
    assert ctx_seen["statistiche"]["trade_chiusi"] == 1
    assert ctx_seen["esperimento_della_settimana"]["text"]
    assert ctx_seen["voti_precedenti"] == [6]


def test_sunday_briefing_runs_review(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    sunday = DAY + timedelta(days=(6 - DAY.weekday()))
    run_wakeup(ctx, "0900", sunday.replace(hour=9))
    assert Journal(ctx.state_dir).lessons()["lessons"]


def test_bad_review_keeps_old_lessons(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    run_review(ctx, at("1600"))
    before = Journal(ctx.state_dir).lessons()

    class Broken(FakeBrain):
        def review(self, c):
            raise BrainError("JSON troncato")
    ctx.brain = Broken()
    assert run_review(ctx, at("1700")) is None
    assert Journal(ctx.state_dir).lessons() == before


def test_review_validation():
    with pytest.raises(BrainError):
        validate_review({"grade": 11, "summary": "", "lessons": [], "experiment": ""})
    with pytest.raises(BrainError):
        validate_review({"grade": 5, "summary": "", "lessons": "una", "experiment": ""})
    ok = validate_review({"grade": 5, "summary": "s", "lessons": [f"l{i}" for i in range(12)], "experiment": "e"})
    assert len(ok["lessons"]) == 8

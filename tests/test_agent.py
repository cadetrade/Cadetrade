"""I sei test del contratto, più gabbia, guardiano e chiavi."""
import json

import pytest

from cadetrade.brain import FakeBrain
from cadetrade.cage import review
from cadetrade.clock import due_slot, missed_between
from cadetrade.secrets import LiveAccountRefused, load_secrets
from cadetrade.wakeup import guard, run_wakeup
from conftest import at, buy_position, hold_brain, make_ctx


def orders_by_symbol(ctx):
    out = {}
    for o in ctx.broker.orders_since():
        if not o["client_order_id"].startswith("setup-"):
            out.setdefault((o["symbol"], o["side"]), []).append(o)
    return out


# 1. Stessa sveglia lanciata due volte -> un solo ordine per titolo
def test_same_wakeup_twice_one_order_per_symbol(ctx):
    run_wakeup(ctx, "1540", at("1540"))
    second = run_wakeup(ctx, "1540", at("1541"))
    assert second["skipped"] == "sveglia già fatta"
    run_wakeup(ctx, "1540", at("1542"), force=True)   # anche forzandola
    assert all(len(v) == 1 for v in orders_by_symbol(ctx).values())
    assert len(orders_by_symbol(ctx)) == 4


# 3. Testimone delle 09:00 e poi niente fino alle 21:40 -> segnala le saltate
def test_missed_slots_are_reported(ctx):
    run_wakeup(ctx, "0900", at("0900"))
    baton = run_wakeup(ctx, "2140", at("2140"))
    assert baton["missed"] == ["2026-11-10-1300", "2026-11-10-1540", "2026-11-10-1830"]
    assert any("Sveglie saltate" in m for m in ctx.notifier.sent)


def test_missed_between_across_midnight():
    assert missed_between("2026-11-09-2140", "2026-11-10-0900") == ["2026-11-10-0100", "2026-11-10-0500"]
    assert missed_between(None, "2026-11-10-0900") == []


# 4. Fuori universo o 40% del conto su un titolo -> rifiutati
def test_cage_rejects_out_of_universe_and_oversize(tmp_path):
    brain = FakeBrain({"orders": [
        {"symbol": "TSLA", "side": "buy", "qty": 10, "why": "x"},
        {"symbol": "SPY", "side": "buy", "qty": 66, "why": "40% del conto"},   # 66*600 = 39.6%
        {"symbol": "QQQ", "side": "buy", "qty": 10, "why": "ok"},
    ], "reasoning": "r", "planNext": "p"})
    ctx = make_ctx(tmp_path / "s", brain)
    baton = run_wakeup(ctx, "1540", at("1540"))
    reasons = {r["symbol"]: r["reason"] for r in baton["rejected"]}
    assert reasons["TSLA"] == "fuori universo"
    assert "oltre il 20%" in reasons["SPY"]
    assert [o["symbol"] for o in baton["orders"]] == ["QQQ"]


# 5. Posizione a -4% -> la gabbia la vende anche se il modello dice di tenere
def test_stop_loss_sells_even_if_model_holds(tmp_path):
    ctx = make_ctx(tmp_path / "s", hold_brain())
    buy_position(ctx, "NVDA", 30, 180.0, 180.0 * 0.96)
    baton = run_wakeup(ctx, "1830", at("1830"))
    assert baton["orders"][0]["symbol"] == "NVDA" and baton["orders"][0]["forced"]
    assert "NVDA" not in ctx.broker.positions()


# 6. Testimone corrotto -> vale come nessun testimone, si va avanti col broker
def test_corrupt_baton_is_ignored(ctx):
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    (ctx.state_dir / "baton.json").write_text('{"run_id": "2026-11-10-09')
    baton = run_wakeup(ctx, "1540", at("1540"))
    assert baton["orders"]
    assert any("Testimone illeggibile" in m for m in ctx.notifier.sent)


def test_baton_missing_fields_is_ignored(ctx):
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    (ctx.state_dir / "baton.json").write_text(json.dumps({"run_id": "2026-11-10-1540"}))
    assert ctx.batons.read() is None


# --- gabbia: le regole in più rispetto al contratto ---
def test_no_rebuy_same_day_after_stop(tmp_path):
    ctx = make_ctx(tmp_path / "s", hold_brain())
    buy_position(ctx, "QQQ", 30, 520.0, 500.0)
    run_wakeup(ctx, "1540", at("1540"))
    ctx.brain = FakeBrain({"orders": [{"symbol": "QQQ", "side": "buy", "qty": 10, "why": "rimbalzo"}],
                           "reasoning": "r", "planNext": "p"})
    baton = run_wakeup(ctx, "2140", at("2140"))
    assert "chiuso dallo stop oggi" in baton["rejected"][0]["reason"]


def test_daily_loss_blocks_buys_but_not_sells(ctx):
    acc = {"equity": 96_000, "cash": 50_000, "last_equity": 100_000}
    pos = {"SPY": {"qty": 10, "avg_entry": 600, "price": 600, "market_value": 6000, "plpc": 0.0}}
    v = review([{"symbol": "QQQ", "side": "buy", "qty": 1, "why": ""}, {"symbol": "SPY", "side": "sell", "qty": 5, "why": ""}],
               slot_hhmm="1540", account=acc, positions=pos, prices={"QQQ": 520, "SPY": 600},
               stock_market_open=True, peak_equity=100_000, stopped_today=set())
    assert v.halted and [o["symbol"] for o in v.accepted] == ["SPY"]
    assert "perdita del giorno" in v.rejected[0]["reason"]


def test_drawdown_kill_switch(ctx):
    acc = {"equity": 84_000, "cash": 84_000, "last_equity": 84_000}
    v = review([{"symbol": "QQQ", "side": "buy", "qty": 1, "why": ""}], slot_hhmm="1540", account=acc, positions={},
               prices={"QQQ": 520}, stock_market_open=True, peak_equity=100_000, stopped_today=set())
    assert v.accepted == [] and "drawdown" in v.rejected[0]["reason"]


def test_crypto_cap_and_whole_shares_and_no_short(ctx):
    acc = {"equity": 100_000, "cash": 100_000, "last_equity": 100_000}
    pos = {"BTC/USD": {"qty": 0.25, "avg_entry": 100000, "price": 100000, "market_value": 25000, "plpc": 0.0}}
    v = review([{"symbol": "ETH/USD", "side": "buy", "qty": 2, "why": ""},      # 7k: crypto al 32%
                {"symbol": "SPY", "side": "buy", "qty": 0.5, "why": ""},
                {"symbol": "GLD", "side": "sell", "qty": 5, "why": ""}],
               slot_hhmm="1540", account=acc, positions=pos, prices={"ETH/USD": 3500, "SPY": 600, "GLD": 300},
               stock_market_open=True, peak_equity=100_000, stopped_today=set(), picks={"ETH/USD"})
    reasons = {r["symbol"]: r["reason"] for r in v.rejected}
    assert "crypto oltre" in reasons["ETH/USD"]
    assert reasons["SPY"] == "solo azioni intere"
    assert "niente short" in reasons["GLD"]


def test_briefing_sends_no_orders_but_crypto_stop_still_runs(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    buy_position(ctx, "BTC/USD", 0.1, 95000, 90000)    # -5.3%
    baton = run_wakeup(ctx, "0900", at("0900"))
    assert [o["symbol"] for o in baton["orders"]] == ["BTC/USD"]
    assert all("nessun ordine" in r["reason"] for r in baton["rejected"] if r["symbol"] != "BTC/USD")


def test_crypto_trades_when_stock_market_closed(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    ctx.broker.set(market_open=False)
    baton = run_wakeup(ctx, "2140", at("2140"))
    assert [o["symbol"] for o in baton["orders"]] == ["BTC/USD"]
    assert {r["symbol"] for r in baton["rejected"]} == {"SPY", "QQQ", "GLD"}


def test_night_wakeup_manages_risk_but_does_not_enter(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    baton = run_wakeup(ctx, "0100", at("0100"))
    assert baton["orders"] == []
    assert all("si entra solo" in r["reason"] for r in baton["rejected"] if r["symbol"] == "BTC/USD")


# --- guardiano: lo stop scatta entro un minuto, non alla sveglia dopo ---
def test_guard_sells_once_and_blocks_rebuy(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    buy_position(ctx, "BTC/USD", 0.1, 95000, 91800)   # -3.4%
    first = guard(ctx, at("0315"))
    assert first[0]["outcome"] == "inviato"
    assert guard(ctx, at("0316")) == []
    ctx.brain = FakeBrain({"orders": [{"symbol": "BTC/USD", "side": "buy", "qty": 0.01, "why": ""}], "reasoning": "r", "planNext": "p"})
    baton = run_wakeup(ctx, "2140", at("2140"))
    assert "chiuso dallo stop oggi" in baton["rejected"][0]["reason"]


def test_guard_skips_stocks_when_market_closed(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    buy_position(ctx, "NVDA", 10, 180, 170)
    ctx.broker.set(market_open=False)
    assert guard(ctx, at("0315")) == []
    assert "NVDA" in ctx.broker.positions()


# --- orari ---
def test_due_slot_window():
    assert due_slot(at("1545")) == "1540"
    assert due_slot(at("2205")) == "2140"
    assert due_slot(at("0730")) is None


# --- chiavi: solo paper ---
def test_refuses_live_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_KEY_ID", "AKLIVE123456")
    with pytest.raises(LiveAccountRefused):
        load_secrets(str(tmp_path / "none.toml"))
    monkeypatch.setenv("ALPACA_KEY_ID", "PKPAPER123456")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(LiveAccountRefused):
        load_secrets(str(tmp_path / "none.toml"))


def test_secrets_masked_in_logs(caplog):
    import logging
    from cadetrade.secrets import MaskSecrets
    lg = logging.getLogger("t")
    lg.addFilter(MaskSecrets({"ALPACA_SECRET_KEY": "supersecret123"}))
    with caplog.at_level(logging.INFO):
        lg.info("chiave %s", "supersecret123")
    assert "supersecret123" not in caplog.text and "***" in caplog.text


def test_notifier_finds_chat_id(monkeypatch):
    import io
    from cadetrade.notify import Notifier
    calls = []

    def fake_urlopen(req, timeout=10):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        body = b'{"ok":true,"result":[{"message":{"chat":{"id":424242}}}]}' if "getUpdates" in url else b'{"ok":true}'
        return io.BytesIO(body)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n = Notifier("123:abc", "")
    n.send("ciao")
    assert n.chat_id == "424242" and "sendMessage" in calls[-1]

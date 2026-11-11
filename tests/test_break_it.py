"""Rompilo: i sei guasti della seconda parte della guida, provati davvero."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from cadetrade.baton import BatonStore
from cadetrade.brain import FakeBrain
from cadetrade.broker import BrokerUnavailable
from cadetrade.lock import RunLock
from cadetrade.wakeup import run_wakeup
from conftest import ROOT, at, buy_position, make_ctx

RUNNER = textwrap.dedent("""
    import os, signal, sys, time
    sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
    from pathlib import Path
    from conftest import make_ctx, at
    from cadetrade.brain import FakeBrain
    from cadetrade.wakeup import run_wakeup
    mode = sys.argv[2]
    ctx = make_ctx(Path(sys.argv[1]))
    if mode == "kill-after-first":
        ctx.broker.after_submit = lambda o: os.kill(os.getpid(), signal.SIGKILL)
    if mode == "slow":
        class Slow(FakeBrain):
            def decide(self, c):
                time.sleep(1.5); return super().decide(c)
        ctx.brain = Slow()
    res = run_wakeup(ctx, "1540", at("1540"))
    print("SKIPPED" if res.get("skipped") else "RAN")
""")


def spawn(tmp_path, state, mode):
    script = tmp_path / "runner.py"
    script.write_text(RUNNER.format(root=str(ROOT), tests=str(ROOT / "tests")))
    return subprocess.Popen([sys.executable, str(script), str(state), mode], stdout=subprocess.PIPE, text=True)


def real_orders(ctx):
    return [o for o in ctx.broker.orders_since() if not o["client_order_id"].startswith("setup-")]


# 1. Morte a metà: kill -9 subito dopo il primo ordine, poi rilancio
def test_killed_after_first_order_then_relaunch(tmp_path):
    state = tmp_path / "state"
    p = spawn(tmp_path, state, "kill-after-first")
    p.wait(timeout=60)
    assert p.returncode == -9
    ctx = make_ctx(state)
    assert RunLock(state).peek() is not None          # il lucchetto è rimasto su disco
    assert len(real_orders(ctx)) == 1
    baton = run_wakeup(ctx, "1540", at("1545"))
    assert any("interrotta a metà" in m for m in ctx.notifier.sent)
    assert baton["reconciled"][0]["client_order_id"] == "2026-11-10-1540-SPY-buy"
    syms = [o["symbol"] for o in real_orders(ctx)]
    assert sorted(syms) == sorted(set(syms)) and len(syms) == 4   # nessun riacquisto
    assert RunLock(state).peek() is None


# 2. Doppio lancio: due sveglie della stessa ora a mezzo secondo di distanza
def test_double_launch(tmp_path):
    state = tmp_path / "state"
    a = spawn(tmp_path, state, "slow")
    time.sleep(0.5)
    b = spawn(tmp_path, state, "slow")
    outs = sorted([a.communicate(timeout=60)[0].strip(), b.communicate(timeout=60)[0].strip()])
    assert outs == ["RAN", "SKIPPED"]
    assert len(real_orders(make_ctx(state))) == 4


# 2b. Doppio lancio su un lucchetto di una sveglia morta: solo una subentra
def test_double_takeover_of_dead_lock(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (state / "run.lock").write_text(json.dumps({"run_id": "2026-11-10-1300", "pid": int(dead.stdout), "ts": time.time()}))
    a, b = spawn(tmp_path, state, "slow"), spawn(tmp_path, state, "slow")
    outs = sorted([a.communicate(timeout=60)[0].strip(), b.communicate(timeout=60)[0].strip()])
    assert outs == ["RAN", "SKIPPED"]
    assert len(real_orders(make_ctx(state))) == 4


# 3. Broker in timeout durante l'invio
def test_timeout_after_broker_accepted(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    calls = {"n": 0}

    def lose_response(order):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerUnavailable("timeout 30s")      # l'ordine è arrivato, la risposta no
    ctx.broker.after_submit = lose_response
    baton = run_wakeup(ctx, "1540", at("1540"))
    assert baton["orders"][0]["outcome"] == "inviato"   # non "rifiutato": la sveglia dopo sa di aver comprato


def test_timeout_before_broker_received(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    ctx.broker.before_submit = lambda *a: (_ for _ in ()).throw(BrokerUnavailable("timeout"))
    baton = run_wakeup(ctx, "1540", at("1540"))
    assert {o["outcome"] for o in baton["orders"]} == {"non partito"}
    assert real_orders(ctx) == []


def test_timeout_and_lookup_down_is_unknown_with_alarm(tmp_path):
    ctx = make_ctx(tmp_path / "s", FakeBrain({"orders": [{"symbol": "SPY", "side": "buy", "qty": 1, "why": ""}],
                                               "reasoning": "r", "planNext": "p"}))
    b = ctx.broker
    real_lookup = b.order_by_client_id
    state = {"sent": False}

    def submit_then_die(*a):
        state["sent"] = True
        raise BrokerUnavailable("timeout")
    b.before_submit = submit_then_die
    b.order_by_client_id = lambda cid: (_ for _ in ()).throw(BrokerUnavailable("down")) if state["sent"] else real_lookup(cid)
    baton = run_wakeup(ctx, "1540", at("1540"))
    assert baton["orders"][0]["outcome"] == "esito sconosciuto"
    assert any("esito incerto" in m for m in ctx.notifier.sent)


def test_broker_down_at_start_fails_clean(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    ctx.broker.account = lambda: (_ for _ in ()).throw(BrokerUnavailable("down"))
    baton = run_wakeup(ctx, "1540", at("1540"))
    assert "error" in baton and any(m.startswith("❌") for m in ctx.notifier.sent)
    assert RunLock(ctx.state_dir).peek() is None


# 4. Il modello risponde male: lo stop gira lo stesso, nessun ordine nuovo
def test_malformed_brain_answers(tmp_path):
    bad = ['{"orders": [{"symbol": "SPY", "side": "buy"',                                    # troncato
           '{"orders": [], "reasoning": "x"}',                                                # campo mancante
           '{"orders": [{"symbol":"SPY","side":"buy","qty":-5,"why":""}], "reasoning":"x","planNext":"y"}',  # negativa
           '{"orders": [{"symbol":"SPY","side":"vendi","qty":5,"why":""}], "reasoning":"x","planNext":"y"}',
           'non ho voglia di rispondere in JSON']
    for i, text in enumerate(bad):
        ctx = make_ctx(tmp_path / f"s{i}", FakeBrain(text))
        buy_position(ctx, "NVDA", 10, 180, 172)
        baton = run_wakeup(ctx, "1540", at("1540"))
        assert baton["brain_ok"] is False
        assert [o["symbol"] for o in baton["orders"]] == ["NVDA"]   # solo lo stop
        assert any("Cervello non disponibile" in m for m in ctx.notifier.sent)


# 5. Testimone scritto a metà: il processo muore mentre salva
def test_baton_half_written(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    run_wakeup(ctx, "0900", at("0900"))
    good = ctx.batons.read()
    # una scrittura interrotta lascia solo il file temporaneo: il testimone vecchio resta intero
    (ctx.state_dir / "baton.json.tmp999").write_text('{"run_id": "2026-11-10-15')
    assert ctx.batons.read() == good
    # e se qualcuno scrive il file direttamente a metà, vale come assente
    (ctx.state_dir / "baton.json").write_text('{"run_id": "2026-11-10-15')
    assert BatonStore(ctx.state_dir).read() is None
    assert run_wakeup(ctx, "1540", at("1540"))["orders"]


def test_baton_write_survives_kill(tmp_path):
    """kill -9 durante mille scritture del testimone: chi legge trova sempre un file valido."""
    state = tmp_path / "s"
    BatonStore(state).write({"run_id": "x-0", "slot": "0900", "ts": "t", "planNext": "p"})
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from cadetrade.baton import BatonStore
        s = BatonStore(Path({str(state)!r}))
        for i in range(100000):
            s.write({{"run_id": f"x-{{i}}", "slot": "0900", "ts": "t", "planNext": "p" * 5000}})
    """)
    p = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(1.0)
    p.kill(); p.wait()
    assert BatonStore(state).read() is not None


# 6. Orologio: la sveglia delle 21:40 parte alle 22:05, a mercato chiuso
def test_late_wakeup_market_closed(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    ctx.broker.set(market_open=False)
    baton = run_wakeup(ctx, "2140", at("2205"))
    assert {o["symbol"] for o in baton["orders"]} == {"BTC/USD"}     # le crypto sono aperte
    assert {r["symbol"] for r in baton["rejected"]} == {"SPY", "QQQ", "GLD"}


def test_market_closes_while_brain_thinks(tmp_path):
    ctx = make_ctx(tmp_path / "s")
    b = ctx.broker
    real_clock = b.clock
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        c = real_clock()
        return c if calls["n"] == 1 else {**c, "is_open": False}   # aperto all'inizio, chiuso all'invio
    b.clock = clock
    baton = run_wakeup(ctx, "2140", at("2159"))
    sent = {o["symbol"] for o in baton["orders"] if o["outcome"] == "inviato"}
    assert sent == {"BTC/USD"}
    assert all(o["reason"] == "mercato chiuso al momento dell'invio" for o in baton["orders"] if o["outcome"] == "rifiutato")


def _dead_lock(state):
    state.mkdir(parents=True, exist_ok=True)
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (state / "run.lock").write_text(json.dumps({"run_id": "2026-11-10-1300", "pid": int(dead.stdout), "ts": time.time()}))


def test_takeover_loses_if_someone_else_took_over_first(tmp_path, monkeypatch):
    """Interleaving deterministico: B vede il lucchetto morto, nel frattempo A subentra. B deve fermarsi."""
    import pytest
    from cadetrade.lock import Busy
    state = tmp_path / "s"
    _dead_lock(state)
    orig = RunLock.peek
    calls = {"n": 0}

    def peek(self):
        calls["n"] += 1
        seen = orig(self)
        if calls["n"] == 1:   # subito dopo che B ha letto, A prende il lucchetto
            (state / "run.lock").write_text(json.dumps({"run_id": "A", "pid": os.getpid(), "ts": time.time()}))
        return seen
    monkeypatch.setattr(RunLock, "peek", peek)
    with pytest.raises(Busy):
        RunLock(state).acquire("B")
    assert json.loads((state / "run.lock").read_text())["run_id"] == "A"


def test_takeover_in_progress_blocks_second(tmp_path):
    import pytest
    from cadetrade.lock import Busy
    state = tmp_path / "s"
    _dead_lock(state)
    (state / "run.lock.takeover").write_text("")
    with pytest.raises(Busy):
        RunLock(state).acquire("B")

"""Comandi:
  python -m cadetrade once [--slot 1540] [--force] [--fake] [--brain claude-code|api]
  python -m cadetrade schedule [--fake]     sempre acceso: sveglie + guardiano degli stop ogni minuto
  python -m cadetrade status [--fake]
  python -m cadetrade reconcile [--fake]
  python -m cadetrade review [--fake]       pagella adesso (di solito la domenica alle 09:00)
  python -m cadetrade backtest              rigioca le regole sugli ultimi 30/60/120 giorni
  python -m cadetrade lessons               cosa ha imparato finora
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .baton import BatonStore
from .brain import ApiBrain, ClaudeCodeBrain, FakeBrain
from .broker import AlpacaPaper, FakeBroker
from .clock import due_slot, latest_today, next_slot, now, run_id
from .config import CAGE
from .lock import RunLock
from .notify import Notifier
from .secrets import MaskSecrets, load_secrets
from .wakeup import Ctx, guard, reconcile, run_wakeup

STATE = Path("state")
LOGS = Path("logs")


def setup_logging(secrets: dict, logfile: Path | None = None):
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)-9s] %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    mask = MaskSecrets(secrets)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(mask)
        root.addHandler(h)


def build_ctx(args, secrets) -> Ctx:
    if args.fake:
        broker, brain = FakeBroker(STATE / "fake_broker.json"), FakeBrain()
        notifier = Notifier()   # in prova i messaggi restano in console
    else:
        broker = AlpacaPaper(secrets["ALPACA_KEY_ID"], secrets["ALPACA_SECRET_KEY"])
        brain = ApiBrain() if args.brain == "api" else ClaudeCodeBrain()
        notifier = Notifier(secrets["TELEGRAM_BOT_TOKEN"], secrets["TELEGRAM_CHAT_ID"])
    return Ctx(broker=broker, brain=brain, notifier=notifier, state_dir=STATE, web_news=not args.fake)


def main(argv=None):
    p = argparse.ArgumentParser(prog="cadetrade")
    p.add_argument("cmd", choices=["once", "schedule", "status", "reconcile", "review", "lessons", "backtest", "backtest-anno", "backtest-confronto", "backtest-3anni", "news"])
    p.add_argument("--slot", help="es. 1540")
    p.add_argument("--force", action="store_true")
    p.add_argument("--fake", action="store_true", help="broker e cervello finti, nessuna rete")
    p.add_argument("--brain", choices=["claude-code", "api"], default="claude-code")
    a = p.parse_args(argv)
    secrets = load_secrets()
    import os
    if secrets.get("CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", secrets["CLAUDE_CODE_OAUTH_TOKEN"])
    if secrets.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", secrets["ANTHROPIC_API_KEY"])

    if a.cmd == "once":
        t = now()
        hhmm = (a.slot or "").replace(":", "") or latest_today(t)
        if not hhmm:
            print("Nessuna sveglia ancora dovuta oggi: usa --slot 0900")
            return 1
        setup_logging(secrets, LOGS / f"{run_id(t, hhmm)}.log")
        ctx = build_ctx(a, secrets)
        res = run_wakeup(ctx, hhmm, t, force=a.force)
        if res.get("skipped"):
            print(f"Sveglia {res['run_id']} non eseguita: {res['skipped']} (usa --force per rifarla)")
        return 0

    if a.cmd == "schedule":
        setup_logging(secrets, LOGS / "schedule.log")
        ctx = build_ctx(a, secrets)
        log = logging.getLogger("schedule")
        from . import dashboard
        import os as _os
        dashboard.start(ctx, _os.environ.get("DASHBOARD_KEY", ""), int(_os.environ.get("DASHBOARD_PORT", "8080")))
        msg = f"Sempre acceso. Prossima sveglia {next_slot(now()):%Y-%m-%d %H:%M}. Guardiano stop ogni {CAGE.guard_interval_s}s."
        log.info(msg)
        brain_on = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
        ctx.notifier.send(f"🟢 CadeTrade acceso (conto demo). {msg}"
                          + ("" if brain_on else "\n⚠️ Cervello spento: manca il token di Claude, applico solo gli stop."))
        last_guard = 0.0
        while True:
            t = now()
            hhmm = due_slot(t)
            if hhmm:
                last = BatonStore(STATE).read()
                if not last or last["run_id"] < run_id(t, hhmm):
                    setup_logging(secrets, LOGS / f"{run_id(t, hhmm)}.log")
                    try:
                        run_wakeup(ctx, hhmm, t)
                    except Exception:
                        log.exception("sveglia %s fallita", hhmm)
                    setup_logging(secrets, LOGS / "schedule.log")
            if time.time() - last_guard >= CAGE.guard_interval_s:
                last_guard = time.time()
                try:
                    guard(ctx)
                except Exception:
                    log.exception("guardiano fallito")
            time.sleep(30 if not a.fake else 5)

    if a.cmd == "status":
        lock = RunLock(STATE).peek()
        baton = BatonStore(STATE).read()
        print("lucchetto:", "nessuno" if lock is None else json.dumps(lock))
        print("ultimo testimone:", "nessuno" if not baton else f"{baton['run_id']} · prossima mossa: {baton['planNext']}")
        print("prossima sveglia:", f"{next_slot(now()):%Y-%m-%d %H:%M}")
        try:
            ctx = build_ctx(a, secrets)
            acc = ctx.broker.account()
            print(f"conto: equity {acc['equity']:.2f} $ · cash {acc['cash']:.2f} $")
            for s, pos in ctx.broker.positions().items():
                print(f"  {s:8} {pos['qty']:g} @ {pos['avg_entry']:.2f} → {pos['price']:.2f} ({pos['plpc']*100:+.1f}%)")
        except BaseException as e:
            print("broker non raggiungibile:", e)
        return 0

    if a.cmd == "review":
        setup_logging(secrets, LOGS / f"{now():%Y-%m-%d}-review.log")
        from .learning import run_review
        res = run_review(build_ctx(a, secrets), now())
        return 0 if res else 1

    if a.cmd == "backtest":   # macchina del tempo: le regole sugli ultimi 30/60/120 giorni
        from . import backtest
        setup_logging(secrets)
        backtest.main(build_ctx(a, secrets).broker)
        return 0
    if a.cmd == "backtest-3anni":   # tre anni fino a oggi + il ribasso del 2022
        from . import backtest
        setup_logging(secrets)
        backtest.main_multi(build_ctx(a, secrets).broker)
        return 0
    if a.cmd == "backtest-confronto":   # le strategie alternative sullo stesso anno
        from . import backtest
        setup_logging(secrets)
        backtest.main_compare(build_ctx(a, secrets).broker)
        return 0
    if a.cmd == "backtest-anno":   # un anno intero con la pagella della domenica che corregge gli errori
        from . import backtest
        setup_logging(secrets)
        backtest.main_year(build_ctx(a, secrets).broker)
        return 0
    if a.cmd == "news":   # controllo delle fonti di notizie extra
        from . import extnews
        t = now()
        h, c = extnews.headlines(t), extnews.calendar(t)
        print(f"notizie macro: {len(h)}"); [print(" ", n["fonte"], "|", n["titolo"]) for n in h[:12]]
        print(f"calendario USA: {len(c)}"); [print(" ", e["quando"], e["impatto"], e["evento"]) for e in c]
        print("fonti:", sorted({n["fonte"] for n in h}))
        return 0
    if a.cmd == "lessons":
        from .learning import Journal, stats
        j = Journal(STATE)
        d = j.lessons()
        print("Lezioni:"); [print(" •", x) for x in d.get("lessons", [])] or print("  nessuna ancora")
        print("Esperimento:", (d.get("experiment") or {}).get("text", "nessuno"))
        print("Voti:", [h["grade"] for h in d.get("history", [])])
        print("Statistiche:", stats(j.closed_trades()))
        return 0

    if a.cmd == "reconcile":
        setup_logging(secrets)
        lock = RunLock(STATE).peek()
        if not lock:
            print("Nessuna sveglia interrotta.")
            return 0
        for o in reconcile(build_ctx(a, secrets), lock):
            print(o)
        return 0


if __name__ == "__main__":
    sys.exit(main())

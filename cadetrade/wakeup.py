"""Il giro di una sveglia, in quest'ordine: lucchetto, testimone, broker, cervello, gabbia, ordini, testimone, Telegram."""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import signals as signals_mod
from .baton import BatonStore, _atomic_write
from .brain import BrainError
from .broker import BrokerRejected, BrokerUnavailable, DuplicateOrder
from .cage import forced_stops, review
from .clock import missed_between, name_of, next_slot, now, run_id
from .config import CAGE, UNIVERSE, is_crypto
from .learning import Journal, run_review
from .picker import picks_for_today
from .lock import Busy, RunLock

log = logging.getLogger("sveglia")


@dataclass
class Ctx:
    broker: object
    brain: object
    notifier: object
    state_dir: Path
    use_signals: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def batons(self):
        return BatonStore(self.state_dir)

    @property
    def lock(self):
        return RunLock(self.state_dir)


def order_id(rid: str, symbol: str, side: str) -> str:
    """Stessa sveglia + stesso titolo + stesso lato = stesso id: il broker rifiuta il doppione da solo."""
    return f"{rid}-{symbol.replace('/', '')}-{side}"


# --- stato di rischio condiviso tra sveglie e guardiano ---
def _risk_path(ctx):
    return ctx.state_dir / "risk.json"


def load_risk(ctx) -> dict:
    import json
    try:
        r = json.loads(_risk_path(ctx).read_text())
        if isinstance(r, dict):
            return r
    except (OSError, ValueError):
        pass
    return {"peak_equity": 0.0, "stopped": {}}


def save_risk(ctx, r: dict) -> None:
    today = f"{now():%Y-%m-%d}"
    r["stopped"] = {d: s for d, s in r.get("stopped", {}).items() if d >= today}   # tiene solo oggi
    _atomic_write(_risk_path(ctx), r)


def stopped_today(risk: dict, day: datetime) -> set:
    return set(risk.get("stopped", {}).get(f"{day:%Y-%m-%d}", []))


# --- invio di un ordine, con tutti i casi di esito incerto ---
def send_order(ctx, rid: str, o: dict) -> dict:
    cid = order_id(rid, o["symbol"], o["side"])
    rec = {"id": cid, "symbol": o["symbol"], "side": o["side"], "qty": o["qty"], "why": o.get("why", ""),
           "forced": o.get("forced", False)}
    b = ctx.broker
    try:
        if b.order_by_client_id(cid):
            log.info("ordine %s già sul broker: non lo rimando", cid)
            return {**rec, "outcome": "già inviato"}
        if not is_crypto(o["symbol"]) and not b.clock()["is_open"]:
            # l'orologio si ricontrolla prima di ogni invio: un cervello lento può sforare la chiusura
            return {**rec, "outcome": "rifiutato", "reason": "mercato chiuso al momento dell'invio"}
    except BrokerUnavailable as e:
        return {**rec, "outcome": "rifiutato", "reason": f"broker non raggiungibile prima dell'invio: {e}"}
    try:
        res = b.submit(o["symbol"], o["side"], o["qty"], cid)
        log.info("inviato %s · %s · %s", cid, o["qty"], res.get("status"))
        return {**rec, "outcome": "inviato", "status": res.get("status")}
    except DuplicateOrder:
        return {**rec, "outcome": "già inviato"}
    except BrokerRejected as e:
        return {**rec, "outcome": "rifiutato", "reason": f"broker: {e}"}
    except BrokerUnavailable as e:
        log.warning("invio %s: errore di rete (%s), ricontrollo per id", cid, e)
        try:
            found = b.order_by_client_id(cid)
        except BrokerUnavailable:
            return {**rec, "outcome": "esito sconosciuto"}
        if found:
            return {**rec, "outcome": "inviato", "status": found.get("status")}
        return {**rec, "outcome": "non partito", "reason": "il broker non l'ha ricevuto"}


def reconcile(ctx, dead: dict) -> list[dict]:
    """Cosa hanno fatto davvero gli ordini della sveglia morta."""
    rid = dead.get("run_id") or ""
    if not rid:
        return []
    return [o for o in ctx.broker.orders_since(48) if (o.get("client_order_id") or "").startswith(rid + "-")]


def _fmt_money(x: float) -> str:
    return f"{x:,.2f} $".replace(",", "X").replace(".", ",").replace("X", ".")


def run_wakeup(ctx: Ctx, hhmm: str, at: datetime | None = None, force: bool = False) -> dict:
    t = at or now()
    rid = run_id(t, hhmm)
    store = ctx.batons
    last = store.read()
    if not force and last and last["run_id"] >= rid:
        return {"run_id": rid, "skipped": "sveglia già fatta"}
    lock = ctx.lock
    try:
        dead = lock.acquire(rid)
    except Busy as e:
        log.warning("%s: %s", rid, e)
        return {"run_id": rid, "skipped": str(e)}
    n = ctx.notifier
    try:
        n.send(f"▶️ Sveglia {t:%Y-%m-%d} {hhmm[:2]}:{hhmm[2:]} · {name_of(hhmm)}\n· run {rid}")
        alarms = []
        reconciled = []
        if dead is not None:
            try:
                reconciled = reconcile(ctx, dead)
            except BrokerUnavailable as e:
                reconciled = [{"errore": str(e)}]
            desc = ", ".join(f"{o.get('side')} {o.get('qty')} {o.get('symbol')} ({o.get('status')})" for o in reconciled if "symbol" in o) or "nessun ordine partito"
            alarms.append(f"La sveglia {dead.get('run_id')} si è interrotta a metà. Sul broker: {desc}.")
        if last is None and store.path.exists():
            alarms.append("Testimone illeggibile: riparto dalla verità del broker.")
        missed = missed_between(last["run_id"] if last else None, rid)
        if missed:
            alarms.append("Sveglie saltate (non le recupero): " + ", ".join(missed))
        for a in alarms:
            n.alarm(a, rid)

        b = ctx.broker
        try:
            account, positions, clock = b.account(), b.positions(), b.clock()
            recent = b.orders_since(48)
        except BrokerUnavailable as e:
            baton = {"run_id": rid, "slot": hhmm, "ts": t.isoformat(), "error": f"broker: {e}",
                     "planNext": (last or {}).get("planNext", ""), "reasoning": "Broker non raggiungibile: nessuna azione."}
            store.write(baton)
            n.send(f"❌ Sveglia {rid} fallita: broker non raggiungibile ({e}). Nessun ordine.\n· run {rid}")
            return baton

        try:
            picks = picks_for_today(b, ctx.state_dir, t)
        except Exception as e:
            log.warning("scelta del giorno non riuscita: %s", e)
            picks = []
        pick_syms = {p["symbol"] for p in picks}
        watch = list(dict.fromkeys(UNIVERSE + sorted(pick_syms) + sorted(positions)))
        sig = signals_mod.compute(b, watch) if ctx.use_signals else {}
        journal = Journal(ctx.state_dir)
        learned = journal.lessons()
        risk = load_risk(ctx)
        peak = max(float(risk.get("peak_equity") or 0.0), account["equity"])
        stopped = stopped_today(risk, t)
        context = {
            "sveglia": {"run_id": rid, "nome": name_of(hhmm), "ora_roma": t.strftime("%Y-%m-%d %H:%M"),
                        "borsa_usa_aperta": clock["is_open"]},
            "testimone_precedente": last,
            "conto": account, "posizioni": positions,
            "ordini_ultime_48h": recent[:30],
            "indicatori": sig,
            "scelti_oggi": picks,
            "lezioni_imparate": learned.get("lessons", []),
            "esperimento_in_corso": learned.get("experiment"),
            "chiusi_dallo_stop_oggi": sorted(stopped),
            "massimo_storico_conto": peak,
        }
        brain_ok, brain_note = True, ""
        try:
            decision = ctx.brain.decide(context)
        except BrainError as e:
            brain_ok, brain_note = False, str(e)
            decision = {"orders": [], "reasoning": "Cervello non disponibile: applico solo lo stop loss.",
                        "planNext": (last or {}).get("planNext", "")}
            n.alarm(f"Cervello non disponibile ({e}): applico solo lo stop loss.", rid)

        prices = {s: p["price"] for s, p in positions.items()}
        prices.update({s: v["prezzo"] for s, v in sig.items() if s not in prices})
        prices.update({p["symbol"]: p["price"] for p in picks if p["symbol"] not in prices})
        for o in decision["orders"]:
            if (o["symbol"] in UNIVERSE or o["symbol"] in pick_syms) and o["symbol"] not in prices:
                try:
                    prices[o["symbol"]] = b.latest_price(o["symbol"])
                except Exception:
                    pass
        verdict = review(decision["orders"], slot_hhmm=hhmm, account=account, positions=positions, prices=prices,
                         stock_market_open=clock["is_open"], peak_equity=peak, stopped_today=stopped, picks=pick_syms)

        results = [send_order(ctx, rid, o) for o in verdict.accepted]
        journal.record(results, positions, prices, t, pick_syms)
        for r in results:
            if r["forced"] and r["outcome"] in ("inviato", "già inviato"):
                stopped.add(r["symbol"])
        unknown = [r for r in results if r["outcome"] == "esito sconosciuto"]
        for r in unknown:
            n.alarm(f"Ordine {r['id']} dall'esito incerto: controlla su Alpaca.", rid)

        try:
            account_after = b.account()
        except BrokerUnavailable:
            account_after = account
        risk["peak_equity"] = max(peak, account_after["equity"])
        risk.setdefault("stopped", {})[f"{t:%Y-%m-%d}"] = sorted(stopped)
        save_risk(ctx, risk)
        spy = prices.get("SPY")
        if spy is None:
            try:
                spy = b.latest_price("SPY")
            except Exception:
                spy = None
        journal.mark(t, account_after["equity"], spy)

        day_pl = account_after["equity"] - account_after["last_equity"]
        baton = {
            "run_id": rid, "slot": hhmm, "slot_name": name_of(hhmm), "ts": t.isoformat(),
            "brain": getattr(ctx.brain, "name", "?"), "brain_ok": brain_ok, "brain_error": brain_note,
            "reasoning": decision["reasoning"], "planNext": decision["planNext"],
            "orders": results, "rejected": verdict.rejected, "halted": verdict.halted,
            "equity": account_after["equity"], "cash": account_after["cash"], "day_pl": day_pl,
            "missed": missed, "reconciled": reconciled, "picks": picks,
        }
        store.write(baton)

        def line(r):
            verb = "comprato" if r["side"] == "buy" else "venduto"
            if r["outcome"] == "inviato":
                tail = ""
            elif r["outcome"] == "già inviato":
                tail = " [già inviato prima, non ripetuto]"
            else:
                tail = f" [{r['outcome']}: {r.get('reason', '')}]"
            return f"{verb} {r['qty']:g} {r['symbol']} ({r['why']}){tail}"
        msg = [f"✅ Fatto · {name_of(hhmm)}",
               "Ordini: " + ("; ".join(line(r) for r in results) if results else "nessuno")]
        if verdict.rejected:
            msg.append("Rifiutati dalla gabbia: " + "; ".join(f"{r['symbol']} {r['side']} ({r['reason']})" for r in verdict.rejected))
        if verdict.halted:
            msg.append(f"Blocco rischio: {verdict.halted}")
        if picks:
            msg.append("Scelti oggi: " + "; ".join(f"{p['symbol']} ({p['why']})" for p in picks))
        msg += [f"Perché: {decision['reasoning']}", f"Prossima mossa: {decision['planNext']}",
                f"P&L giorno: {_fmt_money(day_pl)} · conto {_fmt_money(account_after['equity'])}",
                f"Prossima sveglia: {next_slot(t):%Y-%m-%d %H:%M}", f"· run {rid}"]
        n.send("\n".join(msg))
        if t.weekday() == 6 and hhmm == "0900":   # domenica mattina: pagella e nuove lezioni
            run_review(ctx, t)
        return baton
    finally:
        lock.release()


def guard(ctx: Ctx, at: datetime | None = None) -> list[dict]:
    """Il guardiano: ogni minuto, senza cervello, vende le posizioni sotto lo stop.
    Così lo stop a -3% scatta vicino a -3%, non alla sveglia successiva."""
    t = at or now()
    gid = f"{t:%Y-%m-%d}-guard"
    lock = ctx.lock
    if lock.peek() is not None:
        return []   # una sveglia è in corso o è morta: ci pensa lei (e la riconciliazione) agli stop
    try:
        lock.acquire(gid)
    except Busy:
        return []
    try:
        b = ctx.broker
        try:
            positions = b.positions()
            stops = forced_stops(positions, CAGE)
            if not stops:
                return []
            is_open = b.clock()["is_open"]
        except BrokerUnavailable:
            return []
        out = []
        risk = load_risk(ctx)
        stopped = stopped_today(risk, t)
        prices = {s: p["price"] for s, p in positions.items()}
        for o in stops:
            if not is_crypto(o["symbol"]) and not is_open:
                continue
            r = send_order(ctx, gid, o)
            out.append(r)
            Journal(ctx.state_dir).record([r], positions, prices, t)
            if r["outcome"] in ("inviato", "già inviato"):
                stopped.add(o["symbol"])
                ctx.notifier.send(f"🛑 Stop loss: venduto {o['qty']:g} {o['symbol']} ({o['why']})\n· run {gid}")
            elif r["outcome"] == "esito sconosciuto":
                ctx.notifier.alarm(f"Stop {o['symbol']}: esito incerto, controlla su Alpaca.", gid)
        risk.setdefault("stopped", {})[f"{t:%Y-%m-%d}"] = sorted(stopped)
        save_risk(ctx, risk)
        return out
    finally:
        lock.release()

"""La gabbia. Il modello propone, il codice dispone. Ogni rifiuto ha un motivo scritto."""
import math
from dataclasses import dataclass, field

from .config import CAGE, SLOT_BY_HHMM, UNIVERSE, Cage, is_crypto


@dataclass
class Verdict:
    accepted: list = field(default_factory=list)   # ordini da inviare: {symbol, side, qty, why, forced}
    rejected: list = field(default_factory=list)   # {symbol, side, qty, reason}
    halted: str | None = None


def forced_stops(positions: dict, cage: Cage = CAGE) -> list[dict]:
    """Posizioni sotto lo stop: si vendono tutte, il modello non viene consultato."""
    out = []
    for sym, p in positions.items():
        if p["qty"] > 0 and p["plpc"] <= cage.stop_loss:
            out.append({"symbol": sym, "side": "sell", "qty": p["qty"], "forced": True,
                        "why": f"stop loss: {p['plpc']*100:.1f}% dal carico"})
    return out


def review(proposed: list[dict], *, slot_hhmm: str, account: dict, positions: dict, prices: dict,
           stock_market_open: bool, peak_equity: float, stopped_today: set, picks: set = frozenset(),
           cage: Cage = CAGE) -> Verdict:
    """picks = i titoli scelti oggi dal codice: si possono comprare, con un peso massimo più basso.
    Le posizioni aperte in giorni precedenti si possono sempre vendere."""
    v = Verdict()
    buyable = set(UNIVERSE) | set(picks)
    slot = SLOT_BY_HHMM[slot_hhmm]
    equity, cash = account["equity"], account["cash"]
    day_pl = equity / account["last_equity"] - 1 if account.get("last_equity") else 0.0
    drawdown = equity / peak_equity - 1 if peak_equity else 0.0
    if drawdown <= cage.max_drawdown:
        v.halted = f"drawdown {drawdown*100:.1f}% dal massimo: solo vendite finché un umano non interviene"
    elif day_pl <= cage.daily_loss_halt:
        v.halted = f"perdita del giorno {day_pl*100:.1f}%: niente acquisti fino a domani"

    def market_ok(sym):
        if is_crypto(sym):
            return "crypto" in slot.markets
        return "stock" in slot.markets and stock_market_open

    def reject(o, reason):
        v.rejected.append({"symbol": o["symbol"], "side": o["side"], "qty": o.get("qty"), "reason": reason})

    held = {s: p["qty"] for s, p in positions.items() if p["qty"] > 0}
    value = {s: p["market_value"] for s, p in positions.items()}

    # 1) gli stop vengono prima di tutto e non contano nel limite di ordini
    stops = forced_stops(positions, cage)
    stop_syms = set()
    for o in stops:
        if is_crypto(o["symbol"]) or stock_market_open:   # lo stop vale anche nel briefing
            v.accepted.append(o)
            stop_syms.add(o["symbol"])
        else:
            reject(o, "stop da eseguire ma mercato chiuso: riprovo alla prossima sveglia")

    # 2) le proposte del modello: prima le vendite (liberano spazio), poi gli acquisti
    for o in sorted(proposed, key=lambda o: o["side"] != "sell"):
        sym, side, qty = o["symbol"], o["side"], o["qty"]
        if sym not in buyable and not (side == "sell" and sym in positions):
            reject(o, "non è più tra le scelte di oggi: si può solo vendere" if sym in positions else "fuori universo"); continue
        if sym in stop_syms:
            reject(o, "già venduto dallo stop"); continue
        if not slot.markets:
            reject(o, f"sveglia {slot.name}: nessun ordine"); continue
        if not market_ok(sym):
            reject(o, "mercato chiuso per questa sveglia"); continue
        if sum(1 for a in v.accepted if not a.get("forced")) >= cage.max_orders_per_run:
            reject(o, f"oltre {cage.max_orders_per_run} ordini per sveglia"); continue
        if not is_crypto(sym):
            qty = math.floor(qty)
            if qty < 1:
                reject(o, "solo azioni intere"); continue
        price = prices.get(sym)
        if not price:
            reject(o, "prezzo non disponibile"); continue
        if side == "sell":
            have = held.get(sym, 0.0)
            if have <= 0:
                reject(o, "niente short: posizione assente"); continue
            qty = min(qty, have)
            held[sym] = have - qty
            value[sym] = held[sym] * price
            cash += qty * price
            v.accepted.append({**o, "qty": qty, "forced": False})
            continue
        # acquisti
        if v.halted:
            reject(o, v.halted); continue
        if sym in stopped_today:
            reject(o, "chiuso dallo stop oggi: niente riacquisto fino a domani"); continue
        notional = qty * price
        if notional < cage.min_order_usd:
            reject(o, f"ordine sotto {cage.min_order_usd:.0f} $"); continue
        if sym not in held and len([s for s, q in held.items() if q > 0]) >= cage.max_positions:
            reject(o, f"già {cage.max_positions} posizioni"); continue
        if sym not in buyable:
            reject(o, "non è più tra le scelte di oggi: si può solo vendere"); continue
        cap = cage.max_weight if sym in UNIVERSE else cage.max_pick_weight
        new_weight = (value.get(sym, 0.0) + notional) / equity
        if new_weight > cap + 1e-9:
            reject(o, f"oltre il {int(cap*100)}% del conto per titolo: sarebbe il {new_weight*100:.1f}%"); continue
        if is_crypto(sym):
            crypto_w = (sum(val for s, val in value.items() if is_crypto(s)) + notional) / equity
            if crypto_w > cage.max_crypto_weight + 1e-9:
                reject(o, f"crypto oltre il {int(cage.max_crypto_weight*100)}% del conto: sarebbero il {crypto_w*100:.1f}%"); continue
        if notional > cash:
            reject(o, "liquidità insufficiente: niente leva"); continue
        cash -= notional
        held[sym] = held.get(sym, 0.0) + qty
        value[sym] = value.get(sym, 0.0) + notional
        v.accepted.append({**o, "qty": qty, "forced": False})
    return v

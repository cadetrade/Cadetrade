"""La gabbia. Il modello propone, il codice dispone. Ogni rifiuto ha un motivo scritto."""
import math
from dataclasses import dataclass, field

from .config import CAGE, SINGLE_STOCKS, SLOT_BY_HHMM, UNIVERSE, Cage, is_crypto
from .stops import forced_stops, initial_stop


@dataclass
class Verdict:
    accepted: list = field(default_factory=list)   # ordini da inviare: {symbol, side, qty, why, forced}
    rejected: list = field(default_factory=list)   # {symbol, side, qty, reason}
    halted: str | None = None


def review(proposed: list[dict], *, slot_hhmm: str, account: dict, positions: dict, prices: dict,
           stock_market_open: bool, peak_equity: float, stopped_today: set, picks: set = frozenset(),
           stops: dict | None = None, atr: dict | None = None, trend: dict | None = None,
           pick_blocked: dict | None = None, cage: Cage = CAGE) -> Verdict:
    """picks = i titoli scelti oggi dal codice: si possono comprare, con un peso massimo più basso.
    Le posizioni aperte in giorni precedenti si possono sempre vendere.
    stops = gli stop di ogni posizione (stops.py); atr = volatilità in $ per titolo, per lo stop e la quantità."""
    atr, trend, pick_blocked = atr or {}, trend or {}, pick_blocked or {}
    records = stops or {}
    # rischio già aperto: quanto si perderebbe se scattassero tutti gli stop adesso
    open_risk = sum(max(0.0, (p["price"] - records[s]["stop"]) * p["qty"])
                    for s, p in positions.items() if s in records and p["qty"] > 0)
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
    stops = forced_stops(positions, stops, cage)
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
        if not slot.entries:
            reject(o, f"sveglia {slot.name}: si entra solo all'apertura (15:40) e alla chiusura (21:40)"); continue
        if v.halted:
            reject(o, v.halted); continue
        if sym in pick_blocked:
            reject(o, pick_blocked[sym]); continue
        if cage.trend_filter and trend.get(sym) is False:
            reject(o, "sotto la media a 50 giorni: niente acquisti contro il trend"); continue
        if sym in stopped_today:
            reject(o, "chiuso dallo stop oggi: niente riacquisto fino a domani"); continue
        # la quantità la decide il rischio: se scatta lo stop si perde al massimo lo 0,75% del conto (0,4% sui scelti)
        stop_px = initial_stop(price, atr.get(sym), o.get("stop"), cage)
        risk_pct = (cage.stock_risk_per_trade if sym in SINGLE_STOCKS else
                    cage.risk_per_trade if sym in UNIVERSE else cage.pick_risk_per_trade)
        max_qty = equity * risk_pct / (price - stop_px)
        if not is_crypto(sym):
            max_qty = math.floor(max_qty)
        room = equity * cage.max_open_risk - open_risk   # tetto al rischio totale aperto
        if room / (price - stop_px) < max_qty:
            max_qty = room / (price - stop_px)
            if not is_crypto(sym):
                max_qty = math.floor(max_qty)
            if max_qty <= 0:
                reject(o, f"rischio totale aperto già al {cage.max_open_risk*100:g}% del conto"); continue
        sized = ""
        if qty > max_qty + 1e-9:
            if max_qty < (1 if not is_crypto(sym) else 1e-6):
                reject(o, "stop troppo lontano per il rischio ammesso"); continue
            sized = f" [quantità ridotta da {qty:g} a {max_qty:g} per il rischio]"
            qty = max_qty
        notional = qty * price
        if notional < cage.min_order_usd:
            reject(o, f"ordine sotto {cage.min_order_usd:.0f} $"); continue
        if sym not in held and len([s for s, q in held.items() if q > 0]) >= cage.max_positions:
            reject(o, f"già {cage.max_positions} posizioni"); continue
        if sym not in buyable:
            reject(o, "non è più tra le scelte di oggi: si può solo vendere"); continue
        cap = (cage.max_stock_weight if sym in SINGLE_STOCKS else
               cage.max_weight if sym in UNIVERSE else cage.max_pick_weight)
        new_weight = (value.get(sym, 0.0) + notional) / equity
        if new_weight > cap + 1e-9:
            reject(o, f"oltre il {int(cap*100)}% del conto per titolo: sarebbe il {new_weight*100:.1f}%"); continue
        if is_crypto(sym):
            crypto_w = (sum(val for s, val in value.items() if is_crypto(s)) + notional) / equity
            if crypto_w > cage.max_crypto_weight + 1e-9:
                reject(o, f"crypto oltre il {int(cage.max_crypto_weight*100)}% del conto: sarebbero il {crypto_w*100:.1f}%"); continue
        hit = False
        for gname, gsyms, gcap in cage.groups:
            if sym in gsyms:
                gw = (sum(value.get(s, 0.0) for s in gsyms) + notional) / equity
                if gw > gcap + 1e-9:
                    reject(o, f"{gname} oltre il {int(gcap*100)}% del conto: sarebbero il {gw*100:.1f}%"); hit = True
                    break
        if hit:
            continue
        if notional > cash:
            reject(o, "liquidità insufficiente: niente leva"); continue
        cash -= notional
        open_risk += (price - stop_px) * qty
        held[sym] = held.get(sym, 0.0) + qty
        value[sym] = value.get(sym, 0.0) + notional
        v.accepted.append({**o, "qty": qty, "forced": False, "stop": stop_px, "atr": atr.get(sym),
                           "why": o["why"] + sized})
    return v

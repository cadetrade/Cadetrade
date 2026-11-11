"""Gli stop di ogni posizione: dove stanno, come si muovono a favore, quando chiudere.
Uno stop per posizione, calcolato sulla volatilità del titolo (ATR) e non con una percentuale uguale per tutti.
Vale per gli acquisti (stop sotto il prezzo, d=+1) e per gli short (stop sopra il prezzo, d=-1).
Il record vive in risk.json: {"entry", "stop", "initial_stop", "atr", "high", "dir"}; per gli short "high" è il minimo toccato."""
from .config import CAGE, Cage


def initial_stop(price: float, atr: float | None, proposed: float | None = None, cage: Cage = CAGE, d: int = 1) -> float:
    """Il prezzo a cui si ha torto. Se il cervello ne propone uno lo si usa, ma tenuto tra 1 e 3,5 ATR dal prezzo."""
    ok_side = proposed and (proposed < price if d > 0 else proposed > price)
    if not atr or atr <= 0:   # senza volatilità nota: il torto del cervello tra 1% e 6%, altrimenti 3%
        if ok_side and 0.01 <= abs(proposed / price - 1) <= 0.06:
            return round(proposed, 6)
        return round(price * (1 + d * cage.stop_loss), 6)
    dist = cage.stop_atr * atr
    if ok_side:
        dist = abs(price - proposed)
    dist = min(max(dist, cage.min_stop_atr * atr), cage.max_stop_atr * atr)
    return round(max(price - d * dist, price * 0.01), 6)


def new_record(entry: float, stop: float, atr: float | None, d: int = 1) -> dict:
    return {"entry": entry, "stop": stop, "initial_stop": stop, "atr": atr, "high": entry, "dir": d}


def direction(p: dict) -> int:
    return 1 if p["qty"] > 0 else -1


def fallback_record(avg_entry: float, cage: Cage = CAGE, d: int = 1) -> dict:
    """Per le posizioni senza record (aperte a mano o prima di questa versione): stop di riserva in percentuale."""
    return new_record(avg_entry, round(avg_entry * (1 + d * cage.stop_loss), 6), None, d)


def trail(rec: dict, price: float, cage: Cage = CAGE) -> bool:
    """Aggiorna il prezzo migliore e, dopo +1R, porta lo stop a pareggio e poi lo fa seguire il prezzo.
    Lo stop si muove solo a favore. Ritorna True se lo stop si è mosso."""
    d = rec.get("dir", 1)
    best = rec.get("high") or rec["entry"]
    rec["high"] = max(best, price) if d > 0 else min(best, price)
    r = (rec["entry"] - rec["initial_stop"]) * d
    if r <= 0 or (rec["high"] - rec["entry"]) * d < cage.breakeven_r * r:
        return False
    step = cage.trail_atr * rec["atr"] if rec.get("atr") else 1.5 * r
    target = rec["high"] - d * step
    target = max(target, rec["entry"]) if d > 0 else min(target, rec["entry"])
    target = round(target, 6)
    if (target - rec["stop"]) * d > 1e-9:
        rec["stop"] = target
        return True
    return False


def sync_records(records: dict, positions: dict, cage: Cage = CAGE, now_ts=None) -> bool:
    """Allinea i record alle posizioni vere: toglie quelli chiusi, crea quelli mancanti, muove gli stop a favore.
    Un ordine appena inviato ("pending_since") può non essere ancora eseguito: il suo record resta fino a un giorno."""
    from datetime import datetime, timedelta
    changed = False
    for sym in list(records):
        p = positions.get(sym)
        gone = not p or p["qty"] == 0 or direction(p) != records[sym].get("dir", 1)
        if not gone:
            continue
        pend = records[sym].get("pending_since")
        if pend and now_ts is not None and datetime.fromisoformat(pend) > now_ts - timedelta(days=1) and not p:
            continue
        records.pop(sym)
        changed = True
    for sym, p in positions.items():
        if p["qty"] == 0:
            continue
        if records.get(sym, {}).pop("pending_since", None):
            changed = True
        if sym not in records:
            records[sym] = fallback_record(p["avg_entry"], cage, direction(p))
            changed = True
        if trail(records[sym], p["price"], cage):
            changed = True
    return changed


def open_risk(positions: dict, records: dict) -> float:
    """Quanto si perderebbe se scattassero tutti gli stop adesso (long e short)."""
    tot = 0.0
    for s, p in positions.items():
        rec = records.get(s)
        if rec and p["qty"]:
            tot += max(0.0, (p["price"] - rec["stop"]) * p["qty"])   # qty negativa per gli short: torna giusto
    return tot


def forced_stops(positions: dict, records: dict | None = None, cage: Cage = CAGE) -> list[dict]:
    """Posizioni oltre il loro stop: si chiudono tutte, il modello non viene consultato."""
    out = []
    records = records or {}
    for sym, p in positions.items():
        if p["qty"] == 0:
            continue
        d = direction(p)
        rec = records.get(sym) or fallback_record(p["avg_entry"], cage, d)
        if (p["price"] - rec["stop"]) * d <= 0:
            pl = (p["price"] / p["avg_entry"] - 1) * 100 * d if p["avg_entry"] else 0.0
            kind = "stop di protezione del guadagno" if (rec["stop"] - rec["entry"]) * d >= 0 else "stop loss"
            out.append({"symbol": sym, "side": "sell" if d > 0 else "cover", "qty": abs(p["qty"]), "forced": True,
                        "why": f"{kind} a {rec['stop']:.2f} ({pl:+.1f}% {'dal carico' if d > 0 else 'sullo short'})"})
    return out

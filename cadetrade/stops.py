"""Gli stop di ogni posizione: dove stanno, come salgono, quando vendere.
Uno stop per posizione, calcolato sulla volatilità del titolo (ATR) e non con una percentuale uguale per tutti.
Il record vive in risk.json: {"entry", "stop", "initial_stop", "atr", "high"}."""
from .config import CAGE, Cage


def initial_stop(price: float, atr: float | None, proposed: float | None = None, cage: Cage = CAGE) -> float:
    """Il prezzo a cui si ha torto. Se il cervello ne propone uno lo si usa, ma tenuto tra 1 e 3,5 ATR dal prezzo."""
    if not atr or atr <= 0:   # senza volatilità nota: il torto del cervello tra -1% e -6%, altrimenti -3%
        if proposed and price * 0.94 <= proposed <= price * 0.99:
            return round(proposed, 6)
        return round(price * (1 + cage.stop_loss), 6)
    dist = cage.stop_atr * atr
    if proposed and 0 < proposed < price:
        dist = price - proposed
    dist = min(max(dist, cage.min_stop_atr * atr), cage.max_stop_atr * atr)
    return round(max(price - dist, price * 0.01), 6)


def new_record(entry: float, stop: float, atr: float | None) -> dict:
    return {"entry": entry, "stop": stop, "initial_stop": stop, "atr": atr, "high": entry}


def fallback_record(avg_entry: float, cage: Cage = CAGE) -> dict:
    """Per le posizioni senza record (aperte a mano o prima di questa versione): stop di riserva in percentuale."""
    return new_record(avg_entry, round(avg_entry * (1 + cage.stop_loss), 6), None)


def trail(rec: dict, price: float, cage: Cage = CAGE) -> bool:
    """Aggiorna il massimo e, dopo +1R, porta lo stop a pareggio e poi lo fa seguire il prezzo. Lo stop non scende mai.
    Ritorna True se lo stop è salito."""
    rec["high"] = max(rec.get("high") or rec["entry"], price)
    r = rec["entry"] - rec["initial_stop"]
    if r <= 0 or rec["high"] < rec["entry"] + cage.breakeven_r * r:
        return False
    target = rec["entry"]
    if rec.get("atr"):
        target = max(target, rec["high"] - cage.trail_atr * rec["atr"])
    else:
        target = max(target, rec["high"] - 1.5 * r)
    target = round(target, 6)
    if target > rec["stop"] + 1e-9:
        rec["stop"] = target
        return True
    return False


def sync_records(records: dict, positions: dict, cage: Cage = CAGE, now_ts=None) -> bool:
    """Allinea i record alle posizioni vere: toglie quelli chiusi, crea quelli mancanti, aggiorna gli stop che salgono.
    Un acquisto appena inviato ("pending_since") può non essere ancora eseguito: il suo record resta fino a un giorno."""
    from datetime import datetime, timedelta
    changed = False
    for sym in [s for s in records if s not in positions or positions[s]["qty"] <= 0]:
        pend = records[sym].get("pending_since")
        if pend and now_ts is not None and datetime.fromisoformat(pend) > now_ts - timedelta(days=1):
            continue
        records.pop(sym)
        changed = True
    for sym, p in positions.items():
        if p["qty"] <= 0:
            continue
        if records.get(sym, {}).pop("pending_since", None):
            changed = True
        if sym not in records:
            records[sym] = fallback_record(p["avg_entry"], cage)
            changed = True
        if trail(records[sym], p["price"], cage):
            changed = True
    return changed


def forced_stops(positions: dict, records: dict | None = None, cage: Cage = CAGE) -> list[dict]:
    """Posizioni al di sotto del loro stop: si vendono tutte, il modello non viene consultato."""
    out = []
    records = records or {}
    for sym, p in positions.items():
        if p["qty"] <= 0:
            continue
        rec = records.get(sym) or fallback_record(p["avg_entry"], cage)
        if p["price"] <= rec["stop"]:
            pl = (p["price"] / p["avg_entry"] - 1) * 100 if p["avg_entry"] else 0.0
            kind = "stop di protezione del guadagno" if rec["stop"] >= rec["entry"] else "stop loss"
            out.append({"symbol": sym, "side": "sell", "qty": p["qty"], "forced": True,
                        "why": f"{kind} a {rec['stop']:.2f} ({pl:+.1f}% dal carico)"})
    return out

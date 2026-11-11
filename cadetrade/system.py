"""Il sistema scelto dal banco di prova (vedi research/optimize.py), dal vivo sul conto demo.

60% Nasdaq (QQQ) e 40% oro (GLD), sempre investiti.
Leva a obiettivo di volatilità: più esposizione quando il mercato è calmo, meno quando è nervoso (max 2x).
Nel profilo bilanciato compra in più sui cali brevi di S&P e Nasdaq in trend rialzista e rivende al rimbalzo.
Una volta al giorno, alle 21:40 di Roma (20 minuti prima della chiusura USA), riporta il conto ai pesi di stasera.
Si muove solo se la differenza supera il 3% del conto: niente operazioni inutili."""
import logging
import math
from datetime import datetime

from .baton import BatonStore
from .config import is_crypto
from .research import optimize
from .wakeup import _cancel_broker_stops, order_id, send_order

log = logging.getLogger("sistema")

BASE = {"QQQ": 0.6, "GLD": 0.4}
PROFILES = {
    "prudente": {"lever": ("volat", 0.12), "dip": 0.0},
    "bilanciato": {"lever": ("volat", 0.15), "dip": 0.3},
    "spinto": {"lever": ("volat", 0.20), "dip": 0.0},
}
SYMBOLS = ("SPY", "QQQ", "GLD")
BAND = 0.03          # si ribilancia un titolo solo se si è spostato oltre il 3% del conto
SLOT = "2140"


def target_weights(broker, profile: str) -> tuple[dict, dict]:
    p = PROFILES[profile]
    px = optimize.closes({s: broker.daily_bars(s, 500) for s in SYMBOLS})
    w = optimize.targets(px, BASE, p["lever"], "tiene", p["dip"], False).iloc[-1]
    info = {"giorno_dati": str(px.index[-1].date()), "leva": round(float(w.abs().sum()), 2)}
    return {s: round(float(x), 4) for s, x in w.items() if abs(x) > 1e-6}, info


def plan_orders(weights: dict, equity: float, positions: dict, prices: dict) -> list[dict]:
    """Ordini per portare il conto ai pesi. Vendite prima (liberano margine), poi acquisti. Azioni intere."""
    orders = []
    for s in sorted(set(weights) | set(positions)):
        if is_crypto(s):
            held = positions.get(s, {}).get("qty", 0.0)
            if held:
                orders.append({"symbol": s, "side": "sell", "qty": held, "why": "fuori dal sistema"})
            continue
        price = prices.get(s) or positions.get(s, {}).get("price")
        if not price:
            continue
        held = positions.get(s, {}).get("qty", 0.0)
        want = math.floor(weights.get(s, 0.0) * equity / price)
        diff = want - held
        if diff == 0:
            continue
        if want != 0 and abs(diff) * price < BAND * equity:
            continue
        if held > 0 and want < held:
            orders.append({"symbol": s, "side": "sell", "qty": min(held, held - want), "why": f"peso {weights.get(s, 0)*100:.0f}%"})
        elif held < 0:
            orders.append({"symbol": s, "side": "cover", "qty": -held, "why": "niente short nel sistema"})
            if want > 0:
                orders.append({"symbol": s, "side": "buy", "qty": want, "why": f"peso {weights[s]*100:.0f}%"})
        elif want > held:
            orders.append({"symbol": s, "side": "buy", "qty": want - held, "why": f"peso {weights[s]*100:.0f}%"})
    return sorted(orders, key=lambda o: o["side"] == "buy")


def run_system(ctx, profile: str, t: datetime) -> dict:
    rid = f"{t:%Y-%m-%d}-sistema"
    batons = BatonStore(ctx.state_dir)
    last = batons.read()
    if last and last.get("run_id") == rid:
        return {"skipped": "già fatto oggi"}
    b, n = ctx.broker, ctx.notifier
    if not b.clock()["is_open"]:
        return {"skipped": "borsa chiusa"}
    acc, positions = b.account(), b.positions()
    weights, info = target_weights(b, profile)
    prices = {s: p["price"] for s, p in positions.items()}
    for s in weights:
        if s not in prices:
            prices[s] = b.latest_price(s)
    orders = plan_orders(weights, acc["equity"], positions, prices)
    for s in positions:   # il sistema non usa stop sul broker: la protezione è la leva che scende quando il mercato si agita
        if not is_crypto(s):
            _cancel_broker_stops(ctx, s)
    results = [send_order(ctx, rid, o) for o in orders]
    lev = info["leva"]
    lines = [f"⚖️ Sistema {profile} · {t:%d/%m %H:%M}",
             f"Pesi di stasera: " + ", ".join(f"{s} {w*100:.0f}%" for s, w in sorted(weights.items())) + f" · esposizione {lev:.2f}x",
             f"Conto {acc['equity']:,.0f} $".replace(",", ".")]
    if results:
        lines += [f"{'🟢' if r['side'] in ('buy', 'cover') else '🔴'} {r['side']} {r['qty']:g} {r['symbol']} → {r['outcome']}"
                  + (f" ({r['reason']})" if r.get("reason") else "") for r in results]
    else:
        lines.append("Nessun ordine: il conto è già vicino ai pesi giusti.")
    n.send("\n".join(lines))
    for r in results:
        if r["outcome"] == "esito sconosciuto":
            n.alarm(f"Ordine {r['symbol']}: esito incerto, controlla su Alpaca.", rid)
    baton = {"run_id": rid, "slot": SLOT, "slot_name": "ribilancio", "ts": t.isoformat(),
             "reasoning": f"Sistema {profile}: 60% Nasdaq e 40% oro con leva a obiettivo di volatilità"
                          + (" e acquisti sui cali brevi." if PROFILES[profile]["dip"] else ".")
                          + f" Stasera esposizione {lev:.2f}x: " + ", ".join(f"{s} {w*100:.0f}%" for s, w in sorted(weights.items())) + ".",
             "planNext": "Domani alle 21:40 ricalcolo i pesi e ribilancio se servono più del 3% del conto.",
             "orders": results, "rejected": [], "halted": None, "picks": [], "brain_ok": True,
             "day_pl": acc["equity"] - acc.get("last_equity", acc["equity"]), "news_n": 0, "signals_n": len(SYMBOLS),
             "weights": weights, **info}
    batons.write(baton)
    try:
        from .learning import Journal
        Journal(ctx.state_dir).mark(t, acc["equity"], prices.get("SPY"), None)
    except Exception as e:   # la curva del conto è un di più: non ferma il ribilancio
        log.info("curva del conto non aggiornata: %s", e)
    return baton


__all__ = ["run_system", "plan_orders", "target_weights", "PROFILES", "order_id"]

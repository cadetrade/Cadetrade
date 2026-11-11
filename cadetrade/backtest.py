"""Macchina del tempo: rigioca le regole del bot sugli ultimi N giorni con le candele giornaliere vere.

Il cervello (Claude) non si può rigiocare nel passato senza barare (oggi conosce già come è andata),
quindi al suo posto c'è un trader a regole fisse che segue lo stesso prompt in versione meccanica:
  compra  se prezzo > EMA50, EMA20 > EMA50, RSI tra 50 e 70, ultimi 20 giorni in salita
  short   (solo titoli shortabili) se prezzo < EMA50, EMA20 < EMA50, RSI tra 30 e 50, 20 giorni in discesa
  esce    quando il trend si gira (chiusura dall'altra parte della EMA50) o scatta lo stop.
Tutto il resto è la gabbia vera: stop ad ATR, pareggio a +1R, inseguimento a 2,5 ATR, rischio per trade,
pesi massimi, gruppi, rischio aperto 4%, max 8 posizioni, short coperti dalla liquidità.
Si decide sulla chiusura del giorno (come la sveglia delle 21:40); gli stop scattano sui minimi/massimi del giorno dopo.
I titoli scelti per fomo non ci sono: lo screener del passato non si può ricostruire."""
from dataclasses import dataclass, field

import pandas as pd

from .config import CAGE, CORE, SHORTABLE, SINGLE_STOCKS, is_crypto
from .research.indicators import atr, ema
from .signals import rsi
from .stops import initial_stop, new_record, trail

SLIP_STOCK, SLIP_CRYPTO = 0.0005, 0.0025   # costo per lato: spread/slippage azioni, commissione+spread crypto


@dataclass
class Sim:
    cash: float = 100_000.0
    pos: dict = field(default_factory=dict)      # sym -> {"qty", "entry", "rec", "t"}
    trades: list = field(default_factory=list)
    curve: list = field(default_factory=list)
    cooldown: dict = field(default_factory=dict)


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    df = df[~df.index.duplicated(keep="last")]
    c = df["close"]
    df["e20"], df["e50"], df["rsi"], df["atr"] = ema(c, 20), ema(c, 50), rsi(c), atr(df, 14)
    df["r20"] = c / c.shift(20) - 1
    return df


def _risk(sym):
    return CAGE.pick_risk_per_trade if sym not in CORE else (
        CAGE.stock_risk_per_trade if sym in SINGLE_STOCKS else CAGE.risk_per_trade)


def _wcap(sym):
    return CAGE.max_stock_weight if sym in SINGLE_STOCKS else CAGE.max_weight


def run(data: dict, start: pd.Timestamp, end: pd.Timestamp | None = None, allow_short: bool = True,
        cash: float = 100_000.0) -> dict:
    data = {s: _prep(df) for s, df in data.items() if len(df) > 60}
    days = sorted({d for df in data.values() for d in df.index if d >= start and (end is None or d <= end)})
    sim = Sim(cash=cash)
    last = {}

    def price(s, d):
        df = data[s]
        row = df.loc[:d].iloc[-1] if len(df.loc[:d]) else None
        return None if row is None else float(row["close"])

    def equity(d):
        return sim.cash + sum(p["qty"] * (price(s, d) or p["entry"]) for s, p in sim.pos.items())

    def close(s, px, d, why):
        p = sim.pos.pop(s)
        slip = SLIP_CRYPTO if is_crypto(s) else SLIP_STOCK
        fill = px * (1 - slip) if p["qty"] > 0 else px * (1 + slip)
        sim.cash += p["qty"] * fill
        pnl = (fill - p["entry"]) * p["qty"]
        sim.trades.append({"symbol": s, "lato": "long" if p["qty"] > 0 else "short", "dentro": str(p["t"].date()),
                           "fuori": str(d.date()), "entrata": round(p["entry"], 2), "uscita": round(fill, 2),
                           "pnl_usd": round(pnl, 2), "pnl_%": round((fill / p["entry"] - 1) * 100 * (1 if p["qty"] > 0 else -1), 2),
                           "perché": why})
        sim.cooldown[s] = d

    for d in days:
        # 1) durante il giorno: il guardiano fa scattare gli stop sui minimi/massimi
        for s in list(sim.pos):
            df = data[s]
            if d not in df.index:
                continue
            row, p = df.loc[d], sim.pos[s]
            rec, long_ = p["rec"], p["qty"] > 0
            hit = row["low"] <= rec["stop"] if long_ else row["high"] >= rec["stop"]
            if hit:
                gap = row["open"] < rec["stop"] if long_ else row["open"] > rec["stop"]
                kind = "stop protezione guadagno" if (rec["stop"] - rec["entry"]) * rec["dir"] >= 0 else "stop loss"
                close(s, float(row["open"]) if gap else rec["stop"], d, kind)
                continue
            trail(rec, float(row["high"] if long_ else row["low"]))
        # 2) alla chiusura: decide il trader a regole, la gabbia filtra
        eq = equity(d)
        for s in list(sim.pos):
            df = data[s]
            if d not in df.index:
                continue
            row, long_ = df.loc[d], sim.pos[s]["qty"] > 0
            if (long_ and row["close"] < row["e50"]) or (not long_ and row["close"] > row["e50"]):
                close(s, float(row["close"]), d, "trend girato")
        cands = []
        for s, df in data.items():
            if s in sim.pos or d not in df.index or (s in sim.cooldown and (d - sim.cooldown[s]).days < 3):
                continue
            r = df.loc[d]
            if pd.isna(r["e50"]) or pd.isna(r["r20"]):
                continue
            if r["close"] > r["e50"] and r["e20"] > r["e50"] and 50 <= r["rsi"] <= 70 and r["r20"] > 0:
                cands.append((r["r20"], s, 1))
            elif allow_short and s in SHORTABLE and r["close"] < r["e50"] and r["e20"] < r["e50"] \
                    and 30 <= r["rsi"] <= 50 and r["r20"] < 0:
                cands.append((-r["r20"], s, -1))
        cands.sort(reverse=True)
        for _, s, dr in cands[:CAGE.max_orders_per_run]:
            if len(sim.pos) >= CAGE.max_positions:
                break
            r = data[s].loc[d]
            px, a = float(r["close"]), float(r["atr"])
            stop = initial_stop(px, a, None, CAGE, dr)
            dist = abs(px - stop)
            qty = _risk(s) * eq / dist
            cap = _wcap(s) * eq
            if is_crypto(s):
                cap = min(cap, CAGE.max_crypto_weight * eq - sum(abs(p["qty"]) * price(x, d) for x, p in sim.pos.items() if is_crypto(x)))
            for _, members, lim in CAGE.groups:
                if s in members:
                    used = sum(abs(p["qty"]) * price(x, d) for x, p in sim.pos.items() if x in members)
                    cap = min(cap, lim * eq - used)
            if dr < 0:
                used = sum(abs(p["qty"]) * price(x, d) for x, p in sim.pos.items() if p["qty"] < 0)
                cap = min(cap, CAGE.max_short_weight * eq - used)
            cap = min(cap, sim.cash)   # niente leva: né per comprare né per coprire lo short
            open_r = sum(abs(p["qty"]) * abs((price(x, d) or 0) - p["rec"]["stop"]) for x, p in sim.pos.items()
                         if (price(x, d) - p["rec"]["stop"]) * p["rec"]["dir"] > 0)
            qty = min(qty, max(0.0, CAGE.max_open_risk * eq - open_r) / dist)
            qty = min(qty, cap / px) if cap > 0 else 0
            if not is_crypto(s):
                qty = int(qty)
            if qty * px < CAGE.min_order_usd:
                continue
            slip = SLIP_CRYPTO if is_crypto(s) else SLIP_STOCK
            fill = px * (1 + slip) if dr > 0 else px * (1 - slip)
            sim.cash -= dr * qty * fill
            sim.pos[s] = {"qty": dr * qty, "entry": fill, "rec": new_record(fill, stop, a, dr), "t": d}
        last = d
        sim.curve.append((d, equity(d)))
    for s in list(sim.pos):   # a fine prova si chiude tutto al prezzo dell'ultimo giorno
        close(s, price(s, last), last, "fine prova (ancora aperta)")
    eq0, eq1 = cash, sim.cash
    peak, dd = eq0, 0.0
    for _, v in sim.curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    bh = {}
    for s, df in data.items():
        before = df.loc[:start - pd.Timedelta(days=1)]["close"]
        if len(before) and last in df.index:
            bh[s] = round((float(df.loc[last, "close"]) / float(before.iloc[-1]) - 1) * 100, 2)
    won = [t for t in sim.trades if t["pnl_usd"] > 0]
    return {"dal": str(days[0].date()), "al": str(last.date()), "conto_finale": round(eq1, 2),
            "profitto_usd": round(eq1 - eq0, 2), "profitto_%": round((eq1 / eq0 - 1) * 100, 2),
            "peggior_calo_%": round(dd * 100, 2), "trade": len(sim.trades),
            "vinti": len(won), "vinti_%": round(len(won) / len(sim.trades) * 100, 1) if sim.trades else 0.0,
            "compra_e_tieni_%": bh, "compra_e_tieni_nucleo_%": round(sum(bh.values()) / len(bh), 2) if bh else 0.0,
            "trades": sim.trades}


def report(res: dict, title: str) -> str:
    lines = [f"=== {title}: {res['dal']} → {res['al']} ===",
             f"conto 100.000 → {res['conto_finale']:,.0f}  ({res['profitto_%']:+.2f}%, {res['profitto_usd']:+,.0f} $)",
             f"peggior calo {res['peggior_calo_%']}%  ·  trade {res['trade']}  ·  vinti {res['vinti']} ({res['vinti_%']}%)",
             f"compra e tieni: nucleo a pesi uguali {res['compra_e_tieni_nucleo_%']:+.2f}%  ·  " +
             "  ".join(f"{k} {v:+.1f}%" for k, v in res["compra_e_tieni_%"].items())]
    for t in res["trades"]:
        lines.append(f"  {t['dentro']} → {t['fuori']}  {t['lato']:5} {t['symbol']:8} {t['entrata']:>10} → {t['uscita']:>10}"
                     f"  {t['pnl_%']:+6.2f}%  {t['pnl_usd']:+9.2f} $  {t['perché']}")
    return "\n".join(lines)


def main(broker, days_list=(30, 60, 120)):
    data = {}
    for s in CORE:
        try:
            data[s] = broker.daily_bars(s, 500)
        except Exception as e:
            print(f"{s}: dati non disponibili ({e})")
    today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    for n in days_list:
        start = today - pd.Timedelta(days=n)
        print(report(run(data, start), f"ULTIMI {n} GIORNI con short"))
        r = run(data, start, allow_short=False)
        print(f"--- stessi {n} giorni SOLO long: {r['profitto_%']:+.2f}% ({r['profitto_usd']:+,.0f} $), "
              f"calo max {r['peggior_calo_%']}%, trade {r['trade']}, vinti {r['vinti_%']}%\n", flush=True)

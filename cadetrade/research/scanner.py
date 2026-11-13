"""Intraday con scelta giornaliera: ogni mattina alle 9:35 di New York si guardano ~150 azioni liquide e si tengono
solo le 10 "in gioco", cioè con il volume della prima barra da 5 minuti più alto rispetto alla loro media degli ultimi 14 giorni.
Si opera solo su quelle, si chiude tutto alle 15:55. Il capitale del giorno è diviso in parti uguali tra i titoli scelti.

Strategie sui titoli scelti (regole fissate prima di guardare i risultati):
- segui 5m: direzione della prima barra da 5 minuti (su = long, giù = short), stop all'estremo opposto della barra
- contro 5m: l'opposto, per vedere se invece il mercato rimbalza
- rottura 30m: rottura del range dei primi 30 minuti, stop al lato opposto
- gap e via: gap oltre il 2% confermato dalla prima barra nella stessa direzione, stop all'estremo della prima barra
Costi 0,05% a lato. Attenzione: la lista di titoli è quella di oggi, quindi chi è fallito nell'anno non c'è (il test è un po' ottimista)."""
import urllib.parse
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

UNIVERSE = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC QCOM MU TXN AMAT LRCX KLAC MRVL ARM SMCI "
    "PLTR SNOW CRWD PANW ZS NET DDOG MDB SHOP UBER ABNB DASH COIN HOOD MSTR SQ PYPL AFRM SOFI UPST RBLX U "
    "ROKU SPOT PINS SNAP TTD APP RDDT DUOL CELH ENPH FSLR RIVN LCID NIO XPEV LI F GM BA CAT DE GE "
    "LMT RTX JPM BAC C WFC GS MS SCHW V MA AXP KO PEP MCD SBUX NKE LULU TGT WMT COST HD LOW DIS "
    "CMCSA T VZ PFE MRNA LLY NVO UNH CVS ABBV JNJ MRK BMY GILD AMGN REGN VRTX ISRG XOM CVX OXY SLB "
    "HAL DVN FCX NEM AA CLF X MARA RIOT CLSK IONQ RGTI SOUN AI PATH GME AMC CCL RCL DAL UAL AAL "
    "LUV CVNA W ETSY EBAY BABA JD PDD BIDU TSM ASML ON MCHP WDC STX DELL HPQ IBM CSCO ANET VRT"
).split()
COST = 0.0005
PICKS = 10
SINCE_RV = 14


def fetch_many(broker, symbols, days=370, tf="5Min", batch=10) -> dict:
    from ..broker import DATA_URL
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for k in range(0, len(symbols), batch):
        group = symbols[k:k + batch]
        acc, token = {s: [] for s in group}, None
        while True:
            q = {"symbols": ",".join(group), "timeframe": tf, "start": start, "end": end, "limit": 10000,
                 "feed": "sip", "adjustment": "all"}
            if token:
                q["page_token"] = token
            try:
                r = broker._req("GET", f"{DATA_URL}/v2/stocks/bars?{urllib.parse.urlencode(q)}") or {}
            except Exception as e:
                print(f"{group}: errore {e}", flush=True)
                break
            for s, rows in (r.get("bars") or {}).items():
                if rows:
                    acc.setdefault(s, []).append(_frame(rows))
            token = r.get("next_page_token")
            if not token:
                break
        for s, parts in acc.items():
            if parts:
                out[s] = pd.concat(parts)
        print(f"scaricati {min(k + batch, len(symbols))}/{len(symbols)} titoli", flush=True)
    return out


def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("ts")[["o", "h", "l", "c", "v"]].astype("float32")
    df.columns = ["open", "high", "low", "close", "volume"]
    t = df.index.hour * 60 + df.index.minute
    return df[(t >= 570) & (t < 960)]


def _exit(side, entry, stop, day) -> float:
    """Dalla seconda barra in poi: stop toccato o chiusura alle 15:55."""
    m = day.index.hour * 60 + day.index.minute
    for j in range(1, len(day)):
        h, l, o = float(day["high"].iloc[j]), float(day["low"].iloc[j]), float(day["open"].iloc[j])
        if j > 1 and ((side == 1 and l <= stop) or (side == -1 and h >= stop)):
            px = min(o, stop) if side == 1 else max(o, stop)
            return side * (px / entry - 1) - 2 * COST
        if m[j] >= 955:
            return side * (float(day["close"].iloc[j]) / entry - 1) - 2 * COST
    return side * (float(day["close"].iloc[-1]) / entry - 1) - 2 * COST


def t_segui(day, prev_close, flip=False):
    b0 = day.iloc[0]
    side = 1 if b0["close"] > b0["open"] else (-1 if b0["close"] < b0["open"] else 0)
    if flip:
        side = -side
    if not side or len(day) < 3:
        return None
    entry = float(day["open"].iloc[1])
    stop = float(b0["low"] if side == 1 else b0["high"])
    if flip:   # contro: stop a una distanza pari all'altezza della prima barra
        rng = float(b0["high"] - b0["low"]) or entry * 0.005
        stop = entry - rng if side == 1 else entry + rng
    return _exit(side, entry, stop, day)


def t_contro(day, prev_close):
    return t_segui(day, prev_close, flip=True)


def t_rottura30(day, prev_close):
    m = day.index.hour * 60 + day.index.minute
    first, rest = day[m < 600], day[m >= 600]
    if len(first) < 5 or len(rest) < 5:
        return None
    hi, lo = float(first["high"].max()), float(first["low"].min())
    c = rest["close"].to_numpy()
    for i in range(len(rest) - 1):
        side = 1 if c[i] > hi else (-1 if c[i] < lo else 0)
        if side:
            seg = rest.iloc[i:]
            if (seg.index[1].hour * 60 + seg.index[1].minute) >= 955:
                return None
            return _exit(side, float(seg["open"].iloc[1]), lo if side == 1 else hi, seg)
    return None


def t_gap(day, prev_close):
    if prev_close is None or len(day) < 3:
        return None
    b0 = day.iloc[0]
    g = float(b0["open"]) / prev_close - 1
    side = 1 if (g > 0.02 and b0["close"] > b0["open"]) else (-1 if (g < -0.02 and b0["close"] < b0["open"]) else 0)
    if not side:
        return None
    return _exit(side, float(day["open"].iloc[1]), float(b0["low"] if side == 1 else b0["high"]), day)


STRATEGIES = {"segui 5m": t_segui, "contro 5m": t_contro, "rottura 30m": t_rottura30, "gap e via": t_gap}


def build_days(data: dict) -> dict:
    """{giorno: {titolo: barre del giorno}} e per ogni titolo il volume della prima barra e la chiusura di ieri."""
    per_day, first_vol, prev_close = {}, {}, {}
    for s, df in data.items():
        last = None
        fv = []
        for d, day in df.groupby(df.index.date):
            if len(day) < 20 or day.index[0].hour * 60 + day.index[0].minute != 570:
                last = float(day["close"].iloc[-1]) if len(day) else last
                continue
            per_day.setdefault(d, {})[s] = day
            fv.append((d, float(day["volume"].iloc[0])))
            prev_close[(s, d)] = last
            last = float(day["close"].iloc[-1])
        first_vol[s] = pd.Series(dict(fv))
    return per_day, first_vol, prev_close


def pick(d, today: dict, first_vol: dict, prev_close: dict, n=PICKS) -> list[str]:
    cand = []
    for s, day in today.items():
        hist = first_vol[s]
        past = hist[hist.index < d].tail(SINCE_RV)
        if len(past) < SINCE_RV or past.mean() <= 0 or float(day["open"].iloc[0]) < 5:
            continue
        cand.append((float(day["volume"].iloc[0]) / past.mean(), s))
    cand.sort(reverse=True)
    return [s for rv, s in cand[:n] if rv >= 1.5]


def stats(daily: pd.Series, lev=1.0) -> dict:
    eq = (1 + daily * lev).cumprod()
    yrs = max(len(daily) / 252, 1e-9)
    return {"annuo": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "calo": ((eq / eq.cummax()) - 1).min() * 100,
            "totale": (eq.iloc[-1] - 1) * 100}


def main(broker, days=370):
    data = fetch_many(broker, list(UNIVERSE), days)
    print(f"titoli con dati: {len(data)}", flush=True)
    per_day, first_vol, prev_close = build_days(data)
    dates = sorted(per_day)
    chosen = {d: pick(d, per_day[d], first_vol, prev_close) for d in dates}
    chosen = {d: v for d, v in chosen.items() if d > dates[SINCE_RV]}
    freq = pd.Series([s for v in chosen.values() for s in v]).value_counts()
    print(f"\ngiorni testati: {len(chosen)}, titoli scelti in media al giorno: {np.mean([len(v) for v in chosen.values()]):.1f}")
    print("i più scelti: " + ", ".join(f"{s} {n}" for s, n in freq.head(12).items()))
    print("\n=== INTRADAY SUI TITOLI DEL GIORNO · ultimo anno · costi 0,05% a lato · chiuso ogni sera ===")
    print(f"{'strategia':13} {'operaz.':>8} {'vinte':>6} {'media/op':>9} {'anno 1x':>9} {'calo 1x':>8} {'anno 2x':>9} {'calo 2x':>8} {'senza costi':>12}")
    for name, fn in STRATEGIES.items():
        daily, nocost, trades = {}, {}, []
        for d, syms in chosen.items():
            rs = [r for r in (fn(per_day[d][s], prev_close.get((s, d))) for s in syms) if r is not None]
            trades += rs
            daily[pd.Timestamp(d)] = sum(rs) / PICKS   # quota fissa per titolo: la parte non usata resta ferma
            nocost[pd.Timestamp(d)] = sum(r + 2 * COST for r in rs) / PICKS
        daily, nocost = pd.Series(daily), pd.Series(nocost)
        a, b = stats(daily), stats(daily, 2)
        win = np.mean([t > 0 for t in trades]) * 100 if trades else 0
        avg = np.mean(trades) * 100 if trades else 0
        print(f"{name:13} {len(trades):8d} {win:5.0f}% {avg:+8.3f}% {a['annuo']:+8.1f}% {a['calo']:+7.1f}% "
              f"{b['annuo']:+8.1f}% {b['calo']:+7.1f}% {stats(nocost)['annuo']:+11.1f}%", flush=True)
    for s in ("SPY", "QQQ"):
        try:
            c = broker.daily_bars(s, days)["close"]
            print(f"compra e tieni {s}: {(c.iloc[-1] / c.iloc[0] - 1) * 100:+.1f}%")
        except Exception:
            pass
    print(flush=True)

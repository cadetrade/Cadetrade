"""Banco di prova intraday: si entra e si esce in giornata, mai posizioni la notte.

Barre a 5 minuti di Alpaca (SIP), solo seduta regolare 9:30-16:00 di New York.
Regole fissate prima di guardare i risultati. Il segnale nasce a fine barra e si entra all'apertura della barra dopo
(niente prezzi visti col senno di poi). Costi: 0,02% a lato su SPY/QQQ, 0,05% sulle singole azioni.
Ogni strategia usa tutto il capitale assegnato al titolo (leva 1x); si riporta anche la versione a leva 2x.

Strategie:
- apertura30: rottura del range dei primi 30 minuti, long sopra e short sotto, stop al lato opposto, chiusura alle 15:55
- vwap: se il prezzo si allontana dal VWAP del giorno di oltre 2 deviazioni standard si scommette sul ritorno, uscita al VWAP
- ultima mezz'ora: se la prima mezz'ora (con il gap) sale/scende oltre 0,25%, si segue la direzione dalle 15:30 alle 16:00
- gap: gap d'apertura oltre lo 0,5% chiuso controcorrente, uscita alla chiusura di ieri o alle 11:00
- rsi2 5m: RSI(2) a 5 minuti sotto 10 sopra il VWAP compra, sopra 90 sotto il VWAP vende; uscita a RSI 50 o dopo 1 ora"""
import urllib.parse
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

SYMBOLS = ("SPY", "QQQ", "NVDA", "TSLA", "AMD", "META")
COST = {"SPY": 0.0002, "QQQ": 0.0002}
DEFAULT_COST = 0.0005


def fetch(broker, symbol: str, days: int = 370, tf: str = "5Min") -> pd.DataFrame:
    from ..broker import DATA_URL
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, token = [], None
    while True:
        q = {"symbols": symbol, "timeframe": tf, "start": start, "end": end, "limit": 10000, "feed": "sip", "adjustment": "all"}
        if token:
            q["page_token"] = token
        r = broker._req("GET", f"{DATA_URL}/v2/stocks/bars?{urllib.parse.urlencode(q)}") or {}
        rows += r.get("bars", {}).get(symbol, []) or []
        token = r.get("next_page_token")
        if not token:
            break
    df = pd.DataFrame(rows).rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)
    t = df.index.hour * 60 + df.index.minute
    return df[(t >= 570) & (t < 960)]


def _mins(day: pd.DataFrame) -> np.ndarray:
    return day.index.hour * 60 + day.index.minute


def _trade(side: int, entry: float, exit_: float, cost: float) -> float:
    return side * (exit_ / entry - 1) - 2 * cost


def s_apertura30(day, prev_close, cost):
    m = _mins(day)
    first = day[m < 600]
    rest = day[(m >= 600)]
    if len(first) < 5 or len(rest) < 10:
        return []
    hi, lo = first["high"].max(), first["low"].min()
    o, h, l, c = (rest[k].to_numpy() for k in ("open", "high", "low", "close"))
    rm = _mins(rest)
    for i in range(len(rest) - 1):
        if rm[i + 1] >= 955:
            break
        side = 1 if c[i] > hi else (-1 if c[i] < lo else 0)
        if not side:
            continue
        entry, stop = o[i + 1], (lo if side == 1 else hi)
        for j in range(i + 1, len(rest)):
            if (side == 1 and l[j] <= stop) or (side == -1 and h[j] >= stop):
                px = min(o[j], stop) if side == 1 else max(o[j], stop)
                return [_trade(side, entry, px, cost)]
            if rm[j] >= 955:
                return [_trade(side, entry, c[j], cost)]
        return [_trade(side, entry, c[-1], cost)]
    return []


def _vwap(day):
    tp = (day["high"] + day["low"] + day["close"]) / 3
    cv = day["volume"].cumsum().replace(0, np.nan)
    vw = (tp * day["volume"]).cumsum() / cv
    dev = (day["close"] - vw)
    sd = dev.expanding(6).std()
    return vw.to_numpy(), sd.to_numpy()


def s_vwap(day, prev_close, cost):
    vw, sd = _vwap(day)
    o, c = day["open"].to_numpy(), day["close"].to_numpy()
    m = _mins(day)
    out, i, n = [], 6, len(day)
    while i < n - 1:
        if m[i + 1] >= 950 or not np.isfinite(sd[i]) or sd[i] <= 0:
            i += 1
            continue
        z = (c[i] - vw[i]) / sd[i]
        side = -1 if z > 2 else (1 if z < -2 else 0)
        if not side:
            i += 1
            continue
        entry, j = o[i + 1], i + 1
        while j < n - 1 and m[j] < 955 and not ((side == 1 and c[j] >= vw[j]) or (side == -1 and c[j] <= vw[j])):
            j += 1
        out.append(_trade(side, entry, c[j], cost))
        i = j + 1
    return out


def s_ultima(day, prev_close, cost):
    m = _mins(day)
    if prev_close is None or (m < 600).sum() < 5 or (m >= 930).sum() < 5:
        return []
    r = day["close"][m < 600].iloc[-1] / prev_close - 1
    if abs(r) < 0.0025:
        return []
    side = 1 if r > 0 else -1
    last = day[m >= 930]
    return [_trade(side, last["open"].iloc[0], last["close"].iloc[-1], cost)]


def s_gap(day, prev_close, cost):
    if prev_close is None or len(day) < 20:
        return []
    o0 = day["open"].iloc[0]
    g = o0 / prev_close - 1
    if abs(g) < 0.005:
        return []
    side = -1 if g > 0 else 1
    m = _mins(day)
    seg = day[m < 660]
    for j in range(len(seg)):
        if (side == 1 and seg["high"].iloc[j] >= prev_close) or (side == -1 and seg["low"].iloc[j] <= prev_close):
            return [_trade(side, o0, prev_close, cost)]
    return [_trade(side, o0, seg["close"].iloc[-1], cost)]


def _rsi2(c: pd.Series) -> np.ndarray:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()


def s_rsi2(day, prev_close, cost):
    vw, _ = _vwap(day)
    r = _rsi2(day["close"])
    o, c = day["open"].to_numpy(), day["close"].to_numpy()
    m = _mins(day)
    out, i, n = [], 3, len(day)
    while i < n - 1:
        if m[i + 1] >= 950:
            break
        side = 1 if (r[i] < 10 and c[i] > vw[i]) else (-1 if (r[i] > 90 and c[i] < vw[i]) else 0)
        if not side:
            i += 1
            continue
        entry, j = o[i + 1], i + 1
        while j < n - 1 and m[j] < 955 and j - i < 12 and not ((side == 1 and r[j] > 50) or (side == -1 and r[j] < 50)):
            j += 1
        out.append(_trade(side, entry, c[j], cost))
        i = j + 1
    return out


STRATEGIES = {"apertura30": s_apertura30, "vwap": s_vwap, "ultima mezz'ora": s_ultima, "gap": s_gap, "rsi2 5m": s_rsi2}


def run(bars: pd.DataFrame, fn, cost: float) -> tuple[pd.Series, list[float]]:
    """Rendimento di ogni giorno (le operazioni del giorno si compongono) e lista di tutte le operazioni."""
    days, trades, prev = {}, [], None
    for d, day in bars.groupby(bars.index.date):
        t = fn(day, prev, cost)
        trades += t
        days[pd.Timestamp(d)] = float(np.prod([1 + x for x in t]) - 1) if t else 0.0
        prev = float(day["close"].iloc[-1])
    return pd.Series(days), trades


def stats(daily: pd.Series, lev: float = 1.0) -> dict:
    r = daily * lev
    eq = (1 + r).cumprod()
    yrs = max(len(r) / 252, 1e-9)
    return {"totale_%": (eq.iloc[-1] - 1) * 100, "annuo_%": (eq.iloc[-1] ** (1 / yrs) - 1) * 100,
            "calo_max_%": ((eq / eq.cummax()) - 1).min() * 100}


def main(broker, days: int = 370):
    data = {}
    for s in SYMBOLS:
        try:
            data[s] = fetch(broker, s, days)
            print(f"{s}: {len(data[s])} barre a 5 minuti, {len(set(data[s].index.date))} giorni", flush=True)
        except Exception as e:
            print(f"{s}: dati non disponibili ({e})", flush=True)
    print("\n=== TEST INTRADAY · ultimo anno · barre 5 minuti · costi inclusi · chiuso ogni sera ===")
    print(f"{'strategia':16} {'titolo':6} {'operaz.':>8} {'vinte':>6} {'media/op':>9} {'anno 1x':>9} {'calo 1x':>8} {'anno 2x':>9} {'calo 2x':>8}")
    summary = []
    for name, fn in STRATEGIES.items():
        port = []
        n_all = 0
        for s, bars in data.items():
            if bars.empty:
                continue
            daily, trades = run(bars, fn, COST.get(s, DEFAULT_COST))
            port.append(daily)
            n_all += len(trades)
            a, b = stats(daily), stats(daily, 2)
            win = np.mean([t > 0 for t in trades]) * 100 if trades else 0
            avg = np.mean(trades) * 100 if trades else 0
            print(f"{name:16} {s:6} {len(trades):8d} {win:5.0f}% {avg:+8.3f}% {a['annuo_%']:+8.1f}% {a['calo_max_%']:+7.1f}% "
                  f"{b['annuo_%']:+8.1f}% {b['calo_max_%']:+7.1f}%")
        if port:
            mix = pd.concat(port, axis=1).fillna(0).mean(axis=1)
            a, b = stats(mix), stats(mix, 2)
            summary.append((a["annuo_%"], name, n_all, a, b))
            print(f"{name:16} {'TUTTI':6} {n_all:8d} {'':6} {'':9} {a['annuo_%']:+8.1f}% {a['calo_max_%']:+7.1f}% "
                  f"{b['annuo_%']:+8.1f}% {b['calo_max_%']:+7.1f}%\n")
    # stesse strategie senza costi: quanto si mangiano commissioni e slittamento
    print("=== SENZA COSTI (solo per capire quanto pesano) · tutti i titoli insieme, leva 1x ===")
    for name, fn in STRATEGIES.items():
        port = [run(b, fn, 0.0)[0] for b in data.values() if not b.empty]
        if port:
            print(f"{name:16} anno {stats(pd.concat(port, axis=1).fillna(0).mean(axis=1))['annuo_%']:+7.1f}%")
    # riferimenti sullo stesso periodo
    print("\n=== RIFERIMENTI STESSO PERIODO ===")
    for s in ("SPY", "QQQ"):
        if s in data and not data[s].empty:
            c = data[s]["close"]
            print(f"compra e tieni {s}: {(c.iloc[-1] / c.iloc[0] - 1) * 100:+.1f}%")
    try:
        from .optimize import BASES, closes, simulate
        px = closes({s: broker.daily_bars(s, days + 500) for s in ("SPY", "QQQ", "GLD")})
        eq = simulate(px, BASES["Nasdaq/oro"], ("volat", 0.15), "tiene", 0.3, False)
        first = min(b.index[0] for b in data.values() if not b.empty).tz_localize(None).normalize()
        eq = eq[eq.index >= first]
        print(f"sistema bilanciato attuale: {(eq.iloc[-1] / eq.iloc[0] - 1) * 100:+.1f}% "
              f"(calo max {((eq / eq.cummax()) - 1).min() * 100:.1f}%)")
    except Exception as e:
        print(f"sistema attuale non calcolato: {e}")
    print(flush=True)

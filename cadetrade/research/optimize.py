"""Banco di prova: centinaia di sistemi costruiti a incastro, provati su tutti i dati dal 2021.

Ogni sistema decide ogni sera i pesi di domani usando SOLO i prezzi fino a stasera:
  1. base:   come si divide il conto tra i titoli del nucleo
  2. regime: mercato "toro" o "orso" (S&P 500 sopra/sotto la media a 200 giorni, con margine dell'1%)
  3. leva:   fissa, a obiettivo di volatilità (più leva quando il mercato è calmo), o diversa tra toro e orso
  4. orso:   in fase orso tiene, va in liquidità, sposta su oro, o fa short di copertura su Nasdaq/S&P
  5. ribasso: compra in più sui cali brevi in trend rialzista (RSI a 2 giorni sotto 10), rivende al rimbalzo
  6. filtro: ogni titolo sotto la sua media a 200 giorni esce dalla base
Costi veri: interessi 6,5% sulla leva, 0,05% per operazione (crypto 0,25%), leva lorda max 2x come Alpaca.
La scelta si fa sul 2021-2023 e si verifica sul 2024-2026, che il sistema non ha mai "visto"."""
import itertools

import numpy as np
import pandas as pd

from ..signals import rsi
from .indicators import ema

BORROW, SLIP, SLIP_CRYPTO, MAX_GROSS = 0.065, 0.0005, 0.0025, 2.0

BASES = {
    "SPY/QQQ/oro": {"SPY": 0.42, "QQQ": 0.33, "GLD": 0.25},
    "Nasdaq/oro": {"QQQ": 0.6, "GLD": 0.4},
    "indici+oro+BTC": {"SPY": 0.35, "QQQ": 0.35, "GLD": 0.2, "BTC/USD": 0.1},
    "big tech+oro": {"QQQ": 0.3, "GLD": 0.3, "MSFT": 0.1, "AAPL": 0.1, "NVDA": 0.1, "AMZN": 0.1},
    "tutto il nucleo": {s: 1 / 8 for s in ("SPY", "QQQ", "GLD", "BTC/USD", "MSFT", "AAPL", "NVDA", "AMZN")},
}
LEVERS = [("fissa", 1.0), ("fissa", 1.25), ("fissa", 1.5), ("fissa", 2.0),
          ("volat", 0.12), ("volat", 0.15), ("volat", 0.20),
          ("toro/orso", (1.5, 0.5)), ("toro/orso", (2.0, 0.5)), ("toro/orso", (2.0, 1.0)), ("toro/orso", (1.5, 1.0))]
BEARS = ["tiene", "liquidità", "oro", "short"]
DIPS = [0.0, 0.3, 0.5]
FILTERS = [False, True]


def closes(data: dict) -> pd.DataFrame:
    """Chiusure allineate sui giorni di borsa USA (le crypto prendono il valore dello stesso giorno)."""
    cols = {}
    for s, df in data.items():
        c = df["close"].copy()
        c.index = pd.to_datetime(c.index, utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
        cols[s] = c[~c.index.duplicated(keep="last")]
    px = pd.DataFrame(cols)
    days = px["SPY"].dropna().index
    return px.ffill().loc[days]


def targets(px: pd.DataFrame, base: dict, lever, bear: str, dip: float, filt: bool) -> pd.DataFrame:
    """I pesi decisi ogni sera (riga t = cosa tenere da domani), usando solo i prezzi fino alla sera t.
    È la stessa funzione che usa il bot dal vivo: quello che si prova è quello che gira."""
    rets = px.pct_change().fillna(0.0)
    spy = px["SPY"]
    bull = pd.Series(np.nan, index=px.index)
    e200 = ema(spy, 200)
    bull[spy > e200 * 1.01] = 1.0
    bull[spy < e200 * 0.99] = 0.0
    bull = bull.ffill().fillna(1.0)          # tra le due bande resta quello di prima: niente cambi a ogni tremolio
    w = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    for s, x in base.items():
        if s in w:
            w[s] = x
    if filt:
        above = px > ema(px, 200)
        w = w.where(above, 0.0)
    # in fase orso
    if bear == "liquidità":
        w = w.mul(bull, axis=0)
    elif bear == "oro":
        moved = w.drop(columns="GLD").sum(axis=1) * (1 - bull)
        w = w.mul(bull, axis=0)
        w["GLD"] = w["GLD"] + moved + (pd.Series(base.get("GLD", 0.0), index=w.index) * (1 - bull))
    elif bear == "short":
        w = w.mul(bull, axis=0)
        w["QQQ"] = w["QQQ"] - 0.25 * (1 - bull)
        w["SPY"] = w["SPY"] - 0.25 * (1 - bull)
        w["GLD"] = w["GLD"] + base.get("GLD", 0.0) * (1 - bull)
    # leva
    kind, k = lever
    if kind == "fissa":
        lev = pd.Series(k, index=px.index)
    elif kind == "toro/orso":
        lev = bull * k[0] + (1 - bull) * k[1]
    else:   # obiettivo di volatilità: la volatilità degli ultimi 20 giorni del portafoglio senza leva, aggiornata il venerdì
        raw = (w.shift(1) * rets).sum(axis=1)
        vol = raw.rolling(20).std() * np.sqrt(252)
        lev = (k / vol).clip(upper=MAX_GROSS)
        lev = lev.where(lev.index.dayofweek == 4).ffill().fillna(1.0)
    w = w.mul(lev, axis=0)
    # compra sul ribasso: S&P e Nasdaq in trend rialzista con RSI(2) sotto 10, si rivende quando RSI(2) supera 70 o dopo 5 giorni
    if dip:
        for s in ("SPY", "QQQ"):
            r2 = rsi(px[s], 2)
            up = px[s] > ema(px[s], 200)
            on, held, flag = [], 0, False
            for a, b, c in zip(r2.values, up.values, range(len(r2))):
                if flag and (a > 70 or held >= 5):
                    flag, held = False, 0
                elif not flag and a < 10 and b:
                    flag = True
                held = held + 1 if flag else 0
                on.append(flag)
            w[s] = w[s] + dip * pd.Series(on, index=px.index, dtype=float)
    gross = w.abs().sum(axis=1)
    return w.mul((MAX_GROSS / gross.where(gross > 0, 1.0)).clip(upper=1.0), axis=0)   # mai oltre 2x lordo


def simulate(px: pd.DataFrame, base: dict, lever, bear: str, dip: float, filt: bool) -> pd.Series:
    """Valore del conto giorno per giorno (parte da 1)."""
    rets = px.pct_change().fillna(0.0)
    w = targets(px, base, lever, bear, dip, filt)
    held = w.shift(1).fillna(0.0)
    longs = held.clip(lower=0).sum(axis=1)
    cost_borrow = (longs - 1).clip(lower=0) * BORROW / 252
    turn = (w - held).abs()
    cost_trade = turn.drop(columns=[c for c in turn if "/" in c]).sum(axis=1) * SLIP + \
        turn[[c for c in turn if "/" in c]].sum(axis=1) * SLIP_CRYPTO
    r = (held * rets).sum(axis=1) - cost_borrow - cost_trade.shift(1).fillna(0.0)
    return (1 + r).cumprod()


def metrics(eq: pd.Series) -> dict:
    if len(eq) < 30:
        return {"cagr": float("nan"), "dd": float("nan"), "tot": float("nan")}
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    tot = eq.iloc[-1] / eq.iloc[0]
    dd = (eq / eq.cummax() - 1).min()
    return {"cagr": (tot ** (1 / yrs) - 1) * 100, "dd": dd * 100, "tot": (tot - 1) * 100}


def yearly(eq: pd.Series) -> dict:
    out = {}
    for y, g in eq.groupby(eq.index.year):
        prev = eq[eq.index < g.index[0]]
        start = prev.iloc[-1] if len(prev) else g.iloc[0]
        out[y] = round((g.iloc[-1] / start - 1) * 100, 1)
    return out


def search(data: dict, split="2024-01-01", max_dd=-25.0, start="2021-11-01"):
    px = closes(data)
    combos = list(itertools.product(BASES, LEVERS, BEARS, DIPS, FILTERS))
    rows = []
    for b, lv, br, dp, fl in combos:
        eq = simulate(px, BASES[b], lv, br, dp, fl)
        eq = eq[eq.index >= start] / eq[eq.index >= start].iloc[0]   # i primi mesi servono a calcolare le medie
        ins, oos = eq[eq.index < split], eq[eq.index >= split]
        oos = oos / oos.iloc[0]
        rows.append({"base": b, "leva": f"{lv[0]} {lv[1]}", "orso": br, "ribasso": dp, "filtro200": fl,
                     **{f"tutto_{k}": v for k, v in metrics(eq).items()},
                     **{f"is_{k}": v for k, v in metrics(ins).items()},
                     **{f"oos_{k}": v for k, v in metrics(oos).items()},
                     "anni": yearly(eq), "_eq": eq})
    return pd.DataFrame(rows), px


def _fmt(r) -> str:
    anni = " ".join(f"{y}:{v:+.0f}%" for y, v in r["anni"].items())
    return (f"{r['base']:16} leva {r['leva']:16} orso {r['orso']:9} ribasso {r['ribasso']:.1f} filtro200 {'sì' if r['filtro200'] else 'no'}"
            f" | TUTTO {r['tutto_cagr']:+6.1f}%/anno calo {r['tutto_dd']:6.1f}% | 2021-23 {r['is_cagr']:+6.1f}%/a calo {r['is_dd']:6.1f}%"
            f" | 2024-26 {r['oos_cagr']:+6.1f}%/a calo {r['oos_dd']:6.1f}% | {anni}")


def main(broker):
    from ..config import CORE
    data = {}
    for s in CORE:
        data[s] = broker.daily_bars(s, 365 * 6 + 200)
    df, px = search(data)
    print(f"dati dal {px.index[0].date()} al {px.index[-1].date()}  ·  sistemi provati: {len(df)}")
    print("\n=== RIFERIMENTI ===")
    ref = df[(df.leva == "fissa 1.0") & (df.orso == "tiene") & (df.ribasso == 0) & (~df.filtro200)]
    for _, r in ref.iterrows():
        print(_fmt(r))
    print("\n=== SCELTI SUL 2021-2023 (calo max entro 25%), POI GUARDATI SUL 2024-2026 MAI VISTO ===")
    ok = df[df.is_dd >= -25].sort_values("is_cagr", ascending=False).head(12)
    for _, r in ok.iterrows():
        print(_fmt(r))
    print("\n=== I PIÙ SOLIDI: migliore il peggiore dei due periodi, calo max entro 25% in entrambi ===")
    df["solido"] = df[["is_cagr", "oos_cagr"]].min(axis=1)
    ok = df[(df.is_dd >= -25) & (df.oos_dd >= -25)].sort_values("solido", ascending=False).head(15)
    for _, r in ok.iterrows():
        print(_fmt(r))
    print("\n=== I PIÙ ALTI IN ASSOLUTO SU TUTTO IL PERIODO (calo max entro 30%) ===")
    for _, r in df[df.tutto_dd >= -30].sort_values("tutto_cagr", ascending=False).head(8).iterrows():
        print(_fmt(r))
    print("\n=== QUANTO CONTA OGNI PEZZO (media dei sistemi con calo max entro 25%) ===")
    good = df[df.tutto_dd >= -25]
    for col in ("base", "leva", "orso", "ribasso", "filtro200"):
        g = good.groupby(col)[["tutto_cagr", "tutto_dd", "oos_cagr"]].mean().round(1)
        print(f"-- {col} --\n{g.to_string()}")
    print(flush=True)

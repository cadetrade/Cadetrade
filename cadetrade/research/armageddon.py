"""Salvadanaio Armageddon: pochi colpi all'anno con leva altissima ma perdita massima fissata.

Regole (decise prima di guardare i risultati):
- ogni 1 gennaio si mette da parte il 5% del conto; al massimo 3 colpi all'anno, ognuno con 1/3 del salvadanaio
- "momento caldissimo": S&P 500 sopra la media a 200 giorni, il titolo fa un nuovo massimo a 50 giorni con volume
  almeno 1,5 volte la media, ed è il più forte del gruppo negli ultimi 20 giorni; tra un colpo e l'altro almeno 30 giorni
- strumento: opzione call a 6 settimane (30 giorni di borsa), prezzo d'esercizio 5% sopra il prezzo
- uscita: appena l'opzione vale 3 volte quanto pagata, oppure dopo 20 giorni di borsa al valore di quel giorno
Il prezzo delle opzioni non c'è nei dati storici: si stima con Black-Scholes sulla volatilità vera degli ultimi 20 giorni
aumentata del 20% (le opzioni sui titoli caldi costano care) e il 3% di spread in entrata e in uscita.
Confronto: stesso colpo fatto con leva 5x sulle azioni (perdita del colpo tagliata al 100%)."""
import math

import numpy as np
import pandas as pd

UNIVERSE = ("NVDA", "AMD", "TSLA", "META", "AVGO", "PLTR", "COIN", "MSTR", "NFLX", "AMZN", "MSFT", "AAPL",
            "GOOGL", "SMCI", "ARM", "MU", "CRWD", "SHOP", "UBER", "ANET")
POT, SHOTS, GAP, DAYS_EXP, HOLD, OTM, TAKE, IV_MULT, SPREAD, RATE = 0.05, 3, 30, 30, 20, 1.05, 3.0, 1.2, 0.03, 0.04


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def call_price(s, k, days, vol, r=RATE):
    t = max(days, 0) / 252
    if t <= 0 or vol <= 0:
        return max(s - k, 0.0)
    d1 = (math.log(s / k) + (r + vol * vol / 2) * t) / (vol * math.sqrt(t))
    return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d1 - vol * math.sqrt(t))


def _prep(df):
    c = df["close"].copy()
    c.index = pd.to_datetime(c.index, utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    v = df["volume"].copy()
    v.index = c.index
    return c[~c.index.duplicated(keep="last")], v[~v.index.duplicated(keep="last")]


def run(data: dict, spy: pd.Series, start="2021-11-01", lever5=False) -> list[dict]:
    closes, vols = {}, {}
    for s, df in data.items():
        if len(df) > 80:
            closes[s], vols[s] = _prep(df)
    px, vo = pd.DataFrame(closes), pd.DataFrame(vols)
    days = px.index[px.index >= start]
    spy = spy.reindex(px.index).ffill()
    bull = spy > spy.ewm(span=200, adjust=False).mean()
    hi50 = px.rolling(50).max()
    vr = vo / vo.rolling(20).mean()
    r20 = px.pct_change(20)
    rv = px.pct_change().rolling(20).std() * np.sqrt(252)
    shots, last_day = [], None
    per_year = {}
    for i, d in enumerate(days):
        y = d.year
        if per_year.get(y, 0) >= SHOTS or (last_day is not None and (d - last_day).days < GAP) or not bull.get(d, False):
            continue
        hot = [(r20.at[d, s], s) for s in px if pd.notna(px.at[d, s]) and pd.notna(hi50.at[d, s])
               and px.at[d, s] >= hi50.at[d, s] and vr.at[d, s] >= 1.5]
        if not hot:
            continue
        mom, s = max(hot)
        if mom <= 0:
            continue
        idx = px.index.get_loc(d)
        path = px[s].iloc[idx: idx + HOLD + 1].dropna()
        if len(path) < 2:
            continue
        s0, vol = float(path.iloc[0]), float(rv.at[d, s]) * IV_MULT
        k = s0 * OTM
        if lever5:
            mult, exit_i = 1.0, len(path) - 1
            for j in range(1, len(path)):
                mult = max(0.0, 1 + 5 * (float(path.iloc[j]) / s0 - 1))
                exit_i = j
                if mult == 0 or mult >= TAKE:
                    break
            why = "azzerato" if mult == 0 else ("preso x3" if mult >= TAKE else "20 giorni")
        else:
            paid = call_price(s0, k, DAYS_EXP, vol) * (1 + SPREAD)
            mult, exit_i, why = 0.0, len(path) - 1, "20 giorni"
            for j in range(1, len(path)):
                val = call_price(float(path.iloc[j]), k, DAYS_EXP - j, vol) * (1 - SPREAD)
                mult, exit_i = val / paid, j
                if mult >= TAKE:
                    why = "preso x3"
                    break
        shots.append({"giorno": str(d.date()), "titolo": s, "slancio_20g_%": round(mom * 100, 1), "anno": y,
                      "uscita": str(path.index[exit_i].date()), "titolo_%": round((float(path.iloc[exit_i]) / s0 - 1) * 100, 1),
                      "moltiplica": round(mult, 2), "perché": why})
        per_year[y] = per_year.get(y, 0) + 1
        last_day = d
    return shots


def main(broker):
    from .optimize import BASES, closes, simulate
    data = {}
    for s in UNIVERSE:
        try:
            data[s] = broker.daily_bars(s, 365 * 6 + 200)
        except Exception as e:
            print(f"{s}: dati non disponibili ({e})")
    spy_df = broker.daily_bars("SPY", 365 * 6 + 200)
    spy, _ = _prep(spy_df)
    sys_px = closes({s: broker.daily_bars(s, 365 * 6 + 200) for s in ("SPY", "QQQ", "GLD")})
    eq = simulate(sys_px, BASES["Nasdaq/oro"], ("volat", 0.15), "tiene", 0.3, False)
    eq = eq[eq.index >= "2021-11-01"]
    for label, lev in (("OPZIONI CALL", False), ("AZIONI CON LEVA 5x", True)):
        shots = run(data, spy, lever5=lev)
        print(f"\n=== SALVADANAIO ARMAGEDDON · {label} · 5% del conto all'anno, max 3 colpi ===")
        for x in shots:
            print(f"  {x['giorno']} {x['titolo']:6} slancio {x['slancio_20g_%']:+6.1f}% → {x['uscita']} titolo {x['titolo_%']:+6.1f}%"
                  f"  colpo x{x['moltiplica']:.2f}  ({x['perché']})")
        acc_sys, acc_mix = 100_000.0, 100_000.0
        print("  anno   sistema da solo        sistema + salvadanaio     salvadanaio")
        for y in sorted(set(eq.index.year)):
            g = eq[eq.index.year == y]
            prev = eq[eq.index < g.index[0]]
            r = g.iloc[-1] / (prev.iloc[-1] if len(prev) else g.iloc[0]) - 1
            ys = [x for x in shots if x["anno"] == y]
            pot = acc_mix * POT
            back = sum(pot / SHOTS * x["moltiplica"] for x in ys) + pot / SHOTS * (SHOTS - len(ys))
            acc_sys *= 1 + r
            acc_mix = (acc_mix - pot) * (1 + r) + back
            print(f"  {y}   {r*100:+6.1f}% → {acc_sys:>9,.0f} $   {acc_mix:>9,.0f} $            {pot:>7,.0f} $ → {back:>8,.0f} $"
                  f"  ({len(ys)} colpi)")
        tot = sum(x["moltiplica"] for x in shots)
        print(f"  colpi {len(shots)}: vinti {sum(x['moltiplica'] > 1 for x in shots)}, x3 presi {sum(x['moltiplica'] >= 3 for x in shots)},"
              f" a zero o quasi {sum(x['moltiplica'] < 0.1 for x in shots)} · resa media per colpo x{tot / max(1, len(shots)):.2f}")
    print(flush=True)

"""Quanta leva servirebbe al sistema (60% Nasdaq / 40% oro, leva a obiettivo di volatilità, acquisti sui cali)
per arrivare a +50% l'anno, e quanto costerebbe in rischio. Dati giornalieri dal 2016 (dentro: 2018, crollo Covid 2020, 2022)."""
import numpy as np
import pandas as pd

from . import optimize as op

GRID = [(0.15, 2.0), (0.20, 2.5), (0.25, 3.0), (0.30, 3.0), (0.35, 4.0), (0.40, 4.0), (0.50, 5.0)]


def run(px, start):
    rows = []
    for vol, cap in GRID:
        op.MAX_GROSS = cap
        eq = op.simulate(px, op.BASES["Nasdaq/oro"], ("volat", vol), "tiene", 0.3, False)
        eq = eq[eq.index >= start]
        eq = eq / eq.iloc[0]
        m, y = op.metrics(eq), op.yearly(eq)
        r = eq.pct_change().dropna()
        worst_m = eq.resample("ME").last().pct_change().min() * 100
        rows.append((vol, cap, m, y, worst_m, (eq / eq.cummax() - 1)))
    op.MAX_GROSS = 2.0
    return rows


def main(broker):
    px = op.closes({s: broker.daily_bars(s, 365 * 11) for s in ("SPY", "QQQ", "GLD")})
    start = px.index[0] + pd.Timedelta(days=300)
    print(f"dati dal {px.index[0].date()} · test dal {start.date()} al {px.index[-1].date()}")
    print("\n=== SISTEMA CON PIÙ LEVA · costi del prestito 6,5% l'anno inclusi ===")
    for vol, cap, m, y, wm, dd in run(px, start):
        yrs = " ".join(f"{k}:{v:+.0f}%" for k, v in y.items())
        under = (dd < -0.30).mean() * 100
        print(f"volat {vol:.2f} leva max {cap:.1f}x | {m['cagr']:+6.1f}%/anno · calo max {m['dd']:6.1f}% · mese peggiore {wm:+.0f}%"
              f" · tempo sotto -30%: {under:.0f}%\n   {yrs}", flush=True)
    for s in ("SPY", "QQQ"):
        c = px[s][px.index >= start]
        print(f"compra e tieni {s}: {op.metrics(c / c.iloc[0])['cagr']:+.1f}%/anno · calo {op.metrics(c / c.iloc[0])['dd']:.1f}%")
    print(flush=True)

"""Numeri al posto delle impressioni: indicatori calcolati in codice e passati al cervello come contesto."""
import logging

from .config import UNIVERSE
from .research.indicators import atr, ema

log = logging.getLogger("segnali")


def rsi(close, n: int = 14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def compute(broker, symbols=UNIVERSE) -> dict:
    out = {}
    for sym in symbols:
        try:
            df = broker.daily_bars(sym, 300)
            if len(df) < 60:
                continue
            c = df["close"]
            last = float(c.iloc[-1])
            e50, e200 = float(ema(c, 50).iloc[-1]), float(ema(c, 200).iloc[-1])
            out[sym] = {
                "prezzo": round(last, 4),
                "var_1g_%": round((last / float(c.iloc[-2]) - 1) * 100, 2),
                "var_20g_%": round((last / float(c.iloc[-21]) - 1) * 100, 2),
                "sopra_ema50": last > e50,
                "sopra_ema200": last > e200,
                "rsi14": round(float(rsi(c).iloc[-1]), 1),
                "atr14": round(float(atr(df, 14).iloc[-1]), 6),
                "atr14_%": round(float(atr(df, 14).iloc[-1]) / last * 100, 2),
                "dist_max_60g_%": round((last / float(c.iloc[-60:].max()) - 1) * 100, 2),
            }
        except Exception as e:   # un titolo senza dati non ferma la sveglia
            log.warning("segnali %s non disponibili: %s", sym, e)
    return out

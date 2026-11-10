"""Dati OHLCV: scarico da exchange (endpoint pubblici, nessuna chiave) o lettura CSV."""
import time

import pandas as pd

from .rconfig import ExchangeCfg

COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def to_frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop_duplicates("timestamp").set_index("timestamp").astype(float)


def fetch_recent(exchange, cfg: ExchangeCfg, limit: int = 500) -> pd.DataFrame:
    df = to_frame(exchange.fetch_ohlcv(cfg.symbol, cfg.timeframe, limit=limit))
    return df.iloc[:-1]   # scarta la candela ancora aperta


def fetch_history(cfg: ExchangeCfg, days: int) -> pd.DataFrame:
    import ccxt

    ex = getattr(ccxt, cfg.id)({"enableRateLimit": True})
    since = ex.milliseconds() - days * 86_400_000
    rows = []
    while True:
        batch = ex.fetch_ohlcv(cfg.symbol, cfg.timeframe, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 2:
            break
        time.sleep(ex.rateLimit / 1000)
    return to_frame(rows).iloc[:-1]


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp")[COLS[1:]].astype(float)

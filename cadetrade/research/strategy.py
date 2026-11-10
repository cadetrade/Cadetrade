"""Strategie. Ogni strategia riceve le candele OHLCV e restituisce un segnale per l'ultima candela chiusa."""
from dataclasses import dataclass

import pandas as pd

from .rconfig import StrategyCfg
from .indicators import atr, ema


@dataclass
class Signal:
    action: str          # "buy" | "sell" | "hold"
    price: float
    stop: float | None = None
    take: float | None = None
    reason: str = ""


class EmaCross:
    """Trend following long-only: incrocio EMA veloce/lenta, filtrato dalla EMA di trend, stop e target in ATR."""

    def __init__(self, cfg: StrategyCfg):
        self.cfg = cfg

    @property
    def warmup(self) -> int:
        return max(self.cfg.slow, self.cfg.trend, self.cfg.atr_period) + 2

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        c = self.cfg
        out = df.copy()
        out["ema_fast"] = ema(out["close"], c.fast)
        out["ema_slow"] = ema(out["close"], c.slow)
        out["ema_trend"] = ema(out["close"], c.trend)
        out["atr"] = atr(out, c.atr_period)
        return out

    def signal_at(self, df: pd.DataFrame, i: int) -> Signal:
        """df deve essere già passato da prepare(); i = indice della candela chiusa da valutare."""
        row, prev = df.iloc[i], df.iloc[i - 1]
        price = float(row["close"])
        crossed_up = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
        crossed_down = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]
        if crossed_up and price > row["ema_trend"]:
            a = float(row["atr"])
            return Signal("buy", price, price - self.cfg.stop_atr * a, price + self.cfg.take_atr * a, "ema cross up")
        if crossed_down:
            return Signal("sell", price, reason="ema cross down")
        return Signal("hold", price)


STRATEGIES = {"ema_cross": EmaCross}


def build_strategy(cfg: StrategyCfg):
    return STRATEGIES[cfg.name](cfg)

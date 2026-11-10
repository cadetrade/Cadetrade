"""Logica comune a backtest e bot live: data una candela chiusa, gestisce stop/target, segnali e rischio."""
import pandas as pd

from .sim_broker import PaperBroker
from .risk import RiskManager


def step(df: pd.DataFrame, i: int, strategy, broker: PaperBroker, risk: RiskManager) -> None:
    row = df.iloc[i]
    ts = df.index[i]
    pos = broker.pos

    # 1) uscite su stop / target (valutate sui minimi/massimi della candela; stop prima per prudenza)
    if pos.qty > 0:
        if pos.stop is not None and row["low"] <= pos.stop:
            broker.sell(min(pos.stop, float(row["open"])), ts, "stop")
        elif pos.take is not None and row["high"] >= pos.take:
            broker.sell(max(pos.take, float(row["open"])), ts, "take")

    equity = broker.equity(float(row["close"]))
    risk.update(equity, ts.date())

    # 2) kill switch: chiude tutto se il drawdown massimo è superato
    if risk.halted_reason == "max_drawdown" and broker.pos.qty > 0:
        broker.sell(float(row["close"]), ts, "kill switch")
        return

    # 3) segnali della strategia
    sig = strategy.signal_at(df, i)
    if sig.action == "sell" and broker.pos.qty > 0:
        broker.sell(sig.price, ts, sig.reason)
    elif sig.action == "buy" and broker.pos.qty == 0:
        qty = risk.position_size(equity, sig.price, sig.stop)
        broker.buy(qty, sig.price, sig.stop, sig.take, ts)

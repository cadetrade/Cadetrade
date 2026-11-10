"""Backtest su dati storici. Uso: python -m cadetrade.backtest [--csv file.csv] [--days 365]"""
import argparse
import logging

import numpy as np
import pandas as pd

from .sim_broker import PaperBroker
from .rconfig import Config, load_config
from .data import fetch_history, load_csv
from .engine import step
from .risk import RiskManager
from .strategy import build_strategy


def run_backtest(df: pd.DataFrame, cfg: Config) -> dict:
    strategy = build_strategy(cfg.strategy)
    data = strategy.prepare(df)
    broker = PaperBroker(cfg.risk.starting_equity, cfg.risk.fee_rate)
    risk = RiskManager(cfg.risk)
    curve = []
    for i in range(strategy.warmup, len(data)):
        step(data, i, strategy, broker, risk)
        curve.append(broker.equity(float(data["close"].iloc[i])))
    if broker.pos.qty > 0:
        broker.sell(float(data["close"].iloc[-1]), data.index[-1], "fine backtest")
        curve[-1] = broker.cash

    eq = pd.Series(curve, dtype=float)
    closed = [t for t in broker.trades if t["side"] == "sell"]
    wins = [t for t in closed if t["pnl"] > 0]
    rets = eq.pct_change().dropna()
    first, last = float(data["close"].iloc[strategy.warmup]), float(data["close"].iloc[-1])
    return {
        "start_equity": cfg.risk.starting_equity,
        "end_equity": round(float(eq.iloc[-1]), 2) if len(eq) else cfg.risk.starting_equity,
        "return_pct": round(float(eq.iloc[-1] / cfg.risk.starting_equity - 1) * 100, 2) if len(eq) else 0.0,
        "buy_and_hold_pct": round((last / first - 1) * 100, 2),
        "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min() * 100), 2) if len(eq) else 0.0,
        "trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "sharpe_approx": round(float(rets.mean() / rets.std() * np.sqrt(len(rets))) , 2) if rets.std() else 0.0,
        "halted": risk.halted_reason,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="research.toml")
    p.add_argument("--csv", help="CSV con colonne timestamp,open,high,low,close,volume")
    p.add_argument("--days", type=int, default=365)
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(a.config)
    df = load_csv(a.csv) if a.csv else fetch_history(cfg.exchange, a.days)
    for k, v in run_backtest(df, cfg).items():
        print(f"{k:>18}: {v}")


if __name__ == "__main__":
    main()

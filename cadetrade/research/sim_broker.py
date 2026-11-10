"""Broker simulato usato solo dal backtest."""
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("cadetrade.broker")


@dataclass
class Position:
    qty: float = 0.0
    entry: float = 0.0
    stop: float | None = None
    take: float | None = None


class PaperBroker:
    def __init__(self, cash: float, fee_rate: float):
        self.cash = cash
        self.fee_rate = fee_rate
        self.pos = Position()
        self.trades: list[dict] = []

    def equity(self, price: float) -> float:
        return self.cash + self.pos.qty * price

    def buy(self, qty: float, price: float, stop=None, take=None, ts=None) -> None:
        cost = qty * price * (1 + self.fee_rate)
        if qty <= 0 or cost > self.cash:
            return
        self.cash -= cost
        self.pos = Position(qty, price, stop, take)
        self.trades.append({"ts": ts, "side": "buy", "qty": qty, "price": price})
        log.info("BUY %.6f @ %.2f stop=%s take=%s", qty, price, stop, take)

    def sell(self, price: float, ts=None, reason: str = "") -> None:
        if self.pos.qty <= 0:
            return
        qty = self.pos.qty
        self.cash += qty * price * (1 - self.fee_rate)
        pnl = qty * (price - self.pos.entry) - qty * (price + self.pos.entry) * self.fee_rate
        self.trades.append({"ts": ts, "side": "sell", "qty": qty, "price": price, "pnl": pnl, "reason": reason})
        log.info("SELL %.6f @ %.2f pnl=%.2f (%s)", qty, price, pnl, reason)
        self.pos = Position()



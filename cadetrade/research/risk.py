"""Gestione del rischio: dimensione della posizione e kill switch. Ha l'ultima parola su ogni ordine."""
from dataclasses import dataclass, field
from datetime import date

from .rconfig import RiskCfg


@dataclass
class RiskManager:
    cfg: RiskCfg
    peak_equity: float = 0.0
    day: date | None = None
    day_start_equity: float = 0.0
    halted_reason: str | None = field(default=None)

    def update(self, equity: float, today: date) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        if self.day != today:
            self.day, self.day_start_equity = today, equity
            if self.halted_reason == "daily_loss":
                self.halted_reason = None   # il limite giornaliero si azzera a mezzanotte UTC
        if self.peak_equity and equity <= self.peak_equity * (1 - self.cfg.max_drawdown):
            self.halted_reason = "max_drawdown"   # permanente: richiede intervento umano
        elif self.day_start_equity and equity <= self.day_start_equity * (1 - self.cfg.max_daily_loss):
            self.halted_reason = self.halted_reason or "daily_loss"

    @property
    def can_open(self) -> bool:
        return self.halted_reason is None

    def position_size(self, equity: float, entry: float, stop: float) -> float:
        """Quantità tale che, se scatta lo stop, si perde al massimo risk_per_trade del capitale."""
        if not self.can_open or stop is None or entry <= stop:
            return 0.0
        qty_by_risk = (equity * self.cfg.risk_per_trade) / (entry - stop)
        qty_by_cap = (equity * self.cfg.max_position_pct) / entry
        return max(0.0, min(qty_by_risk, qty_by_cap))

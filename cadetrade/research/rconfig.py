import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class ExchangeCfg:
    id: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"


@dataclass
class StrategyCfg:
    name: str = "ema_cross"
    fast: int = 20
    slow: int = 50
    trend: int = 200
    atr_period: int = 14
    stop_atr: float = 2.0
    take_atr: float = 4.0


@dataclass
class RiskCfg:
    starting_equity: float = 1000.0
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.25
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.15
    fee_rate: float = 0.001


@dataclass
class Config:
    mode: str = "paper"
    exchange: ExchangeCfg = field(default_factory=ExchangeCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)

    @property
    def is_live(self) -> bool:
        # Doppia conferma: config + variabile d'ambiente.
        return self.mode == "live" and os.getenv("CADETRADE_LIVE_CONFIRM") == "yes"


def load_config(path: str = "research.toml") -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return Config(
        mode=raw.get("mode", "paper"),
        exchange=ExchangeCfg(**raw.get("exchange", {})),
        strategy=StrategyCfg(**raw.get("strategy", {})),
        risk=RiskCfg(**raw.get("risk", {})),
    )

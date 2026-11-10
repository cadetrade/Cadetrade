import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cadetrade.brain import FakeBrain  # noqa: E402
from cadetrade.broker import FakeBroker  # noqa: E402
from cadetrade.config import TZ  # noqa: E402
from cadetrade.notify import Notifier  # noqa: E402
from cadetrade.wakeup import Ctx  # noqa: E402

DAY = datetime(2026, 11, 10, tzinfo=TZ)


def at(hhmm: str, day=DAY) -> datetime:
    return day.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]))


def make_ctx(state: Path, brain=None) -> Ctx:
    return Ctx(broker=FakeBroker(state / "fake_broker.json"), brain=brain or FakeBrain(),
               notifier=Notifier(), state_dir=state, use_signals=False)


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path / "state")


def buy_position(ctx, symbol, qty, price, now_price):
    """Apre una posizione sul broker finto e porta il prezzo a now_price."""
    ctx.broker.set_price(symbol, price)
    ctx.broker.submit(symbol, "buy", qty, f"setup-{symbol.replace('/', '')}")
    ctx.broker.set_price(symbol, now_price)


def hold_brain():
    return FakeBrain({"orders": [], "reasoning": "Tengo tutto.", "planNext": "Niente."})

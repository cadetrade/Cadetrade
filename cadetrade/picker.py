"""La scelta del giorno: 1-2 titoli "caldi" fuori dal nucleo, scelti dal CODICE con numeri verificabili
(volume anomalo rispetto alla media, slancio, liquidità). Il cervello poi decide se e quanto comprarne."""
import json
import logging
from datetime import datetime
from pathlib import Path

from .baton import _atomic_write
from .config import CORE, CRYPTO, PICKS, PickRules, is_crypto

log = logging.getLogger("scelta")


def score(broker, symbol: str, rules: PickRules = PICKS) -> dict | None:
    """Restituisce i numeri del candidato, o None con il motivo nel log se non passa i filtri."""
    if symbol in CORE or symbol in rules.banned:
        return None
    if is_crypto(symbol) and symbol not in CRYPTO:
        return None
    try:
        df = broker.daily_bars(symbol, 120)
    except Exception as e:
        log.info("%s scartato: dati non disponibili (%s)", symbol, e)
        return None
    if len(df) < 60:
        return None
    c, v = df["close"], df["volume"]
    price, prev = float(c.iloc[-1]), float(c.iloc[-2])
    chg = price / prev - 1
    avg_vol = float(v.iloc[-21:-1].mean())
    vol_ratio = float(v.iloc[-1]) / avg_vol if avg_vol else 0.0
    dollar_vol = float(v.iloc[-1]) * price
    reasons = []
    if price < rules.min_price and not is_crypto(symbol):
        reasons.append("prezzo troppo basso")
    if not is_crypto(symbol) and dollar_vol < rules.min_dollar_volume:
        reasons.append("poco liquido")
    if vol_ratio < rules.min_volume_ratio:
        reasons.append("volume nella norma")
    if chg <= 0:
        reasons.append("non sale")
    if chg > rules.max_change_1d:
        reasons.append("rialzo eccessivo in un giorno")
    if reasons:
        log.info("%s scartato: %s", symbol, ", ".join(reasons))
        return None
    trend20 = price / float(c.iloc[-21]) - 1
    # fomo = quanto il volume è fuori norma x quanto sale; il trend a 20 giorni pesa a favore se positivo
    s = vol_ratio * chg * (1 + max(trend20, 0))
    return {"symbol": symbol, "score": round(s, 4), "price": round(price, 4), "var_1g_%": round(chg * 100, 2),
            "volume_vs_media20": round(vol_ratio, 2), "trend_20g_%": round(trend20 * 100, 2),
            "why": f"volume {vol_ratio:.1f}x la media, {chg*100:+.1f}% oggi, {trend20*100:+.1f}% in 20 giorni"}


def pick(broker, rules: PickRules = PICKS) -> list[dict]:
    try:
        candidates = broker.screener(20)
    except Exception as e:
        log.warning("screener non disponibile: %s", e)
        return []
    scored = [s for s in (score(broker, c, rules) for c in candidates[:40]) if s]
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored[: rules.max_picks]


class PickStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "picks.json"

    def today(self, day: datetime) -> list[dict] | None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return d.get("picks") if d.get("date") == f"{day:%Y-%m-%d}" else None

    def save(self, day: datetime, picks: list[dict]) -> None:
        _atomic_write(self.path, {"date": f"{day:%Y-%m-%d}", "picks": picks})


def picks_for_today(broker, state_dir: Path, day: datetime) -> list[dict]:
    """Una scelta al giorno, alla prima sveglia: per tutto il giorno i candidati restano gli stessi."""
    store = PickStore(state_dir)
    got = store.today(day)
    if got is None:
        got = pick(broker)
        store.save(day, got)
    return got

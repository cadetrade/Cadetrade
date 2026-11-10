"""Chiavi: da secrets.toml (ignorato da git) o da variabili d'ambiente con lo stesso nome. Mai nei log."""
import logging
import os
import tomllib
from pathlib import Path

from .config import PAPER_URL

FIELDS = ["ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
          "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]


class LiveAccountRefused(SystemExit):
    pass


def load_secrets(path: str = "secrets.toml") -> dict:
    data = {}
    p = Path(path)
    if p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
    out = {k: str(data.get(k) or os.getenv(k) or "") for k in FIELDS}
    base = data.get("ALPACA_BASE_URL") or os.getenv("ALPACA_BASE_URL")
    if base and base.rstrip("/") != PAPER_URL:
        raise LiveAccountRefused("Rifiuto di partire: ALPACA_BASE_URL non è l'indirizzo paper.")
    # Le chiavi del conto paper Alpaca iniziano con PK; quelle del conto vero con AK.
    if out["ALPACA_KEY_ID"] and not out["ALPACA_KEY_ID"].startswith("PK"):
        raise LiveAccountRefused("Rifiuto di partire: la Key ID non è di un conto paper (deve iniziare con PK).")
    return out


class MaskSecrets(logging.Filter):
    def __init__(self, secrets: dict):
        super().__init__()
        self.values = [v for v in secrets.values() if v and len(v) >= 6]

    def filter(self, record):
        msg = record.getMessage()
        for v in self.values:
            msg = msg.replace(v, "***")
        record.msg, record.args = msg, ()
        return True

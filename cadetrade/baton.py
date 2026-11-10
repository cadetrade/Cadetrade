"""Il testimone: le intenzioni dell'ultima sveglia. Scritto in modo atomico; se è rotto vale come assente."""
import json
import os
from pathlib import Path

REQUIRED = {"run_id", "slot", "ts", "planNext"}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)   # atomico: chi legge vede il vecchio file o il nuovo, mai uno a metà


class BatonStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "baton.json"
        self.history = state_dir / "history"

    def read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not REQUIRED <= data.keys():
            return None
        return data

    def write(self, baton: dict) -> None:
        _atomic_write(self.path, baton)
        _atomic_write(self.history / f"{baton['run_id']}.json", baton)

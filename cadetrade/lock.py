"""Il lucchetto: una sveglia alla volta. Se resta su disco, la sveglia prima è morta a metà."""
import json
import os
import time
from pathlib import Path

STALE_AFTER_S = 30 * 60


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Busy(Exception):
    """Un'altra sveglia viva sta lavorando."""


class RunLock:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "run.lock"

    def _create(self, run_id: str) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"run_id": run_id, "pid": os.getpid(), "ts": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())

    def peek(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            return {"run_id": None, "pid": -1, "ts": 0}

    def acquire(self, run_id: str) -> dict | None:
        """Prende il lucchetto. Restituisce le info della sveglia morta da riconciliare, oppure None.
        Solleva Busy se un'altra sveglia viva lo tiene."""
        try:
            self._create(run_id)
            return None
        except FileExistsError:
            pass
        held = self.peek() or {}
        if held.get("pid", -1) > 0 and _alive(held["pid"]) and time.time() - held.get("ts", 0) < STALE_AFTER_S:
            raise Busy(f"sveglia {held.get('run_id')} in corso (pid {held['pid']})")
        # Lucchetto di una sveglia morta. Il subentro passa da un secondo file creato con O_EXCL:
        # se due sveglie lo trovano insieme, una sola vince e l'altra si ferma.
        takeover = self.path.with_name("run.lock.takeover")
        try:
            if time.time() - takeover.stat().st_mtime > 60:
                takeover.unlink(missing_ok=True)   # subentro abbandonato da un processo morto
        except FileNotFoundError:
            pass
        try:
            fd = os.open(takeover, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            raise Busy("un'altra sveglia sta già subentrando al lucchetto")
        try:
            if self.peek() != held:
                raise Busy("il lucchetto è cambiato: un'altra sveglia è partita")
            self.path.unlink(missing_ok=True)
            self._create(run_id)
        finally:
            takeover.unlink(missing_ok=True)
        return held

    def release(self) -> None:
        held = self.peek()
        if held and held.get("pid") == os.getpid():
            self.path.unlink(missing_ok=True)

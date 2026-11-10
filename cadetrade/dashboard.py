"""Il pannello dal vivo: una pagina web servita dal bot stesso (porta 8080), protetta da una chiave nell'indirizzo.
Mostra cosa sta facendo adesso, il conto, le posizioni con i loro stop, la curva del conto e i messaggi della giornata.
Solo lettura: dal pannello non si può comprare né vendere nulla."""
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .clock import name_of, next_slot, now
from .config import CORE

log = logging.getLogger("pannello")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _tail_jsonl(path: Path, n: int) -> list:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


class State:
    """Raccoglie lo stato per la pagina. I dati del broker si chiedono al massimo ogni 20 secondi."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._cache, self._at = None, 0.0
        self._bars = {}
        self._lock = threading.Lock()

    def broker(self):
        with self._lock:
            if self._cache is None or time.time() - self._at > 20:
                try:
                    b = self.ctx.broker
                    self._cache = {"account": b.account(), "positions": b.positions(), "clock": b.clock(), "ok": True}
                except Exception as e:
                    self._cache = {**(self._cache or {}), "ok": False, "error": type(e).__name__}
                self._at = time.time()
            return self._cache

    def bars(self, symbol: str) -> list:
        """Candele giornaliere per il grafico, tenute in memoria 10 minuti."""
        key = symbol.upper()
        if key not in CORE and key not in self._picks():
            return []
        with self._lock:
            hit = self._bars.get(key)
            if hit and time.time() - hit[0] < 600:
                return hit[1]
        try:
            df = self.ctx.broker.daily_bars(key, 140).tail(90)
            rows = [{"t": str(i.date()), "o": round(r.open, 4), "h": round(r.high, 4), "l": round(r.low, 4),
                     "c": round(r.close, 4), "v": float(r.volume)} for i, r in df.iterrows()]
        except Exception as e:
            log.info("candele %s non disponibili: %s", key, e)
            rows = []
        with self._lock:
            self._bars[key] = (time.time(), rows)
        return rows

    def _picks(self) -> set:
        baton = _read_json(self.ctx.state_dir / "baton.json", {}) or {}
        return {p.get("symbol") for p in baton.get("picks") or []}

    def snapshot(self) -> dict:
        sd = self.ctx.state_dir
        t = now()
        lock = _read_json(sd / "run.lock", None)
        baton = _read_json(sd / "baton.json", {}) or {}
        risk = _read_json(sd / "risk.json", {}) or {}
        b = self.broker()
        if lock and not str(lock.get("run_id", "")).endswith("guard"):
            phase = "pensa"
        elif lock:
            phase = "guardia"
        else:
            phase = "attesa"
        nxt = next_slot(t)
        equity = [{"t": p["ts"], "v": p["equity"], "spy": p.get("spy")} for p in _tail_jsonl(sd / "equity.jsonl", 400)]
        return {
            "now": t.isoformat(), "phase": phase, "run": (lock or {}).get("run_id"),
            "next": {"at": nxt.isoformat(), "name": name_of(f"{nxt:%H%M}")},
            "broker": b, "stops": risk.get("stops", {}), "peak": risk.get("peak_equity"),
            "brain_failures": risk.get("brain_failures", 0),
            "baton": {k: baton.get(k) for k in ("run_id", "slot_name", "ts", "reasoning", "planNext", "orders", "rejected",
                                                 "halted", "picks", "brain_ok", "day_pl", "news_n", "signals_n")},
            "guard_at": (_read_json(sd / "heartbeat.json", {}) or {}).get("guard"),
            "equity": equity, "trades": _tail_jsonl(sd / "journal.jsonl", 15),
            "lessons": (_read_json(sd / "lessons.json", {}) or {}).get("lessons", []),
            "feed": [{"t": ts, "text": m} for ts, m in list(getattr(self.ctx.notifier, "feed", []))[-40:]],
            "core": CORE,
        }


def make_handler(state: State, key: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):   # niente rumore nel log
            pass

        def _ok_key(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            got = (q.get("k") or [""])[0]
            return bool(key) and hmac.compare_digest(got, key)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._ok_key():
                return self._send(403, "Chiave mancante o sbagliata.".encode(), "text/plain; charset=utf-8")
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/bars":
                sym = (parse_qs(urlparse(self.path).query).get("s") or ["SPY"])[0]
                return self._send(200, json.dumps(state.bars(sym)).encode(), "application/json")
            if path == "/api/state":
                data = json.dumps(state.snapshot(), ensure_ascii=False, default=str).encode()
                return self._send(200, data, "application/json; charset=utf-8")
            self._send(404, b"", "text/plain")
    return H


def start(ctx, key: str, port: int = 8080):
    if not key:
        log.info("pannello spento: manca DASHBOARD_KEY")
        return None
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(State(ctx), key))
    except OSError as e:   # il pannello non deve mai fermare il bot
        log.warning("pannello non avviato: %s", e)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="pannello").start()
    log.info("pannello acceso sulla porta %d", port)
    return srv


PAGE = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

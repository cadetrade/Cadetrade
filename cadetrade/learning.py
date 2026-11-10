"""Imparare dagli errori: diario dei trade, curva del conto e pagella settimanale.
Il cervello riscrive le sue lezioni e propone un esperimento; la gabbia resta fuori dalla sua portata."""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .baton import _atomic_write
from .brain import BrainError
from .config import CORE

log = logging.getLogger("diario")

FILLED = ("inviato", "già inviato")


class Journal:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.trades = state_dir / "journal.jsonl"
        self.theses = state_dir / "theses.json"
        self.equity = state_dir / "equity.jsonl"
        self.lessons_path = state_dir / "lessons.json"

    # --- tesi all'acquisto, esito alla vendita ---
    def _load_theses(self) -> dict:
        try:
            return json.loads(self.theses.read_text())
        except (OSError, ValueError):
            return {}

    def record(self, results: list[dict], positions: dict, prices: dict, t: datetime, picks: set = frozenset()) -> None:
        theses = self._load_theses()
        for r in results:
            if r["outcome"] != "inviato":
                continue
            sym = r["symbol"]
            if r["side"] == "buy":
                th = theses.get(sym)
                if not th:
                    theses[sym] = {"why": r["why"], "since": t.isoformat(), "entry": prices.get(sym),
                                   "kind": "nucleo" if sym in CORE else "scelto"}
                continue
            pos = positions.get(sym)
            if not pos:
                continue
            exit_px = prices.get(sym) or pos["price"]
            th = theses.get(sym, {})
            row = {"ts": t.isoformat(), "symbol": sym, "kind": th.get("kind", "nucleo" if sym in CORE else "scelto"),
                   "qty": r["qty"], "entry": pos["avg_entry"], "exit": exit_px,
                   "pnl_%": round((exit_px / pos["avg_entry"] - 1) * 100, 2),
                   "pnl_usd": round((exit_px - pos["avg_entry"]) * r["qty"], 2),
                   "since": th.get("since"), "thesis": th.get("why", ""),
                   "exit_reason": r["why"], "stop": bool(r.get("forced"))}
            self.trades.parent.mkdir(parents=True, exist_ok=True)
            with self.trades.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if r["qty"] >= pos["qty"] - 1e-9:
                theses.pop(sym, None)
        _atomic_write(self.theses, theses)

    def record_close(self, sym: str, qty: float, entry: float, exit_px: float, why: str, forced: bool, t: datetime) -> None:
        """Chiusura avvenuta fuori da una sveglia (stop eseguito dal broker)."""
        theses = self._load_theses()
        th = theses.pop(sym, {})
        row = {"ts": t.isoformat(), "symbol": sym, "kind": th.get("kind", "nucleo" if sym in CORE else "scelto"),
               "qty": qty, "entry": entry, "exit": exit_px, "pnl_%": round((exit_px / entry - 1) * 100, 2),
               "pnl_usd": round((exit_px - entry) * qty, 2), "since": th.get("since"), "thesis": th.get("why", ""),
               "exit_reason": why, "stop": forced}
        self.trades.parent.mkdir(parents=True, exist_ok=True)
        with self.trades.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _atomic_write(self.theses, theses)

    def closed_trades(self, since: datetime | None = None) -> list[dict]:
        rows = []
        try:
            for line in self.trades.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue   # una riga troncata da un crash non rompe il diario
                if since is None or row["ts"] >= since.isoformat():
                    rows.append(row)
        except OSError:
            pass
        return rows

    # --- curva del conto contro SPY ---
    def mark(self, t: datetime, equity: float, spy: float | None, core: dict | None = None) -> None:
        self.equity.parent.mkdir(parents=True, exist_ok=True)
        with self.equity.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": t.isoformat(), "equity": equity, "spy": spy, "core": core or {}}) + "\n")

    def performance(self, since: datetime) -> dict:
        pts = []
        try:
            for line in self.equity.read_text().splitlines():
                try:
                    p = json.loads(line)
                except ValueError:
                    continue
                if p["ts"] >= since.isoformat():
                    pts.append(p)
        except OSError:
            pass
        if len(pts) < 2:
            return {}
        a, b = pts[0], pts[-1]
        out = {"conto_%": round((b["equity"] / a["equity"] - 1) * 100, 2)}
        if a.get("spy") and b.get("spy"):
            out["compra_e_tieni_spy_%"] = round((b["spy"] / a["spy"] - 1) * 100, 2)
            out["differenza_vs_spy_%"] = round(out["conto_%"] - out["compra_e_tieni_spy_%"], 2)
        ca, cb = a.get("core") or {}, b.get("core") or {}
        common = [s for s in ca if s in cb and ca[s] and cb[s]]
        if common:   # il nucleo comprato in parti uguali e tenuto fermo
            out["compra_e_tieni_nucleo_%"] = round(sum(cb[s] / ca[s] - 1 for s in common) / len(common) * 100, 2)
        peak, dd = 0.0, 0.0
        for p in pts:
            peak = max(peak, p["equity"])
            dd = min(dd, p["equity"] / peak - 1)
        out["peggior_calo_%"] = round(dd * 100, 2)
        return out

    # --- lezioni ---
    def lessons(self) -> dict:
        try:
            d = json.loads(self.lessons_path.read_text())
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            pass
        return {"lessons": [], "experiment": None, "history": []}


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trade_chiusi": 0}
    pnl = [t["pnl_%"] for t in trades]
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    by_kind = {}
    for t in trades:
        k = by_kind.setdefault(t["kind"], {"n": 0, "pnl_usd": 0.0})
        k["n"] += 1
        k["pnl_usd"] = round(k["pnl_usd"] + t["pnl_usd"], 2)
    return {
        "trade_chiusi": len(trades),
        "vincenti_%": round(len(wins) / len(trades) * 100, 1),
        "guadagno_medio_%": round(sum(t["pnl_%"] for t in wins) / len(wins), 2) if wins else 0.0,
        "perdita_media_%": round(sum(t["pnl_%"] for t in losses) / len(losses), 2) if losses else 0.0,
        "pnl_totale_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        "media_per_trade_%": round(sum(pnl) / len(pnl), 2),   # aspettativa: deve essere sopra zero
        "chiusi_dallo_stop": sum(1 for t in trades if t["stop"]),
        "per_tipo": by_kind,
    }


def cage_rejections(state_dir: Path, since: datetime) -> dict:
    """Quante volte e perché la gabbia ha detto no: anche questo è un errore da cui imparare."""
    counts = {}
    for f in sorted((state_dir / "history").glob("*.json")):
        try:
            b = json.loads(f.read_text())
        except ValueError:
            continue
        if b.get("ts", "") < since.isoformat():
            continue
        for r in b.get("rejected", []):
            key = r["reason"].split(":")[0]
            counts[key] = counts.get(key, 0) + 1
    return counts


def pick_outcomes(ctx, since: datetime) -> dict:
    """I titoli scelti per fomo, comprati o no: quanto hanno fatto da quando sono stati scelti. Dice se la scelta funziona."""
    from .picker import PickStore
    rows = [r for r in PickStore(ctx.state_dir).history() if r["date"] >= f"{since:%Y-%m-%d}" and r.get("price")]
    out = []
    for r in rows[-30:]:
        try:
            now_px = ctx.broker.latest_price(r["symbol"])
        except Exception:
            continue
        out.append({"data": r["date"], "titolo": r["symbol"], "da_allora_%": round((now_px / r["price"] - 1) * 100, 2)})
    if not out:
        return {}
    return {"media_%": round(sum(o["da_allora_%"] for o in out) / len(out), 2), "dettaglio": out}


def run_review(ctx, t: datetime, days: int = 7) -> dict | None:
    j = Journal(ctx.state_dir)
    since = t - timedelta(days=days)
    current = j.lessons()
    trades = j.closed_trades(since)
    context = {
        "periodo": f"{since:%Y-%m-%d} → {t:%Y-%m-%d}",
        "prestazioni": j.performance(since),
        "statistiche": stats(trades),
        "trade_chiusi": trades[-50:],
        "rifiuti_della_gabbia": cage_rejections(ctx.state_dir, since),
        "titoli_scelti_come_sono_andati": pick_outcomes(ctx, since),
        "lezioni_attuali": current.get("lessons", []),
        "esperimento_della_settimana": current.get("experiment"),
        "voti_precedenti": [h["grade"] for h in current.get("history", [])][-8:],
    }
    try:
        rv = ctx.brain.review(context)
    except BrainError as e:
        ctx.notifier.alarm(f"Pagella non riuscita ({e}): tengo le lezioni attuali.", f"{t:%Y-%m-%d}-review")
        return None
    hist = (current.get("history", []) + [{"date": f"{t:%Y-%m-%d}", "grade": rv["grade"], "summary": rv["summary"],
                                            "experiment": current.get("experiment"), "performance": context["prestazioni"]}])[-52:]
    new = {"updated": t.isoformat(), "lessons": rv["lessons"],
           "experiment": {"text": rv["experiment"], "since": f"{t:%Y-%m-%d}"}, "history": hist}
    _atomic_write(j.lessons_path, new)
    perf = context["prestazioni"]
    perf_line = (f"Conto {perf.get('conto_%', 0):+.2f}% contro SPY {perf.get('spy_%', 0):+.2f}%" if perf else "Dati insufficienti")
    ctx.notifier.send("\n".join([
        f"📒 Pagella {t:%Y-%m-%d} · voto {rv['grade']}/10", perf_line, rv["summary"],
        "Lezioni:", *[f"• {x}" for x in rv["lessons"]], f"Esperimento della settimana: {rv['experiment']}",
    ]))
    return new

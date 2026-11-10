"""Broker: Alpaca paper (vero) e un broker finto su file con la stessa interfaccia.
Alpaca è la verità sui soldi: conto, posizioni, ordini, orologio del mercato."""
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import BROKER_RETRIES, DATA_URL, PAPER_URL, is_crypto, norm

log = logging.getLogger("broker")


class BrokerUnavailable(Exception):
    """Rete, timeout, 5xx: l'esito di un invio è sconosciuto finché non si ricontrolla."""


class BrokerRejected(Exception):
    """Il broker ha risposto di no (4xx)."""


class DuplicateOrder(BrokerRejected):
    pass


class AlpacaPaper:
    def __init__(self, key_id: str, secret: str, timeout: float = 15):
        if not key_id or not secret:
            raise SystemExit("Mancano ALPACA_KEY_ID / ALPACA_SECRET_KEY in secrets.toml")
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret,
                        "Content-Type": "application/json", "Accept": "application/json"}
        self.timeout = timeout

    # --- HTTP ---
    def _req(self, method: str, url: str, body: dict | None = None, retries: int = BROKER_RETRIES):
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:300]
                if e.code == 404:
                    return None
                if e.code < 500:
                    if "client_order_id" in text and ("unique" in text or "already" in text):
                        raise DuplicateOrder(text)
                    raise BrokerRejected(f"{e.code} {text}")
                last = BrokerUnavailable(f"{e.code} {text}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = BrokerUnavailable(str(e))
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
        raise last

    def _get(self, path: str, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._req("GET", PAPER_URL + path + q)

    # --- interfaccia ---
    def account(self) -> dict:
        a = self._get("/v2/account")
        return {"equity": float(a["equity"]), "cash": float(a["cash"]), "last_equity": float(a["last_equity"])}

    def positions(self) -> dict:
        out = {}
        for p in self._get("/v2/positions") or []:
            out[norm(p["symbol"])] = {
                "qty": float(p["qty"]), "avg_entry": float(p["avg_entry_price"]),
                "price": float(p["current_price"]), "market_value": float(p["market_value"]),
                "plpc": float(p["unrealized_plpc"]),
            }
        return out

    def orders_since(self, hours: int = 48) -> list[dict]:
        after = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._get("/v2/orders", status="all", after=after, limit=500, direction="desc") or []
        return [self._order(o) for o in rows]

    def order_by_client_id(self, cid: str) -> dict | None:
        o = self._get("/v2/orders:by_client_order_id", client_order_id=cid)
        return self._order(o) if o else None

    def clock(self) -> dict:
        c = self._get("/v2/clock")
        return {"is_open": bool(c["is_open"]), "next_open": c["next_open"], "next_close": c["next_close"]}

    def submit(self, symbol: str, side: str, qty: float, client_order_id: str) -> dict:
        body = {"symbol": symbol, "qty": _fmt_qty(symbol, qty), "side": side, "type": "market",
                "time_in_force": "gtc" if is_crypto(symbol) else "day", "client_order_id": client_order_id}
        # Nessun retry automatico sull'invio: dopo un errore di rete si ricontrolla per id.
        return self._order(self._req("POST", PAPER_URL + "/v2/orders", body, retries=1))

    # --- stop veri sul broker (solo azioni: le crypto le protegge il guardiano, aperte h24 e senza gap) ---
    def open_stops(self) -> list[dict]:
        rows = self._get("/v2/orders", status="open", limit=500) or []
        return [{"id": o["id"], "client_order_id": o.get("client_order_id"), "symbol": norm(o["symbol"]),
                 "qty": float(o.get("qty") or 0), "stop_price": float(o["stop_price"])}
                for o in rows if o.get("type") == "stop" and o.get("side") == "sell" and o.get("stop_price")]

    def submit_stop(self, symbol: str, qty: float, stop_price: float, client_order_id: str) -> dict:
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "sell", "type": "stop", "time_in_force": "gtc",
                "stop_price": _fmt_price(stop_price), "client_order_id": client_order_id}
        return self._order(self._req("POST", PAPER_URL + "/v2/orders", body, retries=1))

    def replace_stop(self, order_id: str, qty: float, stop_price: float) -> dict:
        """Sposta uno stop esistente in un colpo solo (niente buco tra cancella e ricrea)."""
        body = {"qty": str(int(qty)), "stop_price": _fmt_price(stop_price)}
        return self._order(self._req("PATCH", f"{PAPER_URL}/v2/orders/{order_id}", body, retries=1))

    def cancel(self, order_id: str) -> None:
        self._req("DELETE", f"{PAPER_URL}/v2/orders/{order_id}")

    def news(self, symbols: list[str], limit: int = 15) -> list[dict]:
        """Ultime notizie (Benzinga, gratis con Alpaca) sui titoli seguiti."""
        syms = ",".join(s.replace("/", "") for s in symbols)
        r = self._req("GET", f"{DATA_URL}/v1beta1/news?" + urllib.parse.urlencode(
            {"symbols": syms, "limit": limit, "sort": "desc"})) or {}
        return [{"ora": n.get("created_at"), "titolo": n.get("headline"), "simboli": n.get("symbols", [])[:6],
                 "sintesi": (n.get("summary") or "")[:240]} for n in r.get("news", [])]

    def latest_price(self, symbol: str) -> float:
        if is_crypto(symbol):
            r = self._req("GET", f"{DATA_URL}/v1beta3/crypto/us/latest/trades?symbols={urllib.parse.quote(symbol)}")
        else:
            r = self._req("GET", f"{DATA_URL}/v2/stocks/trades/latest?symbols={symbol}&feed=iex")
        return float(r["trades"][symbol]["p"])

    def daily_bars(self, symbol: str, days: int = 300) -> pd.DataFrame:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
        q = urllib.parse.urlencode({"symbols": symbol, "timeframe": "1Day", "start": start, "limit": 10000})
        url = f"{DATA_URL}/v1beta3/crypto/us/bars?{q}" if is_crypto(symbol) else f"{DATA_URL}/v2/stocks/bars?{q}&feed=iex"
        bars = (self._req("GET", url) or {}).get("bars", {}).get(symbol, [])
        df = pd.DataFrame(bars).rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)

    def screener(self, top: int = 20) -> list[str]:
        """Candidati del giorno: azioni più scambiate, azioni in maggior rialzo, crypto in maggior rialzo."""
        out = []
        for path, keys in (("/v1beta1/screener/stocks/most-actives?by=volume&top=", ("most_actives",)),
                           ("/v1beta1/screener/stocks/movers?top=", ("gainers",)),
                           ("/v1beta1/screener/crypto/movers?top=", ("gainers",))):
            try:
                r = self._req("GET", f"{DATA_URL}{path}{top}") or {}
            except (BrokerUnavailable, BrokerRejected) as e:
                log.warning("screener %s non disponibile: %s", path, e)
                continue
            for k in keys:
                out += [norm(x["symbol"]) for x in r.get(k, []) if x.get("symbol")]
        return list(dict.fromkeys(out))

    @staticmethod
    def _order(o: dict) -> dict:
        return {"client_order_id": o.get("client_order_id"), "symbol": norm(o.get("symbol", "")),
                "side": o.get("side"), "qty": float(o.get("qty") or o.get("filled_qty") or 0),
                "status": o.get("status"), "filled_avg_price": float(o["filled_avg_price"]) if o.get("filled_avg_price") else None}


def _fmt_price(p: float) -> str:
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


def _fmt_qty(symbol: str, qty: float) -> str:
    return f"{math.floor(qty * 1e6) / 1e6:.6f}" if is_crypto(symbol) else str(int(qty))


class FakeBroker:
    """Broker finto persistito su file: stessa interfaccia, nessuna rete. Rifiuta gli id doppi come Alpaca."""

    DEFAULT_PRICES = {"SPY": 600.0, "QQQ": 520.0, "GLD": 300.0, "BTC/USD": 95000.0, "NVDA": 180.0, "PLTR": 150.0,
                      "TSLA": 400.0, "TQQQ": 90.0, "PENNY": 2.0, "ETH/USD": 3500.0, "SOL/USD": 180.0, "DOGE/USD": 0.4}
    # titoli "caldi" nel broker finto: moltiplicatore del volume di oggi e variazione di oggi
    DEFAULT_HOT = {"NVDA": [3.0, 0.04], "PLTR": [4.0, 0.08], "TQQQ": [5.0, 0.05], "PENNY": [9.0, 0.3], "DOGE/USD": [2.5, 0.06]}

    def __init__(self, path: Path):
        self.path = path
        self.before_submit = None   # gancio per i test: chiamato prima di registrare l'ordine
        self.after_submit = None    # gancio per i test: chiamato dopo
        if not path.exists():
            self._save({"cash": 100_000.0, "last_equity": 100_000.0, "market_open": True,
                        "prices": dict(self.DEFAULT_PRICES), "positions": {}, "orders": [],
                        "screener": ["TQQQ", "PLTR", "NVDA", "TSLA", "PENNY", "DOGE/USD"], "hot": dict(self.DEFAULT_HOT)})

    def _load(self) -> dict:
        return json.loads(self.path.read_text())

    def _save(self, s: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        tmp.replace(self.path)

    def set(self, **kw) -> None:
        s = self._load()
        s.update(kw)
        self._save(s)

    def set_price(self, symbol: str, price: float) -> None:
        """Muove il prezzo; se scende sotto uno stop sul broker, lo stop si esegue a quel prezzo (anche in gap)."""
        s = self._load()
        s["prices"][symbol] = price
        for o in s.setdefault("stops", []):
            if o["status"] == "new" and o["symbol"] == symbol and price <= o["stop_price"] and symbol in s["positions"]:
                pos = s["positions"][symbol]
                qty = min(o["qty"], pos["qty"])
                s["cash"] += qty * price
                pos["qty"] -= qty
                if pos["qty"] <= 1e-9:
                    s["positions"].pop(symbol)
                o.update(status="filled", filled_avg_price=price)
                s["orders"].append({"client_order_id": o["client_order_id"], "symbol": symbol, "side": "sell",
                                    "qty": qty, "status": "filled", "filled_avg_price": price})
        self._save(s)

    def open_stops(self) -> list[dict]:
        return [dict(o) for o in self._load().get("stops", []) if o["status"] == "new"]

    def submit_stop(self, symbol: str, qty: float, stop_price: float, client_order_id: str) -> dict:
        s = self._load()
        held = s["positions"].get(symbol, {}).get("qty", 0) - sum(o["qty"] for o in s.get("stops", [])
                                                                 if o["status"] == "new" and o["symbol"] == symbol)
        if qty > held + 1e-9:
            raise BrokerRejected("insufficient qty available for order")
        o = {"id": f"stop{len(s.setdefault('stops', []))}", "client_order_id": client_order_id, "symbol": symbol,
             "qty": float(qty), "stop_price": float(_fmt_price(stop_price)), "status": "new"}
        s["stops"].append(o)
        self._save(s)
        return dict(o)

    def replace_stop(self, order_id: str, qty: float, stop_price: float) -> dict:
        s = self._load()
        for o in s.get("stops", []):
            if o["id"] == order_id and o["status"] == "new":
                o.update(qty=float(qty), stop_price=float(_fmt_price(stop_price)))
                self._save(s)
                return dict(o)
        raise BrokerRejected("order not found")

    def cancel(self, order_id: str) -> None:
        s = self._load()
        for o in s.get("stops", []):
            if o["id"] == order_id and o["status"] == "new":
                o["status"] = "canceled"
        self._save(s)

    def news(self, symbols: list[str], limit: int = 15) -> list[dict]:
        return self._load().get("news", [])[:limit]

    def account(self) -> dict:
        s = self._load()
        eq = s["cash"] + sum(p["qty"] * s["prices"][k] for k, p in s["positions"].items())
        return {"equity": eq, "cash": s["cash"], "last_equity": s["last_equity"]}

    def positions(self) -> dict:
        s = self._load()
        out = {}
        for k, p in s["positions"].items():
            price = s["prices"][k]
            out[k] = {"qty": p["qty"], "avg_entry": p["avg"], "price": price, "market_value": p["qty"] * price,
                      "plpc": price / p["avg"] - 1}
        return out

    def orders_since(self, hours: int = 48) -> list[dict]:
        return list(reversed(self._load()["orders"]))

    def order_by_client_id(self, cid: str) -> dict | None:
        return next((o for o in self._load()["orders"] if o["client_order_id"] == cid), None)

    def clock(self) -> dict:
        return {"is_open": self._load()["market_open"], "next_open": None, "next_close": None}

    def submit(self, symbol: str, side: str, qty: float, client_order_id: str) -> dict:
        if self.before_submit:
            self.before_submit(symbol, side, qty, client_order_id)
        s = self._load()
        if any(o["client_order_id"] == client_order_id for o in s["orders"]):
            raise DuplicateOrder(f"client_order_id must be unique: {client_order_id}")
        qty = float(_fmt_qty(symbol, qty))
        price = s["prices"][symbol]
        pos = s["positions"].get(symbol, {"qty": 0.0, "avg": price})
        if side == "buy":
            if qty * price > s["cash"]:
                raise BrokerRejected("insufficient buying power")
            pos = {"qty": pos["qty"] + qty, "avg": (pos["qty"] * pos["avg"] + qty * price) / (pos["qty"] + qty)}
            s["cash"] -= qty * price
            s["positions"][symbol] = pos
        else:
            reserved = sum(o["qty"] for o in s.get("stops", []) if o["status"] == "new" and o["symbol"] == symbol)
            if qty > pos["qty"] - reserved + 1e-9:
                raise BrokerRejected("insufficient qty available for order")
            s["cash"] += qty * price
            pos["qty"] -= qty
            if pos["qty"] <= 1e-9:
                s["positions"].pop(symbol, None)
            else:
                s["positions"][symbol] = pos
        order = {"client_order_id": client_order_id, "symbol": symbol, "side": side, "qty": qty,
                 "status": "filled", "filled_avg_price": price}
        s["orders"].append(order)
        self._save(s)
        if self.after_submit:
            self.after_submit(order)
        return order

    def latest_price(self, symbol: str) -> float:
        return self._load()["prices"][symbol]

    def screener(self, top: int = 20) -> list[str]:
        return self._load().get("screener", [])

    def daily_bars(self, symbol: str, days: int = 300) -> pd.DataFrame:
        import numpy as np
        import zlib
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
        last = self.latest_price(symbol)
        close = last * np.exp(np.cumsum(rng.normal(0, 0.015, days))[::-1] * -1)
        close = close / close[-1] * last
        volume = np.full(days, 1e6)
        mult, chg = self._load().get("hot", {}).get(symbol, [1.0, None])
        volume[-1] *= mult
        if chg is not None:
            close[-2] = close[-1] / (1 + chg)
        idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=days, freq="1D")
        return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": volume}, index=idx)

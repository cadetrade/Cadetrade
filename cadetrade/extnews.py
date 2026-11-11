"""Notizie oltre Benzinga: titoli macro dai feed RSS pubblici (Investing.com, CNBC, MarketWatch)
e il calendario economico della settimana (dati USA ad alto impatto: CPI, Fed, lavoro...).
Tutto facoltativo: se una fonte non risponde si va avanti senza, la sveglia non si ferma mai per le notizie."""
import json
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

log = logging.getLogger("notizie")

FEEDS = {
    "Investing.com borsa": "https://www.investing.com/rss/news_25.rss",
    "Investing.com economia": "https://www.investing.com/rss/news_14.rss",
    "Investing.com materie prime": "https://www.investing.com/rss/news_11.rss",
    "Investing.com crypto": "https://www.investing.com/rss/news_301.rss",
    "CNBC mercati": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "CNBC economia": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cadetrade-rss/1.0", "Accept": "*/*"}
_cache: dict = {}


def _get(url: str, ttl: int, timeout: float = 8) -> bytes | None:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            body = r.read()
    except Exception as e:
        log.info("fonte %s non disponibile: %s", url.split("/")[2], e)
        body = hit[1] if hit else None   # meglio una copia vecchia che niente
    if body is not None:
        _cache[url] = (time.time(), body)
    return body


def parse_rss(body: bytes, source: str, since: datetime) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        when = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(when) if when else None
        except (TypeError, ValueError):
            ts = None
        if not title or (ts and ts.tzinfo and ts < since):
            continue
        out.append({"ora": ts.isoformat() if ts else None, "fonte": source, "titolo": title[:200]})
    return out


def headlines(now: datetime, hours: int = 18, limit: int = 25, feeds: dict = FEEDS) -> list[dict]:
    """Titoli macro e di mercato delle ultime ore, senza doppioni, dal più recente."""
    since = now - timedelta(hours=hours)
    seen, out = set(), []
    for src, url in feeds.items():
        body = _get(url, ttl=600)
        for n in parse_rss(body, src, since) if body else []:
            key = n["titolo"].lower()[:70]
            if key not in seen:
                seen.add(key)
                out.append(n)
    out.sort(key=lambda n: n["ora"] or "", reverse=True)
    return out[:limit]


def parse_calendar(body: bytes, now: datetime, days: int = 2) -> list[dict]:
    try:
        rows = json.loads(body)
    except ValueError:
        return []
    out = []
    for r in rows:
        if r.get("country") != "USD" or r.get("impact") not in ("High", "Medium"):
            continue
        try:
            ts = datetime.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        if now - timedelta(hours=12) <= ts <= now + timedelta(days=days):
            out.append({"quando": ts.astimezone(now.tzinfo).strftime("%a %d/%m %H:%M") + " Roma", "evento": r.get("title"),
                        "impatto": "ALTO" if r["impact"] == "High" else "medio",
                        "previsto": r.get("forecast") or None, "precedente": r.get("previous") or None,
                        "uscito": r.get("actual") or None, "ts": ts.isoformat()})
    return sorted(out, key=lambda x: x["ts"])


def calendar(now: datetime) -> list[dict]:
    """Dati macro USA di ieri sera, oggi e domani: il cervello sa cosa sta per uscire (e non entra subito prima)."""
    body = _get(CALENDAR_URL, ttl=3600)
    return parse_calendar(body, now) if body else []

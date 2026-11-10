"""Orari delle sveglie, in ora di Roma."""
from datetime import datetime, timedelta

from .config import SLOT_BY_HHMM, SLOT_GRACE_MIN, SLOTS, TZ


def now() -> datetime:
    return datetime.now(TZ)


def run_id(day: datetime, hhmm: str) -> str:
    return f"{day:%Y-%m-%d}-{hhmm}"


def slot_time(day: datetime, hhmm: str) -> datetime:
    return day.astimezone(TZ).replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)


def parse_run_id(rid: str) -> datetime:
    return datetime.strptime(rid, "%Y-%m-%d-%H%M").replace(tzinfo=TZ)


def due_slot(t: datetime) -> str | None:
    """L'ultima sveglia di oggi già scattata e ancora entro il margine."""
    best = None
    for s in SLOTS:
        st = slot_time(t, s.hhmm)
        if st <= t <= st + timedelta(minutes=SLOT_GRACE_MIN):
            best = s.hhmm
    return best


def latest_today(t: datetime) -> str | None:
    past = [s.hhmm for s in SLOTS if slot_time(t, s.hhmm) <= t]
    return past[-1] if past else None


def missed_between(last_rid: str | None, current_rid: str) -> list[str]:
    """Sveglie previste tra l'ultimo testimone e la sveglia attuale (escluse), massimo 2 giorni indietro."""
    if not last_rid:
        return []
    start, end = parse_run_id(last_rid), parse_run_id(current_rid)
    start = max(start, end - timedelta(days=2))
    out, day = [], start.replace(hour=0, minute=0)
    while day <= end:
        for s in SLOTS:
            st = slot_time(day, s.hhmm)
            if start < st < end:
                out.append(run_id(st, s.hhmm))
        day += timedelta(days=1)
    return out


def next_slot(t: datetime) -> datetime:
    for d in range(2):
        day = t + timedelta(days=d)
        for s in SLOTS:
            st = slot_time(day, s.hhmm)
            if st > t:
                return st
    raise RuntimeError("nessuna sveglia")


def name_of(hhmm: str) -> str:
    return SLOT_BY_HHMM[hhmm].name

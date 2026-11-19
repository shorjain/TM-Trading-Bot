"""US-Eastern trading sessions: regular / extended / closed, `--until`, and tradeable hours."""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .config import PaceCfg

ET = ZoneInfo("America/New_York")  # Windows needs the `tzdata` package (in requirements.txt)


def now_et() -> datetime:
    return datetime.now(ET)


def _ranges(spec: str) -> list[tuple[dtime, dtime]]:
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        out.append((dtime.fromisoformat(a.strip()), dtime.fromisoformat(b.strip())))
    return out


def _is_trading_day(d: date, p: PaceCfg) -> bool:
    if d.isoformat() in p.holidays:
        return False
    return p.include_weekends or d.weekday() < 5


def _regular_ranges(d: date, p: PaceCfg) -> list[tuple[dtime, dtime]]:
    if not _is_trading_day(d, p):
        return []
    rs = _ranges(p.regular)
    early = p.early_closes.get(d.isoformat())
    if early:
        close = dtime.fromisoformat(early)
        rs = [(a, min(b, close)) for a, b in rs if a < close]
    return rs


def session(t: datetime, p: PaceCfg) -> str:
    """'regular', 'extended' or 'closed' for an ET datetime."""
    t = t.astimezone(ET)
    d, tt = t.date(), t.time()
    if any(a <= tt < b for a, b in _regular_ranges(d, p)):
        return "regular"
    weekday_ok = p.include_weekends or d.weekday() < 5
    if weekday_ok and any(a <= tt < b for a, b in _ranges(p.extended)):
        return "extended"
    return "closed"


def tradeable_hours_between(start: datetime, end: datetime, p: PaceCfg, step_min: int = 5) -> float:
    """Hours in [start, end) that are regular or extended session (5-minute resolution)."""
    if end <= start:
        return 0.0
    t, n = start.astimezone(ET), 0
    step = timedelta(minutes=step_min)
    while t < end:
        if session(t, p) != "closed":
            n += 1
        t += step
    return n * step_min / 60


def parse_until(hhmm: str, now: datetime | None = None) -> datetime:
    """Next occurrence of HH:MM in ET (today if still ahead, else tomorrow)."""
    now = (now or now_et()).astimezone(ET)
    hh, mm = (int(x) for x in hhmm.strip().split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

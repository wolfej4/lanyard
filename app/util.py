import json
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from markupsafe import Markup, escape

from .config import settings

COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto",
    "America/Vancouver", "Europe/London", "Europe/Berlin", "Europe/Stockholm",
    "Australia/Sydney", "Asia/Tokyo",
]


def valid_tz(name: str) -> bool:
    if not name or name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def local_to_utc(value: str, tz: str) -> Optional[datetime]:
    """Parse an <input type=datetime-local> value in the event's zone, return aware UTC."""
    if not value:
        return None
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def utc_to_local(dt: Optional[datetime], tz: str) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz))


def input_value(dt: Optional[datetime], tz: str) -> str:
    loc = utc_to_local(dt, tz)
    return loc.strftime("%Y-%m-%dT%H:%M") if loc else ""


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").replace(":00 ", " ")


def fmt_dt(dt: Optional[datetime], tz: str, with_zone: bool = True) -> str:
    loc = utc_to_local(dt, tz)
    if not loc:
        return ""
    s = f"{loc.strftime('%a, %b %-d')} at {fmt_time(loc)}"
    return f"{s} {loc.tzname()}" if with_zone else s


def fmt_range(start: datetime, end: datetime, tz: str) -> str:
    a, b = utc_to_local(start, tz), utc_to_local(end, tz)
    if a.date() == b.date():
        return f"{a.strftime('%a, %b %-d')}, {fmt_time(a)} to {fmt_time(b)} {b.tzname()}"
    return f"{a.strftime('%a, %b %-d')} {fmt_time(a)} to {b.strftime('%a, %b %-d')} {fmt_time(b)} {b.tzname()}"


def money(cents: int) -> str:
    if not cents:
        return "Free"
    sym = {"usd": "$", "cad": "CA$", "aud": "A$", "eur": "€", "gbp": "£"}.get(settings.currency, "")
    amount = f"{cents / 100:,.2f}".replace(".00", "")
    return f"{sym}{amount}" if sym else f"{amount} {settings.currency.upper()}"


def parse_money(value: str) -> Optional[int]:
    value = (value or "").strip().replace("$", "").replace(",", "")
    if value == "":
        return 0
    try:
        cents = round(float(value) * 100)
    except ValueError:
        return None
    return cents if 0 <= cents <= 100_000_00 else None


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    base = _slug_re.sub("-", title.lower()).strip("-")[:60] or "lan"
    return f"{base}-{secrets.token_hex(2)}"


_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def ticket_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))


_url_re = re.compile(r"(https?://[^\s<]+[^\s<.,;:!?)\]'\"])")


def render_text(value: str) -> Markup:
    """User text to safe HTML: escaped, paragraphs on blank lines, links made clickable."""
    if not value:
        return Markup("")
    paras = re.split(r"\n\s*\n", value.strip())
    out = []
    for p in paras:
        html = str(escape(p))
        html = _url_re.sub(r'<a href="\1" rel="nofollow ugc noopener" target="_blank">\1</a>', html)
        out.append("<p>" + html.replace("\n", "<br>") + "</p>")
    return Markup("\n".join(out))


# ---- Seating ---------------------------------------------------------------

def row_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


def seat_label(r: int, c: int) -> str:
    return f"{row_letter(r)}{c + 1}"


def blocked_set(raw: str) -> set[str]:
    try:
        return set(json.loads(raw or "[]"))
    except (ValueError, TypeError):
        return set()


def seat_grid(rows: int, cols: int, blocked: Iterable[str]) -> list[list[dict]]:
    blocked = set(blocked)
    return [
        [{"label": seat_label(r, c), "blocked": seat_label(r, c) in blocked} for c in range(cols)]
        for r in range(rows)
    ]


def seat_count(rows: int, cols: int, blocked: Iterable[str]) -> int:
    return rows * cols - len([b for b in set(blocked)])


def safe_next(url: str) -> str:
    """Only allow local redirects."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return "/"
    return url

"""Hong-Kong-time helpers.

Lesson carried over from TyLove: never let the container clock decide when a job
runs. Railway containers are UTC, so every schedule decision is made by
converting to Asia/Hong_Kong explicitly.
"""
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")
UTC = ZoneInfo("UTC")

FMT = "%Y-%m-%d %H:%M"


def now_hk():
    return datetime.now(HK)


def today_hk():
    return now_hk().date()


def to_hk(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(HK)


def parse_hk(value, default_year=None):
    """Parse the many datetime shapes a scraped page can throw at us."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_hk(value)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:      # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=HK) if ts > 1e9 else None
    s = str(value).strip()
    s = s.replace("T", " ").replace("Z", "").replace("/", "-")
    if "+" in s[10:]:
        s = s[:10] + " " + s[11:].split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y-%m-%d",
                "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y%m%d %H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if "%H" not in fmt:
                dt = dt.replace(hour=default_year or 0)
            if fmt.startswith("%Y%m%d"):
                pass
            return dt.replace(tzinfo=HK)
        except ValueError:
            continue
    return None


def fmt_hk(dt, with_date=True):
    dt = to_hk(dt)
    if not dt:
        return "-"
    return dt.strftime("%m-%d %H:%M") if with_date else dt.strftime("%H:%M")


def minutes_until(kickoff):
    ko = to_hk(kickoff)
    if not ko:
        return None
    return (ko - now_hk()).total_seconds() / 60.0


def hhmm_now():
    n = now_hk()
    return n.hour, n.minute


def day_bounds_hk(d=None):
    d = d or today_hk()
    start = datetime(d.year, d.month, d.day, tzinfo=HK)
    return start, start + timedelta(days=1)


def parse_csv_ints(s, cast=int):
    out = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(cast(part))
        except ValueError:
            pass
    return out

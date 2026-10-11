"""Central configuration. Everything is env-driven so Railway Variables can override it."""
import os
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- environment
HK_TZ = ZoneInfo("Asia/Hong_Kong")

def _env(key, default=None, cast=None):
    v = os.environ.get(key, default)
    if v is None:
        return None
    if cast is bool:
        return str(v).strip().lower() in ("1", "true", "yes", "y", "on")
    if cast is not None:
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default
    return v

VERSION = "1.0.0"

# ---------------------------------------------------------------- telegram
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
# optional second channel (e.g. a group) - leave empty to disable
TELEGRAM_CHAT_ID_GROUP = _env("TELEGRAM_CHAT_ID_GROUP", "")

# ---------------------------------------------------------------- storage
DB_PATH = _env("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "football.db"))
DATA_DIR = os.path.dirname(DB_PATH)
PROBE_DIR = os.path.join(DATA_DIR, "probe")

# ---------------------------------------------------------------- model
# league average goals per team per match (used as Poisson baseline when a
# league has not enough history). Tune with backtest before trusting.
LEAGUE_BASE_GOALS = _env("LEAGUE_BASE_GOALS", 1.35, float)     # per team, neutral venue
# Home advantage as multipliers on the neutral (mixed venue) base rate.
# The old "+0.22 in log space" double-counted it: league_average already averages
# home *and* away goals, so adding a full home advantage on top inflated every
# lambda by ~12%. The backtest caught it (model log loss 1.139 vs naive 1.063);
# after the fix the same backtest is used to confirm the new numbers.
HOME_FACTOR = _env("HOME_FACTOR", 1.12, float)
AWAY_FACTOR = _env("AWAY_FACTOR", 0.94, float)
SHRINK_MATCHES = _env("SHRINK_MATCHES", 12.0, float)           # shrinkage strength toward league mean
# Starting point measured by the walk-forward backtest on the demo league
# (python3 ~/work/sweep.py style sweep): heavier shrinkage (20-30) was the most
# stable but is nearly the same as ignoring team form, lighter (6) scored worse
# than the naive baseline. Re-run `python cli.py backtest --sweep` on real data
# before trusting any number here.
FORM_WINDOW = _env("FORM_WINDOW", 10, int)                     # matches used for attack/defence form
DIXON_COLES_RHO = _env("DIXON_COLES_RHO", -0.05, float)
MAX_GOALS = _env("MAX_GOALS", 12, int)

# ---------------------------------------------------------------- value / staking
EV_THRESHOLD = _env("EV_THRESHOLD", 0.05, float)               # 5% edge minimum
MIN_ODDS = _env("MIN_ODDS", 1.35, float)                       # ignore short prices
MAX_ODDS = _env("MAX_ODDS", 12.0, float)
MAX_EV_ACCEPT = _env("MAX_EV_ACCEPT", 0.60, float)             # >60% edge => almost certainly a stale/bad price
UNITS_PER_BET_PCT = _env("UNITS_PER_BET_PCT", 1.0, float)      # % of bankroll per bet (flat mode)
STAKING_MODE = _env("STAKING_MODE", "flat", str)               # flat | kelly | half_kelly
KELLY_FRACTION = _env("KELLY_FRACTION", 0.5, float)
BANKROLL = _env("BANKROLL", 10000.0, float)
MAX_BETS_PER_DAY = _env("MAX_BETS_PER_DAY", 8, int)
MAX_DAILY_LOSS_PCT = _env("MAX_DAILY_LOSS_PCT", 5.0, float)    # stop suggesting after -5% day
MIN_MINUTES_BEFORE_KICKOFF = _env("MIN_MINUTES_BEFORE_KICKOFF", 5, int)
DEDUPE_MINUTES = _env("DEDUPE_MINUTES", 45, int)               # don't repeat the same pick within this window
MARKETS = [m.strip().upper() for m in _env("MARKETS", "HAD,TTG", str).split(",") if m.strip()]

# ---------------------------------------------------------------- scheduling (HK time)
SCAN_OFFSETS_MIN = _env("SCAN_OFFSETS_MIN", "60,30", str)      # minutes before kickoff to scan
SYNC_FIXTURES_HOUR = _env("SYNC_FIXTURES_HOUR", 6, int)        # daily schedule refresh
SYNC_ODDS_HOURS = _env("SYNC_ODDS_HOURS", "9,12,18,21", str)   # daytime odds refresh
SETTLE_MINUTE = _env("SETTLE_MINUTE", "15,45", str)            # settle results twice an hour
HEARTBEAT_HOUR = _env("HEARTBEAT_HOUR", 8, int)
WEEKLY_REPORT_DOW = _env("WEEKLY_REPORT_DOW", 0, int)          # 0 = Monday
WEEKLY_REPORT_HOUR = _env("WEEKLY_REPORT_HOUR", 9, int)
ODDS_MOVE_ALERT = _env("ODDS_MOVE_ALERT", 0.10, float)         # 10% odds move -> alert

# ---------------------------------------------------------------- auto follow (paper trading)
# When true the system *books* every suggested bet automatically as a paper
# bet (no money), settles it from results and reports ROI. This is what makes
# a real-data track record possible without you clicking anything.
AUTO_FOLLOW = _env("AUTO_FOLLOW", True, bool)
PAPER_BANKROLL = _env("PAPER_BANKROLL", BANKROLL, float)

# ---------------------------------------------------------------- scraper
HKJC_GRAPHQL_URL = _env("HKJC_GRAPHQL_URL", "https://info.cld.hkjc.com/graphql/base/")
HKJC_SCHEDULE_URL = _env("HKJC_SCHEDULE_URL", "https://bet.hkjc.com/football/schedule.aspx")
HKJC_ODDS_URL = _env("HKJC_ODDS_URL", "https://bet.hkjc.com/football/odds.aspx")
HTTP_TIMEOUT = _env("HTTP_TIMEOUT", 20, int)
HTTP_SLEEP = _env("HTTP_SLEEP", 1.2, float)      # politeness delay between requests
HTTP_RETRIES = _env("HTTP_RETRIES", 3, int)
USER_AGENT = _env("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")

# optional third-party data (only used if you give a key)
API_FOOTBALL_KEY = _env("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = _env("API_FOOTBALL_HOST", "v3.football.api-sports.io")

DRY_RUN = _env("DRY_RUN", False, bool)           # no telegram sends, no writes to bets

def summary():
    return {
        "version": VERSION,
        "markets": MARKETS,
        "ev_threshold": EV_THRESHOLD,
        "staking_mode": STAKING_MODE,
        "bankroll": BANKROLL,
        "auto_follow": AUTO_FOLLOW,
        "scan_offsets_min": SCAN_OFFSETS_MIN,
        "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "db_path": DB_PATH,
    }

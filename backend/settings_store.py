"""Runtime settings: change thresholds from Telegram without redeploying.

Values live in the DB and are applied onto the ``config`` module at startup
(and after every /set), so the rest of the code keeps reading ``config.X``.
"""
import config
import db

# keys that may be tuned at runtime, with their type
TUNABLE = {
    "EV_THRESHOLD": float,
    "MIN_ODDS": float,
    "MAX_ODDS": float,
    "MAX_EV_ACCEPT": float,
    "UNITS_PER_BET_PCT": float,
    "STAKING_MODE": str,
    "KELLY_FRACTION": float,
    "BANKROLL": float,
    "MAX_BETS_PER_DAY": int,
    "MAX_DAILY_LOSS_PCT": float,
    "AUTO_FOLLOW": bool,
    "MIN_MINUTES_BEFORE_KICKOFF": int,
    "FORM_WINDOW": int,
    "HOME_FACTOR": float,
    "AWAY_FACTOR": float,
    "LEAGUE_BASE_GOALS": float,
    "SHRINK_MATCHES": float,
    "DIXON_COLES_RHO": float,
    "ODDS_MOVE_ALERT": float,
    "TELEGRAM_CHAT_ID": str,
    "MARKETS": str,
}


def coerce(key, value):
    cast = TUNABLE.get(key)
    if cast is None:
        return str(value)
    if cast is bool:
        return str(value).strip().lower() in ("1", "true", "yes", "y", "on")
    if cast is float:
        return float(value)
    if cast is int:
        return int(float(value))
    if key == "MARKETS":
        return ",".join(p.strip().upper() for p in str(value).split(",") if p.strip())
    return str(value)


def apply_overrides():
    """Push DB values onto the config module. Returns the keys applied."""
    applied = []
    for row in db.q("SELECT key, value FROM settings"):
        key = row["key"]
        if key not in TUNABLE:
            continue
        try:
            setattr(config, key, coerce(key, row["value"]))
            applied.append(key)
        except (TypeError, ValueError):
            continue
    return applied


def set_value(key, value):
    key = key.upper()
    if key not in TUNABLE:
        raise KeyError(f"{key} 唔可以改（可改：{', '.join(sorted(TUNABLE))}）")
    val = coerce(key, value)
    db.set_setting(key, val)
    setattr(config, key, val)
    return val


def get_value(key):
    key = key.upper()
    return getattr(config, key, None)


def dump():
    return {k: getattr(config, k, None) for k in sorted(TUNABLE)}

"""Stake sizing and the three risk layers carried over from TyLove:
   (1) per-bet risk  (2) per-day bet count / exposure  (3) daily loss stop.
"""
import config
import db
from timeutil import today_hk, day_bounds_hk
from value import kelly_fraction


def current_bankroll(mode="paper"):
    """Start bankroll + realised P&L of that book."""
    start = config.PAPER_BANKROLL if mode == "paper" else config.BANKROLL
    row = db.q("""SELECT COALESCE(SUM(CASE WHEN status IN ('won','lost','push','void')
                                    THEN COALESCE(payout,0) - stake ELSE 0 END),0) AS pnl
                  FROM bets WHERE mode=?""", (mode,), one=True)
    return float(start) + float(row["pnl"] or 0)


def day_exposure(mode="paper"):
    start, end = day_bounds_hk()
    rows = db.q("""SELECT COUNT(*) n, COALESCE(SUM(stake),0) s
                   FROM bets WHERE mode=? AND created_at>=? AND created_at<?""",
                (mode, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")), one=True)
    pnl = db.q("""SELECT COALESCE(SUM(COALESCE(payout,0)-stake),0) p FROM bets
                  WHERE mode=? AND settled_at>=? AND settled_at<? AND status IN ('won','lost','push')""",
               (mode, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")), one=True)
    return {"count": rows["n"], "stake": float(rows["s"] or 0), "pnl": float(pnl["p"] or 0)}


def risk_check(mode="paper"):
    d = day_exposure(mode)
    bank = current_bankroll(mode)
    if config.MAX_BETS_PER_DAY and d["count"] >= config.MAX_BETS_PER_DAY:
        return False, f"今日已 {d['count']} 注，達到上限 {config.MAX_BETS_PER_DAY}"
    if bank > 0 and d["pnl"] <= -(config.MAX_DAILY_LOSS_PCT / 100.0) * bank:
        return False, f"今日已蝕 {d['pnl']:.0f}（上限 -{config.MAX_DAILY_LOSS_PCT}%），停手"
    return True, "ok"


def size(ev_pct, model_p, odds, mode="paper", thresholds=None):
    """Return the stake in currency for one bet."""
    bank = current_bankroll(mode)
    if config.STAKING_MODE == "flat":
        frac = config.UNITS_PER_BET_PCT / 100.0
    elif config.STAKING_MODE in ("kelly", "half_kelly"):
        frac = kelly_fraction(model_p, odds, config.KELLY_FRACTION if config.STAKING_MODE == "half_kelly" else 1.0)
        frac = min(frac, config.UNITS_PER_BET_PCT / 100.0)   # never above the unit cap
    else:
        frac = config.UNITS_PER_BET_PCT / 100.0
    stake = round(bank * frac, 2)
    return max(0.0, stake), frac


def record_bet(value_bet, mode="paper", odds=None, stake=None, note=""):
    """落一張注單。

    同一場 + 同一盤口 + 同一選項 + 同一 mode 只會有一張（由 db 的 unique index
    保證）。若已經存在，回傳 None —— 呼叫方要當作「冇新注」處理。
    """
    from timeutil import now_hk
    stake = stake if stake is not None else value_bet.get("stake")
    odds = odds or value_bet.get("hkjc_odds")
    with db.cursor() as c:
        cur = c.execute("""INSERT OR IGNORE INTO bets (value_bet_id, match_id, created_at, mode, market, line, selection,
                                     odds_taken, stake, status, note, model_p, ev_pct, model_version)
                    VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?)""",
                 (value_bet.get("id"), value_bet.get("match_id"), now_hk().strftime("%Y-%m-%d %H:%M:%S"),
                  mode, value_bet.get("market"), value_bet.get("line"), value_bet.get("selection"),
                  odds, stake, note, value_bet.get("model_p"), value_bet.get("ev_pct"),
                  __import__("config").VERSION))
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

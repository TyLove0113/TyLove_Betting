"""Settlement: turn finished matches into won/lost bets, P&L, CLV and history.

This closes the loop the old programme never closed automatically: results go
in, bets get graded, the realised record accumulates, and the same results are
written into ``team_matches`` so tomorrow's model is trained on today's data.
"""
import config
import db
import hkjc
from timeutil import now_hk, parse_hk, today_hk


def enter_result(match_id, home_goals, away_goals, ht=None, source="manual"):
    """Record a final score (manual entry from Telegram included)."""
    now = now_hk().strftime("%Y-%m-%d %H:%M:%S")
    db.ex("""INSERT INTO results (match_id, home_goals, away_goals, total, ht_home, ht_away, source, fetched_at)
             VALUES (?,?,?,?,?,?,?,?)
             ON CONFLICT(match_id) DO UPDATE SET home_goals=excluded.home_goals,
               away_goals=excluded.away_goals, total=excluded.total, fetched_at=excluded.fetched_at""",
          (match_id, int(home_goals), int(away_goals), int(home_goals) + int(away_goals),
           (ht or [None, None])[0], (ht or [None, None])[1], source, now))
    f = db.q("SELECT * FROM fixtures WHERE match_id=?", (match_id,), one=True)
    if f:
        db.ex("UPDATE fixtures SET home_score=?, away_score=?, status='finished' WHERE match_id=?",
              (int(home_goals), int(away_goals), match_id))
        _into_history(f, int(home_goals), int(away_goals), source)
    return settle_match(match_id)


def _into_history(fixture, hg, ag, source):
    played_on = (parse_hk(fixture["kickoff_hk"]) or now_hk()).strftime("%Y-%m-%d")
    db.ex("""INSERT OR IGNORE INTO team_matches
             (played_on, league, home_team, away_team, home_goals, away_goals, source)
             VALUES (?,?,?,?,?,?,?)""",
          (played_on, fixture.get("league") or "", fixture["home_team"], fixture["away_team"],
           hg, ag, source))


def fetch_results_from_source(date=None, days_back=3):
    rows, note = hkjc.fetch_results(date, days_back=days_back)
    saved = 0
    for r in rows:
        if db.q("SELECT 1 FROM results WHERE match_id=?", (r["match_id"],), one=True):
            continue
        try:
            enter_result(r["match_id"], r["home_goals"], r["away_goals"], source="hkjc")
            saved += 1
        except Exception:                                          # noqa: BLE001
            continue
    db.log_job("fetch_results", "ok" if saved else "empty", f"saved={saved} note={note}")
    return {"saved": saved, "note": note}


def settle_match(match_id):
    """Grade every open bet on this match. Idempotent."""
    res = db.q("SELECT * FROM results WHERE match_id=?", (match_id,), one=True)
    if not res:
        return {"settled": 0, "reason": "no result"}
    hg, ag = res["home_goals"], res["away_goals"]
    total = hg + ag
    bets = db.q("SELECT * FROM bets WHERE match_id=? AND status='open'", (match_id,))
    n = 0
    for b in bets:
        status, payout = _grade(b, hg, ag, total)
        closing = _closing_odds(match_id, b["market"], b["line"], b["selection"])
        clv = None
        if closing and b["odds_taken"]:
            clv = (b["odds_taken"] / closing) - 1.0
        db.ex("""UPDATE bets SET status=?, payout=?, closing_odds=?, clv_pct=?, settled_at=?
                 WHERE id=?""",
              (status, payout, closing, clv, now_hk().strftime("%Y-%m-%d %H:%M:%S"), b["id"]))
        n += 1
    return {"settled": n, "score": f"{hg}-{ag}"}


def _grade(bet, hg, ag, total):
    market, sel, stake = bet["market"], bet["selection"], bet["stake"] or 0
    odds = bet["odds_taken"]
    if not odds:                       # 冇記錄賠率就退注（唔好計爆）
        return "void", stake
    if market == "HAD":
        outcome = "H" if hg > ag else ("D" if hg == ag else "A")
        win = (sel == outcome)
    elif market == "TTG":
        raw = str(bet.get("line") or "")
        if "/" in raw:                       # 2.5/3.0 之類：拆兩個半球，各佔一半注碼
            a_, b_ = [float(x) for x in raw.split("/")[:2]]
            pay = 0.0
            for ln in (a_, b_):
                half = stake / 2.0
                if total > ln:
                    win_half = (sel == "OVER")
                elif total == ln:
                    pay += half                # push，退返半注
                    continue
                else:
                    win_half = (sel == "UNDER")
                pay += half * odds if win_half else 0.0
            return ("won" if pay > stake else "lost" if pay < stake else "push"), round(pay, 2)
        try:
            line = float(raw)
        except (TypeError, ValueError):
            return "void", stake
        over = total > line
        win = (sel == "OVER" and over) or (sel == "UNDER" and not over)
        if total == line:                      # whole-number line -> push
            return "push", stake
    else:
        return "void", stake
    return ("won", round(stake * odds, 2)) if win else ("lost", 0.0)


def _closing_odds(match_id, market, line, selection):
    r = db.q("""SELECT odds FROM odds_snapshots
                WHERE match_id=? AND market=? AND line=? AND selection=? AND is_closing=1
                ORDER BY taken_at DESC LIMIT 1""", (match_id, market, line, selection), one=True)
    if r:
        return r["odds"]
    r = db.q("""SELECT odds FROM odds_snapshots
                WHERE match_id=? AND market=? AND line=? AND selection=?
                ORDER BY taken_at DESC LIMIT 1""", (match_id, market, line, selection), one=True)
    return r["odds"] if r else None


def settle_pending(days_back=2):
    """Catch up on any fixture that has finished but has no result yet."""
    rows = db.q("""SELECT DISTINCT b.match_id FROM bets b
                   JOIN results r ON r.match_id=b.match_id
                   WHERE b.status='open'""")
    out = []
    for r in rows:
        out.append(settle_match(r["match_id"]))
    return out


def capture_closing(window_min=25):
    """開賽前捕捉收市賠率 → 計 CLV（市場同唔同意你嘅價）。"""
    from timeutil import now_hk, parse_hk
    from datetime import timedelta
    lo = (now_hk() + timedelta(minutes=0)).strftime("%Y-%m-%d %H:%M")
    hi = (now_hk() + timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M")
    rows = db.q("""SELECT b.id, b.match_id, b.market, b.line, b.selection, b.odds_taken
                   FROM bets b JOIN fixtures f ON f.match_id=b.match_id
                   WHERE b.status='open' AND b.closing_odds IS NULL
                     AND f.kickoff_hk BETWEEN ? AND ?""", (lo, hi))
    n = 0
    for r in rows:
        cur = db.latest_odds(r["match_id"], market=r["market"])
        hit = [x for x in cur if (x["selection"] or "").upper() == (r["selection"] or "").upper()
               and str(x["line"] or "") == str(r["line"] or "")]
        if not hit:
            continue
        close = hit[0]["odds"]
        clv = (float(close) / float(r["odds_taken"]) - 1) if r["odds_taken"] else None
        db.ex("UPDATE bets SET closing_odds=?, clv_pct=? WHERE id=?", (close, clv, r["id"]))
        n += 1
    return n

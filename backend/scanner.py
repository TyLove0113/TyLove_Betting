"""The scanner: the job that actually finds money.

Flow per run
  1. take today's fixtures with a kickoff inside the scan window
  2. build a prediction (Poisson / Dixon-Coles)
  3. price it against the latest HKJC odds (de-vigged)
  4. filter by EV / odds bounds / risk limits
  5. store the value bets, push them to Telegram
  6. if AUTO_FOLLOW is on, book them automatically as paper bets so that a
     real track record builds up even when you are asleep
"""
import config
import db
import engine
import hkjc
import notifier
import staking
import value
from timeutil import now_hk, minutes_until, today_hk, parse_hk, to_hk

SEL_LABEL = {"H": "主勝", "D": "和", "A": "客勝", "OVER": "大", "UNDER": "細"}
MARKET_LABEL = {"HAD": "主客和", "TTG": "入球大細"}


def _recent_keys(minutes=None):
    """Picks already produced recently, so a 5-minute scan does not spam."""
    from datetime import timedelta
    minutes = minutes or config.DEDUPE_MINUTES
    since = (now_hk() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.q("""SELECT match_id, market, line, selection FROM value_bets WHERE created_at>=?""", (since,))
    return {(r["match_id"], r["market"], r["line"], r["selection"]) for r in rows}


def _open_paper_keys():
    rows = db.q("SELECT match_id, market, line, selection FROM bets WHERE status='open' AND mode='paper'")
    return {(r["match_id"], r["market"], r["line"], r["selection"]) for r in rows}


# --------------------------------------------------------------- sync helpers
def sync_fixtures(date=None):
    date = date or today_hk().strftime("%Y-%m-%d")
    fixtures, note = hkjc.fetch_schedule(date)
    saved = 0
    for f in fixtures:
        if not f.get("kickoff_hk"):
            continue
        league = f.get("league") or ""
        mid = hkjc.make_match_id(league, f["kickoff_hk"], f["home_team"], f["away_team"])
        db.upsert_fixture({
            "match_id": mid, "source_id": f.get("source_id"), "league": league,
            "kickoff_hk": f["kickoff_hk"], "home_team": f["home_team"], "away_team": f["away_team"],
            "home_team_id": f["home_team"], "away_team_id": f["away_team"],
            "status": "scheduled", "markets": f.get("markets", ""),
        })
        saved += 1
    db.log_job("sync_fixtures", "ok" if saved else "empty", f"{date} saved={saved} note={note}")
    return {"saved": saved, "note": note, "date": date}


def refresh_odds(date=None):
    start, end = _window_bounds(fwd_min=360)
    fixtures = db.q("SELECT * FROM fixtures WHERE kickoff_hk>=? AND kickoff_hk<? ORDER BY kickoff_hk",
                    (start, end))
    if not fixtures:
        db.log_job("refresh_odds", "empty", "no fixtures")
        return {"saved": 0, "note": "no fixtures"}
    ids = [f["match_id"] for f in fixtures]
    rows, note = hkjc.fetch_odds(ids)
    n = db.save_odds_batch(rows) if rows else 0
    _mark_opening()
    db.log_job("refresh_odds", "ok" if n else "empty", f"saved={n} note={note}")
    return {"saved": n, "note": note, "fixtures": len(fixtures)}


def _day_bounds(date_str):
    d = parse_hk(date_str).date() if parse_hk(date_str) else today_hk()
    start = parse_hk(f"{d} 00:00")
    from datetime import timedelta
    return start.strftime("%Y-%m-%d %H:%M"), (start + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")


def _window_bounds(back_min=90, fwd_min=300, now=None):
    """滾動時間窗（唔用日曆日）。

    馬會足球日會跨過午夜：用「今日 00:00–24:00」做界，午夜前後就會漏掉
    凌晨開賽嘅場（掃描變 checked 0、賠率亦唔會更新）。改成以現在為中心嘅窗口。
    """
    now = now or now_hk()
    from datetime import timedelta
    return ((now - timedelta(minutes=back_min)).strftime("%Y-%m-%d %H:%M"),
            (now + timedelta(minutes=fwd_min)).strftime("%Y-%m-%d %H:%M"))


def _mark_opening():
    db.ex("""UPDATE odds_snapshots SET is_opening=1
             WHERE id IN (SELECT MIN(id) FROM odds_snapshots GROUP BY match_id, market, line, selection)""")


def mark_closing():
    """Flag the last snapshot taken before kickoff as the closing price (for CLV)."""
    rows = db.q("SELECT match_id, kickoff_hk FROM fixtures WHERE status IN ('scheduled','live')")
    n = 0
    for f in rows:
        ko = parse_hk(f["kickoff_hk"])
        if not ko or minutes_until(ko) > 0:
            continue
        db.ex("""UPDATE odds_snapshots SET is_closing=1
                 WHERE id IN (SELECT MAX(id) FROM odds_snapshots
                              WHERE match_id=? AND taken_at <= ? GROUP BY market, line, selection)""",
              (f["match_id"], f["kickoff_hk"]))
        db.ex("UPDATE fixtures SET status='live' WHERE match_id=?", (f["match_id"],))
        n += 1
    return n


def odds_move_watch():
    """Alert when a price moves sharply - usually news (lineups, injury, weather)."""
    alerts = []
    rows = db.q("""SELECT o.match_id, o.market, o.line, o.selection, o.odds, o.taken_at,
                          f.home_team, f.away_team, f.kickoff_hk
                   FROM odds_snapshots o JOIN fixtures f ON f.match_id=o.match_id
                   WHERE o.taken_at >= datetime('now','-3 hours')
                   ORDER BY o.match_id, o.market, o.line, o.selection, o.taken_at""")
    hist = {}
    for r in rows:
        key = (r["match_id"], r["market"], r["line"], r["selection"])
        prev = hist.get(key)
        if prev and prev["odds"] and abs(r["odds"] / prev["odds"] - 1) >= config.ODDS_MOVE_ALERT:
            alerts.append((prev, r))
        hist[key] = r
    lines = []
    for prev, cur in alerts[:12]:
        move = (cur["odds"] / prev["odds"] - 1) * 100
        lines.append(f"• {cur['home_team']} vs {cur['away_team']} "
                     f"{MARKET_LABEL.get(cur['market'], cur['market'])}"
                     f"{' ' + cur['line'] if cur['line'] else ''} "
                     f"{SEL_LABEL.get(cur['selection'], cur['selection'])} "
                     f"{prev['odds']:.2f} → {cur['odds']:.2f} ({move:+.0f}%)")
    if lines:
        notifier.push("📈 <b>賠率異動</b>\n" + "\n".join(lines))
    return len(alerts)


# --------------------------------------------------------------------- scan
def scan(min_minutes=None, max_minutes=None, fixtures=None, dry_run=False, push=True):
    """Find value in matches starting inside the window. Returns a dict summary."""
    min_minutes = config.MIN_MINUTES_BEFORE_KICKOFF if min_minutes is None else min_minutes
    max_minutes = 240 if max_minutes is None else max_minutes
    now = now_hk()
    if fixtures is None:
        start, end = _window_bounds(fwd_min=max_minutes + 30)
        fixtures = db.q("""SELECT * FROM fixtures
                           WHERE kickoff_hk>=? AND kickoff_hk<? AND status='scheduled'
                           ORDER BY kickoff_hk""", (start, end))

    checked, picked, booked = 0, [], []
    can_bet, why = staking.risk_check("paper")
    auto_ok, auto_why = can_bet, why
    recent = _recent_keys()
    open_paper = _open_paper_keys()

    for f in fixtures:
        ko = parse_hk(f["kickoff_hk"])
        if not ko:
            continue
        mins = minutes_until(ko)
        if mins is None or mins < min_minutes or mins > max_minutes:
            continue
        odds_rows = db.latest_odds(f["match_id"])
        if not odds_rows:
            continue
        checked += 1
        pred = engine.predict_fixture(f)
        priced = value.market_view(f, pred, odds_rows)
        for row in priced:
            if row["selection"] == "INFO":
                continue
            if row["market"] not in config.MARKETS:
                continue
            if not row["is_value"]:
                continue
            key = (row["match_id"], row["market"], row["line"], row["selection"])
            if key in recent:
                continue
            stake, frac = staking.size(row["ev_pct"], row["model_p"], row["hkjc_odds"])
            vb_id = db.ex("""INSERT INTO value_bets (match_id, created_at, market, line, selection,
                                        model_p, fair_odds, hkjc_odds, ev_pct, kelly_pct, stake, reasons)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (row["match_id"], now.strftime("%Y-%m-%d %H:%M:%S"), row["market"], row["line"],
                           row["selection"], row["model_p"], row["fair_odds"], row["hkjc_odds"],
                           row["ev_pct"], row["kelly_pct"], stake,
                           f"pred {pred['note']} top(lambda {pred['lam_home']:.2f}/{pred['lam_away']:.2f})"))
            row["id"] = vb_id
            row["stake"] = stake
            picked.append(row)

            if config.AUTO_FOLLOW and can_bet and not dry_run and key not in open_paper:
                bet_id = staking.record_bet(row, mode="paper", stake=stake, note="auto-follow")
                booked.append({"bet_id": bet_id, "row": row})
                can_bet, why = staking.risk_check("paper")

    summary = {"checked": checked, "picked": len(picked), "booked": len(booked),
               "reason": why, "at": now.strftime("%Y-%m-%d %H:%M")}
    db.log_job("scan", "ok", str(summary))
    if push and picked:
        notifier.push(_format_picks(picked, booked, summary, open_paper, auto_ok, auto_why))
    return {"summary": summary, "picks": picked, "booked": booked}


def _format_picks(picks, booked, summary, open_keys=None, auto_ok=True, auto_why=""):
    booked_ids = {(b["row"]["match_id"], b["row"]["market"], b["row"]["line"], b["row"]["selection"])
                  for b in booked}
    open_keys = open_keys or set()
    lines = [f"💎 <b>價值投注</b>（{summary['at']}）",
             f"睇 {summary['checked']} 場 · 命中 {summary['picked']} 個價值", ""]
    for r in sorted(picks, key=lambda x: -x["ev_pct"])[:12]:
        f = r["fixture"]
        ko = to_hk(parse_hk(f["kickoff_hk"]))
        k = (f["match_id"], r["market"], r["line"], r["selection"])
        tag = ("🎯 新跟注" if k in booked_ids else
               "✅ 已跟注" if k in open_keys else "👀 只提示")
        lines.append(
            f"{tag}\n<b>{notifier.esc(f['home_team'])} vs {notifier.esc(f['away_team'])}</b>"
            f"（{f['league']}）\n"
            f"開賽 {ko.strftime('%m-%d %H:%M')} · {MARKET_LABEL.get(r['market'], r['market'])}"
            f"{' ' + str(r['line']) if r['line'] else ''} · <b>{SEL_LABEL.get(r['selection'], r['selection'])}</b>\n"
            f"模型 {r['model_p']*100:.1f}% ／ 公平賠率 {r['fair_odds']:.2f} ／ 馬會 {r['hkjc_odds']:.2f}\n"
            f"EV <b>{r['ev_pct']*100:+.1f}%</b> · 建議注碼 ${r['stake']:.0f}")
    if not booked:
        if not config.AUTO_FOLLOW:
            lines.append("\n（AUTO_FOLLOW 關咗 → 以上只提示；打 /settings 可以開返）")
        elif not auto_ok:
            lines.append(f"\n（未有新跟注：{auto_why or '風險限制'}）")
        else:
            lines.append("\n（以上注項之前已經跟咗，今次冇新注）")
    return "\n".join(lines)

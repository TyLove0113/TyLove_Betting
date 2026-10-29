"""The book: reporting, ROI, CLV, equity. Two separate lines by design:
   paper (what the system would have done on its own) and real (what you confirmed).
"""
import config
import db
from timeutil import now_hk, today_hk, day_bounds_hk, parse_hk, to_hk, fmt_hk
from scanner import MARKET_LABEL, SEL_LABEL


def bcode(r):
    """注項編號：真實 R38、虛擬 P12（兩個系統分開睇）。"""
    try:
        return ("R" if r["mode"] == "real" else "P") + str(r["id"])
    except Exception:
        return f"#{r.get('id')}"


def summary(mode=None, days=None, since=None):
    where, args = [], []
    if mode:
        where.append("mode=?")
        args.append(mode)
    if since:
        where.append("created_at>=?")
        args.append(since)
    if days:
        where.append("created_at>=?")
        from datetime import timedelta
        args.append((now_hk() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"))
    w = (" WHERE " + " AND ".join(where)) if where else ""
    row = db.q(f"""SELECT
                     COUNT(*) n,
                     SUM(CASE WHEN status IN ('won','lost') THEN 1 ELSE 0 END) graded,
                     SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won,
                     COALESCE(SUM(stake),0) staked,
                     COALESCE(SUM(CASE WHEN status IN ('won','lost','push') THEN COALESCE(payout,0) ELSE 0 END),0) returned,
                     COALESCE(SUM(CASE WHEN status IN ('won','lost','push') THEN COALESCE(payout,0)-stake ELSE 0 END),0) pnl,
                     AVG(odds_taken) avg_odds,
                     AVG(CASE WHEN clv_pct IS NOT NULL THEN clv_pct END) avg_clv
                   FROM bets{w}""", tuple(args), one=True)
    n = row["n"] or 0
    staked = float(row["staked"] or 0)
    pnl = float(row["pnl"] or 0)
    return {
        "bets": n,
        "open": db.q(f"SELECT COUNT(*) c FROM bets{w}{' AND' if w else ' WHERE'} status='open'",
                     tuple(args), one=True)["c"],
        "graded": row["graded"] or 0,
        "won": row["won"] or 0,
        "hit_rate": (row["won"] / row["graded"]) if row["graded"] else None,
        "staked": round(staked, 2),
        "returned": round(float(row["returned"] or 0), 2),
        "pnl": round(pnl, 2),
        "roi_pct": (pnl / staked) if staked else None,
        "avg_odds": row["avg_odds"],
        "avg_clv_pct": row["avg_clv"],
    }


def _line(label, s):
    roi = f"{s['roi_pct']*100:+.2f}%" if s["roi_pct"] is not None else "n/a"
    hit = f"{s['hit_rate']*100:.1f}%" if s["hit_rate"] is not None else "n/a"
    clv = f"{s['avg_clv_pct']*100:+.2f}%" if s["avg_clv_pct"] is not None else "n/a"
    return (f"{label}：{s['bets']} 注（{s['open']} 未結算）\n"
            f"  投注 ${s['staked']:.0f} · 回收 ${s['returned']:.0f} · 盈虧 <b>${s['pnl']:+.0f}</b>\n"
            f"  ROI <b>{roi}</b> · 命中 {hit} · 平均賠率 {s['avg_odds']:.2f} · CLV {clv}" if s["avg_odds"]
            else f"{label}：暫無紀錄")


def report_text(days=None, title=None):
    title = title or ("今日報告" if not days else f"近 {days} 日報告")
    paper = summary(mode="paper", days=days)
    real = summary(mode="real", days=days)
    bank = config.PAPER_BANKROLL + paper["pnl"]
    lines = [f"📊 <b>{title}</b>（{now_hk().strftime('%Y-%m-%d %H:%M')}）", ""]
    lines.append(_line("🤖 自動跟注（paper）", paper))
    lines.append("")
    if real["bets"]:
        lines.append(_line("✍️ 你實際落注（real）", real))
        lines.append("")
    else:
        lines.append("✍️ 你實際落注（real）：暫無紀錄（用 /bet 記錄）\n")
    lines.append(f"💰 自動跟注本金：${config.PAPER_BANKROLL:.0f} → <b>${bank:.0f}</b>")
    pending = db.q("""SELECT COUNT(*) c FROM bets WHERE status='open' AND mode='paper'""", one=True)["c"]
    lines.append(f"⏳ 未結算：{pending} 注")
    return "\n".join(lines)


def daily_report(push=True):
    text = report_text(days=1, title="今日報告")
    if push:
        import notifier
        notifier.push(text)
    return text


def weekly_report(push=True):
    text = report_text(days=7, title="本週報告")
    top = top_bets(days=7)
    if top:
        text += "\n\n<b>本週最佳／最差</b>\n" + top
    if push:
        import notifier
        notifier.push(text)
    return text


def daily_settle(push=True):
    """Settle anything that has a result, then push what changed."""
    import settlement
    got = settlement.fetch_results_from_source()
    out = settlement.settle_pending()
    from datetime import timedelta
    since = (now_hk() - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.q("""SELECT b.*, f.home_team, f.away_team FROM bets b
                   LEFT JOIN fixtures f ON f.match_id=b.match_id
                   WHERE b.settled_at>=? AND b.status IN ('won','lost','push')""", (since,))
    graded = len(rows)
    if graded and push:
        import notifier
        lines = [f"🧾 <b>結算</b>（{graded} 注）"]
        pnl = 0.0
        for r in rows:
            d = (r["payout"] or 0) - (r["stake"] or 0)
            pnl += d
            lines.append(f"{'✅' if d > 0 else '❌'} {r['home_team']} vs {r['away_team']} "
                         f"{MARKET_LABEL.get(r['market'], r['market'])} "
                         f"{SEL_LABEL.get(r['selection'], r['selection'])} "
                         f"@{r['odds_taken']:.2f} ${d:+.0f}")
        lines.append(f"\n合計 <b>${pnl:+.0f}</b>")
        notifier.push("\n".join(lines))
    db.log_job("settle", "ok", f"graded={graded}")
    return {"graded": graded, "detail": out}


def top_bets(days=7, n=3):
    from datetime import timedelta
    since = (now_hk() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.q("""SELECT b.*, f.home_team, f.away_team FROM bets b
                   LEFT JOIN fixtures f ON f.match_id=b.match_id
                   WHERE b.settled_at>=? AND b.status IN ('won','lost')""", (since,))
    if not rows:
        return ""
    rows.sort(key=lambda r: (r["payout"] or 0) - (r["stake"] or 0), reverse=True)
    def fmt(r):
        pnl = (r["payout"] or 0) - (r["stake"] or 0)
        return (f"{'✅' if pnl > 0 else '❌'} {r['home_team']} vs {r['away_team']} "
                f"{MARKET_LABEL.get(r['market'], r['market'])}"
                f"{' ' + str(r['line']) if r['line'] else ''} {SEL_LABEL.get(r['selection'], r['selection'])} "
                f"@{r['odds_taken']:.2f} → ${pnl:+.0f}")
    best = "\n".join(fmt(r) for r in rows[:n])
    worst = "\n".join(fmt(r) for r in rows[-n:]) if len(rows) > n else ""
    return "\n".join(x for x in (best, worst) if x)


def open_bets_text(mode=None, limit=15):
    sql = "SELECT b.*, f.home_team, f.away_team, f.kickoff_hk FROM bets b " \
          "LEFT JOIN fixtures f ON f.match_id=b.match_id WHERE b.status='open'"
    args = []
    if mode:
        sql += " AND b.mode=?"
        args.append(mode)
    sql += " ORDER BY f.kickoff_hk LIMIT ?"
    args.append(limit)
    rows = db.q(sql, tuple(args))
    if not rows:
        return "冇未結算注項。"
    try:
        import live
        scores = {r["match_id"]: r for r in live.board([])}
    except Exception:
        scores = {}
    out = ["⏳ <b>未結算</b>"]
    for r in rows:
        s = scores.get(r["match_id"]) or {}
        sc = ""
        if s.get("home_score") is not None and s.get("away_score") is not None:
            sc = f" · <b>{s['live_status']} {s['home_score']}-{s['away_score']}</b>"
        elif s.get("live_status"):
            sc = f" · {s['live_status']}"
        out.append(f"{bcode(r)} {fmt_hk(parse_hk(r['kickoff_hk']))} {r['home_team']} vs {r['away_team']}"
                   f"{sc} "
                   f"{MARKET_LABEL.get(r['market'], r['market'])}"
                   f"{' ' + str(r['line']) if r['line'] else ''} {SEL_LABEL.get(r['selection'], r['selection'])} "
                   f"@{r['odds_taken']:.2f} ${r['stake']:.0f} [{r['mode']}]")
    return "\n".join(out)


def bet_line(r):
    """一注嘅中文描述（Telegram／網頁共用）。"""
    return (f"{MARKET_LABEL.get(r['market'], r['market'])}"
            f"{' ' + str(r['line']) if r.get('line') else ''} "
            f"{SEL_LABEL.get(r['selection'], r['selection'])} "
            f"@{float(r.get('odds_taken') or r.get('odds') or 0):.2f} "
            f"${float(r.get('stake') or 0):.0f}")


def equity_series(mode="paper", limit=200):
    rows = db.q("""SELECT settled_at, (COALESCE(payout,0)-stake) AS pnl FROM bets
                   WHERE mode=? AND settled_at IS NOT NULL AND status IN ('won','lost','push')
                   ORDER BY settled_at""", (mode,))
    bank = config.PAPER_BANKROLL if mode == "paper" else config.BANKROLL
    out = [{"t": rows[0]["settled_at"] if rows else None, "bank": bank}]
    for r in rows:
        bank += r["pnl"] or 0
        out.append({"t": r["settled_at"], "bank": round(bank, 2)})
    return out[-limit:]


def value_bets_today(order="ev"):
    ob = "f.kickoff_hk ASC" if order == "time" else "v.ev_pct DESC"
    start, end = day_bounds_hk()
    rows = db.q(f"""SELECT v.*, f.home_team, f.away_team, f.kickoff_hk, f.league
                   FROM value_bets v LEFT JOIN fixtures f ON f.match_id=v.match_id
                   WHERE v.created_at>=? AND v.created_at<?
                   ORDER BY {ob}""",
                (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")))
    best, out = {}, []
    for r in rows:
        k = (r["match_id"], r["market"], r["line"] or "", r["selection"])
        if k in best:
            if r["id"] > best[k]["id"]:                 # 留最新一次掃描嘅價
                out[out.index(best[k])] = r
                best[k] = r
            continue
        best[k] = r
        out.append(r)
    return out


def kill_switch(mode="paper"):
    """最近 N 注 ROI 差過門檻 → 自動關 AUTO_FOLLOW 並通知。"""
    import config, settings_store, notifier
    if not getattr(config, "KILL_SWITCH", False):
        return {"fired": False, "reason": "disabled"}
    n = int(getattr(config, "KILL_WINDOW", 30))
    rows = db.q("""SELECT status, stake, payout FROM bets
                   WHERE mode=? AND status IN ('won','lost','push')
                   ORDER BY id DESC LIMIT ?""", (mode, n))
    if len(rows) < max(10, n // 2):
        return {"fired": False, "reason": "樣本不足", "n": len(rows)}
    staked = sum(r["stake"] or 0 for r in rows)
    ret = sum(r["payout"] or 0 for r in rows)
    roi = (ret - staked) / staked if staked else 0
    if roi <= getattr(config, "KILL_ROI", -0.10):
        settings_store.set_value("AUTO_FOLLOW", "false")
        notifier.push(f"🛑 <b>自動停手</b>\n最近 {len(rows)} 注 ROI {roi*100:+.1f}%"
                     f"（門檻 {getattr(config, 'KILL_ROI', -0.1)*100:.0f}%）\n"
                     f"已關閉自動跟注。檢查完打 /resume 重開。")
        return {"fired": True, "roi": roi, "n": len(rows)}
    return {"fired": False, "roi": roi, "n": len(rows)}

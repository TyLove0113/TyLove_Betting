"""診斷：命中率 vs 模型機率、按盤口／EV／賠率分組，睇邊度漏水。"""
from datetime import timedelta

import db
import notifier
from timeutil import now_hk

SETTLED = ("won", "lost", "push", "void")


def _rows(mode="paper", days=60):
    since = (now_hk() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return db.q(f"""SELECT b.*, f.league, f.home_team, f.away_team,
                           v.model_p AS model_p, v.ev_pct AS ev_pct
                    FROM bets b LEFT JOIN fixtures f ON f.match_id=b.match_id
                    LEFT JOIN value_bets v ON v.id=b.value_bet_id
                    WHERE b.mode=? AND b.status IN ({','.join('?' * len(SETTLED))})
                      AND IFNULL(b.settled_at, b.created_at)>=?
                    ORDER BY b.id""", (mode, *SETTLED, since))


def _agg(rows):
    n = len(rows)
    staked = sum(r["stake"] or 0 for r in rows)
    ret = sum(r["payout"] or 0 for r in rows)
    won = sum(1 for r in rows if r["status"] == "won")
    exp = sum(r["model_p"] or 0 for r in rows if r["status"] in ("won", "lost"))
    graded = sum(1 for r in rows if r["status"] in ("won", "lost"))
    odds = [r["odds_taken"] or 0 for r in rows if r["odds_taken"]]
    return {
        "n": n,
        "staked": round(staked, 2),
        "returned": round(ret, 2),
        "pnl": round(ret - staked, 2),
        "roi": (ret - staked) / staked if staked else 0,
        "won": won,
        "graded": graded,
        "hit": won / graded if graded else 0,
        "model": exp / graded if graded else 0,
        "avg_odds": sum(odds) / len(odds) if odds else 0,
    }


def _fmt_line(label, rows):
    a = _agg(rows)
    if not a["n"]:
        return None
    return (f"{label}: {a['n']} 注 · 命中 {a['hit']*100:.0f}% "
            f"(模型 {a['model']*100:.0f}%) · ROI {a['roi']*100:+.1f}%")


def report(mode="paper", days=60):
    rows = _rows(mode, days)
    if not rows:
        return {"ok": False, "text": "未有已結算注項，跑多幾日先有得診斷。"}
    a = _agg(rows)
    out = [f"🔬 <b>診斷（{'真實' if mode == 'real' else '虛擬'} · 近 {days} 日）</b>",
           f"{a['n']} 注 · 注碼 ${a['staked']:.0f} · 回收 ${a['returned']:.0f} · "
           f"盈虧 <b>${a['pnl']:+.0f}</b> · ROI <b>{a['roi']*100:+.1f}%</b>",
           f"命中 {a['won']}/{a['graded']} = <b>{a['hit']*100:.1f}%</b> · "
           f"模型估 {a['model']*100:.1f}% · 差 {(a['hit']-a['model'])*100:+.1f}pt",
           f"平均賠率 {a['avg_odds']:.2f}", ""]

    by_mk = {}
    for r in rows:
        by_mk.setdefault(r["market"], []).append(r)
    out.append("<b>按盤口</b>")
    for k, rs in sorted(by_mk.items(), key=lambda x: -len(x[1])):
        out.append(_fmt_line(k, rs))

    out.append("")
    out.append("<b>按 EV 區間</b>")
    buckets = [("<10%", 0, 0.10), ("10-20%", 0.10, 0.20), ("20-30%", 0.20, 0.30), (">30%", 0.30, 9)]
    for name, lo, hi in buckets:
        rs = [r for r in rows if lo <= (r["ev_pct"] or 0) < hi]
        line = _fmt_line(name, rs)
        if line:
            out.append(line)

    out.append("")
    out.append("<b>按賠率</b>")
    for name, lo, hi in [("≤2.0", 0, 2.0), ("2.0-3.0", 2.0, 3.0), ("3.0-4.5", 3.0, 4.5), (">4.5", 4.5, 99)]:
        rs = [r for r in rows if lo <= (r["odds_taken"] or 0) < hi]
        line = _fmt_line(name, rs)
        if line:
            out.append(line)

    lg = {}
    for r in rows:
        lg.setdefault(r["league"] or "?", []).append(r)
    worst = sorted(((k, _agg(v)) for k, v in lg.items() if len(v) >= 3), key=lambda x: x[1]["pnl"])[:4]
    if worst:
        out.append("")
        out.append("<b>最差聯賽</b>")
        for k, s in worst:
            out.append(f"{k}: {s['n']} 注 · ${s['pnl']:+.0f} ({s['roi']*100:+.0f}%)")

    worst_bets = sorted([r for r in rows if r["status"] in ("won", "lost")],
                        key=lambda r: (r["payout"] or 0) - (r["stake"] or 0))[:5]
    if worst_bets:
        out.append("")
        out.append("<b>最傷 5 注</b>")
        for r in worst_bets:
            out.append(f"{r['id']} {r['home_team']} vs {r['away_team']} · {r['market']}"
                       f"{' ' + str(r['line']) if r['line'] else ''} {r['selection']} "
                       f"@{(r['odds_taken'] or 0):.2f} ${(r['payout'] or 0) - (r['stake'] or 0):+.0f}")

    return {"ok": True, "summary": a, "text": "\n".join(out), "rows": rows}


def coverage():
    """模型到底用咗幾多球隊資料（唔係靠猜）。"""
    import features
    tm = db.q("SELECT COUNT(*) c FROM team_matches", one=True)["c"]
    pr = db.q("""SELECT COUNT(*) n, SUM(baseline) b FROM predictions""", one=True) or {}
    n, b = (pr.get("n") or 0), (pr.get("b") or 0)
    rows = db.q("""SELECT * FROM fixtures WHERE status!='finished'
                   ORDER BY kickoff_hk LIMIT 60""")
    thin = ok = 0
    sample = []
    for f in rows:
        att_h, def_h, att_a, def_a, meta = features.strength_for_match(
            f["home_team"], f["away_team"], f.get("league") or "")
        if meta.get("thin"):
            thin += 1
        else:
            ok += 1
        if len(sample) < 3:
            sample.append((f, meta, att_h, def_h, att_a, def_a))
    out = ["🧠 <b>模型資料覆蓋</b>",
           f"隊史記錄：<b>{tm}</b> 場"
           + ("　⚠️ 空白 → 全部隊都用聯賽平均" if tm < 200 else ""),
           f"已存預測：{n} 條，其中用聯賽平均（baseline）：{b}"
           + (f"（{b / n * 100:.0f}%）" if n else ""),
           f"今日/未來 {len(rows)} 場：有隊史 {ok} 場 · 資料不足 {thin} 場", ""]
    for f, meta, ah, dh, aa, da in sample:
        out.append(f"{notifier.esc(f['home_team'])} vs {notifier.esc(f['away_team'])}"
                   f"（隊史 {meta['home_played']}/{meta['away_played']} 場）")
        out.append(f"　攻 {ah:.2f} 守 {dh:.2f} ｜ 攻 {aa:.2f} 守 {da:.2f}"
                   f"　聯賽平均 {meta.get('league_avg', 0):.2f} 球")
    if tm < 200:
        out += ["", "👉 打 <code>/backfill 120</code> 匯入歷史賽果，才會有真隊強度。"]
    return "\n".join(out)

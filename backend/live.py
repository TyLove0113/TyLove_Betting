"""即時比數：追住有落注嘅場，入球／完場即推送。"""
import db
import hkjc
import journal
import notifier
from timeutil import now_hk

STATUS_CH = {
    "PREEVENT": "未開賽", "KICKEDOFF": "開波", "FIRSTHALF": "上半場", "HALFTIME": "半場",
    "SECONDHALF": "下半場", "FIRSTHALFCOMPLETED": "半場", "SECONDHALFCOMPLETED": "下半場完",
    "EXTRA_TIME": "加時", "PENALTIES": "射十二碼", "FULLTIME": "完場", "FINISHED": "完場", "RESULT": "完場",
    "ABANDONED": "腰斬", "CANCELLED": "取消", "MATCHVOIDED": "取消", "POSTPONED": "延期",
}
LIVE = {"KICKEDOFF", "FIRSTHALF", "HALFTIME", "SECONDHALF"}


def _ensure():
    db.ex("""CREATE TABLE IF NOT EXISTS live_state (
        match_id TEXT PRIMARY KEY, status TEXT, home_score INTEGER, away_score INTEGER,
        updated_at TEXT)""")


def open_ids():
    return [r["match_id"] for r in db.q("SELECT DISTINCT match_id FROM bets WHERE status='open'")]


def snapshot(match_ids=None):
    ids = match_ids or open_ids()
    return {r["match_id"]: r for r in hkjc.fetch_live(ids)} if ids else {}


def board(match_ids=None):
    """有落注嘅場：注項 + 即時比數。"""
    snap = snapshot(match_ids)
    ids = list(snap) or (match_ids or [])
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = db.q(f"""SELECT b.*, f.home_team, f.away_team, f.kickoff_hk, f.league
                    FROM bets b LEFT JOIN fixtures f ON f.match_id=b.match_id
                    WHERE b.status='open' AND b.match_id IN ({ph})
                    ORDER BY f.kickoff_hk, b.id""", tuple(ids))
    for r in rows:
        s = snap.get(r["match_id"]) or {}
        r["live_status"] = STATUS_CH.get(s.get("status"), s.get("status") or "")
        r["home_score"], r["away_score"] = s.get("home_score"), s.get("away_score")
    return rows


def score_text(r):
    hs, as_ = r.get("home_score"), r.get("away_score")
    if hs is None or as_ is None:
        return r.get("live_status") or "未有比數"
    return f"{r.get('live_status', '')} {int(hs)}-{int(as_)}".strip()


def text(limit=15):
    rows = board()
    if not rows:
        return "而家冇未結算注項。"
    out, seen = ["⚽ <b>即時比數</b>（有落注嘅場）"], set()
    for r in rows[:limit * 2]:
        if r["match_id"] not in seen:
            seen.add(r["match_id"])
            out.append(f"<b>{notifier.esc(r['home_team'])} {score_text(r)} "
                       f"{notifier.esc(r['away_team'])}</b>")
        out.append(f"　#{r['id']} {journal.bet_line(r)} [{'實' if r['mode'] == 'real' else '虛'}]")
    return "\n".join(out)


def poll(push=True):
    """比數／狀態有變 → 推送（入球、完場）。"""
    _ensure()
    snap = snapshot()
    if not snap:
        return {"checked": 0, "changed": 0}
    changed = 0
    for mid, s in snap.items():
        new = ((s.get("status") or "").upper(), s.get("home_score"), s.get("away_score"))
        old = db.q("SELECT * FROM live_state WHERE match_id=?", (mid,), one=True)
        db.ex("""INSERT INTO live_state(match_id,status,home_score,away_score,updated_at)
                 VALUES(?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET status=excluded.status,
                 home_score=excluded.home_score, away_score=excluded.away_score,
                 updated_at=excluded.updated_at""",
              (mid, new[0], new[1], new[2], now_hk().strftime("%Y-%m-%d %H:%M:%S")))
        if not old:
            continue
        if (old["status"], old["home_score"], old["away_score"]) == new:
            continue
        changed += 1
        if not push or new[1] is None:
            continue
        f = db.q("SELECT * FROM fixtures WHERE match_id=?", (mid,), one=True) or {}
        bets = db.q("SELECT * FROM bets WHERE match_id=? AND status='open'", (mid,))
        goal = (old["home_score"], old["away_score"]) != (new[1], new[2])
        head = "⚽ <b>入球！</b>" if goal else "🏁 <b>狀態更新</b>"
        lines = [f"{head}{notifier.esc(f.get('home_team', ''))} {new[1]}-{new[2]} "
                 f"{notifier.esc(f.get('away_team', ''))}（{STATUS_CH.get(new[0], new[0])}）"]
        lines += [f"　#{b['id']} {journal.bet_line(b)}" for b in bets[:4]]
        notifier.push("\n".join(lines))
    db.log_job("live_poll", "ok", f"checked={len(snap)} changed={changed}")
    return {"checked": len(snap), "changed": changed}

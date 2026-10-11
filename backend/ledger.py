"""投注記錄（ledger）—— 好似交易日誌咁，逐注留底，方便你事後覆盤。

三個地方睇得到：
  py cli.py log              終端印表（最新 50 注）＋寫出檔案
  GET /ledger.csv            瀏覽器／Excel 下載（UTF-8 BOM，中文唔會亂碼）
  Telegram  /log [日數]      手機睇摘要＋最近注項

寫出嘅檔案（同資料庫同一個資料夾；Railway 上會落喺 /data/logs/）：
  logs/bets_ledger.csv        全部注項（每次重寫，跟最新狀態）
  logs/bets_YYYY-MM-DD.csv    當日注項
  logs/YYYY-MM-DD.md          當日日誌（摘要 + 逐注表）
"""
import csv
import io
import os
from datetime import timedelta

import config
import db
import journal
from scanner import MARKET_LABEL, SEL_LABEL
from timeutil import now_hk, today_hk

STATUS_LABEL = {"open": "⏳ 未結算", "won": "✅ 中", "lost": "❌ 輸",
                "push": "↩️ 退款", "void": "🚫 取消"}
MODE_LABEL = {"paper": "自動跟注", "real": "你實際落注"}
GRADED = ("won", "lost", "push", "void")

SQL = """SELECT b.*, f.home_team, f.away_team, f.league, f.kickoff_hk,
                f.home_score, f.away_score, f.status AS fixture_status,
                vb.ev_pct, vb.model_p, vb.fair_odds
         FROM bets b
         LEFT JOIN fixtures f ON f.match_id = b.match_id
         LEFT JOIN value_bets vb ON vb.id = b.value_bet_id"""

CSV_HEADER = ["編號", "模式", "建立時間", "開賽(HK)", "聯賽", "主隊", "客隊", "市場", "盤口",
              "選擇", "賠率", "注碼", "狀態", "盈虧", "EV%", "模型機率", "公平賠率",
              "收盤賠率", "CLV%", "比分", "結算時間", "備註"]


# ------------------------------------------------------------------ 查詢
def rows(mode=None, days=None, status=None, since=None, limit=None):
    where, args = [], []
    if mode:
        where.append("b.mode=?")
        args.append(mode)
    if status:
        where.append("b.status=?")
        args.append(status)
    if days:
        since = (now_hk() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if since:
        where.append("b.created_at>=?")
        args.append(since)
    sql = SQL + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY b.id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return db.q(sql, tuple(args))


def day_rows(day=None, mode=None):
    day = day or today_hk().strftime("%Y-%m-%d")
    return [r for r in rows(mode=mode) if (r.get("created_at") or "").startswith(day)]


# ------------------------------------------------------------------ 欄位格式化
def pnl(r):
    if r.get("status") not in GRADED:
        return None
    return round(float(r.get("payout") or 0) - float(r.get("stake") or 0), 2)


def match_name(r):
    return f"{r.get('home_team') or '?'} vs {r.get('away_team') or '?'}"


def market_text(r):
    t = MARKET_LABEL.get(r.get("market"), r.get("market") or "")
    if r.get("line"):
        t += f" {r['line']}"
    return f"{t} {SEL_LABEL.get(r.get('selection'), r.get('selection') or '')}".strip()


def score_text(r):
    if r.get("home_score") is None:
        return ""
    return f"{r['home_score']}-{r['away_score']}"


def status_text(r):
    return STATUS_LABEL.get(r.get("status"), r.get("status") or "")


def _num(v, digits=2, pct=False):
    if v is None:
        return ""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{v * 100:+.{digits}f}%" if pct else f"{v:.{digits}f}"


# ------------------------------------------------------------------ CSV
def csv_text(rows_, with_bom=True):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for r in rows_:
        p = pnl(r)
        w.writerow([
            r.get("id"), MODE_LABEL.get(r.get("mode"), r.get("mode")), r.get("created_at"),
            r.get("kickoff_hk"), r.get("league"), r.get("home_team"), r.get("away_team"),
            MARKET_LABEL.get(r.get("market"), r.get("market")), r.get("line"),
            SEL_LABEL.get(r.get("selection"), r.get("selection")),
            _num(r.get("odds_taken")), _num(r.get("stake"), 0), status_text(r),
            "" if p is None else f"{p:+.0f}",
            _num(r.get("ev_pct"), 1, True), _num(r.get("model_p"), 3), _num(r.get("fair_odds")),
            _num(r.get("closing_odds")), _num(r.get("clv_pct"), 1, True), score_text(r),
            r.get("settled_at"), r.get("note") or "",
        ])
    text = buf.getvalue()
    return ("\ufeff" + text) if with_bom else text


# ------------------------------------------------------------------ 終端表
def _w(s):
    """顯示寬度（中文算 2）。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _pad(s, width, right=False):
    s = str(s)
    gap = max(0, width - _w(s))
    return (" " * gap + s) if right else (s + " " * gap)


def table_text(rows_, limit=50):
    rows_ = rows_[:limit]
    if not rows_:
        return "（暫時冇注項記錄）"
    cols = [("#", 4, True), ("開賽", 12, False), ("賽事", 24, False), ("市場", 12, False),
            ("賠率", 6, True), ("注碼", 7, True), ("狀態", 8, False), ("盈虧", 8, True),
            ("CLV", 7, True)]
    out = ["  ".join(_pad(c[0], c[1], c[2]) for c in cols)]
    out.append("-" * (_w(out[0]) + 2 * len(cols)))
    for r in rows_:
        p = pnl(r)
        cells = [
            f"#{r.get('id')}", (r.get("kickoff_hk") or "")[5:16], match_name(r)[:24],
            market_text(r)[:12], _num(r.get("odds_taken")), f"${_num(r.get('stake'), 0)}",
            status_text(r), ("" if p is None else f"{p:+.0f}"),
            _num(r.get("clv_pct"), 1, True),
        ]
        out.append("  ".join(_pad(c, cols[i][1], cols[i][2]) for i, c in enumerate(cells)))
    return "\n".join(out)


def summary_text(mode=None, days=None, since=None):
    lines = []
    for m in ([mode] if mode else ["paper", "real"]):
        s = journal.summary(mode=m, days=days, since=since)
        roi = f"{s['roi_pct'] * 100:+.2f}%" if s["roi_pct"] is not None else "n/a"
        hit = f"{s['hit_rate'] * 100:.1f}%" if s.get("hit_rate") is not None else "n/a"
        lines.append(f"{MODE_LABEL.get(m, m)}：{s['bets']} 注（已結算 {s['graded']}）· "
                     f"投注 ${s['staked']:.0f} · 回收 ${s['returned']:.0f} · 盈虧 ${s['pnl']:+.0f} · "
                     f"ROI {roi} · 命中 {hit}")
    return "\n".join(lines)


def telegram_text(days=None, n=10):
    head = f"📒 <b>投注記錄</b>（近 {days} 日）" if days else "📒 <b>投注記錄</b>（全部）"
    body = [head, "", summary_text(days=days), ""]
    rs = rows(days=days, limit=n)
    if not rs:
        body.append("暫時冇注項。")
    else:
        for r in rs:
            p = pnl(r)
            tail = "未結算" if p is None else f"{p:+.0f}"
            body.append(f"#{r['id']} {MODE_LABEL.get(r['mode'], r['mode'])} "
                        f"{(r.get('kickoff_hk') or '')[:16]} {match_name(r)} "
                        f"{market_text(r)} @{_num(r.get('odds_taken'))} "
                        f"${_num(r.get('stake'), 0)} {status_text(r)} {tail}")
    return "\n".join(body)


# ------------------------------------------------------------------ markdown
def daily_markdown(day=None, mode=None):
    day = day or today_hk().strftime("%Y-%m-%d")
    rs = day_rows(day, mode=mode)
    since = f"{day} 00:00:00"
    lines = [f"# 投注記錄 {day}", ""]
    if mode:
        lines.append(f"（模式：{MODE_LABEL.get(mode, mode)}）")
    else:
        lines.append("（兩種帳目分開記錄：自動跟注 paper ／ 你實際落注 real）")
    lines += ["", "## 當日摘要", ""]
    for m in (["paper", "real"] if not mode else [mode]):
        s = journal.summary(mode=m, since=since)
        roi = f"{s['roi_pct'] * 100:+.2f}%" if s["roi_pct"] is not None else "n/a"
        hit = f"{s['hit_rate'] * 100:.1f}%" if s.get("hit_rate") is not None else "n/a"
        lines.append(f"- **{MODE_LABEL.get(m, m)}**：{s['bets']} 注（已結算 {s['graded']}）"
                     f"・投注 ${s['staked']:.0f}・回收 ${s['returned']:.0f}・盈虧 ${s['pnl']:+.0f}"
                     f"・ROI {roi}・命中 {hit}")
    lines += ["", "## 逐注", ""]
    if not rs:
        lines.append("（當日冇注項）")
    else:
        lines.append("| # | 模式 | 開賽 | 賽事 | 市場 | 賠率 | 注碼 | 狀態 | 盈虧 | EV | CLV | 比分 |")
        lines.append("|---|------|------|------|------|------|------|------|------|----|-----|------|")
        for r in rs:
            p = pnl(r)
            lines.append("| {id} | {mode} | {ko} | {mt} | {mk} | {od} | {st} | {stt} | {pn} | {ev} | {clv} | {sc} |".format(
                id=r.get("id"), mode=MODE_LABEL.get(r.get("mode"), r.get("mode")),
                ko=(r.get("kickoff_hk") or "")[5:16], mt=match_name(r), mk=market_text(r),
                od=_num(r.get("odds_taken")), st=_num(r.get("stake"), 0), stt=status_text(r),
                pn="" if p is None else f"{p:+.0f}", ev=_num(r.get("ev_pct"), 1, True),
                clv=_num(r.get("clv_pct"), 1, True), sc=score_text(r)))
    lines += ["", "---", f"_產生時間：{now_hk().strftime('%Y-%m-%d %H:%M')}（香港時間）_", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ 寫檔
def log_dir():
    folder = os.path.join(config.DATA_DIR, "logs")
    os.makedirs(folder, exist_ok=True)
    return folder


def write_files(day=None, mode=None):
    """寫出全部注項 CSV、當日 CSV、當日日誌；回傳檔案路徑清單。"""
    day = day or today_hk().strftime("%Y-%m-%d")
    folder = log_dir()
    all_csv = os.path.join(folder, "bets_ledger.csv")
    day_csv = os.path.join(folder, f"bets_{day}.csv")
    day_md = os.path.join(folder, f"{day}.md")

    with open(all_csv, "w", encoding="utf-8", newline="") as fh:
        fh.write(csv_text(rows(mode=mode)))
    with open(day_csv, "w", encoding="utf-8", newline="") as fh:
        fh.write(csv_text(day_rows(day, mode=mode)))
    with open(day_md, "w", encoding="utf-8") as fh:
        fh.write(daily_markdown(day, mode=mode))
    return [all_csv, day_csv, day_md]

"""Telegram bot: two-way, long polling, inline confirmation.

Carried over from TyLove: an alert is a *draft*; nothing becomes a real record
until you press a button or type /bet. Paper bets, by contrast, are booked by
the system automatically so a genuine track record builds up on its own.
"""
import threading
import time
import traceback

import requests

import config
import db
import hkjc
import journal
import notifier
import scanner
import settlement
import settings_store
import staking
from engine import predict_pair
from timeutil import now_hk, parse_hk, fmt_hk, today_hk
from scanner import MARKET_LABEL, SEL_LABEL

API = "https://api.telegram.org/bot{token}/{method}"
_running = threading.Event()
STATE_KEY = "telegram_offset"

HELP = """<b>HKJC 足球價值投注系統</b>

<b>📅 賽事同賠率</b>
/fixtures － 今日馬會賽程（連開咩盤）
/odds &lt;關鍵字&gt; － 睇某場最新賠率（例：/odds 曼聯）
/setodds 主隊 vs 客隊 | HAD 2.10 3.35 3.60 | TTG 2.5 OVER 1.85 UNDER 1.95
　↳ 馬會網頁抓唔到時，人手輸入賠率

<b>💎 價值同落注</b>
/value － 今日價值清單（EV 排序）
/scan － 即刻重新掃描（唔等排程）
/bet &lt;id&gt; &lt;注碼&gt; [@賠率] － 記錄你真實落咗嘅注
/open － 未結算注項
/result &lt;id&gt; &lt;主-客&gt; － 人手入賽果（例：/result 3 2-1）

<b>📊 成績</b>
/roi [日數] － ROI / 命中率 / CLV（例如 /roi 30）
/report － 今日報告
/weekly － 本週報告
/log [日數] － 投注記錄（帳目：逐注＋盈虧＋CLV）

<b>🔧 系統</b>
/status － 系統同排程狀態
/sync － 即刻同步賽程
/settle － 即刻結算
/probe － 測試馬會網頁端點（會存 raw 檔）
/sim A vs B － 即時試算任何兩隊（唔需要賠率）
/ratings [隊數] － 球隊強度排名
/settings － 睇現時參數
/set KEY VALUE － 改參數（例如 /set EV_THRESHOLD 0.06）
/help － 呢個清單
"""


# ------------------------------------------------------------------ plumbing
def _api(method, **payload):
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        r = requests.post(url, json=payload, timeout=35)
        return r.json()
    except requests.RequestException as e:
        print("[bot] api error", e)
        return None


def _reply(chat_id, text, buttons=None):
    return _api("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                disable_notification=False,
                reply_markup={"inline_keyboard": buttons} if buttons else None)


def _answer_cb(cb_id, text=""):
    _api("answerCallbackQuery", callback_query_id=cb_id, text=text[:200])


def run_forever():
    """Long-polling loop. Started from main.py on a daemon thread."""
    if not config.TELEGRAM_BOT_TOKEN:
        print("[bot] no token configured, polling disabled")
        return
    _running.set()
    print("[bot] polling started")
    while _running.is_set():
        try:
            offset = int(db.get_setting(STATE_KEY, 0) or 0)
            res = _api("getUpdates", offset=offset + 1, timeout=30,
                       allowed_updates=["message", "callback_query"])
            if not res or not res.get("ok"):
                time.sleep(3)
                continue
            for upd in res.get("result", []):
                db.set_setting(STATE_KEY, upd["update_id"])
                try:
                    handle_update(upd)
                except Exception as e:                             # noqa: BLE001
                    print("[bot] handler error", e)
                    traceback.print_exc()
                    if upd.get("message"):
                        _reply(upd["message"]["chat"]["id"], f"⚠️ 指令出錯：{notifier.esc(str(e)[:200])}")
        except Exception as e:                                     # noqa: BLE001
            print("[bot] loop error", e)
            time.sleep(5)


def stop():
    _running.clear()


def handle_update(upd):
    if "callback_query" in upd:
        return _handle_callback(upd["callback_query"])
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if not text or chat_id is None:
        return
    if not _authorised(chat_id):
        return _reply(chat_id, "⛔ 未授權。請將此 chat id 加入 TELEGRAM_CHAT_ID。")
    reply, buttons = dispatch(text)
    if reply:
        _reply(chat_id, reply, buttons)


def _authorised(chat_id):
    allowed = {str(config.TELEGRAM_CHAT_ID), str(config.TELEGRAM_CHAT_ID_GROUP)}
    if not config.TELEGRAM_CHAT_ID:
        return True
    return str(chat_id) in allowed


def _handle_callback(cb):
    data = cb.get("data") or ""
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    parts = data.split(":")
    if parts[0] == "take" and len(parts) >= 3:
        vb_id, stake = parts[1], float(parts[2])
        vb = db.q("SELECT * FROM value_bets WHERE id=?", (vb_id,), one=True)
        if not vb:
            _answer_cb(cb["id"], "搵唔到呢個建議")
            return
        bet_id = staking.record_bet(vb, mode="real", stake=stake, note="confirmed via telegram")
        _answer_cb(cb["id"], "已記錄真實注項")
        _reply(chat_id, f"✅ 已記錄 <b>真實</b> 注項 #{bet_id}：${stake:.0f} @ {vb['hkjc_odds']:.2f}")
    elif parts[0] == "skip":
        _answer_cb(cb["id"], "已跳過")
        _reply(chat_id, "👌 已跳過，唔會記錄。自動跟注（paper）照常運作。")


# ------------------------------------------------------------------ commands
def dispatch(text):
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    if cmd in ("/start", "/help"):
        return HELP, None

    if cmd == "/status":
        return _cmd_status(), None

    if cmd == "/fixtures":
        return _cmd_fixtures(), None

    if cmd == "/value":
        return _cmd_value(), None

    if cmd == "/scan":
        s = scanner.scan()
        return (f"🔍 掃描完成：睇 {s['summary']['checked']} 場，"
                f"命中 {s['summary']['picked']} 個價值，自動跟注 {s['summary']['booked']} 注。"), None

    if cmd == "/sync":
        s = scanner.sync_fixtures()
        o = scanner.refresh_odds()
        return f"🔄 賽程 {s['saved']} 場（{s['note']}）\n賠率 {o['saved']} 條（{o['note']}）", None

    if cmd == "/settle":
        out = settlement.settle_pending()
        n = sum(x.get("settled", 0) for x in out if isinstance(x, dict))
        return f"🧾 結算完成：處理 {n} 注。", None

    if cmd == "/probe":
        rep = hkjc.probe(verbose=False)
        lines = ["🔌 <b>馬會端點測試</b>"]
        for r in rep["report"]:
            lines.append(f"• {r['name']}: {r.get('status')} {r.get('bytes','')} {r.get('error','')}")
        lines.append(f"\nraw 檔：<code>{rep['summary']}</code>")
        return "\n".join(lines), None

    if cmd == "/log":
        import ledger
        days = int(args[0]) if args and args[0].isdigit() else None
        return ledger.telegram_text(days=days), None

    if cmd == "/open":
        return journal.open_bets_text(), None

    if cmd == "/roi":
        days = int(args[0]) if args and args[0].isdigit() else None
        return journal.report_text(days=days), None

    if cmd == "/report":
        return journal.report_text(days=1, title="今日報告"), None

    if cmd == "/weekly":
        return journal.weekly_report(push=False), None

    if cmd == "/settings":
        d = settings_store.dump()
        return "⚙️ <b>現時參數</b>\n" + "\n".join(f"{k} = {v}" for k, v in d.items()), None

    if cmd == "/set" and len(args) >= 2:
        key = args[0].upper()
        val = " ".join(args[1:])
        try:
            newv = settings_store.set_value(key, val)
            return f"✅ {key} = {newv}", None
        except Exception as e:                                     # noqa: BLE001
            return f"⚠️ {notifier.esc(str(e))}", None

    if cmd == "/odds" and args:
        return _cmd_odds(" ".join(args)), None

    if cmd == "/setodds":
        return _cmd_setodds(text), None

    if cmd == "/bet" and len(args) >= 2:
        vb_id, stake = args[0].lstrip("#"), args[1]
        odds = None
        if len(args) >= 3 and args[2].startswith("@"):
            odds = args[2][1:]
        vb = db.q("SELECT * FROM value_bets WHERE id=?", (vb_id,), one=True)
        if not vb:
            return f"搵唔到建議 #{vb_id}", None
        try:
            stake = float(stake)
        except ValueError:
            return "注碼要係數字", None
        bet_id = staking.record_bet(vb, mode="real", stake=stake,
                                    odds=float(odds) if odds else None, note="typed via telegram")
        f = db.q("SELECT * FROM fixtures WHERE match_id=?", (vb["match_id"],), one=True) or {}
        return (f"✅ 已記錄真實注項 #{bet_id}\n{f.get('home_team','')} vs {f.get('away_team','')} "
                f"{MARKET_LABEL.get(vb['market'])}{' ' + str(vb['line']) if vb['line'] else ''} "
                f"{SEL_LABEL.get(vb['selection'])} ${stake:.0f} @ "
                f"{(float(odds) if odds else vb['hkjc_odds']):.2f}"), None

    if cmd == "/result" and len(args) >= 2:
        key = args[0].lstrip("#")
        score = args[1]
        try:
            hg, ag = [int(x) for x in score.replace(":", "-").split("-")]
        except ValueError:
            return "格式：/result <id> <主-客>，例如 /result 3 2-1", None
        match_id = _resolve_match(key)
        if not match_id:
            return f"搵唔到賽事/注項 {key}", None
        out = settlement.enter_result(match_id, hg, ag, source="manual")
        f = db.q("SELECT * FROM fixtures WHERE match_id=?", (match_id,), one=True) or {}
        return (f"🧾 已入賽果 {f.get('home_team','?')} {hg}-{ag} {f.get('away_team','?')}\n"
                f"結算 {out.get('settled',0)} 注"), None

    if cmd == "/sim" and args:
        return _cmd_sim(" ".join(args)), None

    if cmd == "/ratings":
        n = int(args[0]) if args and args[0].isdigit() else 15
        import features
        rows = features.build_strength_table()[:n]
        if not rows:
            return "未有足夠歷史數據（用 /import? 或等賽果累積）。", None
        out = ["📈 <b>球隊強度（攻／守）</b>"]
        for r in rows:
            out.append(f"{r['team']} ({r['league']}) 攻 {r['att']:.2f} 守 {r['def']:.2f} "
                       f"{r['played']} 場 場均入 {r['gf']:.2f} 失 {r['ga']:.2f}")
        return "\n".join(out), None

    return f"唔識呢個指令：{cmd}\n\n{HELP}", None


def _resolve_match(key):
    if key.isdigit():
        b = db.q("SELECT match_id FROM bets WHERE id=?", (key,), one=True)
        if b:
            return b["match_id"]
        v = db.q("SELECT match_id FROM value_bets WHERE id=?", (key,), one=True)
        if v:
            return v["match_id"]
    rows = db.q("""SELECT match_id FROM fixtures WHERE lower(home_team) LIKE lower(?)
                   ORDER BY kickoff_hk DESC LIMIT 1""", (f"%{key}%",))
    return rows[0]["match_id"] if rows else None


def _cmd_status():
    s = config.summary()
    scans = db.last_job("scan")
    sync = db.last_job("sync_fixtures")
    odds = db.last_job("refresh_odds")
    j = journal.summary(mode="paper")
    out = [f"🟢 <b>系統狀態</b> v{s['version']} · 香港時間 {now_hk().strftime('%Y-%m-%d %H:%M')}",
           f"市場：{','.join(s['markets'])} · EV 門檻 {s['ev_threshold']*100:.1f}% · "
           f"注碼模式 {s['staking_mode']} · 自動跟注 {'開' if s['auto_follow'] else '關'}",
           f"上次同步賽程：{sync['ts'] if sync else '未跑'}（{sync['status'] if sync else '-'}）",
           f"上次抓賠率：{odds['ts'] if odds else '未跑'}（{odds['detail'][:60] if odds else '-'}）",
           f"上次掃描：{scans['ts'] if scans else '未跑'}",
           f"自動跟注：{j['bets']} 注 · ROI "
           f"{(j['roi_pct']*100 if j['roi_pct'] is not None else 0):+.2f}% · 盈虧 ${j['pnl']:+.0f}",
           f"Telegram：{'已設定' if s['telegram'] else '未設定'} · DB：{s['db_path']}"]
    return "\n".join(out)


def _cmd_fixtures():
    start = today_hk().strftime("%Y-%m-%d")
    from scanner import _day_bounds
    a, b = _day_bounds(start)
    rows = db.q("""SELECT * FROM fixtures WHERE kickoff_hk>=? AND kickoff_hk<? ORDER BY kickoff_hk""", (a, b))
    if not rows:
        return "今日資料庫冇賽程。用 /sync 同步，或叫系統管理員睇 /probe 結果。", None
    out = [f"📅 <b>今日賽程</b>（{len(rows)} 場）"]
    for r in rows:
        odds_n = db.q("SELECT COUNT(*) c FROM odds_snapshots WHERE match_id=?", (r["match_id"],), one=True)["c"]
        out.append(f"{fmt_hk(parse_hk(r['kickoff_hk']))} <b>{notifier.esc(r['home_team'])} vs "
                   f"{notifier.esc(r['away_team'])}</b> {notifier.esc(r['league'])} "
                   f"[{r['status']}]{' 賠率✓' if odds_n else ' 冇賠率'}")
    return "\n".join(out), None


def _cmd_value():
    rows = journal.value_bets_today()
    if not rows:
        return "今日暫時冇價值投注。用 /scan 即刻掃一次。", None
    out = ["💎 <b>今日價值清單</b>"]
    for r in rows[:15]:
        out.append(f"#{r['id']} {fmt_hk(parse_hk(r['kickoff_hk']))} "
                   f"{notifier.esc(r['home_team'])} vs {notifier.esc(r['away_team'])}\n"
                   f"　{MARKET_LABEL.get(r['market'])}{' ' + str(r['line']) if r['line'] else ''} "
                   f"{SEL_LABEL.get(r['selection'], r['selection'])} @ {r['hkjc_odds']:.2f} "
                   f"（模型 {r['model_p']*100:.1f}% / EV {r['ev_pct']*100:+.1f}% / ${r['stake']:.0f}）")
    return "\n".join(out), None


def _cmd_odds(keyword):
    rows = db.q("""SELECT * FROM fixtures WHERE lower(home_team) LIKE lower(?)
                   OR lower(away_team) LIKE lower(?) OR lower(league) LIKE lower(?)
                   ORDER BY kickoff_hk DESC LIMIT 1""",
                (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"))
    if not rows:
        return "搵唔到呢場。用 /fixtures 睇清單。"
    f = rows[0]
    snap = db.latest_odds(f["match_id"])
    if not snap:
        return f"{f['home_team']} vs {f['away_team']}：暫無賠率紀錄（/sync 或 /setodds 人手入）。"
    out = [f"🎲 <b>{f['home_team']} vs {f['away_team']}</b> · {fmt_hk(parse_hk(f['kickoff_hk']))}"]
    for market in ("HAD", "TTG"):
        got = [s for s in snap if s["market"] == market]
        if not got:
            continue
        if market == "HAD":
            d = {s["selection"]: s["odds"] for s in got}
            out.append(f"主客和：主 {d.get('H','-')} / 和 {d.get('D','-')} / 客 {d.get('A','-')}")
        else:
            byline = {}
            for s in got:
                byline.setdefault(str(s["line"]), {})[s["selection"]] = s["odds"]
            for line, d in byline.items():
                out.append(f"大細 {line}：大 {d.get('OVER','-')} / 細 {d.get('UNDER','-')}")
    out.append(f"最後更新：{snap[0]['taken_at']}")
    return "\n".join(out)


def _cmd_setodds(text):
    payload = text.split(" ", 1)[1] if " " in text else ""
    parsed, err = hkjc.parse_manual_odds(payload)
    if not parsed:
        return f"⚠️ {notifier.esc(err)}"
    n = db.save_odds_batch([{**r, "source": "manual"} for r in parsed["rows"]])
    f = parsed["fixture"]
    return f"✅ 已入 {n} 條賠率：{f['home_team']} vs {f['away_team']}\n即刻 /scan 就會用。"


def _cmd_sim(text):
    body = text.split(" ", 1)[1] if " " in text else ""
    body = body.replace("對", " vs ").replace("  ", " ")
    if " vs " not in body.lower():
        return "格式：/sim 曼聯 vs 利物浦"
    a, b = [x.strip() for x in body.split(" vs ", 1)]
    res = predict_pair(a, b)
    m = res["meta"]
    lines = [f"🧮 <b>{notifier.esc(a)} vs {notifier.esc(b)}</b>",
             f"預期入球：{res['lam_home']:.2f} - {res['lam_away']:.2f}",
             f"主勝 {res['p_home']*100:.1f}% / 和 {res['p_draw']*100:.1f}% / 客勝 {res['p_away']*100:.1f}%",
             f"公平賠率：{1/res['p_home']:.2f} / {1/res['p_draw']:.2f} / {1/res['p_away']:.2f}"]
    if 2.5 in res["over"]:
        po = res["over"][2.5]
        lines.append(f"大細 2.5：大 {po*100:.1f}%（{1/po:.2f}） / 細 {(1-po)*100:.1f}%（{1/(1-po):.2f}）")
    lines.append(f"歷史樣本：主隊 {m['home_played']} 場 / 客隊 {m['away_played']} 場"
                 + ("　⚠️ 樣本不足，屬粗略估算" if m["thin"] else ""))
    return "\n".join(lines)

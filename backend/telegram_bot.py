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
_fails = {}
STATE_KEY = "telegram_offset"

HELP = """<b>HKJC 足球價值投注系統</b>

<b>📅 賽事同賠率</b>
/fixtures － 今日馬會賽程（連開咩盤）
/odds &lt;關鍵字&gt; － 睇某場最新賠率（例：/odds 曼聯）
/setodds 主隊 vs 客隊 | HAD 2.10 3.35 3.60 | TTG 2.5 OVER 1.85 UNDER 1.95
　↳ 馬會網頁抓唔到時，人手輸入賠率

<b>💎 價值同落注</b>
/value － 今日價值清單（EV 排序）
/scan － 即刻重新掃描（唔等排程）\n/why － 解釋今次掃描：每個篩選條件擋走幾多
/bet － 今日注項＋一撳記錄真實注
/bet &lt;id&gt; &lt;注碼&gt; [@賠率] － 記錄真實注（例：/bet 38 10 @4.75）
/edit &lt;R38&gt; &lt;注碼&gt; [@賠率] － 改金額／賠率
/close &lt;R38&gt; won|lost|push － 手動 mark 結算
/open － 未結算注項（R=真實 P=虛擬）
/live － 有落注嘅場即時比數
/result &lt;id&gt; &lt;主-客&gt; － 人手入賽果（例：/result 3 2-1）

<b>🔬 分析同改進</b>
/ai [test] － AI 分析層狀態（可選）\n/model － 模型用咗幾多球隊資料（覆蓋率）\n/diag [real] [日數] － 診斷：命中率 vs 模型／按盤口／EV／賠率分組
/backfill [日數|起 止] － 匯入歷史賽果（訓練模型，例：/backfill 60）

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
/check － 自我檢查（token／webhook／收發訊息／排程，一鍵診斷）
/sim A vs B － 即時試算任何兩隊（唔需要賠率）
/ratings [隊數] － 球隊強度排名
/settings － 睇現時參數
/pause /resume － 暫停／重開自動跟注\n/set KEY VALUE － 改參數（例如 /set EV_THRESHOLD 0.06）
/help － 呢個清單
"""


# ------------------------------------------------------------------ plumbing
def _api(method, **payload):
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        # ★ Telegram 唔接受 null（例如 reply_markup=None 會回 400 object expected as reply markup）
        payload = {k: v for k, v in payload.items() if v is not None}
        r = requests.post(url, json=payload, timeout=35)
        return r.json()
    except requests.RequestException as e:
        print("[bot] api error", e)
        return None


def _reply(chat_id, text, buttons=None):
    res = _api("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
               disable_notification=False,
               reply_markup={"inline_keyboard": buttons} if buttons else None)
    if not res or not res.get("ok"):
        _d = (res or {}).get("description") or ""
        print("[bot] send failed:", (res or {}).get("error_code"), _d)
        if (res or {}).get("error_code") == 400:
            for label, extra in (("去掉按鈕重送", {"parse_mode": "HTML"}),
                                 ("改用純文字重送", {})):
                res = _api("sendMessage", chat_id=chat_id, text=text[:4000], **extra)
                print(f"[bot] {label}:", (res or {}).get("ok"),
                      (res or {}).get("description") or "")
                if res and res.get("ok"):
                    break
    return res


def _answer_cb(cb_id, text=""):
    _api("answerCallbackQuery", callback_query_id=cb_id, text=text[:200])


def run_forever():
    """Long-polling loop. Started from main.py on a daemon thread."""
    if not config.TELEGRAM_BOT_TOKEN:
        print("[bot] no token configured, polling disabled")
        return
    _running.set()
    print(f"[bot] polling started（去 Telegram 打 /help 測試；chat id={config.TELEGRAM_CHAT_ID or '未設定'}）")
    try:
        info = _api("getWebhookInfo")
        url = ((info or {}).get("result") or {}).get("url") or ""
        if url:
            print(f"[bot] ⚠️ 發現 webhook 指向 {url} —— 佢會截走所有訊息，自動清走…")
            res = _api("deleteWebhook")
            print("[bot] deleteWebhook:",
                  "成功" if (res or {}).get("ok") else (res or {}).get("description"))
    except Exception as e:                                         # noqa: BLE001
        print("[bot] webhook 檢查失敗", e)
    while _running.is_set():
        try:
            offset = int(db.get_setting(STATE_KEY, 0) or 0)
            res = _api("getUpdates", offset=offset + 1, timeout=30,
                       allowed_updates=["message", "callback_query"])
            if not res or not res.get("ok"):
                if res:
                    desc = (res.get("description") or "")
                    code = res.get("error_code")
                    if code == 409 or "terminated by other getUpdates" in desc:
                        print("[bot] 409 Conflict：同一個 token 有另一個地方正在輪詢 —— "
                              "本機（py cli.py serve／bot）同 Railway 只可以開一個，請關掉其中一邊。")
                    elif code == 401:
                        print("[bot] 401 Unauthorized：token 唔對或者已經 revoke，"
                              "請去 @BotFather 拎新 token 並更新 TELEGRAM_BOT_TOKEN。")
                time.sleep(3)
                continue
            for upd in res.get("result", []):
                db.set_setting(STATE_KEY, upd["update_id"])
                msg = upd.get("message") or {}
                text_in = (msg.get("text") or "").strip()
                if msg:
                    print(f"[bot] recv {text_in.split()[0] if text_in else '(非文字)'} "
                          f"from chat {msg.get('chat', {}).get('id')}")
                try:
                    handle_update(upd)
                    print(f"[bot] handled update {upd.get('update_id')}")
                except Exception as e:                             # noqa: BLE001
                    uid = upd["update_id"]
                    _fails[uid] = _fails.get(uid, 0) + 1
                    print(f"[bot] handler error（第 {_fails[uid]} 次）", e)
                    traceback.print_exc()
                    if upd.get("message"):
                        _reply(upd["message"]["chat"]["id"],
                               f"⚠️ 指令出錯：{notifier.esc(str(e)[:200])}")
                    if _fails[uid] < 3:
                        db.set_setting(STATE_KEY, uid - 1)
                        print("[bot] 回退 offset，等 Telegram 重送")
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
        if bet_id is None:
            _answer_cb(cb["id"], "呢個注項之前已經記錄過")
            _reply(chat_id, "⚠️ 呢個注項之前已經記錄過，唔會重複落注。")
            return
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
        return _cmd_value("time" if "time" in text.lower().split() else "ev")

    if cmd == "/why":
        T = {}
        import scanner as _sc
        r = _sc.scan(min_minutes=-90, max_minutes=900, dry_run=True, push=False, trace=T)
        sm = r["summary"]
        rows = [f"🔎 <b>為何冇注／點分析</b>",
                f"未來時段 {T.get('window', 0)} 場有賠率（冇賠率 {T.get('no_odds', 0)} 場跳過）",
                f"模型計出候選 {T.get('cand', 0)} 個（含唔值博）", ""]
        rows += [
            f"· 唔夠 EV（<{config.EV_THRESHOLD*100:.0f}%）：{T.get('value', 0)}",
            f"· 賠率 < {config.MIN_ODDS:.2f}：{T.get('odds_low', 0)}",
            f"· 賠率 > {getattr(config, 'MAX_ODDS', 99):.2f}：{T.get('odds_high', 0)}",
            f"· 機率 < {getattr(config, 'MIN_MODEL_P', 0)*100:.0f}%：{T.get('min_p', 0)}",
            f"· 封鎖組合（{getattr(config, 'BLOCK_SELECTIONS', '') or '無'}）：{T.get('sel_block', 0)}",
            f"· 已經落過注：{T.get('booked_dup', 0)}",
            f"· 同場已滿 1 注：{T.get('per_match', 0)}",
            f"· AI 否決：{T.get('veto', 0)}",
            "",
            f"結果：過閘 {sm['picked']} 個 · 落注 {sm['booked']} 注",
        ]
        ob, why = staking.risk_check("paper")
        rows.append(f"自動跟注：{'開' if config.AUTO_FOLLOW else '關'}"
                    + ("" if ob else f"（被擋：{why}）"))
        if sm['picked'] == 0:
            rows += ["", "👉 全部候選都過唔到閘，即係今日冇「值得買」嘅注。呢個係正常，唔係壞。",
                     "想放寬：/set EV_THRESHOLD 0.04 或 /set BLOCK_SELECTIONS \"\""]
        return "\n".join(rows), None

    if cmd == "/scan":
        s = scanner.scan()
        return (f"🔍 掃描完成：睇 {s['summary']['checked']} 場，"
                f"命中 {s['summary']['picked']} 個價值，自動跟注 {s['summary']['booked']} 注。"), None

    if cmd == "/sync":
        s = scanner.sync_fixtures()
        o = scanner.refresh_odds()
        return f"🔄 賽程 {s['saved']} 場（{s['note']}）\n賠率 {o['saved']} 條（{o['note']}）", None

    if cmd == "/settle":
        before = db.q("SELECT COUNT(*) c FROM bets WHERE status='open'", one=True)["c"]
        got = settlement.fetch_results_from_source()
        out = settlement.settle_pending()
        n = sum(x.get("settled", 0) for x in out if isinstance(x, dict))
        after = db.q("SELECT COUNT(*) c FROM bets WHERE status='open'", one=True)["c"]
        n = max(n, before - after)
        return (f"🧾 <b>結算</b>\n賽果：{got.get('note') or '—'}\n"
                f"未結算：{before} → {after}\n今次結算 {n} 注"), None

    if cmd == "/check":
        import doctor
        return doctor.render_text(doctor.report(send_test=False))[:4000], None

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
    if cmd == "/live":
        import live
        return live.text(), None

    if cmd == "/backfill":
        import threading
        from datetime import timedelta

        def _bg():
            try:
                import hkjc
                from timeutil import now_hk
                if args and args[0].startswith("20"):
                    start = args[0]
                    end = args[1] if len(args) >= 2 else args[0]
                else:
                    days = int(args[0]) if args and args[0].isdigit() else 30
                    days = min(days, 120)
                    end = (now_hk() - timedelta(days=1)).strftime("%Y-%m-%d")
                    start = (now_hk() - timedelta(days=days)).strftime("%Y-%m-%d")
                notifier.push(f"⏳ 開始匯入 {start} → {end} 嘅賽果，完成會通知你。")
                r = hkjc.backfill(start, end)
                n = db.q("SELECT COUNT(*) c FROM team_matches", one=True)["c"]
                notifier.push(f"📚 <b>匯入完成</b>\n{start} → {end}\n"
                              f"賽果 {r.get('saved', 0)} 場（{r.get('days', 0)} 日）\n"
                              f"隊史總數 {n} 條 —— 之後預測會用真實隊強度。")
            except Exception as e:                      # noqa: BLE001
                notifier.push_error("backfill", e)

        threading.Thread(target=_bg, daemon=True, name="backfill").start()
        return ("⏳ 收到，背景開始匯入歷史賽果（1–8 分鐘）。\n"
                "完成會自動通知你，期間可以繼續用其他指令。"), None

    if cmd == "/ai":
        import ai
        if args and args[0] == "test":
            return ai.test_call(), None
        return ai.status_text() + "\n\n" + ai.usage_report(), None

    if cmd == "/model":
        import diagnose
        return diagnose.coverage(), None

    if cmd == "/diag":
        import diagnose
        mode = "real" if (args and args[0].lower() in ("real", "真", "真實")) else "paper"
        days = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 60
        return diagnose.report(mode=mode, days=days)["text"], None

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

    if cmd in ("/pause", "/resume"):
        import settings_store
        want = "true" if cmd == "/resume" else "false"
        settings_store.set_value("AUTO_FOLLOW", want)
        if want == "true":
            ts = now_hk().strftime("%Y-%m-%d %H:%M:%S")
            db.set_setting("KILL_RESUME_AT", ts)
            return (f"▶️ 已重開自動跟注（{ts}）。\n"
                    f"停手檢查改為只計「{ts} 之後結算」嘅注，夠 {int(getattr(config, 'KILL_MIN_N', 10))} 注才會再評估，"
                    f"唔會即刻再停。"), None
        return "⏸ 已暫停自動跟注（只提示，唔會落注）。", None

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

    if cmd == "/bet" and len(args) < 2:
        return _cmd_bet_list()

    if cmd == "/bet" and len(args) >= 2:
        vb_id, stake = args[0].lstrip("#"), args[1]
        if not vb_id.isdigit():                      # 用隊名：「/bet 艾費哈 10」
            k = vb_id.lower()
            for row in journal.value_bets_today():
                fx = db.q("SELECT home_team, away_team FROM fixtures WHERE match_id=?",
                          (row["match_id"],), one=True) or {}
                if k in (fx.get("home_team") or "").lower() or k in (fx.get("away_team") or "").lower():
                    vb_id = str(row["id"])
                    break
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
        if bet_id is None:
            return "⚠️ 呢個注項之前已經記錄過，唔會重複落注。", None
        f = db.q("SELECT * FROM fixtures WHERE match_id=?", (vb["match_id"],), one=True) or {}
        return (f"✅ 已記錄真實注項 <b>R{bet_id}</b>\n{f.get('home_team','')} vs {f.get('away_team','')} "
                f"{MARKET_LABEL.get(vb['market'])}{' ' + str(vb['line']) if vb['line'] else ''} "
                f"{SEL_LABEL.get(vb['selection'])} ${stake:.0f} @ "
                f"{(float(odds) if odds else vb['hkjc_odds']):.2f}\n"
                f"想改金額／賠率：<code>/edit R{bet_id} 10 @4.75</code>"), None

    if cmd == "/edit" and len(args) >= 2:
        bet = _resolve_bet(args[0])
        if not bet:
            return "搵唔到呢注。格式：/edit R38 10 @4.75", None
        sets, vals = [], []
        if args[1] not in ("-", ""):
            try:
                sets.append("stake=?")
                vals.append(float(args[1]))
            except ValueError:
                return "注碼要係數字", None
        if len(args) >= 3 and args[2].startswith("@"):
            try:
                sets.append("odds_taken=?")
                vals.append(float(args[2][1:]))
            except ValueError:
                return "賠率要係數字（例 @4.75）", None
        if not sets:
            return "格式：/edit R38 10 @4.75（@賠率可省略）", None
        vals.append(bet["id"])
        db.ex(f"UPDATE bets SET {', '.join(sets)} WHERE id=?", tuple(vals))
        r = db.q("SELECT * FROM bets WHERE id=?", (bet["id"],), one=True)
        return (f"✏️ 已更新 <b>{journal.bcode(r)}</b>：金額 ${r['stake']:.0f} · 賠率 "
                f"{(r['odds_taken'] or 0):.2f}\n改狀態用 /close {journal.bcode(r)} won"), None

    if cmd == "/close" and len(args) >= 2:
        bet = _resolve_bet(args[0])
        if not bet:
            return "搵唔到呢注。格式：/close R38 won", None
        m = {"win": "won", "won": "won", "贏": "won", "輸": "lost", "lose": "lost", "lost": "lost",
             "走": "push", "push": "push", "void": "void"}
        st = m.get(args[1].lower(), args[1].lower())
        if st not in ("won", "lost", "push", "void"):
            return "狀態只可以係 won / lost / push", None
        if len(args) >= 3:
            try:
                payout = float(args[2])
            except ValueError:
                return "派彩要係數字", None
        else:
            odds = bet["odds_taken"] or 0
            payout = bet["stake"] * odds if st == "won" else (bet["stake"] if st in ("push", "void") else 0)
        db.ex("UPDATE bets SET status=?, payout=?, settled_at=? WHERE id=?",
              (st, round(payout, 2), now_hk().strftime("%Y-%m-%d %H:%M:%S"), bet["id"]))
        return f"✅ {journal.bcode(bet)} → {st}，派彩 ${payout:.2f}", None

    if cmd == "/result" and len(args) >= 2:
        key = args[0].lstrip("#")
        score = args[1]
        try:
            hg, ag = [int(x) for x in score.replace(":", "-").split("-")]
        except ValueError:
            return "格式：/result &lt;id&gt; &lt;主-客&gt;，例如 /result 3 2-1", None
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

    usage = {
        "/result": "格式：/result &lt;注號或隊名&gt; &lt;主-客&gt;\n例：/result R38 2-1 或 /result 山形山神 1-0",
        "/edit": "格式：/edit &lt;R38&gt; &lt;注碼&gt; [@賠率]\n例：/edit R38 10 @4.75",
        "/close": "格式：/close &lt;R38&gt; won|lost|push [派彩]\n例：/close R38 won",
        "/set": "格式：/set KEY VALUE\n例：/set EV_THRESHOLD 0.08",
        "/odds": "格式：/odds &lt;關鍵字&gt;\n例：/odds 山形山神",
        "/sim": "格式：/sim A vs B\n例：/sim 曼城 vs 利物浦",
    }
    if cmd in usage:
        return usage[cmd], None
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
    jp = journal.summary(mode="paper")
    jr = journal.summary(mode="real")
    try:
        import ai as _ai2
        _ai_line = ("AI：開 · 今日 %d/%d 次 · %d tokens" % (
            _ai2.calls_today(), int(config.AI_MAX_CALLS_PER_DAY), _ai2.tokens_today())
            if _ai2.enabled() else "AI：關")
    except Exception:                                            # noqa: BLE001
        _ai_line = "AI：—"
    mods = []
    for _m in ("ai", "diagnose", "live", "settlement"):
        try:
            __import__(_m)
            mods.append(f"{_m}✅")
        except Exception:                                        # noqa: BLE001
            mods.append(f"{_m}✖")
    _has = lambda c: "✅" if c in HELP else "✖"
    out = [f"🟢 <b>系統狀態</b> v{s['version']} · 香港時間 {now_hk().strftime('%Y-%m-%d %H:%M')}",
           f"模組：{' '.join(mods)}　指令：/model{_has('/model')} /ai{_has('/ai')} /diag{_has('/diag')}",
           _ai_line,
           f"市場：{','.join(s['markets'])} · EV 門檻 {s['ev_threshold']*100:.1f}% · "
           f"注碼模式 {s['staking_mode']} · 自動跟注 {'開' if s['auto_follow'] else '關'}",
           f"上次同步賽程：{sync['ts'] if sync else '未跑'}（{sync['status'] if sync else '-'}）",
           f"上次抓賠率：{odds['ts'] if odds else '未跑'}（{odds['detail'][:60] if odds else '-'}）",
           f"上次掃描：{scans['ts'] if scans else '未跑'}",
           f"🧪 虛擬（自動跟注）：{jp['bets']} 注 · ROI "
           f"{(jp['roi_pct']*100 if jp['roi_pct'] is not None else 0):+.2f}% · 盈虧 ${jp['pnl']:+.0f}",
           f"💰 真實（你落嘅）：{jr['bets']} 注 · ROI "
           f"{(jr['roi_pct']*100 if jr['roi_pct'] is not None else 0):+.2f}% · 盈虧 ${jr['pnl']:+.0f}",
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


def _cmd_value(order="ev"):
    rows = journal.value_bets_today(order)
    if not rows:
        return "今日暫時冇價值投注。用 /scan 即刻掃一次。", None
    out = [f"💎 <b>今日價值清單</b>（{'按開賽時間' if order == 'time' else '按 EV 由高至低'}）"]
    for r in rows[:15]:
        out.append(f"#{r['id']} {fmt_hk(parse_hk(r['kickoff_hk']))} "
                   f"{notifier.esc(r['home_team'])} vs {notifier.esc(r['away_team'])}\n"
                   f"　{MARKET_LABEL.get(r['market'])}{' ' + str(r['line']) if r['line'] else ''} "
                   f"{SEL_LABEL.get(r['selection'], r['selection'])} @ {r['hkjc_odds']:.2f} "
                   f"（模型 {r['model_p']*100:.1f}% / EV {r['ev_pct']*100:+.1f}% / ${r['stake']:.0f}）")
    btns = [[{"text": f"💰 #{r['id']} 落 ${r['stake']:.0f}",
              "callback_data": f"take:{r['id']}:{r['stake']:.0f}"}] for r in rows[:5]]
    out += ["", "睇法：EV 正＝有價值；公平賠率 ＜ 馬會賠率 先值得買。落注後撳按鈕記錄真實注。"]
    return "\n".join(out), btns




def _resolve_bet(key):
    k = (key or "").strip().lstrip("#").upper()
    if k[:1] in ("R", "P") and k[1:].isdigit():
        mode = "real" if k[0] == "R" else "paper"
        return db.q("SELECT * FROM bets WHERE id=? AND mode=?", (k[1:], mode), one=True)
    if k.isdigit():
        return db.q("SELECT * FROM bets WHERE id=?", (k,), one=True)
    return None

def _cmd_bet_list():
    rows = journal.value_bets_today("time")
    if not rows:
        return "今日暫時冇價值注項。用 /scan 即刻掃一次。", None
    lines = ["💰 <b>記錄你嘅真實注項</b>",
             "撳下面按鈕直接記錄；或打 /bet &lt;編號&gt; &lt;金額&gt;（例：/bet 3 10）", ""]
    btns = []
    for r in rows[:8]:
        fx = db.q("SELECT home_team, away_team FROM fixtures WHERE match_id=?",
                  (r["match_id"],), one=True) or {}
        lines.append(f"#{r['id']} {notifier.esc(fx.get('home_team', ''))} vs "
                     f"{notifier.esc(fx.get('away_team', ''))}\n"
                     f"　{MARKET_LABEL.get(r['market'], r['market'])}"
                     f"{' ' + str(r['line']) if r['line'] else ''} "
                     f"{SEL_LABEL.get(r['selection'], r['selection'])} @ {r['hkjc_odds']:.2f} · "
                     f"建議 ${r['stake']:.0f} · EV {r['ev_pct'] * 100:+.1f}%")
        btns.append([{"text": f"💰 #{r['id']} 落 ${r['stake']:.0f}",
                      "callback_data": f"take:{r['id']}:{r['stake']:.0f}"}])
    lines += ["", "睇法：EV 正＝有價值；公平賠率 ＜ 馬會賠率 先值得買；建議注碼＝跟注金額。"]
    return "\n".join(lines), btns

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

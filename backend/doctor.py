"""自我檢查（doctor）：一鍵話你知邊樣通、邊樣唔通，同埋點修。

三個地方睇得到：
  py cli.py doctor            終端報告（會真係發一條測試訊息去 Telegram）
  py cli.py doctor --fix      順手自動修正（例如清走阻住輪詢嘅 webhook）
  Telegram /check             手機睇同一份報告
  儀表板「🩺 自我檢查」掣      網頁睇（`GET /api/doctor`；`?fix=1` 會先自動修正）

每個檢查都獨立 try／except：一項爆都唔會影響其他項，報告照出。
"""
import os
import platform
import re
import sys

import requests

import config
import db
from timeutil import now_hk

TG = "https://api.telegram.org/bot{token}/{method}"
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_\-]{30,}$")
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌", SKIP: "➖"}


# ------------------------------------------------------------------ plumbing
def _tg(method, **params):
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    try:
        r = requests.post(TG.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                          json=params, timeout=25)
        return r.json()
    except Exception as e:                                         # noqa: BLE001
        return {"ok": False, "description": f"網絡問題：{e}"}


def _item(key, title, status, detail="", fix=""):
    return {"key": key, "title": title, "status": status, "detail": detail, "fix": fix}


def _api_desc(res):
    if not res:
        return "冇回應"
    return f"{res.get('error_code', '')} {res.get('description', '')}".strip()


# ------------------------------------------------------------------ checks
def check_env():
    try:
        return _item("env", "執行環境", OK,
                     f"{platform.system()} · Python {sys.version.split()[0]} · 程式 v{config.VERSION} · "
                     f"香港時間 {now_hk():%Y-%m-%d %H:%M}")
    except Exception as e:                                         # noqa: BLE001
        return _item("env", "執行環境", FAIL, str(e))


def check_files():
    try:
        envfile = config.ENV_FILE or "（冇找到 .env，只用系統環境變數／Railway Variables）"
        data_dir = config.DATA_DIR
        detail = (f".env：{envfile}\n資料庫：{config.DB_PATH}\n"
                  f"資料夾：{data_dir}")
        st = OK
        fix = ""
        if os.path.isdir("/data") and not str(data_dir).startswith("/data"):
            st = WARN
            fix = ("呢部係 Railway/Linux 而資料唔喺 /data —— 重新部署就會清空紀錄。"
                   "去 Railway → Variables 加 DB_PATH=/data/football.db，並掛 Volume 去 /data")
        return _item("files", "設定檔同存放位置", st, detail, fix)
    except Exception as e:                                         # noqa: BLE001
        return _item("files", "設定檔同存放位置", FAIL, str(e))


def check_token_format():
    tok = config.TELEGRAM_BOT_TOKEN
    if not tok:
        return _item("token", "TELEGRAM_BOT_TOKEN", FAIL, "未設定",
                     "去 @BotFather → /mybots → API Token 抄個 token，填落 backend/.env，"
                     "Railway 就填 Variables")
    masked = tok[:8] + "…" + tok[-4:] if len(tok) > 14 else "…"
    if not TOKEN_RE.match(tok.strip()):
        return _item("token", "TELEGRAM_BOT_TOKEN", FAIL,
                     f"格式唔似 bot token（{masked}）",
                     "token 應該係『數字:英文數字』大約 46 個字，中間有冒號。"
                     "可能抄漏／多咗空白／抄咗 chat id。去 @BotFather 重新抄一次")
    return _item("token", "TELEGRAM_BOT_TOKEN", OK, f"格式正確（{masked}）")


def check_chat_id():
    cid = config.TELEGRAM_CHAT_ID
    if not cid:
        return _item("chatid", "TELEGRAM_CHAT_ID", FAIL, "未設定",
                     "去 Telegram 同你個 bot 講一句，再開 "
                     "https://api.telegram.org/bot<TOKEN>/getUpdates 抄 \"chat\":{\"id\":…}")
    if not str(cid).lstrip("-").isdigit():
        return _item("chatid", "TELEGRAM_CHAT_ID", FAIL, f"唔係數字（{cid}）",
                     "chat id 係一串數字（個人通常 9-10 位，群組前面有 - 號）")
    return _item("chatid", "TELEGRAM_CHAT_ID", OK, str(cid))


def check_getme():
    if not config.TELEGRAM_BOT_TOKEN:
        return _item("getme", "Token 有效？（getMe）", SKIP, "冇 token，跳過")
    res = _tg("getMe")
    if res and res.get("ok"):
        r = res.get("result") or {}
        return _item("getme", "Token 有效？（getMe）", OK,
                     f"連到 @{r.get('username', '?')}（{r.get('first_name', '')}）")
    if (res or {}).get("error_code") == 401:
        return _item("getme", "Token 有效？（getMe）", FAIL, "401 Unauthorized —— token 錯或者已經 revoke",
                     "去 @BotFather → /mybots → 你個 bot → API Token → 重新抄（或 Revoke 再拎新）。"
                     "★ 本機 backend/.env 同 Railway Variables 兩邊都要換，唔係一樣會 401")
    return _item("getme", "Token 有效？（getMe）", FAIL, _api_desc(res),
                 "檢查網絡／token 有冇多餘空白")


def check_webhook():
    """★ 最常見嘅「收到推送但收唔到命令」原因：bot 裝過 webhook。"""
    if not config.TELEGRAM_BOT_TOKEN:
        return _item("webhook", "Webhook（會搶走你嘅訊息）", SKIP, "冇 token，跳過")
    res = _tg("getWebhookInfo")
    if res and res.get("ok"):
        r = res.get("result") or {}
        url = (r.get("url") or "").strip()
        pending = r.get("pending_update_count", 0)
        if url:
            return _item("webhook", "Webhook（會搶走你嘅訊息）", FAIL,
                         f"有 webhook 指向 {url}（Telegram 會將所有訊息送去嗰邊，"
                         f"你嘅輪詢永遠收唔到）· 積壓 {pending} 條",
                         "用 `py cli.py doctor --fix`，或直接開 "
                         "https://api.telegram.org/bot<TOKEN>/deleteWebhook 清走")
        return _item("webhook", "Webhook（會搶走你嘅訊息）", OK,
                     f"冇設定（正確 —— 我哋用 long polling）· 積壓 {pending} 條")
    return _item("webhook", "Webhook（會搶走你嘅訊息）", FAIL, _api_desc(res))


def check_updates():
    """探測 getUpdates：拎得到 = 冇衝突；409 = 有另一個 poller 或 webhook。"""
    if not config.TELEGRAM_BOT_TOKEN:
        return _item("updates", "收訊息（getUpdates）", SKIP, "冇 token，跳過")
    res = _tg("getUpdates", offset=-1, timeout=0, limit=1)
    if res and res.get("ok"):
        n = len(res.get("result") or [])
        return _item("updates", "收訊息（getUpdates）", OK,
                     f"拎得到更新（今次探測見到 {n} 條）")
    code = (res or {}).get("error_code")
    desc = (res or {}).get("description", "")
    if code == 409:
        fix = ("同一個 token 有另一個地方喺度輪詢（或者有 webhook）。"
               "只可以開一個：本機 `py cli.py bot`／`serve` 同 Railway 二揀一；"
               "亦要確認 Railway 冇兩個 service／多過 1 個 replica 用同一個 bot")
        if "webhook" in desc:
            fix = "webhook 阻住輪詢 → 用 `py cli.py doctor --fix` 清走"
        return _item("updates", "收訊息（getUpdates）", FAIL, _api_desc(res), fix)
    if code == 401:
        return _item("updates", "收訊息（getUpdates）", FAIL, _api_desc(res),
                     "token 無效 → 換 token（本機 + Railway 兩邊）")
    return _item("updates", "收訊息（getUpdates）", FAIL, _api_desc(res))


def check_send(send_test=True):
    """真係 sendMessage 一次：證明『發』呢條線通，亦證明 chat id 對。"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return _item("send", "發訊息（sendMessage）", SKIP, "token 或 chat id 未設定，跳過")
    if not send_test:
        return _item("send", "發訊息（sendMessage）", SKIP, "（已略過實測）")
    res = _tg("sendMessage", chat_id=config.TELEGRAM_CHAT_ID,
              text=f"🩺 自我檢查測試訊息（v{config.VERSION} · {now_hk():%H:%M}）\n"
                   "見到呢條 = 「發訊息」正常。")
    if res and res.get("ok"):
        return _item("send", "發訊息（sendMessage）", OK, "已送出一條測試訊息（手機應該見到）")
    code = (res or {}).get("error_code")
    desc = (res or {}).get("description", "")
    if code == 403:
        return _item("send", "發訊息（sendMessage）", FAIL,
                     f"403 —— bot 未同你開始對話，或者你封鎖咗佢（{desc}）",
                     "去 Telegram 開你個 bot → 打 /start（要由你主動發起先可以收訊息）")
    if code == 400:
        return _item("send", "發訊息（sendMessage）", FAIL, f"400 —— chat id 唔對（{desc}）",
                     "重新確認 TELEGRAM_CHAT_ID（個人 id 係正數，群組前面有 -）")
    return _item("send", "發訊息（sendMessage）", FAIL, _api_desc(res))


def check_db():
    try:
        rows = db.q("SELECT COUNT(*) c FROM fixtures", one=True)["c"]
        odds = db.q("SELECT COUNT(*) c FROM odds_snapshots", one=True)["c"]
        bets = db.q("SELECT COUNT(*) c FROM bets", one=True)["c"]
        # 寫入測試（順手記錄檢查時間）
        db.set_setting("doctor_last_run", f"{now_hk():%Y-%m-%d %H:%M:%S}")
        st, fix = OK, ""
        if rows == 0:
            st = WARN
            fix = ("今日／將來都冇賽程入庫 —— 即係馬會賽程未打通（`py cli.py probe`）或者未 /sync。"
                   "第一次部署想睇儀表板可以設 SEED_DEMO_ON_BOOT=1")
        return _item("db", "資料庫", st,
                     f"{config.DB_PATH}\n賽程 {rows} 場 · 賠率快照 {odds} 條 · 注項 {bets} 注（寫入正常）",
                     fix)
    except Exception as e:                                         # noqa: BLE001
        return _item("db", "資料庫", FAIL, f"讀寫出錯：{e}", "檢查 DB_PATH 同資料夾權限")


def check_jobs():
    try:
        lines, st, fix = [], OK, ""
        n = 0
        for job in ("scan", "settle", "sync", "heartbeat", "weekly_report", "daylog", "probe"):
            row = db.last_job(job)
            if not row:
                continue
            n += 1
            lines.append(f"{job}: {row['ts']} [{row['status']}] {(row['detail'] or '')[:40]}")
        if not n:
            st = WARN
            fix = "未有任何排程紀錄 —— 如果係 Railway，睇 Deployments log 有冇 [sched] started"
        return _item("jobs", "排程（掃描／結算）", st,
                     "\n".join(lines) if lines else "（未跑過）", fix)
    except Exception as e:                                         # noqa: BLE001
        return _item("jobs", "排程（掃描／結算）", FAIL, str(e))


# ------------------------------------------------------------------ assemble
def report(send_test=True):
    items = [check_env(), check_files(), check_token_format(), check_chat_id(),
             check_getme(), check_webhook(), check_updates(), check_send(send_test),
             check_db(), check_jobs()]
    return {"at": f"{now_hk():%Y-%m-%d %H:%M:%S}", "version": config.VERSION,
            "items": items, "verdict": _verdict(items)}


def _verdict(items):
    st = {i["key"]: i["status"] for i in items}
    detail = {i["key"]: (i.get("detail") or "") for i in items}
    out = []
    token_bad = st.get("token") == FAIL or "401" in detail.get("getme", "")
    if token_bad:
        if st.get("token") == FAIL:
            out.append("① Token 未設定或格式唔對 —— 唔係程式問題，係要重新抄一次 token。")
        else:
            out.append("① Token 無效或已 revoke（401）—— 去 @BotFather 拎新 token，"
                       "本機 backend/.env 同 Railway Variables 兩邊都要更新。")
        out.append("　（下面 webhook／收訊息／發訊息嘅紅燈都係因為 token 無效；"
                   "換好 token 再檢查一次就會變綠。）")
        return out
    if st.get("webhook") == FAIL:
        out.append("② 有 webhook 設住 —— 佢會截走所有訊息，所以你「收唔到命令」。"
                   "行 `py cli.py doctor --fix`（或儀表板撳『自動修正』）就會清走。")
    if st.get("updates") == FAIL:
        out.append("② 同一個 bot 有另一個地方喺度輪詢（或者 webhook 未清）—— "
                   "本機 serve／bot 同 Railway 二揀一；Railway 亦要確認冇兩個 service、replica = 1。")
    if st.get("send") == FAIL:
        out.append("③ 「發訊息」失敗 —— 多數係你未喺 Telegram 向 bot 打過 /start，"
                   "或者 chat id 抄錯。")
    if not out:
        out.append("全部正常：收得到、發得到。你打 /help 應該即刻有回應。")
        if st.get("db") == WARN:
            out.append("（唯一未打通：馬會賽程／賠率來源 —— 見「資料庫」一項。）")
    return out


def render_text(rep):
    lines = [f"🩺 自我檢查 {rep['at']}（v{rep['version']}）", ""]
    for i in rep["items"]:
        lines.append(f"{ICON.get(i['status'], '•')} {i['title']}")
        if i.get("detail"):
            for ln in str(i["detail"]).split("\n"):
                lines.append("    " + ln)
        if i.get("fix") and i["status"] in (WARN, FAIL):
            lines.append(f"    ↳ 點修：{i['fix']}")
    lines.append("")
    lines.append("總結：")
    lines += [f"  {v}" for v in rep["verdict"]]
    return "\n".join(lines)


def auto_fix():
    """自動修正：清走阻住輪詢嘅 webhook（其他唔會亂改）。"""
    acts = []
    try:
        info = _tg("getWebhookInfo")
        url = ((info or {}).get("result") or {}).get("url") or ""
        if url:
            res = _tg("deleteWebhook")
            acts.append(("deleteWebhook", (res or {}).get("ok"),
                         f"原本指向 {url} —— 已清走" if (res or {}).get("ok")
                         else f"清唔到：{_api_desc(res)}"))
        else:
            acts.append(("deleteWebhook", True, "冇 webhook，唔需要清"))
    except Exception as e:                                         # noqa: BLE001
        acts.append(("deleteWebhook", False, str(e)))
    return acts

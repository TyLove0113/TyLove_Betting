"""可選 AI 層（OpenAI 兼容 API）：只做「否決／註記／賽後檢討」。

設計原則（控 token 為第一優先）：
- 冇 key 就完全唔執行（預設關閉）。
- 只對「準備落注」嘅注問，唔會逐場問。
- 同一場／同一盤／同一日只問一次（DB 快取）。
- 每日硬上限 AI_MAX_CALLS_PER_DAY，用完即停。
- 每次 max_tokens=220，temperature 0.2，失敗即跳過（唔重試）。
"""
import hashlib
import json

import requests

import config
import db
from timeutil import now_hk


_LAST_TOKENS = [0]


def enabled():
    return bool(getattr(config, "AI_ENABLED", False) and getattr(config, "AI_API_KEY", ""))


def _dk(suffix=""):
    return suffix + now_hk().strftime("%Y-%m-%d")


def _geti(key):
    try:
        return int(db.get_setting(key) or 0)
    except Exception:
        return 0


def calls_today():
    return _geti(_dk("ai_calls_"))


def tokens_today():
    return _geti(_dk("ai_tokens_"))


def _bump(usage=None):
    db.set_setting(_dk("ai_calls_"), str(calls_today() + 1))
    if usage:
        n = int(usage.get("total_tokens") or 0)
        db.set_setting(_dk("ai_tokens_"), str(tokens_today() + n))


def _ckey(match_id, market, line, selection):
    raw = f"{match_id}|{market}|{line}|{selection}|{_dk()}"
    return "ai_cache_" + hashlib.sha1(raw.encode()).hexdigest()[:20]


def _cache_get(k):
    v = db.get_setting(k)
    if not v:
        return None
    try:
        return json.loads(v)
    except Exception:
        return None


def _cache_set(k, val):
    try:
        db.set_setting(k, json.dumps(val, ensure_ascii=False))
    except Exception:
        pass


def chat(messages, json_mode=True, timeout=40, max_tokens=220):
    base = (getattr(config, "AI_BASE_URL", "") or "https://api.openai.com/v1").rstrip("/")
    payload = {"model": getattr(config, "AI_MODEL", "gpt-4o-mini"),
               "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(base + "/chat/completions", json=payload, timeout=timeout,
                      headers={"Authorization": "Bearer " + config.AI_API_KEY})
    r.raise_for_status()
    data = r.json()
    _bump(data.get("usage"))
    _LAST_TOKENS[0] = int((data.get("usage") or {}).get("total_tokens") or 0)
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


PROMPT = """你係足球賽前風險審查員。只講你確定嘅公開資訊（傷停、停賽、輪換、賽程密度、內部問題）。
規則：
- 唔准預測賽果、唔准講賠率建議、唔准重複我俾你嘅數字。
- 冇明確負面資訊就答 low，唔准為咗顯得有用而編嘢。
- 只回 JSON：{"risk":"low|medium|high","reason":"<25字內廣東話>","key_out":["缺陣球員"]}
risk=high 只用在「確定有主力缺陣／確定大幅輪換」。"""


def assess(match_id, home, away, league, market, selection, line, kickoff):
    """回傳 {risk, reason} 或 None。有快取就唔會再 call。"""
    if not enabled():
        return None
    k = _ckey(match_id, market, line, selection)
    hit = _cache_get(k)
    if hit is not None:
        return hit
    if calls_today() >= int(getattr(config, "AI_MAX_CALLS_PER_DAY", 12)):
        return None
    q = f"{home} vs {away}（{league}），開賽 {kickoff}。我打算買 {market} {line or ''} {selection}。有冇確定嘅負面資訊？"
    try:
        raw = chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": q}])
        data = json.loads(raw)
        risk = str(data.get("risk", "")).lower()
        if risk not in ("low", "medium", "high"):
            return None
        out = {"risk": risk, "reason": str(data.get("reason", ""))[:50]}
        _cache_set(k, out)
        _log("veto", f"{home} vs {away} {market} {line or ''} {selection}",
             risk, out["reason"], _LAST_TOKENS[0])
        return out
    except Exception as e:                                       # noqa: BLE001
        _log("veto", f"{home} vs {away} {market} {line or ''} {selection}",
             ok=False, err=str(e))
        return None


REVIEW_PROMPT = """你係投注檢討員。根據我俾你嘅當日已結算注項（盤口、賠率、結果），
用 5 點以內講：1) 邊類注最蝕 2) 有冇重複同一場 3) 明日要避免乜。
唔准講鼓勵說話，唔准預測賽果，只回 JSON：{"points":["...","..."],"avoid":["..."]}"""


def daily_review(rows):
    """一次 call 總結當日（唔逐注問）。rows: [(desc, status, stake, payout)]"""
    if not enabled() or not rows:
        return None
    if calls_today() >= int(getattr(config, "AI_MAX_CALLS_PER_DAY", 12)):
        return None
    k = _dk("ai_review_")
    if db.get_setting(k):
        return None                                              # 每日一次
    body = "\n".join(f"{d} | {s} | ${st:.0f} -> ${pa:.0f}" for d, s, st, pa in rows[:30])
    try:
        raw = chat([{"role": "system", "content": REVIEW_PROMPT},
                    {"role": "user", "content": body}], max_tokens=300)
        data = json.loads(raw)
        db.set_setting(k, "1")
        return data
    except Exception:                                            # noqa: BLE001
        return None


def _esc(x):
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _log(purpose, subject, risk="", reason="", tokens=0, ok=True, err=""):
    try:
        db.ex("""INSERT INTO ai_calls(ts, purpose, subject, risk, reason, tokens, ok, err)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (now_hk().strftime("%Y-%m-%d %H:%M:%S"), purpose, subject, risk,
               reason[:120], int(tokens or 0), 1 if ok else 0, (err or "")[:200]))
    except Exception:                                            # noqa: BLE001
        pass


def recent(n=8):
    return db.q("SELECT * FROM ai_calls ORDER BY id DESC LIMIT ?", (n,))


def usage_report(n=8):
    """今日用量 + 最近幾次 call（俾 /ai 同網頁用）。"""
    out = [f"📊 <b>AI 用量</b>：今日 {calls_today()}/{int(getattr(config, 'AI_MAX_CALLS_PER_DAY', 12))} 次"
           f" · {tokens_today()} tokens"]
    rows = recent(n)
    if not rows:
        out.append("（今日暫時冇 call —— 表示未有注通過篩選，或者 AI 未開／未設 key）")
        return "\n".join(out)
    out.append("")
    for r in rows:
        mark = "✅" if r["ok"] else "❌"
        tag = f"{r['risk']}" if r["risk"] else "-"
        out.append(f"{mark} {r['ts'][5:16]} {_esc(r['subject'] or '')} → {tag} "
                   f"{_esc(r['reason'] or '')} [{r['tokens']}t]")
    bad = [r for r in rows if not r["ok"]]
    if bad:
        out.append("")
        out.append(f"⚠️ 最近有 {len(bad)} 次失敗，最後錯誤：{_esc(bad[0]['err'] or '')}")
    return "\n".join(out)


def status_text():
    if not getattr(config, "AI_ENABLED", False):
        return ("🤖 <b>AI 分析</b>：已關閉\n"
                "開啟：<code>/set AI_ENABLED true</code>（要先設 AI_API_KEY）")
    if not getattr(config, "AI_API_KEY", ""):
        return ("🤖 <b>AI 分析</b>：已開但冇 key\n"
                "設定：<code>/set AI_API_KEY sk-xxxx</code>")
    used = calls_today()
    cap = int(getattr(config, "AI_MAX_CALLS_PER_DAY", 12))
    tok = tokens_today()
    return (f"🤖 <b>AI 分析</b>：已開\n"
            f"模型 {getattr(config, 'AI_MODEL', '')}\n"
            f"今日：{used}/{cap} 次 · {tok} tokens\n"
            f"用途：只對準備落注嘅注做風險否決（有快取，同一注唔會問兩次）")


def test_call():
    if not enabled():
        return status_text()
    try:
        out = chat([{"role": "user", "content": "回覆兩個字：正常"}], json_mode=False, timeout=25,
                   max_tokens=10)
        return f"✅ AI 連線正常：{out.strip()[:20]}\n\n" + status_text()
    except Exception as e:                                       # noqa: BLE001
        return f"⚠️ AI 連線失敗：{str(e)[:120]}\n\n" + status_text()

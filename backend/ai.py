"""可選 AI 層（OpenAI 兼容 API）：只用嚟「否決／註記／解釋」，唔直接改機率。

設計原則：
- 冇 key 就完全唔會執行（預設關閉）。
- 每次掃描最多 AI_MAX_CALLS_PER_DAY 次，避免洗錢。
- AI 只可以 veto（風險高就唔落）或寫註記，唔可以自己發明預測。
"""
import json

import requests

import config
import db
from timeutil import now_hk


def enabled():
    return bool(getattr(config, "AI_ENABLED", False) and getattr(config, "AI_API_KEY", ""))


def _today_key():
    return "ai_calls_" + now_hk().strftime("%Y-%m-%d")


def calls_today():
    try:
        return int(db.get_setting(_today_key()) or 0)
    except Exception:
        return 0


def _bump():
    db.set_setting(_today_key(), str(calls_today() + 1))


def chat(messages, json_mode=True, timeout=45):
    base = (getattr(config, "AI_BASE_URL", "") or "https://api.openai.com/v1").rstrip("/")
    payload = {"model": getattr(config, "AI_MODEL", "gpt-4o-mini"),
               "messages": messages, "temperature": 0.2}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(base + "/chat/completions", json=payload, timeout=timeout,
                      headers={"Authorization": "Bearer " + config.AI_API_KEY})
    r.raise_for_status()
    _bump()
    return (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""


PROMPT = """你係足球賽前資訊分析員。只根據你確定的公開資訊回答；
唔確定就講唔確定，唔准估賽果，唔准講賠率建議。
回覆 JSON：
{"risk":"low|medium|high","reason":"<30字廣東話>","factors":["傷停","輪換","賽程","天氣"]}
risk=high 只用在有明確負面因素（主力重傷、賽程密集輪換、內部動盪）。"""


def assess(home, away, league, market, selection, line, kickoff):
    """回傳 {risk, reason} 或 None。"""
    if not enabled():
        return None
    if calls_today() >= int(getattr(config, "AI_MAX_CALLS_PER_DAY", 12)):
        return None
    q = (f"{home} vs {away}（{league}），開賽 {kickoff}。"
         f"我打算買：{market} {line or ''} {selection}。"
         f"有冇明確公開資訊令我唔應該買？")
    try:
        raw = chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": q}])
        data = json.loads(raw)
        risk = str(data.get("risk", "")).lower()
        if risk not in ("low", "medium", "high"):
            return None
        return {"risk": risk, "reason": str(data.get("reason", ""))[:60]}
    except Exception:
        return None


def status_text():
    if not getattr(config, "AI_ENABLED", False):
        return ("🤖 <b>AI 分析</b>：已關閉\\n"
                "開啟：<code>/set AI_ENABLED true</code>（要先設 AI_API_KEY）")
    if not getattr(config, "AI_API_KEY", ""):
        return ("🤖 <b>AI 分析</b>：已開但冇 key\\n"
                "設定：<code>/set AI_API_KEY sk-xxxx</code>\\n"
                "模型：<code>/set AI_MODEL gpt-4o-mini</code>")
    return (f"🤖 <b>AI 分析</b>：已開\\n模型 {getattr(config, 'AI_MODEL', '')} · "
            f"今日已用 {calls_today()}/{getattr(config, 'AI_MAX_CALLS_PER_DAY', 12)} 次\\n"
            f"用途：傷停／輪換風險否決、注項註記（唔會自己改機率）")


def test_call():
    if not enabled():
        return status_text()
    try:
        out = chat([{"role": "user", "content": "回覆兩個字：正常"}], json_mode=False, timeout=25)
        return f"✅ AI 連線正常：{out.strip()[:40]}\\n\\n" + status_text()
    except Exception as e:                                        # noqa: BLE001
        return f"⚠️ AI 連線失敗：{str(e)[:120]}\\n\\n" + status_text()

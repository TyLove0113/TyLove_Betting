"""Telegram push. Never lets a notification failure kill a job."""
import json
import traceback

import requests

import config

API = "https://api.telegram.org/bot{token}/{method}"


def _send(chat_id, text, buttons=None, silent=False):
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        print("[notifier] telegram not configured, message dropped:\n" + text[:500])
        return {"ok": False, "reason": "not_configured"}
    if config.DRY_RUN:
        print("[notifier][DRY_RUN] " + text[:300])
        return {"ok": True, "dry_run": True}
    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
               "disable_notification": silent}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage"),
                          json=payload, timeout=20)
        if r.status_code != 200:
            print("[notifier] send failed", r.status_code, r.text[:200])
        return {"ok": r.status_code == 200}
    except requests.RequestException as e:
        print("[notifier] send error", e)
        return {"ok": False, "reason": str(e)[:200]}


def push(text, buttons=None, silent=False):
    return _send(config.TELEGRAM_CHAT_ID, text, buttons, silent)


def push_group(text, buttons=None, silent=False):
    if config.TELEGRAM_CHAT_ID_GROUP:
        return _send(config.TELEGRAM_CHAT_ID_GROUP, text, buttons, silent)
    return {"ok": False, "reason": "no group channel"}


def push_error(job, err):
    tb = traceback.format_exc()
    return push(f"🚨 <b>系統錯誤</b>\njob: <code>{job}</code>\n"
                f"<code>{str(err)[:300]}</code>\n\n<pre>{tb[-1200:]}</pre>")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

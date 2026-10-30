"""自測：逐個指令跑一次，捉 NameError / 邏輯錯誤（唔會 send Telegram）。

用法：
    python3 selftest.py            # 用臨時 DB，唔會影響你真正嘅資料
    DB_PATH=/data/football.db python3 selftest.py   # 想用真資料（唯讀指令較安全）
"""
import os
import sys
import tempfile
import traceback

if "--keep-db" not in sys.argv:
    os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "selftest.db"))
os.environ["AUTO_FOLLOW"] = "false"          # 自測唔好落注
os.environ["TELEGRAM_BOT_TOKEN"] = ""        # 自測唔好發訊息

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db          # noqa: E402
import telegram_bot as tb   # noqa: E402

CMDS = [
    "/help", "/status", "/model", "/ai", "/diag", "/roi", "/report",
    "/open", "/live", "/value", "/fixtures", "/ratings", "/settings",
    "/why", "/bet", "/log", "/settle",
]


def main():
    db.init_db()
    ok = 0
    bad = []
    for c in CMDS:
        try:
            out = tb.dispatch(c)
            text = out[0] if isinstance(out, tuple) else out
            if not text or not str(text).strip():
                raise AssertionError("回覆係空")
            print(f"✅ {c:12} {len(str(text)):5} 字")
            ok += 1
        except Exception as e:                                   # noqa: BLE001
            bad.append((c, e))
            print(f"❌ {c:12} {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    for job in ("ai_review_today", "daily_settle", "summary"):
        try:
            fn = getattr(__import__("journal"), job)
            fn() if job != "summary" else fn(mode="paper")
            print(f"✅ job {job}")
            ok += 1
        except Exception as e:                                   # noqa: BLE001
            bad.append((job, e))
            print(f"❌ job {job}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    try:
        import journal
        journal.kill_switch()
        print("✅ job kill_switch")
        ok += 1
    except Exception as e:                                       # noqa: BLE001
        bad.append(("kill_switch", e))
        print(f"❌ job kill_switch: {type(e).__name__}: {e}")
    print(f"\n完成：{ok} 項通過，{len(bad)} 項失敗")
    if bad:
        print("失敗清單：" + ", ".join(c for c, _ in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

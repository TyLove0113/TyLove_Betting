"""One process: Flask web service (Railway needs a port) + scheduler thread + bot thread.

Scheduler lessons from TyLove are baked in:
  * every decision is made in HK time, never container time
  * the loop is a plain minute tick, so a redeploy mid-day does not lose jobs
  * one worker only (see gunicorn.conf.py) otherwise jobs run several times
  * a daily heartbeat makes silence distinguishable from a dead system
"""
import json
import os
import threading
import time
import traceback

from flask import Flask, Response, jsonify, render_template, request

import config
import db
import journal
import ledger
import notifier
import scanner
import settlement
import settings_store
from timeutil import now_hk, today_hk, parse_hk, minutes_until, hhmm_now, parse_csv_ints

app = Flask(__name__)
_started = threading.Event()
_last = {}


# ------------------------------------------------------------------ scheduler
def scheduler_loop():
    print("[sched] started, HK time", now_hk().strftime("%Y-%m-%d %H:%M:%S"))
    last_minute_key = None
    while True:
        try:
            now = now_hk()
            key = now.strftime("%Y-%m-%d %H:%M")
            if key == last_minute_key:
                time.sleep(5)
                continue
            last_minute_key = key
            run_due_jobs(now)
        except Exception as e:                                     # noqa: BLE001
            print("[sched] error", e)
            traceback.print_exc()
            try:
                notifier.push_error("scheduler", e)
            except Exception:                                      # noqa: BLE001
                pass
        time.sleep(20)


def run_due_jobs(now):
    hour, minute = now.hour, now.minute
    day = now.strftime("%Y-%m-%d")

    def once(name, condition):
        if not condition:
            return False
        stamp_key = f"{name}:{day}"
        if _last.get(stamp_key):
            return False
        _last[stamp_key] = True
        return True

    # 1. daily schedule + odds + heartbeat
    if once("sync", hour == config.SYNC_FIXTURES_HOUR and minute < 5):
        _job("sync_fixtures", lambda: scanner.sync_fixtures())
    if once("heartbeat", hour == config.HEARTBEAT_HOUR and minute < 5):
        _job("heartbeat", _heartbeat)
    if once("weekly", now.weekday() == config.WEEKLY_REPORT_DOW
            and hour == config.WEEKLY_REPORT_HOUR and minute < 5):
        _job("weekly_report", lambda: journal.weekly_report())
    if once("daylog", hour == config.DAYLOG_HOUR and minute < 5):
        _job("daylog", lambda: ledger.write_files())

    # 2. odds refresh a few times a day
    for h in parse_csv_ints(config.SYNC_ODDS_HOURS):
        if once(f"odds{h}", hour == h and minute < 5):
            _job("refresh_odds", lambda: scanner.refresh_odds())

    # 3. settle twice an hour (only if there is something to settle)
    if minute in parse_csv_ints(config.SETTLE_MINUTE):
        _job("mark_closing", scanner.mark_closing)
        _job("settle", lambda: journal.daily_settle())

    # 4. the actual scan: every 5 minutes, window = T-60 .. T-3
    if minute % 5 == 0:
        _job("scan", lambda: scanner.scan())

    # 5. odds movement watch every 15 minutes
    if minute % 15 == 0:
        _job("odds_watch", scanner.odds_move_watch)


def _job(name, fn):
    try:
        out = fn()
        print(f"[sched] {name} -> {str(out)[:200]}")
        return out
    except Exception as e:                                         # noqa: BLE001
        db.log_job(name, "error", str(e)[:500])
        notifier.push_error(name, e)
        return None


def _heartbeat():
    s = config.summary()
    j = journal.summary(mode="paper", days=1)
    fx = db.q("SELECT COUNT(*) c FROM fixtures WHERE kickoff_hk LIKE ?",
              (today_hk().strftime("%Y-%m-%d") + "%",), one=True)["c"]
    roi = f"{j['roi_pct']*100:+.2f}%" if j["roi_pct"] is not None else "n/a"
    notifier.push(
        f"💚 <b>每日心跳</b> {now_hk().strftime('%Y-%m-%d %H:%M')}\n"
        f"今日賽程 {fx} 場 · 價值建議 {j['bets']} 注（近1日）\n"
        f"自動跟注 ROI {roi} · 盈虧 ${j['pnl']:+.0f}\n"
        f"EV 門檻 {s['ev_threshold']*100:.1f}% · 自動跟注 {'開' if s['auto_follow'] else '關'}")
    db.log_job("heartbeat", "ok", f"fixtures={fx}")


# ---------------------------------------------------------------------- routes
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": config.VERSION, "hk_time": now_hk().strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/status")
def api_status():
    return jsonify({
        "config": config.summary(),
        "paper": journal.summary(mode="paper"),
        "real": journal.summary(mode="real"),
        "jobs": {
            "scan": db.last_job("scan"), "sync_fixtures": db.last_job("sync_fixtures"),
            "refresh_odds": db.last_job("refresh_odds"), "settle": db.last_job("settle"),
            "heartbeat": db.last_job("heartbeat"),
        },
    })


@app.route("/api/equity")
def api_equity():
    return jsonify({"paper": journal.equity_series("paper"), "real": journal.equity_series("real")})


@app.route("/api/value")
def api_value():
    return jsonify(journal.value_bets_today())


@app.route("/api/bets")
def api_bets():
    rows = db.q("""SELECT b.*, f.home_team, f.away_team, f.kickoff_hk FROM bets b
                   LEFT JOIN fixtures f ON f.match_id=b.match_id
                   ORDER BY b.id DESC LIMIT 100""")
    return jsonify(rows)


@app.route("/api/ledger")
def api_ledger():
    days = request.args.get("days", type=int)
    mode = request.args.get("mode") or None
    return jsonify({"rows": ledger.rows(mode=mode, days=days, limit=500),
                    "paper": journal.summary(mode="paper", days=days),
                    "real": journal.summary(mode="real", days=days)})


@app.route("/ledger.csv")
def ledger_csv():
    days = request.args.get("days", type=int)
    mode = request.args.get("mode") or None
    body = ledger.csv_text(ledger.rows(mode=mode, days=days))
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bets_ledger.csv"})


@app.route("/api/fixtures")
def api_fixtures():
    rows = db.q("SELECT * FROM fixtures ORDER BY kickoff_hk DESC LIMIT 100")
    return jsonify(rows)


@app.route("/api/scan", methods=["POST", "GET"])
def api_scan():
    return jsonify(scanner.scan()["summary"])


@app.route("/")
def dashboard():
    return render_template("index.html", version=config.VERSION,
                           cfg=config.summary())


# ------------------------------------------------------------------ bootstrap
def bootstrap():
    if _started.is_set():
        return
    _started.set()
    db.init_db()
    settings_store.apply_overrides()
    if config.SEED_DEMO_ON_BOOT:
        try:
            empty = db.q("SELECT COUNT(*) c FROM fixtures", one=True)["c"] == 0
            if empty:
                import demo_data
                print("[boot] SEED_DEMO_ON_BOOT=1 → 播種示範資料（只為驗證部署，睇完可刪呢個 Variable）")
                demo_data.seed()
        except Exception as e:                                     # noqa: BLE001
            print("[boot] demo seed failed", e)
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    try:
        import telegram_bot

        def _telegram_supervisor():
            """輪詢唔可以死咗冇人知：一死就自動重開（排程係另一條 thread，唔受影響）。"""
            import traceback
            while True:
                try:
                    telegram_bot.run_forever()
                    print("[bot] polling loop ended —— 5 秒後重開")
                except Exception as e:                             # noqa: BLE001
                    print("[bot] polling crashed:", e)
                    traceback.print_exc()
                time.sleep(5)

        print("[bot] thread started（有 token 就會長開輪詢）")
        threading.Thread(target=_telegram_supervisor, daemon=True, name="telegram").start()
    except Exception as e:                                         # noqa: BLE001
        print("[boot] telegram thread failed", e)


bootstrap()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

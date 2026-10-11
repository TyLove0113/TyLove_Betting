"""Local command line: the tool you run on your own machine.

Typical first run:
    python cli.py initdb
    python cli.py probe                     # see what HKJC actually returns
    python cli.py import-csv data/history.csv
    python cli.py sim "Manchester United" "Liverpool"
    python cli.py sync && python cli.py odds && python cli.py scan
    python cli.py serve                     # run the whole service locally
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import db              # noqa: E402
import settings_store  # noqa: E402
from timeutil import now_hk  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="HKJC football value betting system")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb")
    sub.add_parser("probe")
    sub.add_parser("sync")
    sub.add_parser("odds")
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--dry-run", action="store_true")
    p_scan.add_argument("--no-push", action="store_true")
    sub.add_parser("settle")
    sub.add_parser("report")
    p_rep = sub.add_parser("weekly")
    p_sim = sub.add_parser("sim")
    p_sim.add_argument("home")
    p_sim.add_argument("away")
    p_sim.add_argument("--league", default="")
    sub.add_parser("ratings")
    p_imp = sub.add_parser("import-csv")
    p_imp.add_argument("path")
    p_imp.add_argument("--league", default="")
    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--csv", default="")
    p_bt.add_argument("--odds-csv", default="")
    p_bt.add_argument("--sweep", action="store_true")
    p_demo = sub.add_parser("demo")
    p_demo.add_argument("--seed", action="store_true", help="write a synthetic league and run end-to-end")
    sub.add_parser("serve")
    sub.add_parser("settings")
    p_not = sub.add_parser("notify")
    p_not.add_argument("message", nargs="?", default="✅ Telegram 設定成功 — HKJC 足球價值投注系統")

    a = ap.parse_args()
    db.init_db()
    settings_store.apply_overrides()

    if a.cmd == "initdb":
        print("database ready:", db.init_db())

    elif a.cmd == "probe":
        import hkjc
        rep = hkjc.probe()
        print("\nsummary saved:", rep["summary"])

    elif a.cmd == "sync":
        import scanner
        print(scanner.sync_fixtures())

    elif a.cmd == "odds":
        import scanner
        print(scanner.refresh_odds())

    elif a.cmd == "scan":
        import scanner
        out = scanner.scan(dry_run=a.dry_run, push=not a.no_push)
        print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
        for p in out["picks"]:
            print(f"  {p['fixture']['home_team']} vs {p['fixture']['away_team']} "
                  f"{p['market']} {p['line']} {p['selection']} @{p['hkjc_odds']:.2f} "
                  f"EV {p['ev_pct']*100:+.1f}% stake {p['stake']:.0f}")

    elif a.cmd == "settle":
        import settlement
        print(settlement.settle_pending())

    elif a.cmd == "report":
        import journal
        print(journal.report_text(days=1))

    elif a.cmd == "weekly":
        import journal
        print(journal.weekly_report(push=False))

    elif a.cmd == "sim":
        from engine import predict_pair
        res = predict_pair(a.home, a.away, a.league)
        print(f"{a.home} vs {a.away}")
        print(f"  lambdas {res['lam_home']:.2f} / {res['lam_away']:.2f}")
        print(f"  H {res['p_home']*100:.1f}%  D {res['p_draw']*100:.1f}%  A {res['p_away']*100:.1f}%")
        for line, p in (res.get("over") or {}).items():
            print(f"  over {line}: {p*100:.1f}%   under: {(1-p)*100:.1f}%")
        print(f"  history: home {res['meta']['home_played']} / away {res['meta']['away_played']}"
              + ("  (THIN - treat as rough)" if res["meta"]["thin"] else ""))

    elif a.cmd == "ratings":
        import features
        for r in features.build_strength_table()[:25]:
            print(f"{r['team']:<28s} {r['league']:<14s} att {r['att']:.2f} def {r['def']:.2f} "
                  f"({r['played']} played)")

    elif a.cmd == "import-csv":
        import features
        n = features.import_results_csv(a.path, a.league)
        print(f"imported {n} matches from {a.path}")

    elif a.cmd == "backtest":
        import backtest
        backtest.run(csv_path=a.csv or None, odds_csv=a.odds_csv or None, sweep=a.sweep)

    elif a.cmd == "demo":
        import demo_data
        demo_data.seed() if a.seed else None

    elif a.cmd == "settings":
        print(json.dumps(settings_store.dump(), ensure_ascii=False, indent=2))

    elif a.cmd == "notify":
        import notifier
        res = notifier.push(a.message)
        print("sent" if res.get("ok") else f"not sent: {res.get('reason')}")

    elif a.cmd == "serve":
        os.environ.setdefault("PORT", "8080")
        import main                                             # noqa: F401
        main.app.run(host="0.0.0.0", port=int(os.environ["PORT"]))


if __name__ == "__main__":
    main()

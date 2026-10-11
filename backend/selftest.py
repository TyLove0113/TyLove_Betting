"""One-shot self test: seed demo data -> scan -> book -> settle -> report.
Run:  python selftest.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db
import settings_store


def main():
    config.DRY_RUN = False
    db.init_db()
    settings_store.apply_overrides()
    config.AUTO_FOLLOW = True

    import demo_data
    demo_data.seed(force=True)

    import scanner
    out = scanner.scan(push=False)
    print("\n=== SCAN ===")
    print(json.dumps(out["summary"], ensure_ascii=False))
    for p in out["picks"]:
        print(f"  {p['fixture']['home_team']} vs {p['fixture']['away_team']} "
              f"{p['market']} {p['line']} {p['selection']} @{p['hkjc_odds']:.2f} EV {p['ev_pct']*100:+.1f}%")

    # settle with realistic scores drawn from each match's own lambdas
    import settlement
    import features
    import poisson
    import random
    rnd = random.Random(11)
    open_matches = db.q("SELECT DISTINCT b.match_id, f.home_team, f.away_team, f.league "
                        "FROM bets b JOIN fixtures f ON f.match_id=b.match_id WHERE b.status='open'")
    for r in open_matches:
        att_h, def_h, att_a, def_a, meta = features.strength_for_match(
            r["home_team"], r["away_team"], r["league"])
        pred = poisson.predict(att_h, def_h, att_a, def_a, league_avg_goals=meta.get("league_avg"))
        hg = demo_data._poisson(pred["lam_home"], rnd)
        ag = demo_data._poisson(pred["lam_away"], rnd)
        settlement.enter_result(r["match_id"], hg, ag, source="selftest")

    import journal
    print("\n=== REPORT ===")
    print(journal.report_text(days=1))
    s = journal.summary(mode="paper")
    assert s["bets"] > 0, "no bets were booked - check AUTO_FOLLOW / risk limits"
    assert s["graded"] > 0, "no bets were settled"
    print("\nSELFTEST OK")


if __name__ == "__main__":
    main()

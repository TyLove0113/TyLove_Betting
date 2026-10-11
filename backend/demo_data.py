"""Synthetic demo data: lets you see the whole loop work before HKJC parsing is fixed.

It creates
  * one fake league with 10 teams and ~2 seasons of results
  * today's fixtures with kickoff times 45 / 75 / 150 minutes from now
  * HKJC-style odds with a 9% overround, where three matches are deliberately
    mispriced so the scanner has something to find

Run:  python cli.py demo --seed
      python cli.py scan
      python cli.py settle      (after entering results yourself)
"""
import random
from datetime import timedelta

import config
import db
import hkjc
from timeutil import now_hk, today_hk

TEAMS = ["曼聯", "利物浦", "阿仙奴", "車路士", "曼城", "熱刺",
         "紐卡素", "維拉", "白禮頓", "韋斯咸"]
LEAGUE = "英超(示範)"


def _true_strength():
    rnd = random.Random(42)
    return {t: {"att": 0.75 + rnd.random() * 0.6, "def": 0.75 + rnd.random() * 0.6} for t in TEAMS}


def _poisson(lam, rnd):
    import math
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L:
            return k
        k += 1
        if k > 12:
            return k


def seed(force=False):
    db.init_db()
    existing = db.q("SELECT COUNT(*) c FROM team_matches WHERE source='demo'", one=True)["c"]
    if existing and not force:
        print(f"demo data already present ({existing} matches) - use force to add more")
        return
    rnd = random.Random(7)
    st = _true_strength()
    today = today_hk()

    # ---- two seasons of results, ~180 matches
    hist = []
    for day in range(240, 5, -1):
        d = today - timedelta(days=day)
        pairs = list(zip(TEAMS[:-1], TEAMS[1:]))
        rnd.shuffle(pairs)
        for home, away in pairs[:2]:
            lg = 1.35
            lh = lg * st[home]["att"] / st[away]["def"] * 1.25
            la = lg * st[away]["att"] / st[home]["def"]
            hg, ag = _poisson(lh, rnd), _poisson(la, rnd)
            hist.append((d.strftime("%Y-%m-%d"), LEAGUE, home, away, hg, ag, round(lh, 2), round(la, 2)))
    for h in hist:
        db.ex("""INSERT OR IGNORE INTO team_matches
                 (played_on, league, home_team, away_team, home_goals, away_goals, home_xg, away_xg, source)
                 VALUES (?,?,?,?,?,?,?,?, 'demo')""", h)
    print(f"seeded {len(hist)} historical matches")

    # ---- today's fixtures: 3 kickoffs inside the scan window, 1 later
    now = now_hk()
    schedule = [(45, 0, 1), (75, 2, 3), (150, 4, 5), (300, 6, 7)]
    mispriced = {0, 2}                       # index of fixtures where we fake a soft price
    for i, (mins, hi, ai) in enumerate(schedule):
        ko = (now + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
        home, away = TEAMS[hi], TEAMS[ai]
        mid = hkjc.make_match_id(LEAGUE + " DEMO", ko, home, away)
        db.upsert_fixture({"match_id": mid, "source_id": f"demo{i}", "league": LEAGUE,
                           "kickoff_hk": ko, "home_team": home, "away_team": away,
                           "home_team_id": home, "away_team_id": away,
                           "status": "scheduled", "markets": "HAD,TTG"})
        _seed_odds(mid, home, away, mispriced=(i in mispriced))
    print(f"seeded {len(schedule)} fixtures for today ({now.strftime('%H:%M')} HK)")


def _seed_odds(match_id, home, away, mispriced=False):
    import poisson
    import features
    att_h, def_h, att_a, def_a, meta = features.strength_for_match(home, away, LEAGUE)
    true = poisson.predict(att_h, def_h, att_a, def_a, league_avg_goals=meta.get("league_avg"))
    margin = 1.09

    def price(p, extra=1.0):
        return round(1.0 / (p * margin * extra), 2)

    rows = [
        {"match_id": match_id, "market": "HAD", "line": "", "selection": "H", "odds": price(true["p_home"], 0.78 if mispriced else 1.0)},
        {"match_id": match_id, "market": "HAD", "line": "", "selection": "D", "odds": price(true["p_draw"])},
        {"match_id": match_id, "market": "HAD", "line": "", "selection": "A", "odds": price(true["p_away"])},
    ]
    p_over = true["over"].get(2.5, 0.5)
    rows += [
        {"match_id": match_id, "market": "TTG", "line": "2.5", "selection": "OVER",
         "odds": price(p_over, 0.80 if mispriced else 1.0)},
        {"match_id": match_id, "market": "TTG", "line": "2.5", "selection": "UNDER", "odds": price(1 - p_over)},
    ]
    db.save_odds_batch(rows)


def clear():
    for t in ("team_matches", "bets", "value_bets", "predictions", "odds_snapshots", "results"):
        db.ex(f"DELETE FROM {t} WHERE 1=1" if t != "team_matches" else "DELETE FROM team_matches WHERE source='demo'")
    db.ex("DELETE FROM fixtures WHERE source_id LIKE 'demo%'")
    print("demo data cleared")

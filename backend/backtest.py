"""Walk-forward backtest - the only honest way to tune this system.

What it does
  1. reads match history (CSV: date,league,home_team,away_team,home_goals,away_goals)
  2. for every match, in date order, predicts it using ONLY earlier matches
     (no look-ahead, no leakage)
  3. scores the probabilities: log loss, Brier, accuracy, and a comparison
     against the naive "always league average" baseline
  4. optionally re-prices the model against historical closing odds
     (CSV: date,home_team,away_team,market,line,selection,odds) and reports
     ROI for a range of EV thresholds, so a threshold is chosen by evidence
     instead of by feel.

Metrics you should look at first: log loss (lower is better, ~1.099 = random
3-way), Brier (multi-class), and the ROI column of the sweep.
"""
import csv
import json
import math
import os
from collections import defaultdict

import config
import db
import features
import poisson
from engine import MODEL_VERSION
from timeutil import now_hk


def load_history(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            r = {k.lower().strip(): (v or "").strip() for k, v in r.items()}
            try:
                hg, ag = int(r["home_goals"]), int(r["away_goals"])
            except (KeyError, ValueError):
                continue
            date = (r.get("date") or "").replace("/", "-")
            if len(date) == 10 and date[2] == "-":
                date = f"{date[6:]}-{date[3:5]}-{date[:2]}"
            rows.append({"date": date, "league": r.get("league", ""), "home": r.get("home_team"),
                         "away": r.get("away_team"), "hg": hg, "ag": ag})
    rows.sort(key=lambda x: x["date"])
    return rows


def load_odds(csv_path):
    """date,home_team,away_team,market,line,selection,odds -> {(date,home,away): {(mkt,line,sel): odds}}"""
    out = defaultdict(dict)
    if not csv_path or not os.path.exists(csv_path):
        return out
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            r = {k.lower().strip(): (v or "").strip() for k, v in r.items()}
            key = (r.get("date", "").replace("/", "-"), r.get("home_team"), r.get("away_team"))
            try:
                odds = float(r["odds"])
            except (KeyError, ValueError):
                continue
            out[key][(r.get("market", "HAD").upper(), str(r.get("line", "")), r.get("selection", "").upper())] = odds
    return out


def _score(p_h, p_d, p_a, hg, ag):
    """log loss + brier for one 3-way prediction."""
    actual = 0 if hg > ag else (1 if hg == ag else 2)
    p = [max(min(p_h, 1 - 1e-9), 1e-9), max(min(p_d, 1 - 1e-9), 1e-9), max(min(p_a, 1 - 1e-9), 1e-9)]
    s = sum(p)
    p = [x / s for x in p]
    ll = -math.log(p[actual])
    brier = sum((p[i] - (1.0 if i == actual else 0.0)) ** 2 for i in range(3))
    return ll, brier, actual, p


def run(csv_path=None, odds_csv=None, sweep=False, min_history=30):
    db.init_db()
    rows = load_history(csv_path or os.path.join(config.DATA_DIR, "history.csv"))
    if not rows:
        print("冇歷史資料。放一份 CSV（date,league,home_team,away_team,home_goals,away_goals）"
              "並用 --csv 指路徑，或者先 import-csv。")
        return
    odds_map = load_odds(odds_csv)

    # in-memory history so the walk-forward never touches the DB
    hist = defaultdict(list)          # team -> [(date, gf, ga)]
    lg_goals, lg_n = 0.0, 0
    mh_sum, ma_sum, mh_n = 0.0, 0.0, 0

    n = ll_sum = br_sum = 0
    ll_base = br_sum_base = 0.0
    correct = 0
    results = []
    book_value = []                   # (ev, won, odds, stake)
    thresholds = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]

    for i, r in enumerate(rows):
        home, away, date = r["home"], r["away"], r["date"]
        hs, as_ = hist.get(home, []), hist.get(away, [])
        lg, hf, af = _rates(lg_goals, lg_n, mh_sum, ma_sum, mh_n)
        att_h, def_h = _strength(hs, lg)
        att_a, def_a = _strength(as_, lg)
        res = poisson.predict(att_h, def_h, att_a, def_a, league_avg_goals=lg,
                              home_factor=hf, away_factor=af)

        # baseline: pure league average, no team information
        base = poisson.predict(1.0, 1.0, 1.0, 1.0, league_avg_goals=lg,
                               home_factor=hf, away_factor=af)

        ll, br, actual, p = _score(res["p_home"], res["p_draw"], res["p_away"], r["hg"], r["ag"])
        lb, bb, _, _ = _score(base["p_home"], base["p_draw"], base["p_away"], r["hg"], r["ag"])

        if i >= min_history:                       # only score once there is real history
            ll_sum += ll
            br_sum += br
            ll_base += lb
            br_sum_base += bb
            n += 1
            pred = max(range(3), key=lambda i: p[i])
            correct += (pred == actual)

            key = (date, home, away)
            if key in odds_map:
                had = {k[2]: v for k, v in odds_map[key].items() if k[0] == "HAD"}
                if len(had) == 3:
                    from value import devig_proportional, ev_pct
                    fair = devig_proportional([had.get("H"), had.get("D"), had.get("A")])
                    model = {"H": res["p_home"], "D": res["p_draw"], "A": res["p_away"]}
                    for sel, fp, o in zip(("H", "D", "A"), fair, (had.get("H"), had.get("D"), had.get("A"))):
                        ev = ev_pct(model[sel], o)
                        won = (sel == ("H" if r["hg"] > r["ag"] else "D" if r["hg"] == r["ag"] else "A"))
                        book_value.append({"ev": ev, "odds": o, "won": won})

        results.append((date, home, away, r["hg"], r["ag"],
                        round(res["p_home"], 3), round(res["p_draw"], 3), round(res["p_away"], 3),
                        round(ll, 4)))

        # append to history AFTER predicting (no leakage)
        hist[home].append((date, r["hg"], r["ag"]))
        hist[away].append((date, r["ag"], r["hg"]))
        lg_goals += r["hg"] + r["ag"]
        lg_n += 2
        mh_sum += r["hg"]
        ma_sum += r["ag"]
        mh_n += 1

    out = {
        "matches_total": len(rows),
        "matches_scored": n,
        "model": {"log_loss": round(ll_sum / n, 4) if n else None,
                  "brier": round(br_sum / n, 4) if n else None,
                  "accuracy": round(correct / n, 4) if n else None},
        "baseline_league_average": {"log_loss": round(ll_base / n, 4) if n else None,
                                    "brier": round(br_sum_base / n, 4) if n else None},
        "generated_at": now_hk().strftime("%Y-%m-%d %H:%M:%S"),
        "model_version": MODEL_VERSION,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out["model"]["log_loss"] and out["model"]["log_loss"] > 1.099:
        print("⚠️ log loss 比隨機更差 - 唔好落真錢。")

    if book_value:
        print("\nEV 門檻掃描（假設平注 1 單位）")
        print(f"{'threshold':>10} {'bets':>6} {'hit':>7} {'ROI':>8} {'CLV proxy':>10}")
        sweep_rows = []
        for t in thresholds:
            sel = [b for b in book_value if b["ev"] is not None and b["ev"] >= t and b["odds"] > config.MIN_ODDS]
            if not sel:
                print(f"{t*100:>9.1f}% {0:>6}")
                continue
            staked = sum(1 for _ in sel)
            ret = sum(b["odds"] for b in sel if b["won"])
            roi = (ret - staked) / staked
            hit = sum(1 for b in sel if b["won"]) / len(sel)
            print(f"{t*100:>9.1f}% {len(sel):>6} {hit*100:>6.1f}% {roi*100:>7.2f}%")
            sweep_rows.append({"threshold": t, "bets": len(sel), "roi": roi, "hit": hit})
        out["sweep"] = sweep_rows
    else:
        print("\n（冇歷史賠率 CSV，跳過 ROI 掃描。賠率 CSV 格式："
              "date,home_team,away_team,market,line,selection,odds）")

    outp = os.path.join(config.DATA_DIR, "backtest_report.json")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(outp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("\nreport:", outp)
    return out


def _rates(lg_goals, lg_n, mh_sum, ma_sum, mh_n, min_matches=20):
    """Neutral base rate + home/away multipliers measured only from matches seen so far."""
    if mh_n < min_matches or not mh_sum or not ma_sum:
        return (lg_goals / lg_n if lg_n else config.LEAGUE_BASE_GOALS,
                config.HOME_FACTOR, config.AWAY_FACTOR)
    mh, ma = mh_sum / mh_n, ma_sum / mh_n
    base = (mh + ma) / 2.0
    if base <= 0:
        return config.LEAGUE_BASE_GOALS, config.HOME_FACTOR, config.AWAY_FACTOR
    return base, mh / base, ma / base


def _strength(team_hist, lg, window=None):
    window = window or config.FORM_WINDOW
    recent = team_hist[-window:]
    if not recent:
        return 1.0, 1.0
    gf = sum(x[1] for x in recent) / len(recent)
    ga = sum(x[2] for x in recent) / len(recent)
    w = len(recent) / (len(recent) + config.SHRINK_MATCHES)
    att = 1.0 + w * ((gf / lg) - 1.0) if lg else 1.0
    dfn = 1.0 + w * ((ga / lg) - 1.0) if lg else 1.0
    return max(0.25, min(att, 3.5)), max(0.25, min(dfn, 3.5))

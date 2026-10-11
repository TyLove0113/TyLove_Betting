"""Prediction engine: fixture -> probabilities (1X2 + totals)."""
import config
import db
import features
import poisson
from timeutil import now_hk

MODEL_VERSION = f"poisson-dc-{config.VERSION}"


def default_lines(fixture):
    """Which total-goal line to price: follow the line HKJC has opened (2.5 default)."""
    rows = db.latest_odds(fixture["match_id"], market="TTG")
    lines = sorted({float(r["line"]) for r in rows if r["line"] and _isnum(r["line"])})
    return lines or [2.5]


def _isnum(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def predict_fixture(fixture, save=True, lines=None):
    lines = lines or default_lines(fixture)
    att_h, def_h, att_a, def_a, meta = features.strength_for_match(
        fixture["home_team"], fixture["away_team"], fixture.get("league") or "")
    res = poisson.predict(att_h, def_h, att_a, def_a, league_avg_goals=meta.get("league_avg"),
                          lines=tuple(lines), home_factor=meta.get("home_factor"),
                          away_factor=meta.get("away_factor"))

    rec = {
        "match_id": fixture["match_id"],
        "created_at": now_hk().strftime("%Y-%m-%d %H:%M:%S"),
        "model_version": MODEL_VERSION,
        "p_home": res["p_home"], "p_draw": res["p_draw"], "p_away": res["p_away"],
        "lam_home": res["lam_home"], "lam_away": res["lam_away"],
        "p_over25": res["over"].get(2.5),
        "p_under25": (1 - res["over"][2.5]) if 2.5 in res["over"] else None,
        "baseline": 1 if meta.get("thin") else 0,
        "note": f"hist h={meta['home_played']} a={meta['away_played']}",
    }
    if save:
        db.ex("""INSERT INTO predictions (match_id, created_at, model_version, p_home, p_draw, p_away,
                                          lam_home, lam_away, p_over25, p_under25, baseline, note)
                 VALUES (:match_id,:created_at,:model_version,:p_home,:p_draw,:p_away,
                         :lam_home,:lam_away,:p_over25,:p_under25,:baseline,:note)""", rec)
    rec["over"] = res["over"]
    rec["matrix"] = res["matrix"]
    rec["meta"] = meta
    rec["lines"] = list(lines)
    return rec


def predict_pair(home_team, away_team, league="", lines=(2.5,)):
    """Ad-hoc prediction without a fixture row (used by /sim and tests)."""
    att_h, def_h, att_a, def_a, meta = features.strength_for_match(home_team, away_team, league)
    res = poisson.predict(att_h, def_h, att_a, def_a, league_avg_goals=meta.get("league_avg"),
                          lines=tuple(lines), home_factor=meta.get("home_factor"),
                          away_factor=meta.get("away_factor"))
    res["meta"] = meta
    return res

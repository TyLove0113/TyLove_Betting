"""Team strength from match history.

Two defects in the old programme are fixed here:
  1. leakage - the rolling window included the match being predicted. Every
     history read here is strictly *before* the match date (``as_of``).
  2. rate shrinkage - a team with 2 matches played must not look like a
     finished article; strengths are shrunk toward the league mean by
     ``config.SHRINK_MATCHES``.
"""
import math
from collections import defaultdict

import config
import db

DEFAULT_LEAGUE_AVG = config.LEAGUE_BASE_GOALS


def team_id(name):
    """Stable, source-independent key for a team name."""
    if not name:
        return ""
    s = "".join(str(name).split()).lower()
    for junk in ("fc", "cf", "sc", "afc", "體育會", "足球俱樂部"):
        if s.endswith(junk) and len(s) > len(junk) + 2:
            s = s[: -len(junk)]
    return s


def _window_matches(team, league, as_of, limit):
    """Matches played by `team` strictly before `as_of` (HK date string)."""
    rows = db.q("""SELECT played_on, league, home_team, away_team, home_goals, away_goals, home_xg, away_xg
                   FROM team_matches
                   WHERE (home_team=? OR away_team=?) AND played_on < ?
                   ORDER BY played_on DESC LIMIT ?""",
                (team, team, as_of or "9999-12-31", limit))
    return rows


def league_average(league=None, as_of=None):
    """Average goals scored per team per match across the whole history table."""
    sql = "SELECT AVG(g) FROM (SELECT home_goals AS g FROM team_matches WHERE home_goals IS NOT NULL"
    args = []
    if as_of:
        sql += " AND played_on < ?"
        args.append(as_of)
    sql += " UNION ALL SELECT away_goals FROM team_matches WHERE away_goals IS NOT NULL"
    if as_of:
        sql += " AND played_on < ?"
        args.append(as_of)
    sql += ")"
    row = db.q(sql, tuple(args), one=True)
    val = list(row.values())[0] if row else None
    return float(val) if val else DEFAULT_LEAGUE_AVG


def league_factors(league=None, as_of=None, min_matches=20):
    """Neutral base rate + home/away multipliers measured from the history table.

    Returns (base, home_factor, away_factor, n_matches). With real data this is
    what stops home advantage from being counted twice: the base is the mean of
    home and away goals, and the two factors reproduce the observed split.
    """
    sql = ("SELECT AVG(home_goals), AVG(away_goals), COUNT(*) FROM team_matches "
           "WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL")
    args = []
    if as_of:
        sql += " AND played_on < ?"
        args.append(as_of)
    row = db.q(sql, tuple(args), one=True)
    vals = list(row.values()) if row else []
    mh, ma, n = (vals + [None, None, 0])[:3]
    if not n or n < min_matches or not mh or not ma:
        return league_average(league, as_of), config.HOME_FACTOR, config.AWAY_FACTOR, int(n or 0)
    base = (float(mh) + float(ma)) / 2.0
    if base <= 0:
        return league_average(league, as_of), config.HOME_FACTOR, config.AWAY_FACTOR, int(n)
    return base, float(mh) / base, float(ma) / base, int(n)


def team_stats(team, league, as_of, window=None):
    """Attacking / defensive rates for one team, as of a date (no leakage)."""
    window = window or config.FORM_WINDOW
    rows = _window_matches(team, league, as_of, window)
    played = len(rows)
    if played == 0:
        return {"played": 0, "gf": None, "ga": None, "att": 1.0, "def": 1.0, "ppg": None}

    gf = ga = 0
    for r in rows:
        if r["home_team"] == team:
            gf += r["home_goals"] or 0
            ga += r["away_goals"] or 0
        else:
            gf += r["away_goals"] or 0
            ga += r["home_goals"] or 0
    gf_pm = gf / played
    ga_pm = ga / played
    lg = league_average(league, as_of) or DEFAULT_LEAGUE_AVG

    # raw multiplicative strength, then shrink toward 1.0
    w = played / (played + config.SHRINK_MATCHES)
    att = 1.0 + w * ((gf_pm / lg) - 1.0)
    dfn = 1.0 + w * ((ga_pm / lg) - 1.0)
    return {
        "played": played, "gf": gf_pm, "ga": ga_pm,
        "att": max(0.25, min(att, 3.5)),
        "def": max(0.25, min(dfn, 3.5)),
        "ppg": None,
    }


def strength_for_match(home_team, away_team, league, as_of=None):
    """Return (att_h, def_h, att_a, def_a, meta). meta tells you how thin the data is."""
    from timeutil import today_hk
    as_of = as_of or today_hk().strftime("%Y-%m-%d")
    hs = team_stats(home_team, league, as_of)
    as_ = team_stats(away_team, league, as_of)
    lf = league_factors(league, as_of)
    meta = {
        "home_played": hs["played"], "away_played": as_["played"],
        "thin": (hs["played"] + as_["played"]) < 6,
        "league_avg": lf[0],
        "home_factor": lf[1],
        "away_factor": lf[2],
        "league_matches": lf[3],
    }
    return hs["att"], hs["def"], as_["att"], as_["def"], meta


# ---------------------------------------------------------------- strength table
def build_strength_table(as_of=None, min_matches=4):
    """One-pass table of every team's attack/defence - handy for /ratings."""
    from timeutil import today_hk
    as_of = as_of or today_hk().strftime("%Y-%m-%d")
    teams = db.q("""SELECT DISTINCT home_team AS t, league FROM team_matches
                    UNION SELECT DISTINCT away_team, league FROM team_matches""")
    out = []
    for row in teams:
        s = team_stats(row["t"], row["league"], as_of)
        if s["played"] >= min_matches:
            out.append({"team": row["t"], "league": row["league"], **s})
    out.sort(key=lambda r: r["att"] - r["def"], reverse=True)
    return out


# ---------------------------------------------------------------- importing history
def import_results_csv(path, league=None, source="csv"):
    """CSV with columns: date,home_team,away_team,home_goals,away_goals[,league,xg...]"""
    import csv
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            r = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
            try:
                hg, ag = int(r.get("home_goals") or r.get("fthg")), int(r.get("away_goals") or r.get("ftag"))
            except (TypeError, ValueError):
                continue
            date = (r.get("date") or r.get("played_on") or "").replace("/", "-")
            if len(date) == 10 and date[2] == "-":      # dd-mm-yyyy
                date = f"{date[6:]}-{date[3:5]}-{date[:2]}"
            try:
                db.ex("""INSERT OR IGNORE INTO team_matches
                         (played_on, league, home_team, away_team, home_goals, away_goals, home_xg, away_xg, source)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (date, r.get("league") or league or "", r.get("home_team") or r.get("hometeam"),
                       r.get("away_team") or r.get("awayteam"), hg, ag,
                       _f(r.get("home_xg")), _f(r.get("away_xg")), source))
                n += 1
            except Exception:
                continue
    return n


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

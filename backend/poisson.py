"""Poisson / Dixon-Coles scoring engine.

Why this file exists: the old programme computed
    lam = att - def + home_adv          # WRONG (subtracting rates)
which produced lambdas around 0.4. With total goals ~0.8 a match, every
over/under call was biased to under and 1X2 was nonsense.

Here lambdas are built on a log scale, which is the correct space for rates:
    log lam_home = base + att_home - def_away + home_adv
    log lam_away = base + att_away - def_home
where base = log(league average goals per team), and att/def are shrunk
deviations from the league mean (so a team with no history falls back to the
league average instead of to zero).
"""
import math
from math import exp, factorial, log

import config


def poisson_p(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * exp(-lam) / factorial(k)


def _tau(h, a, lh, la, rho):
    """Dixon-Coles low-score correction."""
    if h == 0 and a == 0:
        return 1.0 - lh * la * rho
    if h == 0 and a == 1:
        return 1.0 + lh * rho
    if h == 1 and a == 0:
        return 1.0 + la * rho
    if h == 1 and a == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lh, la, max_goals=None, rho=None):
    """Return (matrix, total) where matrix[h][a] = P(home h, away a)."""
    max_goals = max_goals or config.MAX_GOALS
    rho = config.DIXON_COLES_RHO if rho is None else rho
    m = []
    total = 0.0
    for h in range(max_goals + 1):
        row = []
        ph = poisson_p(h, lh)
        for a in range(max_goals + 1):
            p = ph * poisson_p(a, la) * _tau(h, a, lh, la, rho)
            p = max(p, 0.0)
            row.append(p)
            total += p
        m.append(row)
    if total > 0:
        m = [[p / total for p in row] for row in m]
    return m, 1.0


def markets_from_matrix(m, lines=(2.5,)):
    """1X2 + over/under for the requested total-goal lines."""
    n = len(m)
    p_home = p_draw = p_away = 0.0
    for h in range(n):
        for a in range(n):
            p = m[h][a]
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
    over = {}
    for line in lines:
        need = math.floor(line) + 1          # 2.5 -> 3 goals or more
        s = 0.0
        for h in range(n):
            for a in range(n):
                if h + a >= need:
                    s += m[h][a]
        over[line] = s
    total = p_home + p_draw + p_away
    if total > 0:
        p_home, p_draw, p_away = p_home / total, p_draw / total, p_away / total
    return {
        "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
        "over": {ln: over[ln] for ln in lines},
        "over_2_5": over.get(2.5),
    }


def lambdas_from_strength(att_home, def_home, att_away, def_away, league_avg_goals=None,
                          home_factor=None, away_factor=None):
    """att_* / def_* are multiplicative strength factors around 1.0 (1.0 = league average).

    ``league_avg_goals`` is the neutral base rate (mean goals per team over mixed
    venues), so home advantage enters as a multiplier measured from real data
    (features.league_factors) rather than as a constant added on top.
    """
    base = log(max(league_avg_goals or config.LEAGUE_BASE_GOALS, 0.05))
    hf = config.HOME_FACTOR if home_factor is None else home_factor
    af = config.AWAY_FACTOR if away_factor is None else away_factor
    lh = exp(base + log(max(att_home, 0.05)) - log(max(def_away, 0.05))) * max(hf, 0.5)
    la = exp(base + log(max(att_away, 0.05)) - log(max(def_home, 0.05))) * max(af, 0.5)
    return _clamp(lh), _clamp(la)


def _clamp(lam, lo=0.10, hi=6.0):
    return float(min(max(lam, lo), hi))


def predict(att_home, def_home, att_away, def_away, league_avg_goals=None, lines=(2.5,),
            home_factor=None, away_factor=None):
    lh, la = lambdas_from_strength(att_home, def_home, att_away, def_away, league_avg_goals,
                                   home_factor, away_factor)
    m, _ = score_matrix(lh, la)
    out = markets_from_matrix(m, lines)
    out["lam_home"] = lh
    out["lam_away"] = la
    out["matrix"] = m
    return out


def fair_odds(p):
    return None if not p or p <= 0 else 1.0 / p

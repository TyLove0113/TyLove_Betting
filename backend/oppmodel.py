"""對手校正攻守指數：把「入球率」升級成「對住邊個對手入幾多球」。

同 backtest_model.py 嘅 v2 模型一致（阻尼迭代 + 時間衰減 + 按場數收縮）。
只在該聯賽有足夠歷史（>= MIN_LEAGUE_N 場）時啟用，否則沿用舊做法。
"""
import math
from collections import defaultdict

import config
import db

HALF_LIFE_DAYS = 150.0
MIN_LEAGUE_N = 120
_CACHE = {}


def _load(league, as_of):
    rows = db.q("""SELECT played_on, home_team, away_team, home_goals, away_goals
                   FROM team_matches
                   WHERE league=? AND played_on<=? AND home_goals IS NOT NULL
                   ORDER BY played_on""", (league, as_of))
    return rows or []


def _days(a, b):
    from timeutil import parse_hk
    da, db_ = parse_hk(f"{a[:10]} 00:00"), parse_hk(f"{b[:10]} 00:00")
    if not da or not db_:
        return 0
    return max(0, (db_ - da).days)


def fit(league, as_of):
    """Return (att, dfn, avg, fac, n) or None if not enough history."""
    key = (league, as_of, db.q("SELECT COUNT(*) c FROM team_matches", one=True)["c"])
    if key in _CACHE:
        return _CACHE[key]
    rows = _load(league, as_of)
    if len(rows) < MIN_LEAGUE_N:
        _CACHE[key] = None
        return None

    # 聯賽基準（每隊每場平均入球 + 主客場因子）
    tg = [x["home_goals"] + x["away_goals"] for x in rows]
    avg_all = (sum(tg) / len(tg) / 2.0) or 1.35
    hg = sum(x["home_goals"] for x in rows) / len(rows)
    ag = sum(x["away_goals"] for x in rows) / len(rows)
    fac = (hg / ag) if ag > 0 else 1.15
    if not (0.7 <= fac <= 2.0):
        fac = 1.15
    as_of_s = as_of[:10]
    w = [0.5 ** (_days(x["played_on"], as_of_s) / HALF_LIFE_DAYS) for x in rows]
    home_f, away_f = 2 * fac / (1 + fac), 2 / (1 + fac)

    att = defaultdict(lambda: 1.0)
    dfn = defaultdict(lambda: 1.0)
    cnt = defaultdict(int)
    for x in rows:
        cnt[x["home_team"]] += 1
        cnt[x["away_team"]] += 1

    for _ in range(40):
        na, da = defaultdict(float), defaultdict(float)
        nd, dd = defaultdict(float), defaultdict(float)
        for i, x in enumerate(rows):
            h, a, wi = x["home_team"], x["away_team"], w[i]
            eh = avg_all * home_f * att[h] * dfn[a]
            ea = avg_all * away_f * att[a] * dfn[h]
            na[h] += wi * x["home_goals"]; da[h] += wi * eh
            na[a] += wi * x["away_goals"]; da[a] += wi * ea
            nd[a] += wi * x["home_goals"]; dd[a] += wi * eh
            nd[h] += wi * x["away_goals"]; dd[h] += wi * ea
        for t in set(list(na) + list(nd)):
            if da[t] > 0:
                att[t] = min(2.2, max(0.45, att[t] ** 0.7 * (na[t] / da[t]) ** 0.3))
            if dd[t] > 0:
                dfn[t] = min(2.2, max(0.45, dfn[t] ** 0.7 * (nd[t] / dd[t]) ** 0.3))
        for d in (att, dfn):
            if d:
                gm = math.exp(sum(math.log(v) for v in d.values()) / len(d))
                if gm > 0:
                    for t in d:
                        d[t] = min(2.2, max(0.45, d[t] / gm))

    k = float(getattr(config, "SHRINK_MATCHES", 6))
    for t in list(att):
        wt = cnt[t] / (cnt[t] + k)
        att[t] = 1 + (att[t] - 1) * wt
        dfn[t] = 1 + (dfn[t] - 1) * wt
    out = (dict(att), dict(dfn), avg_all, fac, len(rows))
    _CACHE[key] = out
    return out

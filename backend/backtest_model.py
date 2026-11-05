"""離線回測：比較三個模型的預測準確度（Brier score，越低越好）。

M0 聯賽平均（等於冇球隊資訊）
M1 現時系統：近 N 場入球率 + shrinkage（冇對手校正、冇時間衰減）
M2 v2：對手校正攻守指數 + 時間衰減 + 休息日數

只用香港馬會歷史賽果（team_matches），唔需要賠率。
用法：DB_PATH=/tmp/x.db python3 backtest_model.py [測試日數]
"""
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime

DB = os.environ.get("DB_PATH", "data/football.db")
TEST_DAYS = int(os.environ.get("TEST_DAYS", "45"))
RHO = -0.05          # Dixon-Coles 低比分修正
HALF_LIFE_DAYS = 45  # v2 時間衰減半衰期
FORM_WINDOW = 10     # M1 用近幾場
SHRINK = 4.0


def load():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT played_on, league, home_team, away_team, home_goals, away_goals
                          FROM team_matches
                          WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
                          ORDER BY played_on""").fetchall()
    con.close()
    return [dict(r) for r in rows]


def _d(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _grid(lh, la, maxg=8):
    ph = [_pois(i, lh) for i in range(maxg + 1)]
    pa = [_pois(i, la) for i in range(maxg + 1)]
    m = [[ph[i] * pa[j] for j in range(maxg + 1)] for i in range(maxg + 1)]
    # Dixon-Coles 低比分修正
    for i in range(2):
        for j in range(2):
            if i == 1 and j == 1:
                m[i][j] *= 1 - RHO
            elif i == 0 and j == 0:
                m[i][j] *= 1 + RHO
            elif i + j == 1:
                m[i][j] *= 1 + RHO
    s = sum(sum(r) for r in m)
    return [row[:] for row in m], s


def outcome_probs(lh, la):
    m, s = _grid(lh, la)
    h = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i > j) / s
    d = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i == j) / s
    a = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i < j) / s
    return h, d, a


def league_base(matches, league, shrink=20.0):
    """聯賽基準；樣本少就向全局平均收縮（避免 3 場定生死）。"""
    sub = [x for x in matches if not league or x["league"] == league]
    gsub = [x["home_goals"] for x in sub] + [x["away_goals"] for x in sub]
    gall = [x["home_goals"] for x in matches] + [x["away_goals"] for x in matches]
    avg_all = (sum(gall) / len(gall)) if gall else 1.40
    hf_all = sum(x["home_goals"] for x in matches); af_all = sum(x["away_goals"] for x in matches)
    fac_all = (hf_all / af_all) if af_all else 1.15
    n = len(gsub)
    wsub = n / (n + shrink)
    avg = wsub * ((sum(gsub) / n) if n else avg_all) + (1 - wsub) * avg_all
    hf = sum(x["home_goals"] for x in sub); af = sum(x["away_goals"] for x in sub)
    fac = wsub * ((hf / af) if af else fac_all) + (1 - wsub) * fac_all
    return avg, fac, n
    g = [x["home_goals"] for x in sub]
    g += [x["away_goals"] for x in matches if not league or x["league"] == league]
    avg = sum(g) / len(g) / 2 if g else 1.35
    hf = sum(x["home_goals"] for x in matches if not league or x["league"] == league)
    af = sum(x["away_goals"] for x in matches if not league or x["league"] == league)
    fac = (hf / af) if af else 1.15
    return avg, fac, len(g) // 2


def m0_probs(matches, league):
    avg, fac, _ = league_base(matches, league)
    # 主場優勢：總入球 split 按 fac
    lh = avg * (2 * fac / (1 + fac))
    la = avg * (2 / (1 + fac))
    return outcome_probs(lh, la)


def m1_probs(train, match, league):
    """現時系統：隊近 10 場入球率，向聯賽平均收縮。"""
    avg, fac, _ = league_base(train, league)

    def st(team):
        rs = [x for x in train if x["home_team"] == team or x["away_team"] == team][-FORM_WINDOW:]
        if not rs:
            return 1.0, 1.0, 0
        gf = ga = 0
        for r in rs:
            if r["home_team"] == team:
                gf += r["home_goals"]; ga += r["away_goals"]
            else:
                gf += r["away_goals"]; ga += r["home_goals"]
        n = len(rs)
        w = n / (n + SHRINK)
        return 1 + w * ((gf / n / avg) - 1), 1 + w * ((ga / n / avg) - 1), n

    ah, dh, _ = st(match["home_team"])
    aa, da, _ = st(match["away_team"])
    lh = avg * (2 * fac / (1 + fac)) * ah * da
    la = avg * (2 / (1 + fac)) * aa * dh
    return outcome_probs(lh, la)


def v2_fit(train, league, as_of):
    """對手校正攻守指數（阻尼乘法迭代 + 時間衰減）。att/dfn 幾何平均固定為 1。"""
    rows = [x for x in train if x["league"] == league] or train
    if not rows:
        return {}, {}, 1.35, 1.15
    avg, fac, _ = league_base(train, league)
    home_f = 2 * fac / (1 + fac)
    away_f = 2 / (1 + fac)
    asof = _d(as_of)
    w = [0.5 ** (max(0, (asof - _d(x["played_on"])).days) / HALF_LIFE_DAYS) for x in rows]
    att, dfn = defaultdict(lambda: 1.0), defaultdict(lambda: 1.0)
    cnt = defaultdict(int)
    for x in rows:
        cnt[x["home_team"]] += 1
        cnt[x["away_team"]] += 1
    for _ in range(60):
        num_a, den_a = defaultdict(float), defaultdict(float)
        num_d, den_d = defaultdict(float), defaultdict(float)
        for i, x in enumerate(rows):
            h, a, wi = x["home_team"], x["away_team"], w[i]
            eh = avg * home_f * att[h] * dfn[a]
            ea = avg * away_f * att[a] * dfn[h]
            num_a[h] += wi * x["home_goals"]; den_a[h] += wi * eh
            num_a[a] += wi * x["away_goals"]; den_a[a] += wi * ea
            num_d[a] += wi * x["home_goals"]; den_d[a] += wi * eh
            num_d[h] += wi * x["away_goals"]; den_d[h] += wi * ea
        for t in set(list(num_a) + list(num_d)):
            if den_a[t] > 0:
                r = num_a[t] / den_a[t]
                att[t] = min(2.2, max(0.45, att[t] ** 0.7 * r ** 0.3))   # 阻尼
            if den_d[t] > 0:
                r = num_d[t] / den_d[t]
                dfn[t] = min(2.2, max(0.45, dfn[t] ** 0.7 * r ** 0.3))
        for d in (att, dfn):                       # 幾何平均 = 1，消除尺度漂移
            if d:
                gm = math.exp(sum(math.log(v) for v in d.values()) / len(d))
                if gm > 0:
                    for t in d: d[t] = min(2.2, max(0.45, d[t] / gm))
    k = 6.0                                   # 收縮強度：場數少就拉返 1
    for t in list(att):
        w = cnt[t] / (cnt[t] + k)
        att[t] = 1 + (att[t] - 1) * w
        dfn[t] = 1 + (dfn[t] - 1) * w
    return att, dfn, avg, fac


def rest_days(train, team, as_of):
    ds = [_d(x["played_on"]) for x in train if team in (x["home_team"], x["away_team"])]
    if not ds:
        return 99
    return (as_of - max(ds)).days


def m2_probs(att, dfn, avg, fac, match, train):
    lh = avg * (2 * fac / (1 + fac)) * att.get(match["home_team"], 1.0) * dfn.get(match["away_team"], 1.0)
    la = avg * (2 / (1 + fac)) * att.get(match["away_team"], 1.0) * dfn.get(match["home_team"], 1.0)
    # 休息日數：少於 4 日有懲罰（每少一日 -2%）
    asof = _d(match["played_on"])
    rh, ra = rest_days(train, match["home_team"], asof), rest_days(train, match["away_team"], asof)
    lh *= max(0.90, 1 - 0.02 * max(0, 4 - rh))
    la *= max(0.90, 1 - 0.02 * max(0, 4 - ra))
    return outcome_probs(lh, la)


def brier(p, y):
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y))


def main():
    allm = load()
    if len(allm) < 400:
        print("歷史資料不足（需要先 /backfill）。現有", len(allm))
        return
    days = sorted({x["played_on"][:10] for x in allm})
    cut = days[-TEST_DAYS] if len(days) > TEST_DAYS else days[len(days) // 2]
    test = [x for x in allm if x["played_on"][:10] >= cut]
    train_all = [x for x in allm if x["played_on"][:10] < cut]
    print(f"訓練 {len(train_all)} 場（< {cut}）· 測試 {len(test)} 場")

    res = {"M0": [], "M1": [], "M2": []}
    hits = {"M0": 0, "M1": 0, "M2": 0}
    cache = {}
    for m in test:
        lg = m["league"]
        y = [1 if m["home_goals"] > m["away_goals"] else 0,
             1 if m["home_goals"] == m["away_goals"] else 0,
             1 if m["home_goals"] < m["away_goals"] else 0]
        p0 = m0_probs(train_all, lg)
        p1 = m1_probs(train_all, m, lg)
        key = (lg, m["played_on"][:10])
        if key not in cache:
            cache[key] = v2_fit(train_all, lg, m["played_on"])
        att, dfn, avg, fac = cache[key]
        p2 = m2_probs(att, dfn, avg, fac, m, train_all)
        for name, p in (("M0", p0), ("M1", p1), ("M2", p2)):
            res[name].append(brier(p, y))
            if max(range(3), key=lambda i: p[i]) == max(range(3), key=lambda i: y[i]):
                hits[name] += 1

    print(f"\n{'模型':6} {'Brier':>8} {'準確率':>8}   說明")
    desc = {"M0": "聯賽平均（冇球隊資訊）", "M1": "現系統（入球率+收縮）", "M2": "v2 對手校正+時間衰減+休息日"}
    for k in ("M0", "M1", "M2"):
        b = sum(res[k]) / len(res[k])
        print(f"{k:6} {b:8.4f} {hits[k]/len(test)*100:7.1f}%   {desc[k]}")

    print("\nBrier 越低越好；差距 > 0.002 才算有用（隨機誤差約 ±0.003）。")


if __name__ == "__main__":
    main()

"""盤口抽水（overround）量度：同一個模型，抽水越低就越易打和。

overround = Σ(1/賠率)。3-way 主客和長期 13%+，2-way（大細／讓球）約 7–9%。
"""
import config
import db

LABEL = {"HAD": "主客和 3-way", "TTG": "入球大細 2-way"}


def _pairs(rows):
    return [(r["selection"], float(r["odds"])) for r in rows if r["odds"] and float(r["odds"]) > 1]


def match_vig(match_id, market, line=""):
    """某場某盤某條線嘅 overround；資料不足回 None。"""
    rows = db.q("""SELECT selection, odds FROM odds_snapshots
                   WHERE match_id=? AND market=? AND IFNULL(line,'')=?
                     AND taken_at=(SELECT MAX(taken_at) FROM odds_snapshots
                                    WHERE match_id=? AND market=? AND IFNULL(line,'')=?)""",
                (match_id, market, line or "", match_id, market, line or ""))
    p = _pairs(rows)
    if len(p) < 2:
        return None
    return sum(1.0 / o for _, o in p)


def gate(match_id, market, line=""):
    """回 (過唔過, overround)。超過 MAX_VIG 就唔落。"""
    ov = match_vig(match_id, market, line)
    cap = float(getattr(config, "MAX_VIG", 0.09))
    if ov is None:
        return True, None                     # 冇資料唔擋（避免整停系統）
    return (ov - 1.0) <= cap, ov


def summary():
    """按場×盤×線計 overround，再按盤口平均（唔可以跨場相加）。"""
    rows = db.q("""SELECT o.match_id, o.market, IFNULL(o.line,'') AS line, o.selection, o.odds, o.taken_at
                   FROM odds_snapshots o
                   WHERE o.odds > 1""")
    latest = {}
    for r in rows:
        k = (r["match_id"], r["market"], r["line"], r["selection"])
        if k not in latest or (r["taken_at"] or "") > (latest[k]["taken_at"] or ""):
            latest[k] = r
    groups = {}
    for (mid, market, line, _sel), r in latest.items():
        groups.setdefault((mid, market, line), []).append(1.0 / float(r["odds"]))
    agg = {}
    for (mid, market, line), lst in groups.items():
        if len(lst) < 2:                      # 要有齊兩邊（或三邊）才算得出抽水
            continue
        agg.setdefault(market, []).append(sum(lst))
    out = []
    for market, lst in agg.items():
        avg = sum(lst) / len(lst)
        out.append({"market": market, "label": LABEL.get(market, market),
                    "lines": len(lst), "overround": round(avg, 4),
                    "vig_pct": round((avg - 1) * 100, 2),
                    "worst_pct": round((max(lst) - 1) * 100, 2),
                    "best_pct": round((min(lst) - 1) * 100, 2)})
    return sorted(out, key=lambda x: x["overround"])


def break_even(vig_pct):
    """抽水 X% 之下，模型要比市場準幾多個百分點才打和。"""
    return round(vig_pct, 2)

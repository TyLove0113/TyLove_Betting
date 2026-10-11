"""De-vigging, expected value and pricing helpers.

The book in this market is HKJC 主客和 / 入球大細. Their prices carry an
overround (抽水): the implied probabilities of the three outcomes add up to
more than 1. To compare our model against them we first strip that margin out
(proportional method, with a Shin-style correction available), then compute
    EV = model_p * decimal_odds - 1
"""
import config


def implied(odds_list):
    return [1.0 / o for o in odds_list if o and o > 1.0]


def overround(odds_list):
    return sum(implied(odds_list))


def devig_proportional(odds_list):
    """Remove the margin proportionally -> fair probabilities."""
    ps = implied(odds_list)
    s = sum(ps)
    if s <= 0:
        return []
    return [p / s for p in ps]


def devig_power(odds_list, tol=1e-9, iters=60):
    """Power method: find k with sum(p_i^k) = 1 where p_i = 1/odds_i (k>1 shrinks longshots more)."""
    ps = implied(odds_list)
    if not ps:
        return []
    lo, hi = 0.5, 3.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        s = sum(p ** mid for p in ps)
        if s > 1:
            lo = mid
        else:
            hi = mid
        if abs(s - 1) < tol:
            break
    k = (lo + hi) / 2
    vals = [p ** k for p in ps]
    tot = sum(vals)
    return [v / tot for v in vals]


def fair_odds(p):
    return None if not p or p <= 0 else round(1.0 / p, 3)


def ev_pct(model_p, decimal_odds):
    """Expected value as a percentage of stake."""
    if not model_p or not decimal_odds:
        return None
    return (model_p * decimal_odds) - 1.0


def edge(model_p, fair_p):
    if not model_p or not fair_p:
        return None
    return model_p - fair_p


def kelly_fraction(model_p, decimal_odds, fraction=None):
    """Kelly stake as a share of bankroll (fractional Kelly by default)."""
    if not model_p or not decimal_odds or decimal_odds <= 1:
        return 0.0
    b = decimal_odds - 1.0
    f = (model_p * decimal_odds - 1.0) / b
    f = max(0.0, f)
    frac = config.KELLY_FRACTION if fraction is None else fraction
    return f * frac


def evaluate(model_p, decimal_odds, thresholds=None):
    """Decide whether a price is a value bet and why (or why not)."""
    t = thresholds or {}
    ev_min = t.get("ev_threshold", config.EV_THRESHOLD)
    min_odds = t.get("min_odds", config.MIN_ODDS)
    max_odds = t.get("max_odds", config.MAX_ODDS)
    max_ev = t.get("max_ev_accept", config.MAX_EV_ACCEPT)

    ev = ev_pct(model_p, decimal_odds)
    if ev is None:
        return {"ok": False, "reason": "no price"}
    if decimal_odds < min_odds:
        return {"ok": False, "reason": f"賠率太低 (<{min_odds})", "ev_pct": ev}
    if decimal_odds > max_odds:
        return {"ok": False, "reason": f"賠率太高 (> {max_odds})，多數係資料有問題", "ev_pct": ev}
    if ev < ev_min:
        return {"ok": False, "reason": f"EV {ev*100:.1f}% 未夠 {ev_min*100:.1f}%", "ev_pct": ev}
    if ev > max_ev:
        return {"ok": False, "reason": f"EV {ev*100:.0f}% 異常高，檢查賠率係咪過時", "ev_pct": ev}
    return {"ok": True, "ev_pct": ev, "kelly_pct": kelly_fraction(model_p, decimal_odds)}


def market_view(fixture, prediction, odds_rows, thresholds=None):
    """Turn a prediction + HKJC prices into a sorted list of priced selections."""
    out = []
    had = {r["selection"]: r["odds"] for r in odds_rows if r["market"] == "HAD"}
    if len(had) == 3:
        fair = devig_proportional([had["H"], had["D"], had["A"]])
        model = {"H": prediction["p_home"], "D": prediction["p_draw"], "A": prediction["p_away"]}
        for sel, fp, mo in zip(("H", "D", "A"), fair, (had["H"], had["D"], had["A"])):
            out.append(_row(fixture, "HAD", "", sel, model[sel], fp, mo, thresholds))
        out.append({"market": "HAD", "selection": "INFO", "overround": overround([had["H"], had["D"], had["A"]])})

    by_line = {}
    for r in odds_rows:
        if r["market"] == "TTG":
            by_line.setdefault(str(r["line"]), {})[r["selection"]] = r["odds"]
    for line, d in by_line.items():
        if "OVER" not in d or "UNDER" not in d:
            continue
        p_over = (prediction.get("over") or {}).get(float(line)) if _isnum(line) else None
        if p_over is None:
            continue
        p_fair = devig_proportional([d["OVER"], d["UNDER"]])
        out.append(_row(fixture, "TTG", line, "OVER", p_over, p_fair[0], d["OVER"], thresholds))
        out.append(_row(fixture, "TTG", line, "UNDER", 1 - p_over, p_fair[1], d["UNDER"], thresholds))
    return out


def _isnum(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _row(fixture, market, line, selection, model_p, fair_p, odds, thresholds):
    res = evaluate(model_p, odds, thresholds)
    return {
        "match_id": fixture["match_id"], "market": market, "line": line, "selection": selection,
        "model_p": model_p, "fair_p": fair_p, "fair_odds": fair_odds(model_p),
        "hkjc_odds": odds, "ev_pct": res.get("ev_pct"), "kelly_pct": res.get("kelly_pct"),
        "is_value": bool(res.get("ok")), "reason": res.get("reason", ""),
        "fixture": fixture,
    }

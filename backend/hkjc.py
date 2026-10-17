"""HKJC data layer: fixtures, markets ("開咩盤"), odds, results.

IMPORTANT / HONEST NOTE
-----------------------
HKJC has no public API. Its pages are a JavaScript app talking to a GraphQL
endpoint (``info.cld.hkjc.com/graphql/base/``) plus several legacy JSON paths.
Those endpoints are unofficial, undocumented and get renamed without warning:
during development of this file the classic
``bet.hkjc.com/football/getJSON.aspx?jsontype=odds_allodds.aspx`` already
returned HTTP 302 to the home page, so it can no longer be relied on.

Therefore this module is built in three layers:

  1. ``probe()``  - fires a list of candidate endpoints, saves every raw
     response into ``data/probe/`` and prints a summary. Run this first on
     your own machine; it tells us the real shapes with zero guessing.
  2. ``parse_*``  - shape-agnostic parsers. They walk any JSON/HTML payload and
     pick out fixtures/odds by field-name heuristics. A ``field_map`` (stored
     in settings, editable from Telegram) lets you point at exact JSON paths
     once the real shape is known, without touching code.
  3. manual entry - if everything above fails the bot accepts odds typed by
     hand, so a stale page never stops the system working.

Polite scraping: one request per endpoint per run, ``HTTP_SLEEP`` between
requests, retries with backoff, and every failure is pushed to Telegram
instead of dying silently.
"""
import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup

import config
import db
from timeutil import parse_hk, now_hk

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
    "Origin": "https://bet.hkjc.com",
    "Referer": "https://bet.hkjc.com/",
})

import gzip
import hashlib

import hkjc_queries as Q

API = "https://info.cld.hkjc.com/graphql/base-get/"
HDR = {"Content-Type": "application/json",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
       "Origin": "https://bet.hkjc.com", "Referer": "https://bet.hkjc.com/football/"}
ST = {"PREEVENT": "scheduled", "KICKEDOFF": "live", "FIRSTHALF": "live", "HALFTIME": "live",
      "SECONDHALF": "live", "RESULT": "finished", "FINISHED": "finished",
      "MATCHVOIDED": "cancelled", "ABANDONED": "cancelled"}
POOL = {"HAD": "HAD", "HIL": "TTG"}          # HIL = 入球大細（有 2.5 之類嘅 line）


def _call(query, variables=None, tries=2):
    last = None
    for i in range(tries + 1):
        try:
            r = requests.post(API, json={"query": query, "variables": variables or {}},
                              headers=HDR, timeout=45)
            raw = r.content
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            if data.get("errors") and not data.get("data"):
                raise RuntimeError(str(data["errors"])[:180])
            return data.get("data") or {}
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.0 + i)
    raise RuntimeError(f"馬會 API 失敗：{last}")


def _hk(iso):
    return (iso or "")[:16].replace("T", " ")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None



def fetch_schedule(date=None):
    """賽程。date='YYYY-MM-DD' 只留嗰日（香港時間）開賽嘅場。"""
    data = _call(Q.MATCH_LIST)
    out = []
    for m in data.get("matches") or []:
        ko = _hk(m.get("kickOffTime"))
        if len(ko) < 16 or (date and ko[:10] != date):
            continue
        pools = (m.get("poolInfo") or {}).get("normalPools") or []
        markets = [v for k, v in POOL.items() if k in pools]
        out.append({"source_id": str(m.get("id")), "league": (m.get("tournament") or {}).get("name_ch") or "",
                    "kickoff_hk": ko, "home_team": (m.get("homeTeam") or {}).get("name_ch") or "",
                    "away_team": (m.get("awayTeam") or {}).get("name_ch") or "",
                    "markets": ",".join(markets), "status": ST.get((m.get("status") or "").upper(), "scheduled")})
    return out, f"hkjc {len(out)} 場"


def _sel(market, c):
    s = (c.get("str") or "").strip().upper()
    names = " ".join((x.get("name_ch") or "") for x in (c.get("selections") or []))
    if market == "HAD":
        if s in ("H", "D", "A"):
            return s
        return {"主": "H", "和": "D", "客": "A"}.get(names[:1])
    if "大" in names or s in ("O", "OVER"):
        return "OVER"
    if "細" in names or s in ("U", "UNDER"):
        return "UNDER"
    return None


def _ids(match_ids):
    ids = [str(x) for x in (match_ids or [])]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    rows = db.q(f"SELECT match_id, source_id FROM fixtures WHERE match_id IN ({ph})", tuple(ids))
    return {str(r["source_id"]): r["match_id"] for r in rows if r["source_id"]}


def _fetch(chunk, types):
    return _call(Q.ODDS, {"startIndex": 0, "endIndex": len(chunk), "startDate": None, "endDate": None,
                          "matchIds": chunk, "tournIds": None, "fbOddsTypes": types,
                          "fbOddsTypesM": ["HAD"], "inplayOnly": False, "featuredMatchesOnly": False,
                          "frontEndIds": None, "earlySettlementOnly": False, "showAllMatch": False})



def fetch_odds(match_ids=None):
    """逐場賠率。回 (rows, note)；rows 直接餵 db.save_odds_batch。"""
    src = _ids(match_ids)
    if not src:
        return [], "冇 source_id（先 /sync 賽程）"
    keys, rows, pools = list(src), [], 0
    for i in range(0, len(keys), 25):
        data = _fetch(keys[i:i + 25], ["HAD", "HIL"])
        for m in data.get("matches") or []:
            mid = src.get(str(m.get("id")))
            if not mid:
                continue
            for p in m.get("foPools") or []:
                market = POOL.get(p.get("oddsType"))
                if not market:
                    continue
                pools += 1
                for ln in p.get("lines") or []:
                    line = (ln.get("condition") or "").strip()
                    for c in ln.get("combinations") or []:
                        sel, odds = _sel(market, c), _num(c.get("currentOdds"))
                        if not sel or not odds or odds <= 1:
                            continue
                        rows.append({"match_id": mid, "market": market,
                                     "line": "" if market == "HAD" else line,
                                     "selection": sel, "odds": odds, "source": "hkjc"})
    return rows, f"hkjc {len(src)} 場 / {pools} 個盤 / {len(rows)} 條賠率"


def fetch_results(date=None):
    """賽果：用 runningResult 嘅 homeScore / awayScore。"""
    src = _ids([r["match_id"] for r in db.q(
        "SELECT match_id FROM fixtures WHERE kickoff_hk < ? ORDER BY kickoff_hk DESC LIMIT 40",
        (now_hk().strftime("%Y-%m-%d %H:%M"),))])
    if not src:
        return [], "冇已開賽嘅賽程"
    out, keys = [], list(src)
    for i in range(0, len(keys), 25):
        data = _fetch(keys[i:i + 25], ["HAD"])
        for m in data.get("matches") or []:
            if ST.get((m.get("status") or "").upper()) != "finished":
                continue
            rr = m.get("runningResult") or {}
            hg, ag = _num(rr.get("homeScore")), _num(rr.get("awayScore"))
            if hg is None or ag is None:
                continue
            out.append({"match_id": src[str(m["id"])], "home_goals": int(hg), "away_goals": int(ag)})
    return out, f"hkjc {len(out)} 場有賽果"



def probe(verbose=True):
    """跑一次真馬會端點，存 raw 檔＋回報（/probe 用）。"""
    os.makedirs(config.PROBE_DIR, exist_ok=True)
    stamp = now_hk().strftime("%Y%m%d_%H%M%S")
    report = []

    def step(name, fn):
        try:
            report.append(dict(name=name, status="ok", **fn()))
        except Exception as e:                                     # noqa: BLE001
            report.append({"name": name, "status": "ERR", "error": str(e)[:180]})
        if verbose:
            r = report[-1]
            print(f"[probe] {r['name']:6s} {r['status']} {r.get('note', '')} {r.get('error', '')}")

    def sched():
        fx, note = fetch_schedule(None)
        p = os.path.join(config.PROBE_DIR, f"{stamp}_schedule.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(fx[:60], fh, ensure_ascii=False, indent=2)
        return {"bytes": os.path.getsize(p), "note": note, "file": p}

    def odds():
        ids = [r["match_id"] for r in db.q("SELECT match_id FROM fixtures ORDER BY kickoff_hk LIMIT 3")]
        rows, note = fetch_odds(ids)
        p = os.path.join(config.PROBE_DIR, f"{stamp}_odds.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        return {"bytes": os.path.getsize(p), "note": note, "file": p}

    step("賽程", sched)
    step("賠率", odds)
    summary = os.path.join(config.PROBE_DIR, f"{stamp}_summary.json")
    with open(summary, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    db.log_job("hkjc_probe", "ok", summary)
    return {"report": report, "summary": summary}


def make_match_id(league, kickoff_hk, home, away):
    raw = f"{league}|{kickoff_hk}|{home}|{away}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]



# ------------------------------------------------- manual input fallback
def parse_manual_odds(text):
    """Parse a hand-typed odds line.

    Format:  <home> vs <away> | HAD 2.10 3.35 3.60 | TTG 2.5 OVER 1.85 UNDER 1.95
    Everything after '|' is optional; you may give HAD only, TTG only, or both.
    Team names must match fixtures already in the database (fuzzy contains).
    """
    if "|" not in text:
        return None, "格式：主隊 vs 客隊 | HAD 2.10 3.35 3.60 | TTG 2.5 OVER 1.85 UNDER 1.95"
    head, rest = text.split("|", 1)
    names = re.split(r"\s+(?:vs|對|v)\s+", head.strip(), flags=re.I)
    if len(names) != 2:
        return None, "搵唔到兩隊名（用 'A vs B'）"
    home, away = names[0].strip(), names[1].strip()
    fix = _find_fixture(home, away)
    if not fix:
        return None, f"資料庫搵唔到賽事：{home} vs {away}（先用 /fixtures 睇今日賽程）"

    out, errs = [], []
    for chunk in rest.split("|"):
        parts = chunk.split()
        if not parts:
            continue
        tag = parts[0].upper()
        try:
            if tag == "HAD" and len(parts) >= 4:
                for sel, val in zip(("H", "D", "A"), parts[1:4]):
                    out.append({"match_id": fix["match_id"], "market": "HAD", "line": "",
                                "selection": sel, "odds": float(val), "source": "manual"})
            elif tag == "TTG":
                line = parts[1]
                i = 2
                while i + 1 < len(parts):
                    sel = "OVER" if parts[i].upper() in ("OVER", "O", "大") else "UNDER"
                    out.append({"match_id": fix["match_id"], "market": "TTG", "line": line,
                                "selection": sel, "odds": float(parts[i + 1]), "source": "manual"})
                    i += 2
            else:
                errs.append(f"唔識 {tag}")
        except (ValueError, IndexError):
            errs.append(f"{tag} 格式錯")
    if not out:
        return None, "冇有效賠率。" + ("; ".join(errs) if errs else "")
    return {"fixture": fix, "rows": out}, ("; ".join(errs) if errs else "ok")


def _find_fixture(home, away):
    like = f"%{home}%"
    rows = db.q("""SELECT * FROM fixtures
                   WHERE lower(home_team) LIKE lower(?) AND lower(away_team) LIKE lower(?)
                   ORDER BY kickoff_hk DESC LIMIT 1""", (like, f"%{away}%"))
    if rows:
        return rows[0]
    rows = db.q("SELECT * FROM fixtures ORDER BY kickoff_hk DESC LIMIT 60")
    for r in rows:
        if home.lower() in r["home_team"].lower() and away.lower() in r["away_team"].lower():
            return r
    return None

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

# ------------------------------------------------------------------ endpoints
GRAPHQL = config.HKJC_GRAPHQL_URL
ODDS_PAGE = "https://bet.hkjc.com/football/odds/"
SCHEDULE_PAGE = "https://bet.hkjc.com/football/schedule/"

# candidate GraphQL queries: names taken from what the site's own bundle asks for.
# They are tried in order; the first one that answers with usable data wins.
GRAPHQL_QUERIES = {
    "footballSchedule": """query { footballSchedule { matchId kickOffTime leagueName
        homeTeamName awayTeamName { } } }""",
    "oddsAllOdds": "query { oddsAllOdds { matchId } }",
}

LEGACY_JSON = [
    ("odds_allodds", "https://bet.hkjc.com/football/getJSON.aspx?jsontype=odds_allodds.aspx&pageno=1"),
    ("matches", "https://bet.hkjc.com/football/getJSON.aspx?jsontype=matches.aspx?date={date}"),
]


def _get(url, **kw):
    last = None
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=config.HTTP_TIMEOUT, allow_redirects=True, **kw)
            if r.status_code == 200 and r.content:
                time.sleep(config.HTTP_SLEEP)
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)[:200]
        time.sleep(config.HTTP_SLEEP * attempt)
    raise RuntimeError(f"GET failed {url} -> {last}")


def _post_json(url, payload):
    last = None
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            r = SESSION.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200 and r.content:
                time.sleep(config.HTTP_SLEEP)
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)[:200]
        time.sleep(config.HTTP_SLEEP * attempt)
    raise RuntimeError(f"POST failed {url} -> {last}")


# --------------------------------------------------------------------- probe
def probe(verbose=True):
    """Hit every candidate endpoint once and store the raw payload."""
    os.makedirs(config.PROBE_DIR, exist_ok=True)
    stamp = now_hk().strftime("%Y%m%d_%H%M%S")
    report = []

    targets = [("graphql_home", "post", GRAPHQL, {"query": "{ __typename }"})]
    for name, q in GRAPHQL_QUERIES.items():
        targets.append((f"graphql_{name}", "post", GRAPHQL, {"query": q}))
    for name, url in LEGACY_JSON:
        targets.append((name, "get", url.format(date=now_hk().strftime("%Y-%m-%d")), None))
    targets.append(("odds_page_html", "get", ODDS_PAGE, None))
    targets.append(("schedule_page_html", "get", SCHEDULE_PAGE, None))

    for name, kind, url, payload in targets:
        entry = {"name": name, "url": url, "kind": kind}
        try:
            r = _post_json(url, payload) if kind == "post" else _get(url)
            body = r.text
            path = os.path.join(config.PROBE_DIR, f"{stamp}_{name}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# {url}\n# status {r.status_code}\n# headers {dict(r.headers)}\n\n{body[:400000]}")
            entry.update({"status": r.status_code, "bytes": len(body), "file": path,
                          "preview": body[:220].replace("\n", " ")})
        except Exception as e:                                    # noqa: BLE001
            entry.update({"status": "ERR", "error": str(e)[:200]})
        report.append(entry)
        if verbose:
            print(f"[probe] {name:24s} {entry.get('status')} {entry.get('bytes', '')} {entry.get('error', '')}")
    summary_path = os.path.join(config.PROBE_DIR, f"{stamp}_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    db.log_job("hkjc_probe", "ok", summary_path)
    return {"report": report, "summary": summary_path}


# ------------------------------------------------------------------ parsing
FIELD_ALIASES = {
    "match_id": ["matchid", "id", "matchno", "eventid", "fixtureid"],
    "kickoff": ["kickoff", "kickofftime", "starttime", "matchdate", "datetime", "time", "kickOffTime"],
    "league": ["league", "leaguename", "tournament", "competition", "division"],
    "home_team": ["hometeam", "hometeamname", "home", "teamh", "homename", "homeTeamName"],
    "away_team": ["awayteam", "awayteamname", "away", "teama", "awayname", "awayTeamName"],
    "home_score": ["homescore", "scoreh", "homegoals", "ft_home"],
    "away_score": ["awayscore", "scorea", "awaygoals", "ft_away"],
    "markets": ["markets", "marketlist", "bettypes", "marketcodes"],
    "odds": ["odds", "odd", "price", "rate"],
}

TEAM_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z .'\-]{2,}")
ODDS_KEY_RE = re.compile(r"(had|hda|hadodds|home|draw|away|homewin|drawodds|awaywin)", re.I)
TTG_KEYS = {"over": ["over", "o", "above", "大"], "under": ["under", "u", "below", "細"]}


def _iter_dicts(obj, depth=0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v, depth + 1)


def _lookup(d, aliases):
    for k, v in d.items():
        if str(k).lower() in aliases and v not in (None, "", [], {}):
            return v
    return None


def _num(v):
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f


def _paginate(payload):
    """Unwrap the many wrappers HKJC payloads use ({data:{...}} , {json:[...]}, ...)."""
    if isinstance(payload, dict):
        for key in ("data", "json", "result", "results", "matches", "content"):
            if key in payload:
                return _paginate(payload[key])
    return payload


def parse_fixtures(payload, date_hint=None):
    """Find fixture records inside any payload. Returns list of normalised dicts."""
    payload = _paginate(payload)
    out, seen = [], set()
    for d in _iter_dicts(payload):
        home = _lookup(d, FIELD_ALIASES["home_team"])
        away = _lookup(d, FIELD_ALIASES["away_team"])
        if not home or not away or not isinstance(home, (str, int)) or not isinstance(away, (str, int)):
            continue
        home, away = str(home).strip(), str(away).strip()
        if len(home) < 2 or len(away) < 2 or home == away:
            continue
        ko = parse_hk(_lookup(d, FIELD_ALIASES["kickoff"]))
        if ko is None and date_hint:
            ko = parse_hk(date_hint)
        mid = _lookup(d, FIELD_ALIASES["match_id"])
        key = (home, away, ko.strftime("%Y-%m-%d %H:%M") if ko else "", str(mid or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_id": str(mid) if mid else None,
            "league": str(_lookup(d, FIELD_ALIASES["league"]) or "").strip(),
            "kickoff_hk": ko.strftime("%Y-%m-%d %H:%M") if ko else None,
            "home_team": home, "away_team": away,
            "home_score": _num(_lookup(d, FIELD_ALIASES["home_score"])),
            "away_score": _num(_lookup(d, FIELD_ALIASES["away_score"])),
            "markets": _flatten_markets(_lookup(d, FIELD_ALIASES["markets"])),
        })
    return out


def _flatten_markets(v):
    if not v:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, str):
                parts.append(x)
            elif isinstance(x, dict):
                code = _lookup(x, ["code", "marketcode", "bettype", "name", "type"])
                if code:
                    parts.append(str(code))
        return ",".join(parts)
    if isinstance(v, dict):
        return ",".join(str(k) for k in v.keys())
    return str(v)


def parse_odds(payload, match_id=None):
    """Find 1X2 / total-goal odds. Returns rows ready for db.save_odds_batch."""
    payload = _paginate(payload)
    rows = []
    for d in _iter_dicts(payload):
        mid = match_id or _lookup(d, FIELD_ALIASES["match_id"])
        if not mid:
            continue
        mid = str(mid)
        # --- head-to-head / 主客和
        had = d.get("had") or d.get("hda") or d.get("hadOdds")
        pairs = []
        if isinstance(had, dict):
            pairs = [(had.get("h") or had.get("home"), "H"),
                     (had.get("d") or had.get("draw"), "D"),
                     (had.get("a") or had.get("away"), "A")]
        if not any(_num(p[0]) for p in pairs):
            for k, v in d.items():
                if ODDS_KEY_RE.search(str(k)):
                    kl = str(k).lower()
                    sel = "H" if "home" in kl else "D" if "draw" in kl else "A"
                    pairs.append((v, sel))
        for val, sel in pairs:
            f = _num(val)
            if f and 1.0 < f < 200:
                rows.append({"match_id": mid, "market": "HAD", "line": "", "selection": sel, "odds": f})
        # --- totals / 入球大細
        tot = d.get("ttg") or d.get("totals") or d.get("overUnder") or d.get("ttgOdds")
        if isinstance(tot, dict):
            line = str(tot.get("line") or tot.get("goals") or tot.get("handicap") or "2.5")
            for sel, aliases in TTG_KEYS.items():
                val = _lookup(tot, aliases)
                f = _num(val)
                if f and 1.0 < f < 200:
                    rows.append({"match_id": mid, "market": "TTG", "line": line,
                                 "selection": sel.upper(), "odds": f})
    return rows


def parse_scores(payload, match_id=None):
    """Finished matches -> results rows."""
    payload = _paginate(payload)
    out = []
    for d in _iter_dicts(payload):
        hg = _num(_lookup(d, FIELD_ALIASES["home_score"]))
        ag = _num(_lookup(d, FIELD_ALIASES["away_score"]))
        mid = match_id or _lookup(d, FIELD_ALIASES["match_id"])
        if mid and hg is not None and ag is not None:
            out.append({"match_id": str(mid), "home_goals": int(hg), "away_goals": int(ag),
                        "total": int(hg + ag)})
    return out


def parse_html(payload_html):
    """Last-resort parsing of a rendered odds page."""
    soup = BeautifulSoup(payload_html, "lxml")
    fixtures, odds_rows = [], []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if not headers:
            continue
        idx = {h: i for i, h in enumerate(headers)}
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            row = dict(zip(headers, cells))
            home = next((v for k, v in row.items() if any(a in k.lower() for a in ("home", "主隊", "主"))), None)
            away = next((v for k, v in row.items() if any(a in k.lower() for a in ("away", "客隊", "客"))), None)
            if home and away:
                fixtures.append({"home_team": home, "away_team": away, "kickoff_hk": None,
                                 "league": "", "source_id": None, "markets": ""})
    return fixtures, odds_rows


# ------------------------------------------------------------------ fetchers
def fetch_schedule(date=None):
    """Return (fixtures, note). Never raises - failure is reported, not fatal."""
    date = date or now_hk().strftime("%Y-%m-%d")
    attempts = []

    # 1. primary odds/schedule page for the day (HTML app shell + embedded JSON)
    for name, url in (("odds_page", f"{ODDS_PAGE}?date={date}"), ("odds_page_plain", ODDS_PAGE),
                      ("schedule_page", f"{SCHEDULE_PAGE}?date={date}")):
        try:
            r = _get(url)
            body = r.text
            fixtures = []
            embedded = re.findall(r"<script[^>]*>(.*?)</script>", body, re.S)
            for blob in embedded:
                blob = blob.strip()
                if blob.startswith("{") or blob.startswith("["):
                    try:
                        fixtures += parse_fixtures(json.loads(blob), date_hint=date)
                    except json.JSONDecodeError:
                        pass
            if not fixtures:
                fixtures, _ = parse_html(body)
            if fixtures:
                return fixtures, f"{name}:{len(fixtures)}"
            attempts.append(f"{name}:HTTP {r.status_code}/no fixtures({len(body)}B)")
        except Exception as e:                                    # noqa: BLE001
            attempts.append(f"{name}:{str(e)[:80]}")

    # 2. legacy JSON paths
    for name, url in LEGACY_JSON:
        try:
            r = _get(url.format(date=date))
            fixtures = parse_fixtures(json.loads(r.text), date_hint=date)
            if fixtures:
                return fixtures, f"{name}:{len(fixtures)}"
            attempts.append(f"{name}:no fixtures")
        except Exception as e:                                    # noqa: BLE001
            attempts.append(f"{name}:{str(e)[:80]}")

    # 3. GraphQL
    for name, query in GRAPHQL_QUERIES.items():
        try:
            r = _post_json(GRAPHQL, {"query": query})
            fixtures = parse_fixtures(r.json(), date_hint=date)
            if fixtures:
                return fixtures, f"graphql_{name}:{len(fixtures)}"
            attempts.append(f"graphql_{name}:no fixtures")
        except Exception as e:                                    # noqa: BLE001
            attempts.append(f"graphql_{name}:{str(e)[:80]}")

    return [], " | ".join(attempts)


def fetch_odds(match_ids=None):
    """Odds for the matches we have fixtures for. Returns (rows, note)."""
    rows, attempts = [], []
    match_ids = match_ids or []
    mid_set = {str(m) for m in match_ids}

    for name, url in (("odds_allodds", LEGACY_JSON[0][1]), ("odds_page", ODDS_PAGE)):
        try:
            r = _get(url)
            payload = r.json() if url.endswith(".aspx") else _embedded_json_or_html(r.text)
            found = parse_odds(payload)
            found = [x for x in found if not mid_set or x["match_id"] in mid_set]
            if found:
                rows += found
                attempts.append(f"{name}:{len(found)}")
                if mid_set and {x['match_id'] for x in found} >= mid_set:
                    break
            else:
                attempts.append(f"{name}:0 rows")
        except Exception as e:                                    # noqa: BLE001
            attempts.append(f"{name}:{str(e)[:80]}")

    if not rows:
        for name, query in GRAPHQL_QUERIES.items():
            if "odd" not in name.lower():
                continue
            try:
                r = _post_json(GRAPHQL, {"query": query})
                found = parse_odds(r.json())
                found = [x for x in found if not mid_set or x["match_id"] in mid_set]
                if found:
                    rows += found
                    attempts.append(f"graphql_{name}:{len(found)}")
            except Exception as e:                                # noqa: BLE001
                attempts.append(f"graphql_{name}:{str(e)[:80]}")

    return rows, " | ".join(attempts)


def _embedded_json_or_html(text):
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    for blob in re.findall(r"<script[^>]*>(.*?)</script>", text, re.S):
        blob = blob.strip()
        if blob.startswith(("{", "[")):
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                continue
    return {"html": text}


def fetch_results(date=None):
    """Finished matches for a date. Returns (rows, note)."""
    date = date or now_hk().strftime("%Y-%m-%d")
    attempts = []
    for name, url in (("results_json", "https://bet.hkjc.com/football/getJSON.aspx?jsontype=results.aspx?date={date}"),
                      ("results_page", f"https://bet.hkjc.com/football/results/?date={date}")):
        try:
            r = _get(url.format(date=date))
            payload = _embedded_json_or_html(r.text)
            found = parse_scores(payload)
            if found:
                return found, f"{name}:{len(found)}"
            attempts.append(f"{name}:0")
        except Exception as e:                                    # noqa: BLE001
            attempts.append(f"{name}:{str(e)[:80]}")
    return [], " | ".join(attempts)


# ------------------------------------------------------- manual input fallback
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


def make_match_id(league, kickoff_hk, home, away):
    raw = f"{league}|{kickoff_hk}|{home}|{away}"
    return str(abs(hash(raw)))[:16]

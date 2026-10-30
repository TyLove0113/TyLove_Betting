"""SQLite layer: schema + tiny query helpers. No ORM, no surprises."""
import os
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime

import config
from timeutil import now_hk

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS job_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    job      TEXT,
    status   TEXT,
    detail   TEXT
);

-- every match HKJC has opened (any league, any date)
CREATE TABLE IF NOT EXISTS fixtures (
    match_id     TEXT PRIMARY KEY,       -- stable internal id
    source_id    TEXT,                   -- id from HKJC if any
    league       TEXT,
    kickoff_hk   TEXT,                   -- 'YYYY-MM-DD HH:MM' HK time
    home_team    TEXT,
    away_team    TEXT,
    home_team_id TEXT,
    away_team_id TEXT,
    status       TEXT DEFAULT 'scheduled',  -- scheduled | live | finished | cancelled
    markets      TEXT DEFAULT '',        -- e.g. 'HAD,TTG,HDC'
    home_score   INTEGER,
    away_score   INTEGER,
    updated_at   TEXT
);

-- odds snapshots: one row per (match, market, line, selection, scrape time)
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    TEXT,
    market      TEXT,        -- HAD | TTG | HDC | ...
    line        TEXT,        -- '2.5' for totals, '' for 1X2
    selection   TEXT,        -- H | D | A | OVER | UNDER
    odds        REAL,
    taken_at    TEXT,
    is_opening  INTEGER DEFAULT 0,
    is_closing  INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'hkjc'
);
CREATE INDEX IF NOT EXISTS idx_odds_match ON odds_snapshots(match_id, market, selection, taken_at);

-- match history used to build team strength (filled from results, CSV import or API)
CREATE TABLE IF NOT EXISTS team_matches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    played_on  TEXT,
    league     TEXT,
    home_team  TEXT,
    away_team  TEXT,
    home_goals INTEGER,
    away_goals INTEGER,
    home_xg    REAL,
    away_xg    REAL,
    source     TEXT DEFAULT 'hkjc',
    UNIQUE(played_on, home_team, away_team)
);
CREATE INDEX IF NOT EXISTS idx_tm_teams ON team_matches(home_team, away_team, played_on);

CREATE TABLE IF NOT EXISTS ai_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    purpose    TEXT,
    subject    TEXT,
    risk       TEXT,
    reason     TEXT,
    tokens     INTEGER DEFAULT 0,
    ok         INTEGER DEFAULT 1,
    err        TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      TEXT,
    created_at    TEXT,
    model_version TEXT,
    p_home        REAL,
    p_draw        REAL,
    p_away        REAL,
    lam_home      REAL,
    lam_away      REAL,
    p_over25      REAL,
    p_under25     REAL,
    baseline      INTEGER DEFAULT 0,   -- 1 = league-average fallback (weak)
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_match ON predictions(match_id, created_at);

-- what the model thinks is worth betting, at the odds we saw
CREATE TABLE IF NOT EXISTS value_bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      TEXT,
    created_at    TEXT,
    market        TEXT,
    line          TEXT,
    selection     TEXT,
    model_p       REAL,
    fair_odds     REAL,
    hkjc_odds     REAL,
    ev_pct        REAL,
    kelly_pct     REAL,
    stake         REAL,
    reasons       TEXT
);
CREATE INDEX IF NOT EXISTS idx_vb_match ON value_bets(match_id, market, selection);

-- the book. mode='paper' = system booked it itself; mode='real' = you confirmed it
CREATE TABLE IF NOT EXISTS bets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    value_bet_id INTEGER,
    match_id     TEXT,
    created_at   TEXT,
    mode         TEXT DEFAULT 'paper',     -- paper | real
    market       TEXT,
    line         TEXT,
    selection    TEXT,
    odds_taken   REAL,
    stake        REAL,
    status       TEXT DEFAULT 'open',      -- open | won | lost | void | push
    closing_odds REAL,
    clv_pct      REAL,
    payout       REAL,
    settled_at   TEXT,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status, mode);

-- results pulled from HKJC (or typed in) used to settle bets
CREATE TABLE IF NOT EXISTS results (
    match_id   TEXT PRIMARY KEY,
    home_goals INTEGER,
    away_goals INTEGER,
    total      INTEGER,
    ht_home    INTEGER,
    ht_away    INTEGER,
    source     TEXT,
    fetched_at TEXT
);
"""


def connect():
    folder = os.path.dirname(config.DB_PATH)
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            # 例如 /data（Railway 卷）喺本機唔存在或冇權限：退回本機 data 夾，唔好整個程式死。
            fallback_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
            print(f"[db] 寫唔入 {folder}（{exc}）→ 改用 {fallback_dir}")
            os.makedirs(fallback_dir, exist_ok=True)
            config.DB_PATH = os.path.join(fallback_dir, "football.db")
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_bets_unique(c):
    """同一場 + 盤口 + 選項 + mode 只准有一張未作廢的注單。

    舊資料若已有重複（每次掃描重複落注造成），保留最早一張，其餘標為 void
    並在 note 註明；之後由 partial unique index 阻止新的重複。
    """
    keep = [r[0] for r in c.execute(
        """SELECT MIN(id) FROM bets
            WHERE IFNULL(status,'') <> 'void'
            GROUP BY match_id, market, IFNULL(line,''), selection, mode""")]
    if keep:
        marks = ",".join("?" * len(keep))
        c.execute(
            f"""UPDATE bets
                   SET status='void',
                       note=COALESCE(NULLIF(note,''),'') || ' [duplicate-archived]'
                 WHERE IFNULL(status,'') <> 'void' AND id NOT IN ({marks})""", keep)
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_bets_unique
                 ON bets(match_id, market, IFNULL(line,''), selection, mode)
                 WHERE IFNULL(status,'') <> 'void'""")


def _migrate():
    """舊 DB 補欄位（唔會刪資料）。"""
    for col, typ in (("model_p", "REAL"), ("ev_pct", "REAL"), ("model_version", "TEXT")):
        try:
            ex(f"ALTER TABLE bets ADD COLUMN {col} {typ}")
        except Exception:
            pass


def init_db():
    with cursor() as c:
        c.executescript(SCHEMA)
        _migrate_bets_unique(c)
    _migrate()
    return config.DB_PATH


def q(sql, args=(), one=False):
    with cursor() as c:
        rows = c.execute(sql, args).fetchall()
    rows = [dict(r) for r in rows]
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    with cursor() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def upsert_fixture(f):
    """f: dict with match_id/kickoff_hk/home_team/away_team/... """
    now = now_hk().strftime("%Y-%m-%d %H:%M:%S")
    ex("""INSERT INTO fixtures (match_id, source_id, league, kickoff_hk, home_team, away_team,
                               home_team_id, away_team_id, status, markets, updated_at)
          VALUES (:match_id, :source_id, :league, :kickoff_hk, :home_team, :away_team,
                  :home_team_id, :away_team_id, :status, :markets, :updated_at)
          ON CONFLICT(match_id) DO UPDATE SET
            source_id=excluded.source_id, league=excluded.league, kickoff_hk=excluded.kickoff_hk,
            home_team=excluded.home_team, away_team=excluded.away_team,
            home_team_id=excluded.home_team_id, away_team_id=excluded.away_team_id,
            markets=excluded.markets, updated_at=excluded.updated_at""",
       {"match_id": f.get("match_id"), "source_id": f.get("source_id"), "league": f.get("league"),
        "kickoff_hk": f.get("kickoff_hk"), "home_team": f.get("home_team"), "away_team": f.get("away_team"),
        "home_team_id": f.get("home_team_id"), "away_team_id": f.get("away_team_id"),
        "status": f.get("status", "scheduled"), "markets": f.get("markets", ""), "updated_at": now})


def log_job(job, status, detail=""):
    ex("INSERT INTO job_log (ts, job, status, detail) VALUES (?,?,?,?)",
       (now_hk().strftime("%Y-%m-%d %H:%M:%S"), job, status, str(detail)[:2000]))


def last_job(job):
    return q("SELECT * FROM job_log WHERE job=? ORDER BY id DESC LIMIT 1", (job,), one=True)


def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, ValueError):
        return r["value"]


def set_setting(key, value):
    ex("""INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
          ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
       (key, json.dumps(value, ensure_ascii=False), now_hk().strftime("%Y-%m-%d %H:%M:%S")))


def all_settings():
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


def save_odds_batch(rows, taken_at=None):
    """rows: list of dicts(match_id, market, line, selection, odds, is_opening=0, is_closing=0)"""
    taken_at = taken_at or now_hk().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with cursor() as c:
        for r in rows:
            if r.get("odds") is None:
                continue
            c.execute("""INSERT INTO odds_snapshots
                         (match_id, market, line, selection, odds, taken_at, is_opening, is_closing, source)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (r["match_id"], r["market"], r.get("line", ""), r["selection"], float(r["odds"]),
                       taken_at, int(r.get("is_opening", 0)), int(r.get("is_closing", 0)),
                       r.get("source", "hkjc")))
            n += 1
    return n


def latest_odds(match_id, market=None):
    """Most recent snapshot per (market, line, selection)."""
    sql = """SELECT o.* FROM odds_snapshots o
             JOIN (SELECT market, line, selection, MAX(taken_at) mt
                   FROM odds_snapshots WHERE match_id=? GROUP BY market, line, selection) t
               ON o.market=t.market AND o.line=t.line AND o.selection=t.selection AND o.taken_at=t.mt
             WHERE o.match_id=?"""
    args = (match_id, match_id)
    if market:
        sql += " AND o.market=?"
        args = args + (market,)
    return q(sql, args)

"""驗證去重修正：舊資料清理、unique index、record_bet 重複即略、掃描鎖。"""
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

tmpdir = tempfile.mkdtemp(prefix="bt_test_")
test_db = os.path.join(tmpdir, "football.db")
shutil.copy(os.path.join(HERE, "..", "data", "football.db"), test_db)
os.environ["DB_PATH"] = test_db

import config
import db
import staking
import scanner

assert os.path.abspath(config.DB_PATH) == os.path.abspath(test_db), config.DB_PATH
print("DB:", config.DB_PATH)

# 1) 先造出「舊 bug」造成的重複注單（3 張同場同盤同選項）
con = sqlite3.connect(test_db)
con.executescript("""
DROP INDEX IF EXISTS ux_bets_unique;
DELETE FROM bets;
INSERT INTO bets (match_id, created_at, mode, market, line, selection, odds_taken, stake, status)
VALUES ('M1','2026-10-18 09:00:00','paper','主客和',NULL,'客勝',4.20,100,'open'),
       ('M1','2026-10-18 09:05:00','paper','主客和',NULL,'客勝',4.20,100,'open'),
       ('M1','2026-10-18 09:10:00','paper','主客和',NULL,'客勝',4.20,100,'open'),
       ('M2','2026-10-18 09:00:00','paper','入球大細','3.5','細',1.88,100,'open');
""")
con.commit()
con.close()

# 2) init_db 應清理舊重複並建 index
db.init_db()
rows = db.q("SELECT id, match_id, status, note FROM bets ORDER BY id")
for r in rows:
    print("  bet", r["id"], r["match_id"], r["status"], r["note"])
assert sum(1 for r in rows if r["match_id"] == "M1" and r["status"] != "void") == 1, "M1 應只剩 1 張有效"
assert sum(1 for r in rows if r["status"] == "void") == 2, "應有 2 張標為 void"
idx = db.q("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_bets_unique'")
assert idx, "unique index 未建立"
print("OK 舊重複已清理 + index 已建立")

# 3) 再落同一注 → 應該回傳 None，而且唔會多一張
vb = {"id": 1, "match_id": "M1", "market": "主客和", "line": None, "selection": "客勝",
      "hkjc_odds": 4.20, "stake": 100}
first = staking.record_bet(vb, mode="paper", stake=100, note="test")
second = staking.record_bet(vb, mode="paper", stake=100, note="test")
print("  first =", first, " second =", second)
assert second is None, "第二次落注應該被擋"
n = db.q("SELECT COUNT(*) c FROM bets WHERE match_id='M1' AND status<>'void'")[0]["c"]
assert n == 1, n
print("OK 重複落注被擋（M1 有效注單 = 1）")

# 4) 不同選項仍可落（唔會過度封鎖）
vb2 = dict(vb, selection="主勝")
assert staking.record_bet(vb2, mode="paper", stake=100) is not None
print("OK 唔同選項照樣可以落注")

# 5) 掃描鎖：第二次應該攞唔到
l1 = scanner._acquire_scan_lock()
l2 = scanner._acquire_scan_lock()
print("  lock1 =", bool(l1), " lock2 =", bool(l2))
assert l1 and l2 is None
scanner._release_scan_lock(l1)
l3 = scanner._acquire_scan_lock()
assert l3, "釋放後應可再取鎖"
scanner._release_scan_lock(l3)
print("OK 掃描鎖有效（重疊執行會被跳過）")

# 6) booked_keys 應該包含未作廢的注
keys = scanner._bet_keys()
assert ("M1", "主客和", None, "客勝") in keys
print("OK booked_keys 包含現有注單，掃描時會跳過")

print("\nALL PASS")
shutil.rmtree(tmpdir, ignore_errors=True)

# 香港賽馬會足球價值投注系統（HKJC Football Value System）

一個**自己擁有、成本極低**嘅足球價值投注系統：本地（或 Railway 上）用 Poisson / Dixon-Coles
算出自己嘅機率，再對馬會賠率去水後比較，搵出期望值（EV）為正嘅盤口，Telegram 通知你，
**你負責落注，系統負責一切記錄、結算同績效追蹤**。

玩法範圍：**主客和（HAD）＋ 入球大細（TTG）**。唔玩讓球、唔玩波膽。

---

## 1. 核心迴圈

```
                ┌──────────────── 離線（每日一次，零成本）────────────────┐
                │  賽程 / 賠率抓取（馬會官網）→ SQLite → 球隊歷史 → 強度參數  │
                └───────────────────────────┬────────────────────────────┘
                                            ▼
   開賽前 T-60 / T-30   本地計算：λ → 比數矩陣 → 1X2 機率 + 大細機率
                                            ▼
                        去水（移除馬會抽水）→ 公平賠率 → EV = 模型機率 × 賠率 − 1
                                            ▼
                      過門檻 → 三層風控 → Telegram 推送 ＋ **自動跟注（paper）落帳**
                                            ▼
                        賽果回填 → 自動結算 → ROI / 命中率 / CLV → 日報、週報
                                            ▼
                        賽果寫回歷史 → 明天嘅模型用今日嘅數據（自動累積）
```

## 2. 兩條記錄線（重要設計）

| 線 | 誰記錄 | 用途 |
|---|---|---|
| 🤖 **paper（自動跟注）** | 系統自己落帳，賠率用抓取時嘅快照 | 令「有冇 edge」有真實數據可睇，唔受你忙／唔記得影響 |
| ✍️ **real（你實際落注）** | 你按 Telegram 按鈕或 `/bet` 確認 | 睇你真實執行嘅成績，同 paper 對比 |

兩條線分開計 ROI，咁你一眼就知「系統建議」同「你真實落注」差幾遠。

## 3. 檔案

| 檔案 | 做乜 |
|---|---|
| `config.py` | 全部參數（可用環境變數覆蓋） |
| `db.py` | SQLite 表結構同存取（單一寫入點） |
| `timeutil.py` | 香港時間工具（**唔用容器 UTC**，呢個坑已經踩過） |
| `hkjc.py` | 馬會官網抓取：`probe()` / 賽程 / 賠率 / 賽果 ＋ 人手輸入 parser |
| `features.py` | 球隊強度（無資料洩漏）、CSV 匯入 |
| `poisson.py` | λ 計算、Dixon-Coles、比數矩陣、1X2 + 大細機率 |
| `engine.py` | 一場比賽 → 一個預測（寫入 `predictions`） |
| `value.py` | 去水、公平賠率、EV、價值判斷 |
| `staking.py` | 平注／Kelly、三層風控、落帳（paper / real） |
| `scanner.py` | 編排：同步 → 掃描 → 過濾 → 推送 → 自動跟注 |
| `settlement.py` | 賽果 → 勝負 → 派彩 → CLV → 寫回歷史 |
| `journal.py` | ROI / 命中率 / CLV / 日報週報 / 資產曲線 |
| `notifier.py` | Telegram 推送（出錯一定推，唔會靜靜死） |
| `telegram_bot.py` | 雙向 bot：指令 ＋ inline 按鈕確認落注 |
| `settings_store.py` | 線上改參數，唔需要重新部署 |
| `main.py` | 一個 process = Flask ＋ 排程 thread ＋ bot thread |
| `backtest.py` | walk-forward 回測（唯一可接受嘅調參方法） |
| `demo_data.py` | 示範聯賽，令你未接好馬會之前都可以睇到成個流程 |
| `selftest.py` | 一鍵自測：seed → scan → settle → report |
| `cli.py` | 本地命令行 |
| `templates/index.html` | 手機睇得嘅儀表板 |

## 4. 本地快速開始（5 分鐘）

```bash
cd backend
pip install -r requirements.txt

python cli.py initdb
python cli.py demo --seed          # 建示範聯賽同今日賽事（有 2 場故意做偏價）
python cli.py sim "曼聯" "利物浦"   # 試算任何兩隊
python cli.py scan --no-push       # 睇會唔會搵到價值
python cli.py report
python selftest.py                 # 一鍵自測全流程
python cli.py serve                # 本機開儀表板 http://localhost:8080
```

## 5. 接真數據（唯一需要你落手嘅一步）

馬會**冇公開 API**，官網前端用自己嘅 JSON／GraphQL 端點。呢類端點隨時改版，
所以系統唔會假設格式，而係：

```bash
python cli.py probe
```

`probe` 會試幾個候選端點，將**原始回應**存入 `data/probe/*.json`，並印出摘要。
把其中一日嘅檔案交返嚟，就可以將 `hkjc.py` 嘅欄位對應一次過校準
（`settings` 內可設 `hkjc_field_map`，唔使改 code）。

抓唔到都唔會停：`/setodds` 可以人手入賠率，系統照樣計 EV、照樣記錄。

```bash
# 人手入賠率格式（Telegram /setodds 同一格式）
主隊 vs 客隊 | HAD 2.10 3.35 3.60 | TTG 2.5 OVER 1.85 UNDER 1.95
```

## 6. 歷史數據（決定模型準唔準）

模型需要球隊歷史賽果。三條路：

1. **自動累積**：系統每次結算會把賽果寫入 `team_matches`，跑得越久越準。
2. **CSV 匯入**（最快）：`python cli.py import-csv data/history.csv`
   格式見 `data/history.sample.csv`。
3. 可選第三方 API（`API_FOOTBALL_KEY`），非必需。

## 7. 回測（落真錢之前必做）

```bash
python cli.py backtest --csv data/history.csv
python cli.py backtest --csv data/history.csv --odds-csv data/odds.sample.csv --sweep
```

輸出：log loss、Brier、準確率、同「純聯賽平均」基準嘅比較；有賠率 CSV 就再出
EV 門檻掃描（每個門檻嘅注數、命中率、ROI），用數據揀門檻，唔好拍腦袋。

> 誠實聲明：**未經回測證實有正期望值之前，唔應該落真錢。** 系統預設
> `AUTO_FOLLOW=true` 只係做 paper 記錄，唔會、亦都無能力替你真正落注。

## 8. 部署

見另一個檔案《部署同操作手冊》（Railway ＋ Telegram 逐步設定）。
關鍵三點：**Root Directory = backend**、**掛 Volume 並設 `DB_PATH=/data/football.db`**、
**gunicorn 只跑 1 個 worker**。

## 9. 環境變數

全部參數都可以用環境變數覆蓋（見 `.env.example`）。重點幾個：

| 變數 | 預設 | 意思 |
|---|---|---|
| `AUTO_FOLLOW` | true | 系統自動把每個價值建議記錄成 paper 注 |
| `EV_THRESHOLD` | 0.05 | 幾多 EV 才算價值（5%） |
| `STAKING_MODE` | flat | `flat` 或 `kelly` |
| `UNITS_PER_BET_PCT` | 1.0 | 每注佔本金 %（三層風控嘅第一層） |
| `MAX_BETS_PER_DAY` | 8 | 每日最多注數 |
| `MAX_DAILY_LOSS_PCT` | 5 | 單日虧損上限，到咗就停手 |
| `MARKETS` | HAD,TTG | 只玩呢兩個盤 |
| `DB_PATH` | data/football.db | Railway 上一定要指去 Volume |

## 10. 授權 / 風險

- 馬會官網嘅自動化抓取屬灰色地帶，官網隨時改版或者封 IP，請自行衡量；系統設計上
  一定會 catch 到失敗並通知你，唔會靜靜地跑錯數。
- 呢個系統係分析同記錄工具，**唔會替你下注**。落注風險自負。

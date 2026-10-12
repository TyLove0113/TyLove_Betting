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

> **★ Windows 用戶（你）：只照 4a。** 4b 係 Linux／macOS／Railway 專用；喺 Windows 貼 4b 一定會出
> 「`pip` 無法辨識」「`python3` 無法辨識」——因為 Windows 根本冇 `python3` 呢個名，`pip` 亦要用
> `python -m pip` 叫。
> 如果你係用 Cursor 嘅 AI（Chat / Agent）叫佢幫你跑，佢好可能照 4b 嘅 Linux 寫法去做，於是就出
> 呢兩個錯。請自己開 **Terminal → New Terminal**，親手貼 4a。

### 4a. Windows（Cursor / PowerShell — 你部機）

Cursor 預設終端係 **PowerShell**。你部機嘅情況：**Python 3.11.9 已經裝好，但 `python` 唔喺 PATH**
（所以打 `python`／`python3` 都會「無法辨識」）。唔需要重裝，**一律用 `py` 叫佢**就冇問題。

> **★ 先確認你企喺邊個資料夾**（最常中嘅坑）：打 `dir`
> - 見到 `README.md` 同 `backend`（資料夾）→ 你喺 **repo 根目錄**，照下面 block 打。
> - 見到 `cli.py`、`config.py`、`engine.py` → 你已經喺 **backend 裡面**。唔好再打 `cd backend`
>   （會變 `backend\backend`），亦唔好打 `backend\requirements.txt`（該檔喺上一層）。
>   改用：`cd ..` 之後照下面 block 打。


**第一步：裝套件（只做一次；喺 repo 根目錄打，即見到 `README.md` 同 `backend` 資料夾嗰層）**

```powershell
py -3.11 -m pip install -r backend\requirements.txt
```

**🩺 一鍵診斷：`py cli.py doctor`**（Telegram 打 `/check`，或儀表板「🩺 自我檢查」卡）—— 逐項話你知 token 有冇效、有冇 webhook 搶走訊息、收／發訊息通唔通、資料庫同排程狀態，最後有白話總結同「點修」。有問題可以加 `--fix` 自動修正。

**★ Telegram 冇反應（`/start`、`/help` 冇回覆）？** bot 只會喺**有程式喺度輪詢**嘅時候收訊息：
本機 `py cli.py bot`（或 `py cli.py serve`），或者 Railway 開住（Variables 要有 `TELEGRAM_BOT_TOKEN`，
Deployments log 見到 `[bot] polling started`）。`py cli.py notify` 只係一次性推送，推完就結束。
**同一個 token 任何時間只可以有一個地方輪詢** —— 本機同 Railway 同時開會互相搶 update（log 出
`409 Conflict`），請只開一邊。

**喺 Railway 邊句 log 睇？** Deployments → View Logs：開機要有 `[bot] thread started` 同
`[bot] polling started`；你喺 Telegram 打字時要出現 `[bot] recv /help from chat …` 同
`[bot] handled update …`。只有心跳／報告但冇 `recv` → 更新冇送到（多數係有第二個 poller）；
有 `recv` 但冇回覆 → log 會有 `[bot] send failed: …` 講原因。v1.2.2 起輪詢 thread 死咗會自動重開。

**第二步：跑（喺 `backend` 資料夾內，即見到 `cli.py` 嗰層）**

```powershell
py cli.py env                    # 先睇：Python 版本、.env 讀到未、資料庫位置
py cli.py initdb
py cli.py demo --seed            # 建示範聯賽同今日賽事（有 2 場故意做偏價）
py cli.py scan --no-push         # 睇會唔會搵到價值
py cli.py report
py cli.py log                    # 投注記錄（帳簿）：最新 50 注＋摘要，並寫出 CSV／日誌檔
py cli.py bot                    # 只開 Telegram 收訊息（唔開排程；Ctrl+C 停）— 測試 /help 最方便
py cli.py doctor                 # 🩺 自我檢查：token／webhook／收發訊息／資料庫／排程逐項報告
py cli.py doctor --fix           # 順手自動修正（例如清走阻住輪詢嘅 webhook）
py selftest.py                   # 一鍵自測全流程
py cli.py serve                  # 本機開儀表板 http://localhost:8080
```

**投注記錄（帳簿）去邊睇？** ① 本機 `py cli.py log`（亦可以 `--days 7`、`--mode real`）
② 瀏覽器 `https://<你個app>/ledger.csv` 下載 Excel（中文唔亂碼），或儀表板最底「📒 投注記錄」卡
③ Telegram `/log` 或 `/log 7`。
寫出嘅檔喺 `logs/`（跟資料庫同一個資料夾；Railway 上係 `/data/logs/`）：`bets_ledger.csv`（全部注項）、
`bets_YYYY-MM-DD.csv`（當日）、`YYYY-MM-DD.md`（當日日誌）；每日 23:00（香港時間）自動寫一次，
`DAYLOG_HOUR` 可改。

`py` 唔靠 PATH，喺任何新終端都一樣得，所以日常就用佢。

**`.env` 放邊？** 放 `backend\.env` 或者 **repo 根目錄** `.env` 都可以 —— 程式兩個位都會搵
（`backend\` 優先）。**唔需要搬走**：`.gitignore` 已經擋咗 `.env` 同 `.env.*`，唔會被 push 上 GitHub；
只有冇密碼嘅範本 `.env.example` 會上 GitHub。想確認讀咗邊個檔、token 有冇生效：`py cli.py env`。

**（可選）想用獨立 venv，唔污染系統 Python**：

```powershell
py -3.11 -m venv .venv                                                     # 喺 repo 根目錄
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python.exe cli.py scan --no-push                          # 唔需要 activate
```

**一鍵版**：在 repo 根目錄打 `.\run_local.cmd`（雙擊都可以）——會自動建 venv、裝套件、initdb、
seed、自測，最後開儀表板。唔需要事先改 PowerShell 執行原則。呢個腳本會自己搵自己嘅位置，
所以就算你身處 `backend` 裡面、或者喺其他資料夾用完整路徑叫它，都一樣跑得對。

**已經身處 `backend` 資料夾裡面？** 唔需要 `cd`，直接：

```powershell
py cli.py env                    # 睇版本／.env 讀到未／資料庫／token（最新版先有呢個指令）
py cli.py scan --no-push         # 出價值清單
py cli.py report                 # 日報（ROI、命中率、CLV）
py selftest.py                   # 全流程自測
py cli.py serve                  # 儀表板 http://localhost:8080
```

**★ `cli.py` 喺 `backend\` 裡面，唔喺 repo 根目錄。** 所以喺根目錄打 `py cli.py …` 會出
`can't open file '…\TyLove_Betting\cli.py'`。喺根目錄想直接跑，用提供好嘅入口：

```powershell
.\cli.cmd env                    # 等同 backend\ 內嘅 py cli.py env
.\cli.cmd scan --no-push
.\cli.cmd serve
```

**點更新到最新版**：下載最新 `football-value-system-v1.zip`，解壓後用裡面嘅 `backend\` 同幾個根目錄檔
（`README.md`、`VERSION.txt`、`.gitignore`、`run_local.ps1`、`run_local.cmd`）覆蓋你本機嘅同名檔。
你嘅資料庫喺 `TyLove_Betting\data\football.db`（喺 `backend\` **外面**），所以覆蓋唔會影響紀錄。
更新後打 `py cli.py env`，第一行就會顯示版本（現時係 `HKJC 足球價值投注系統 v1.1.1`）。

> **★ 如果你選擇用 venv 嘅 `Activate.ps1` 方式**：啟動只係當前終端有效；每次開新終端、或重開
> Cursor，都要再跑一次，否則 `python` 又會「無法辨識」。唔想理就用 `py cli.py …`，
> 或者 `..\.venv\Scripts\python.exe cli.py …`（兩者都唔靠 PATH）。

> **Windows 錯誤對照**：`python`／`python3`／`pip` 無法辨識 → 一律改用 `py`（`py cli.py scan`、
> `py -m pip install …`）；`cp` 無法辨識 → 用 `copy`（PowerShell 本身有 `cp` 別名，通常唔會出事）；
> `FileNotFoundError: [WinError 2] … '/data'` → `.env` 開咗 Railway 專用嘅 `DB_PATH=/data/football.db`，
> 喺 `.env` 前面加 `#` 註釋佢即可（最新版本亦會自動退回本機 `data\` 夾並印出提示）；
> `scan` 顯示 `checked 0` → 一係已達每日注數上限（睇 `reason`），一係手上版本未修跨午夜嘅窗
> （v1.1.3 起掃描改用以現在為中心嘅時間窗，凌晨開賽嘅場都掃到）。
>
> **Python 版本**：套件鎖定 3.11（numpy 1.26.4 / xgboost 2.0.3 只有 3.11–3.12 嘅預編譯輪檔）。
> 如果你部機係 3.13，請另外裝一個 3.11，再用 `py -3.11 -m venv .venv`。

### 4b. Linux / macOS / Railway Shell（Windows 唔好用）

```bash
cd backend
pip install -r requirements.txt        # Windows 要用 python -m pip install -r requirements.txt

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

# run_local.ps1 — Windows 一鍵試跑
#   建 venv -> 裝套件 -> initdb -> demo seed -> scan -> report -> selftest -> 開儀表板
# 用法（在 repo 根目錄的 PowerShell）：
#     .\run_local.ps1              全部跑完，最後開儀表板
#     .\run_local.ps1 -NoServe     只跑自測，唔開服務
# 唔需要事先改 PowerShell 執行原則：用 run_local.cmd 會自動 Bypass。

param([switch]$NoServe)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
Write-Host "== HKJC 足球價值投注系統 — 本機試跑 ==" -ForegroundColor Cyan
Write-Host "資料夾：$root`n"

# --- 1) 找 Python（Windows 冇 python3，只有 py / python）---
# 套件鎖定 3.11：numpy 1.26.4 / xgboost 2.0.3 / scikit-learn 1.4.2 只有 3.11-3.12 嘅預編譯輪檔，
# 用 3.13 會嘗試即場編譯然後失敗。所以優先揀 3.11，其次 3.12。
$py = $null
$pyPre = @()
$pyver = ""
foreach ($cand in @("py -3.11", "py -3.12", "py -3", "python")) {
    $parts = $cand.Split(" ")
    $exe = $parts[0]
    $pre = @()
    if ($parts.Count -gt 1) { $pre = @($parts[1]) }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $out = & $exe @pre -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    } catch { continue }
    if ($LASTEXITCODE -eq 0 -and $out -match '^\d+\.\d+\.\d+$') { $py = $exe; $pyPre = $pre; $pyver = $out; break }
}
if (-not $py) {
    Write-Host "找不到 Python。" -ForegroundColor Red
    Write-Host "請去 https://www.python.org/downloads/ 下載 Python 3.11，安裝第一步要勾『Add python.exe to PATH』，" -ForegroundColor Yellow
    Write-Host "然後關掉再開一個新嘅 PowerShell 視窗，再跑一次。" -ForegroundColor Yellow
    exit 1
}
$pyMM = (($pyver -split '\.')[0..1]) -join '.'
if (@('3.11', '3.12') -notcontains $pyMM) {
    Write-Host "你部機嘅 Python 係 $pyver。" -ForegroundColor Yellow
    Write-Host "本程式套件鎖定 Python 3.11／3.12（numpy 1.26.4、xgboost 2.0.3 只有呢兩個版本嘅預編譯輪檔；3.13 會裝唔到）。" -ForegroundColor Yellow
    Write-Host "請另外裝一個 Python 3.11（https://www.python.org/downloads/），安裝時勾 Add python.exe to PATH，" -ForegroundColor Yellow
    Write-Host "之後我自動會用 py -3.11，唔會影響你原有嘅 Python。" -ForegroundColor Yellow
    exit 1
}
Write-Host "用 $py（Python $pyver）" -ForegroundColor Green

# --- 2) 虛擬環境 ---
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "建立虛擬環境 .venv（唔會污染你系統嘅 Python）..."
    & $py -m venv .venv
}
if (-not (Test-Path $venvPy)) { Write-Host "建立 .venv 失敗。" -ForegroundColor Red; exit 1 }

# --- 3) 套件 ---
Write-Host "安裝套件（第一次約 1-2 分鐘）..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root "backend\requirements.txt") --quiet

# --- 4) .env（本機可以暫時留空 Telegram token）---
$envFile = Join-Path $root "backend\.env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root "backend\.env.example") $envFile
    Write-Host "已建立 backend\.env（Telegram token 之後再填）"
}

# --- 5) 跑一次全流程 ---
Set-Location (Join-Path $root "backend")
foreach ($step in @(
        @("initdb",        { & $venvPy cli.py initdb }),
        @("demo --seed",   { & $venvPy cli.py demo --seed }),
        @("scan --no-push",{ & $venvPy cli.py scan --no-push }),
        @("report",        { & $venvPy cli.py report }),
        @("selftest",      { & $venvPy selftest.py })
    )) {
    Write-Host "`n-- $($step[0]) --" -ForegroundColor Cyan
    & $step[1]
}

# --- 6) 開儀表板 ---
if (-not $NoServe) {
    Write-Host "`n全部通過。儀表板：http://localhost:8080   （Ctrl+C 停）" -ForegroundColor Green
    & $venvPy cli.py serve
} else {
    Write-Host "`n完成（-NoServe：唔開服務）。" -ForegroundColor Green
}

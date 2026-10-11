@echo off
REM 一路都可以用嘅入口：喺 repo 根目錄打
REM     .\cli.cmd env
REM     .\cli.cmd scan --no-push
REM 等同於 cd 入 backend\ 之後打 py cli.py ...
REM 唔需要記住 cli.py 喺邊，亦唔會撞「python 無法辨識」（用 py launcher）。
py "%~dp0backend\cli.py" %*

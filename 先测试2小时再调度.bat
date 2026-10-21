@echo off
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

echo ============================================================
echo   微博六榜采集 - 2 小时验证测试（跑完自动进入三天调度）
echo ============================================================
echo.

if not exist "%PY%" (
    echo [错误] 找不到 .venv\Scripts\python.exe
    echo.
    pause
    exit /b 1
)

if not exist "config\cookie.txt" (
    echo [错误] 找不到 config\cookie.txt —— 分类榜必须有登录 Cookie
    echo.
    pause
    exit /b 1
)

echo 即将：立即开始 2 小时连续采集测试（约 4 轮），
echo       测试结束后调度器自动待命，等到 10-19 16:00 正式开始。
echo.
echo [重要] 测试期间请保持电脑开机并插电源。
echo.
echo 按任意键开始测试...
pause >nul

"%PY%" -u scripts\scheduler.py --test

echo.
echo ============================================================
echo   调度器已退出（详见 logs\scheduler.log）
echo ============================================================
pause

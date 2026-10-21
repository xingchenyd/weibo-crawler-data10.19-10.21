@echo off
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

echo ============================================================
echo   微博六榜采集 - 三天定时调度器（10-19 / 10-20 / 10-21）
echo ============================================================
echo.
echo   每天 16:00 自动开始，22:00 收工（跑完最后一轮约 22:16 退出）
echo   采集期间系统不会睡眠，但屏幕会正常熄灭（不影响采集）
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

echo [重要] 采集时段请保持电脑开机且插着电源（电池 10 分钟会睡眠）。
echo [重要] 这个窗口不要关。最小化没关系。
echo.
echo   日志： logs\scheduler.log  （调度器）
echo         logs\night_run.log  （采集）
echo         logs\watchdog.log   （守护）
echo.
echo 按任意键启动调度器（启动后会一直等到明天 16:00 才开始采集）...
pause >nul

"%PY%" -u scripts\scheduler.py

echo.
echo ============================================================
echo   调度器已退出（三天全部结束或发生错误，详见 logs\scheduler.log）
echo ============================================================
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
set LOG=logs\run_%date:~0,4%%date:~5,2%%date:~8,2%.log

echo ============================================================
echo   微博六榜采集 - 一键启动
echo ============================================================
echo.

if not exist "%PY%" (
    echo [错误] 找不到 .venv\Scripts\python.exe
    echo        请先在本目录执行： python -m venv .venv
    echo.
    pause
    exit /b 1
)

if not exist "config\cookie.txt" (
    echo [错误] 找不到 config\cookie.txt
    echo        请先按 README 配置登录 Cookie。
    echo.
    pause
    exit /b 1
)

echo [1/3] 检查登录状态（10 秒，只发 6 次请求）...
echo.
"%PY%" scripts\multi_board_tracker.py --probe
if errorlevel 1 (
    echo.
    echo [中止] 探榜失败。多半是 Cookie 失效，重新获取后再跑。
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [2/3] 开始长跑  18:00 --^> 24:00
echo ============================================================
echo   每 30 分钟一轮，采榜位 + 热度 + 正文 + 评论
echo   到 24:00 会再跑完最后一轮，然后自动结束
echo.
echo   ^* 这个窗口不要关 ^*
echo   ^* 电脑不要睡眠（控制面板 - 电源选项 - 睡眠改成 从不）^*
echo.
pause

if not exist "logs" mkdir logs
echo.
echo 日志同时写入： %LOG%
echo 想中途看进度：另开一个窗口跑 --status（不影响采集）
echo.

rem 用 powershell 做 tee：屏幕实时显示 + 同时存文件
powershell -NoProfile -Command "& '%PY%' 'scripts\multi_board_tracker.py' --until '00:00' --max-comment-pages 5 2>&1 | Tee-Object -FilePath '%LOG%'"
set RC=%ERRORLEVEL%

echo.
echo ============================================================
echo [3/3] 采集结束（退出码 %RC%）
echo ============================================================
echo   0 = 正常跑完
echo   4 = 撞风控，已自动停手（24 小时内别重跑）
echo   5 = 六榜连续两轮全空（Cookie 失效）
echo   6 = 登录态失效（重新获取 Cookie）
echo.

if not "%RC%"=="0" (
    echo [提示] 退出码不是 0，先看上面的原因，别急着导出。
    echo.
    pause
    exit /b %RC%
)

echo 正在导出表格并下载配图，请稍候...
echo.
"%PY%" scripts\export_tracker.py --fetch-pics

echo.
echo ============================================================
echo   全部完成
echo ============================================================
echo   产出目录： dataset_tracker\事件数据集\
echo     - 事件.xlsx
echo     - 事件评论信息.xlsx
echo     - 事件图\  （配图）
echo.
pause

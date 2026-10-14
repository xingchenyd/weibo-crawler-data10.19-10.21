@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

echo ============================================================
echo   试水一轮（约 15 分钟） - 不带长跑
echo ============================================================
echo   目的：确认带评论抓取也不会撞风控
echo   跑完看最后一行"风控 X" —— X 必须是 0
echo.
pause

if not exist "config\cookie.txt" (
    echo [错误] 找不到 config\cookie.txt，先配置 Cookie。
    pause
    exit /b 1
)

"%PY%" scripts\multi_board_tracker.py --probe
if errorlevel 1 (
    echo.
    echo [中止] 探榜失败，Cookie 可能失效。
    pause
    exit /b 1
)

echo.
echo ---- 开始跑一轮，请稍候 ----
echo.
"%PY%" scripts\multi_board_tracker.py --tick --max-comment-pages 5
set RC=%ERRORLEVEL%

echo.
echo ============================================================
echo   一轮结束（退出码 %RC%）
echo ============================================================
echo   请往上看最后几行，确认：
echo     1) "风控 0"
echo     2) 评论数不是 0（"评论 +N"）
echo.
echo   如果风控是 0，就双击"开始采集.bat"跑长跑。
echo.
pause

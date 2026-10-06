@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  Hotkey test  -  check the three keys are detected
echo ==========================================================
echo.
echo  Press these one by one. The window prints what it sees:
echo      [ <- ]  left arrow
echo      [ -> ]  right arrow
echo      [ Numpad Enter ]   (NOT the big main Enter)
echo.
echo  This only LISTENS. It never blocks or injects keys.
echo  Press Ctrl+C to stop.
echo.
pause
python replay_director.py --test-keys --config config.json
pause

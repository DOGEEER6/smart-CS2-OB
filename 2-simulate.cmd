@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  Step 2 : engine logic test  (no CS2, no OBS required)
echo ==========================================================
echo.
echo  This feeds fake GSI packets into the engine and shows
echo  which rounds it decides to replay.
echo.
python replay_director.py --simulate --config config.json
echo.
pause

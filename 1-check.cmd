@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  Step 1 : check OBS websocket + scene config
echo ============================================
echo.
python replay_director.py --probe --config config.json
echo.
echo --------------------------------------------
echo  If you see "obs-websocket connection OK" and all
echo  the request names are green, you are good to go.
echo --------------------------------------------
pause

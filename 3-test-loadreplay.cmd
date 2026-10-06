@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  Step 3 : test the "Load replay" hotkey
echo ==========================================================
echo.
echo  IMPORTANT: the Replay Source does NOT create a replay by
echo  itself. The replay filter only keeps the last N seconds in
echo  memory; you must trigger its "Load replay" hotkey to turn
echo  that into a playable clip.
echo.
echo  This test triggers the hotkey and then reads the OBS log
echo  to confirm "replay added of X seconds".
echo.
echo  Make sure OBS is on the HUD scene (so the game capture is
echo  rendering) and CS2 is running. Wait 6+ seconds first.
echo.
pause
python replay_director.py --test-load-replay --config config.json
echo.
pause

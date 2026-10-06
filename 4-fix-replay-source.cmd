@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  Step 4 : fix Replay Source settings
echo ==========================================================
echo.
echo  Replay Source factory defaults are WRONG for auto replay:
echo    visibility_action = Continue   (must be Restart)
echo    end_action        = Loop single (should be Pause after single)
echo.
echo  This will now show you a DRY RUN (nothing is written).
echo.
python replay_director.py --check-replay-source --config config.json
echo.
echo ==========================================================
set /p GO="Write these values into OBS now? (y/N) "
if /i "%GO%"=="y" (
  python replay_director.py --configure-replay-source --apply --config config.json
  echo.
  echo  Done. Now open the Replay Source properties in OBS and
  echo  click OK once, or restart OBS, so the plugin reloads its state.
)
pause

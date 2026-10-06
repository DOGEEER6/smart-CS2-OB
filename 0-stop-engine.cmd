@echo off
chcp 65001 >nul
echo ==========================================================
echo  Stop the replay director
echo ==========================================================
echo.
echo  Looking for a process listening on port 23416 ...
echo.
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":23416" ^| findstr "LISTENING"') do (
  echo   Found PID %%a - killing it ...
  taskkill /PID %%a /F
  set FOUND=1
)
if "%FOUND%"=="0" (
  echo   Nothing is listening on 23416 - the engine is not running.
  echo   ^(Or check Task Manager for a stray python.exe^)
)
echo.
echo  Done. The control URLs will no longer respond, which is expected.
echo.
pause

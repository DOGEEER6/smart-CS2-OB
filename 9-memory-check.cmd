@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CS2 导播副驾 - 内存体检
echo ==========================================================
echo   CS2 导播副驾  --  回放插件内存体检
echo ==========================================================
echo.
echo   回答一个老问题: 为什么用这个插件会占好几 G 内存？
echo.
echo   它会按你的真实画布 / 帧率 / 时长把数字算出来，并核对
echo   Replay Source 里几个"会悄悄让内存翻倍"的设置。
echo.
echo   先开着 OBS（要连 obs-websocket 才能读到真实画布）。
echo ==========================================================
echo.
python replay_director.py --memory-report --config config.json
echo.
pause

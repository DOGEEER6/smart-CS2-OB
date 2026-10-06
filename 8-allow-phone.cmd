@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 放行手机访问 - CS2 导播副驾
echo ==========================================================
echo  放行手机访问（Windows 防火墙）
echo ==========================================================
echo.
echo  手机打不开提示器页面 / 一直转圈，几乎总是 Windows 防火墙
echo  拦了入站 TCP 23417。这一步需要管理员权限。
echo.
echo  如果弹出 UAC 授权框，点「是」。
echo.
echo  （正常情况下「首次设置」已经自动帮你加好了，
echo    只有它失败了、或者你换了电脑才需要手动跑这个。）
echo.
pause
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 正在申请管理员权限...
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)
echo.
echo  正在添加防火墙规则（只放行同一局域网，公网访问不到）...
netsh advfirewall firewall delete rule name="CS2DirectorViewer" >nul 2>&1
netsh advfirewall firewall delete rule name="DSH CS2 Viewer" >nul 2>&1
netsh advfirewall firewall add rule name="CS2DirectorViewer" dir=in protocol=TCP localport=23417 remoteip=LocalSubnet action=allow
echo.
if %errorlevel% equ 0 (
  echo  完成。现在用手机打开引擎窗口里打印的那个地址
  echo  （形如 http://192.168.x.x:23417/?k=XXXX）。
) else (
  echo  失败了。把这里的截图发给懂电脑的人看。
)
echo.
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  CS2 即时回放导播  --  一键模式
echo ==========================================================
echo.
echo   你平时只需要按【一个键】:
echo.
echo       [ -> ]  右方向键      抓取最近 10 秒 --^> 自动切画面慢放 --^> 自动切回
echo.
echo   备用:
echo       [小键盘 Enter]        手动切画面播放 / 再按一次立刻切回
echo                              (自动播关了的话就用它)
echo.
echo   什么时候按:
echo       回合刚结束 / 冻结时间刚开始  按 [ -> ]  就完事了
echo.
echo   为什么这样设计（单人导播）:
echo       回合进行中你只管切视角（你的 1~0 数字键），
echo       回合结束才按回放键。两件事在时间上完全分开，不用抢手。
echo.
echo   注意:
echo       1. 一直待在 HUD 场景 -- 切到 BP/数据看板就抓不到画面
echo       2. 按键会照常传给当前窗口，让 OBS 保持前台
echo       3. 关掉这个黑窗口 = 热键失效
echo.
echo   手机副驾提示器（引擎窗口里会打印带口令的网址，形如）:
echo       http://192.168.x.x:23417/?k=XXXXXX
echo     手机上打开后加到主屏幕。页面上有:
echo       - 该按哪个数字键（提示器排序第一名）
echo       - 自动切换开关（开了它替你按，需要 CS2 在前台）
echo       - 回合结束会提示你按 -^> 抓回放
echo     连不上的话双击 8-allow-phone.cmd 放行防火墙。
echo.
echo   浏览器控制端点（等价于按键）:
echo       http://127.0.0.1:23416/control/record_stop    = 按 -^>
echo       http://127.0.0.1:23416/control/replay         = 按 小键盘Enter
echo       http://127.0.0.1:23416/control/status         = 看状态
echo.
echo   按 Ctrl+C 停止。
echo ==========================================================
echo.
python replay_director.py --config config.json --log-file director.log
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CS2 导播副驾 - 首次设置向导
echo ==========================================================
echo   CS2 导播副驾  --  首次设置向导
echo ==========================================================
echo.
echo   它会自动帮你做这些事（能自己找到的绝不问你）:
echo.
echo     1. 从 Steam 注册表找到 CS2 装在哪
echo     2. 把 GSI 配置文件装进 CS2（不用你手抄路径）
echo     3. 读 OBS 的 obs-websocket 端口和密码（不用你手抄密码）
echo     4. 连上 OBS，检查 Replay Source 插件装没装
echo     5. 认出哪个场景是直播场景、哪个是回放场景
echo     6. 把回放源的参数调成导播要的样子
echo     7. 端到端自检，有问题会直接告诉你怎么办
echo.
echo   重要: 先启动 OBS！向导要连它才能自动配场景。
echo.
echo   想先看看它会做什么、不改任何东西，就关掉这个窗口，
echo   然后在文件夹地址栏输入 cmd 回车，运行:
echo       python setup_wizard.py --dry-run
echo ==========================================================
echo.
python setup_wizard.py
if errorlevel 1 (
  echo.
  echo ！！向导没能全部完成，上面有黄色的待办事项。
  echo ！！照着做完，再双击本文件跑一次。
)

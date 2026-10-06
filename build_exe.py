#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把导播工具打包成双击就能用的成品。

    python build_exe.py                 # 默认：文件夹版（推荐，最稳）
    python build_exe.py --mode onefile  # 单文件版（拷来拷去方便）
    python build_exe.py --clean         # 先清干净再打包

产物：
  文件夹版   CS2导播助手\\开始导播.exe      （旁边还有 _internal\\ 和 先运行我-首次设置.cmd）
  单文件版   开始导播.exe                   （就一个文件）

为什么默认是文件夹版？
  单文件版每次启动都要把自己解压到 %TEMP%，遇到杀软/组策略会被拦
  （报 "Could not create temporary directory!"），而且每次启动慢 1 秒左右。
  文件夹版不落地临时文件，启动快、不容易出幺蛾子。

「首次设置」不是单独的 exe，而是同一个程序的 `--setup`：
  开始导播.exe --setup
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(HERE, "dist")

try:  # 中文 Windows 下被重定向时 stdout 是 GBK，打 ✅ 会直接崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENTRY = "replay_director.py"          # 唯一入口，--setup 也在里面
INNER = "cs2_dir_engine"              # PyInstaller 内部名（ASCII，避免奇怪问题）
EXE_NAME = "开始导播.exe"
FOLDER_NAME = "CS2导播助手"
ONEFILE_NAME = "开始导播.exe"

# 用不到的大家伙，排掉能小一圈
EXCLUDES = ["tkinter", "unittest", "pydoc", "doctest", "test", "distutils",
            "lib2to3", "numpy", "PIL", "matplotlib", "pandas", "scipy",
            "PyQt5", "PySide2", "setuptools", "pip"]

# setup_wizard 是在 main() 里延迟 import 的，PyInstaller 静态分析看不到它
HIDDEN = ["websocket", "setup_wizard"]

FIRST_RUN_CMD = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 首次设置 - CS2 导播副驾
echo ==========================================================
echo  CS2 导播副驾 · 首次设置
echo ==========================================================
echo.
echo  这个向导会自动帮你：
echo    1. 找到 CS2 并把 GSI 配置装进游戏
echo    2. 读出 OBS 的 obs-websocket 密码（不用手抄）
echo    3. 检查 Replay Source 插件
echo    4. 识别直播场景、准备好回放场景和参数
echo    5. 放行手机访问（会弹一次 UAC 授权框，点"是"）
echo.
echo  配置已经好过的话，重新跑一遍也不会有任何改动。
echo.
pause
if not exist "开始导播.exe" (
  echo 找不到 开始导播.exe —— 请确认它和本文件在同一个文件夹里。
  pause
  exit /b 1
)
"开始导播.exe" --setup
pause
"""


# 跟着成品一起发出去的说明/工具
EXTRAS = ["使用说明.md", "8-allow-phone.cmd", "导播流程卡.md"]


def copy_extras(dest_dir):
    """把说明书和一键放行脚本拷进成品目录，让整个文件夹自包含。"""
    for name in EXTRAS:
        src = os.path.join(HERE, name)
        dst = os.path.join(dest_dir, name)
        if os.path.isfile(src) and os.path.realpath(src) != os.path.realpath(dst):
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"  （{name} 没拷过去：{e}）")


def run(cmd):
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=HERE)


def pyinstaller_args(mode, distpath):
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--onedir" if mode == "onedir" else "--onefile",
           "--console",            # 保留黑窗口（要打印手机地址、要能 Ctrl+C 停）
           "--name", INNER,
           "--distpath", distpath,
           "--workpath", os.path.join(BUILD, mode),
           "--specpath", os.path.join(BUILD, mode),
           ]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(ENTRY)
    return cmd


def build_onedir():
    """打成一个自包含的文件夹，直接落在项目目录里。"""
    out = os.path.join(HERE, FOLDER_NAME)
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    r = run(pyinstaller_args("onedir", HERE))
    if r.returncode != 0:
        return None, r.returncode
    src_dir = os.path.join(HERE, INNER)
    if not os.path.isdir(src_dir):
        print(f"❌ 没生成 {src_dir}")
        return None, 1
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.rename(src_dir, out)

    src_exe = os.path.join(out, INNER + ".exe")
    dst_exe = os.path.join(out, EXE_NAME)
    if not os.path.isfile(src_exe):
        print(f"❌ 没生成 {src_exe}")
        return None, 1
    os.replace(src_exe, dst_exe)

    with open(os.path.join(out, "先运行我-首次设置.cmd"), "w",
              encoding="utf-8-sig", newline="\r\n") as f:
        f.write(FIRST_RUN_CMD)
    copy_extras(out)

    size = sum(os.path.getsize(os.path.join(dp, fn))
               for dp, _, fns in os.walk(out) for fn in fns) / 1024 / 1024
    return (out, dst_exe, size), 0


def build_onefile():
    out = os.path.join(HERE, ONEFILE_NAME)
    r = run(pyinstaller_args("onefile", DIST))
    if r.returncode != 0:
        return None, r.returncode
    src = os.path.join(DIST, INNER + ".exe")
    if not os.path.isfile(src):
        print(f"❌ 没生成 {src}")
        return None, 1
    shutil.copy2(src, out)
    size = os.path.getsize(out) / 1024 / 1024
    cmd_path = os.path.join(HERE, "先运行我-首次设置.cmd")
    with open(cmd_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(FIRST_RUN_CMD)
    copy_extras(HERE)
    return (out, out, size), 0


def main():
    argv = sys.argv[1:]
    mode = "onedir"
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]
    if "--onefile" in argv:
        mode = "onefile"
    if mode not in ("onedir", "onefile"):
        print(f"❌ 不认识的 --mode {mode}（只能是 onedir / onefile）")
        return 2

    if "--clean" in argv:
        for d in (BUILD, DIST, os.path.join(HERE, FOLDER_NAME)):
            if os.path.isdir(d):
                print(f"清掉 {d}")
                shutil.rmtree(d, ignore_errors=True)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("❌ 没装 PyInstaller。先运行：  python -m pip install pyinstaller")
        return 2
    if not os.path.isfile(os.path.join(HERE, ENTRY)):
        print(f"❌ 找不到 {ENTRY}")
        return 2

    t0 = time.time()
    print()
    print("=" * 66)
    print(f"打包（{mode} 版）—— 引擎 + 首次设置在同一个程序里")
    print("=" * 66)
    built, rc = (build_onedir() if mode == "onedir" else build_onefile())
    if rc != 0:
        print(f"❌ 打包失败（退出码 {rc}）")
        return rc

    out, exe, size = built
    print()
    print("=" * 66)
    print(f"✅ 打包完成，用了 {time.time() - t0:.0f} 秒")
    print("=" * 66)
    if mode == "onedir":
        print(f"  成品文件夹: {out}")
        print(f"  整个文件夹: {size:.1f} MB")
        print(f"  双击这个  : {exe}")
        print()
        print("  第一次用：先双击文件夹里的「先运行我-首次设置.cmd」")
        print("  之后每次：双击「开始导播.exe」")
        print()
        print("  注意：整个文件夹一起拷走才能用（别只拷 exe）。")
    else:
        print(f"  成品: {out}   {size:.1f} MB")
        print()
        print("  第一次用：双击「先运行我-首次设置.cmd」")
        print("  之后每次：双击「开始导播.exe」")
        print()
        print("  单文件版拷到任何 Windows 电脑都能跑，不需要装 Python。")
        print("  如果它报 “Could not create temporary directory”，")
        print("  说明这台电脑的 %TEMP% 被限制/被杀软拦了，改用文件夹版即可。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

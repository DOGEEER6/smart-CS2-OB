#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把导播工具打包成双击就能用的成品（**开箱即用：连 ffmpeg 一起带上**）。

    python build_exe.py                 # 默认：文件夹版（推荐，最稳）+ 自动打 zip
    python build_exe.py --no-zip        # 不打 zip
    python build_exe.py --no-ffmpeg     # 不带 ffmpeg（体积小 30 MB 左右）
    python build_exe.py --mode onefile  # 单文件版（拷来拷去方便）
    python build_exe.py --clean         # 先清干净再打包

产物：
  文件夹版   CS2导播助手\\开始导播.exe      （旁边还有 _internal\\ 和 先运行我-首次设置.cmd）
             CS2导播助手\\ffmpeg.exe         （★ 自带的 ffmpeg，obs 后端开箱即用）
             CS2导播助手.zip                （同目录下，方便发给别人 / 传 Release 附件）
  单文件版   开始导播.exe                   （就一个文件；ffmpeg 要自己另外放旁边）

双击「开始导播.exe」= 图形设置窗口（2026-10-07 起）；窗口里点「启动引擎」才开始导播。
命令行用法完全不变：`开始导播.exe --engine` 直接跑引擎（6-director.cmd 走的就是这条）。

为什么默认是文件夹版？
  单文件版每次启动都要把自己解压到 %TEMP%，遇到杀软/组策略会被拦
  （报 "Could not create temporary directory!"），而且每次启动慢 1 秒左右。
  文件夹版不落地临时文件，启动快、不容易出幺蛾子。

「首次设置」不是单独的 exe，而是同一个程序的 `--setup`：
  开始导播.exe --setup

★ 为什么要把 ffmpeg 一起打包（2026-10-09 用户要求"所有依赖打包好、开箱即用"）：
  "OBS 自带 Replay Buffer" 这个后端（内存只占几十~一百多 MB）裁片段要用 ffmpeg，
  而让用户自己去 winget/官网下载、解压、配 PATH 是**最容易卡住的一步**。
  现在打包时自动把 ffmpeg.exe 复制到成品文件夹（跟 exe 同级），引擎会自己找到它，
  用户什么都不用装。想不带就加 `--no-ffmpeg`。
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(HERE, "dist")

try:  # 中文 Windows 下被重定向时 stdout 是 GBK，打 ✅ 会直接崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENTRY = "replay_director.py"          # 唯一入口，--setup / --gui 也在里面
INNER = "cs2_dir_engine"              # PyInstaller 内部名（ASCII，避免奇怪问题）
EXE_NAME = "开始导播.exe"
FOLDER_NAME = "CS2导播助手"
ONEFILE_NAME = "开始导播.exe"

# 用不到的大家伙，排掉能小一圈
# ⚠️ tkinter 不能排！2026-10-07 起图形设置窗口（director_gui.py）要用它。
EXCLUDES = ["unittest", "pydoc", "doctest", "test", "distutils",
            "lib2to3", "numpy", "PIL", "matplotlib", "pandas", "scipy",
            "PyQt5", "PySide2", "setuptools", "pip"]

# setup_wizard / director_gui 都是在 main() 里延迟 import 的，
# PyInstaller 静态分析看不到它们 → 必须显式 --hidden-import。
HIDDEN = ["websocket", "setup_wizard", "director_gui"]

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
echo    3. 检查回放能力（插件 / OBS 自带 Replay Buffer，二选一；两个都不用装也能用自动切镜头）
echo    4. 识别直播场景、准备好回放场景和参数
echo    5. 放行手机访问（会弹一次 UAC 授权框，点"是"）
echo.
echo  配置已经好过的话，重新跑一遍也不会有任何改动。
echo.
echo  ★ 想改按键 / 开关回放功能：双击「开始导播.exe」打开设置窗口。
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
EXTRAS = ["新手教程.md", "使用说明.md", "导播流程卡.md", "README.md",
          "8-allow-phone.cmd"]

# ffmpeg 相关：要找的候选位置（按优先级），以及随包要带的许可证说明
FFMPEG_LICENSE_FILES = ["docs/ffmpeg-说明.txt", "docs/ffmpeg-LICENSE.txt"]


def preflight_deps():
    """打包前：确认这台机器上真的有运行时必需的东西。

    ⚠️ 2026-10-07 实测踩到的坑：打包机没装 `websocket-client` 时，
    PyInstaller 的 `--hidden-import websocket` **只是打个警告就跳过**，
    打出来的 exe 双击能开、一连 OBS 就 `RuntimeError: 缺少 websocket-client` ——
    别人拿到就是"用不了"，还看不出原因。所以这里必须**硬拦**。
    """
    missing = []
    try:
        import websocket  # noqa: F401
        print(f"  ✅ websocket-client "
              f"{getattr(websocket, '__version__', '?')}（连 OBS 必需，会被打进 exe）")
    except Exception as e:
        missing.append(f"websocket-client（{e}）")
    try:
        import tkinter  # noqa: F401
        print(f"  ✅ tkinter {tkinter.TkVersion}（图形设置窗口必需，会被打进 exe）")
    except Exception as e:
        print(f"  ⚠️  没有 tkinter（{e}）—— 打出来的包将**打不开设置窗口**，"
              f"只能用命令行。建议装一个带 tkinter 的官方 Python 再打包。")
    if missing:
        print()
        print("❌ 缺运行时依赖，打出来的 exe 会在别人电脑上直接报错：")
        for m in missing:
            print(f"     · {m}")
        print("   先装：  python -m pip install websocket-client")
        return False
    return True


def verify_built_exe(exe_path):
    """打包后：直接拿产物跑一次自检（`--selftest`），确认关键依赖真的进去了。"""
    if not os.path.isfile(exe_path):
        print(f"  ❌ 找不到产物 {exe_path}")
        return False
    try:
        r = subprocess.run([exe_path, "--selftest"], cwd=os.path.dirname(exe_path),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except Exception as e:
        print(f"  ❌ 跑不起来：{e}")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if any(k in line for k in ("✅", "❌", "⚠️")):
            print("     " + line.strip())
    if r.returncode != 0:
        print(f"  ❌ 产物自检没通过（退出码 {r.returncode}）—— 别发出去。")
        return False
    print("  ✅ 产物自检通过（websocket-client / tkinter / 键位表 / 内存公式都在）")
    return True


def make_zip(folder, zip_path):
    """把成品文件夹打成 zip（发给别人 / 传 GitHub Release 附件用）。

    为什么要有这一步：以前只出文件夹，用户会问"压缩包在哪？" ——
    README 里又提到过"压缩包 7.6 MB"，那是手压的，脚本从来不打。
    现在默认就顺手打一个，`--no-zip` 可以关掉。
    """
    if os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except OSError as e:
            print(f"  ⚠️ 删不掉旧的 {zip_path}（{e}）—— 跳过打 zip")
            return None
    total = 0
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            base_parent = os.path.dirname(os.path.abspath(folder))
            for dp, _dn, fns in os.walk(folder):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, base_parent)   # 压缩包里保留顶层文件夹名
                    z.write(full, rel)
                    total += 1
    except Exception as e:
        print(f"  ⚠️ 打 zip 失败：{e}")
        return None
    size = os.path.getsize(zip_path) / 1024 / 1024
    print(f"  ✅ 已打包 {total} 个文件 → {zip_path}（{size:.1f} MB）")
    print("     这个 zip 可以直接发给别人；发 GitHub Release 就把它传成附件。")
    return zip_path


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


def find_ffmpeg_to_bundle():
    """找一份可以跟着成品发出去的 ffmpeg.exe（找不到返回 ""）。

    顺序：仓库根目录 → tools/ffmpeg/bin → PATH → 常见安装位置。
    之所以先看仓库根目录：把 ffmpeg.exe 丢在这里最省事（`--no-ffmpeg` 可以不带）。
    """
    cands = [
        os.path.join(HERE, "ffmpeg.exe"),
        os.path.join(HERE, "tools", "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(HERE, "ffmpeg", "bin", "ffmpeg.exe"),
    ]
    try:
        w = shutil.which("ffmpeg")
        if w:
            cands.append(w)
    except Exception:
        pass
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except Exception:
            continue
    return ""


def bundle_ffmpeg(dest_dir):
    """把 ffmpeg.exe 和它的许可证说明拷进成品目录（开箱即用的关键一步）。"""
    src = find_ffmpeg_to_bundle()
    if not src:
        print("  ⚠️  没找到 ffmpeg.exe —— 这次不带。")
        print("      （想带就把它放到仓库根目录，或先 winget install Gyan.FFmpeg 再打包；")
        print("        不带也能用：用户自己装 ffmpeg，或改用插件后端。）")
        return None
    dst = os.path.join(dest_dir, "ffmpeg.exe")
    if os.path.realpath(src) != os.path.realpath(dst):
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            print(f"  ❌ 拷 ffmpeg 失败：{e}")
            return None
    mb = os.path.getsize(dst) / 1024 / 1024
    print(f"  ✅ 已带上 ffmpeg：ffmpeg.exe（{mb:.1f} MB，源：{src}）")
    for rel in FFMPEG_LICENSE_FILES:
        s = os.path.join(HERE, rel)
        if os.path.isfile(s):
            try:
                shutil.copy2(s, os.path.join(dest_dir, os.path.basename(rel)))
            except Exception as e:
                print(f"  （{rel} 没拷过去：{e}）")
    print("     → 用户什么都不用装：引擎会在程序目录旁边找到它（obs 后端开箱即用）")
    return dst


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


def _rmtree_or_die(path, what):
    """删掉旧产物；删不掉就说清楚原因（最常见的是产物还开着）。"""
    if not os.path.isdir(path):
        return
    shutil.rmtree(path, ignore_errors=True)
    if os.path.isdir(path):
        print(f"❌ 删不掉旧的{what}：{path}")
        print("   最常见的原因：正在运行的「开始导播.exe」（或它的设置窗口）没关。")
        print("   关掉它再跑一次；也可以在任务管理器里结束「开始导播」进程。")
        raise SystemExit(2)


def build_onedir(want_ffmpeg=True):
    """打成一个自包含的文件夹，直接落在项目目录里。"""
    out = os.path.join(HERE, FOLDER_NAME)
    _rmtree_or_die(out, "成品文件夹")
    r = run(pyinstaller_args("onedir", HERE))
    if r.returncode != 0:
        return None, r.returncode
    src_dir = os.path.join(HERE, INNER)
    if not os.path.isdir(src_dir):
        print(f"❌ 没生成 {src_dir}")
        return None, 1
    _rmtree_or_die(out, "成品文件夹")
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
    if want_ffmpeg:
        print("自带依赖（不装任何东西就能用「OBS 自带 Replay Buffer」后端）：")
        bundle_ffmpeg(out)

    size = sum(os.path.getsize(os.path.join(dp, fn))
               for dp, _, fns in os.walk(out) for fn in fns) / 1024 / 1024
    return (out, dst_exe, size), 0


def build_onefile(want_ffmpeg=True):
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
    if want_ffmpeg:
        # 单文件版没法把 ffmpeg 塞进 exe（PyInstaller 只解压到临时目录，
        # 引擎找的是"exe 旁边"），所以就直接放在 exe 旁边 —— 一样是开箱即用。
        print("自带依赖（ffmpeg 放在 exe 旁边）：")
        bundle_ffmpeg(HERE)
    return (out, out, size), 0


def main():
    argv = sys.argv[1:]
    mode = "onedir"
    want_zip = "--no-zip" not in argv
    want_ffmpeg = "--no-ffmpeg" not in argv
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

    print()
    print("打包前检查运行环境：")
    if not preflight_deps():
        return 2

    t0 = time.time()
    print()
    print("=" * 66)
    print(f"打包（{mode} 版）—— 引擎 + 图形设置窗口 + 首次设置都在同一个程序里")
    print("=" * 66)
    built, rc = (build_onedir(want_ffmpeg) if mode == "onedir"
                 else build_onefile(want_ffmpeg))
    if rc != 0:
        print(f"❌ 打包失败（退出码 {rc}）")
        return rc

    out, exe, size = built
    print()
    print("打包后自检产物（拿真的 exe 跑一次 --selftest）：")
    if not verify_built_exe(exe):
        print()
        print("⚠️  产物自检没过 —— 请把上面的输出发出来，先别把它拷给别人。")
        return 3

    zip_path = None
    if want_zip and mode == "onedir":
        print()
        print("打 zip（发给别人 / 传 Release 附件用）：")
        zip_path = make_zip(out, os.path.join(HERE, FOLDER_NAME + ".zip"))

    ff_bundled = (mode == "onedir" and os.path.isfile(os.path.join(out, "ffmpeg.exe"))) or \
                 (mode == "onefile" and os.path.isfile(os.path.join(HERE, "ffmpeg.exe")))

    print()
    print("=" * 66)
    print(f"✅ 打包完成，用了 {time.time() - t0:.0f} 秒")
    print("=" * 66)
    if mode == "onedir":
        print(f"  成品文件夹: {out}")
        print(f"  整个文件夹: {size:.1f} MB"
              + ("（含自带的 ffmpeg）" if ff_bundled else ""))
        print(f"  双击这个  : {exe}")
        if zip_path:
            print(f"  发给别人用: {zip_path}"
                  f"（{os.path.getsize(zip_path) / 1024 / 1024:.1f} MB，解压后双击 开始导播.exe）")
        print()
        print("  第一次用：先双击文件夹里的「先运行我-首次设置.cmd」")
        print("  之后每次：双击「开始导播.exe」打开设置窗口，点「启动引擎」开始导播")
        print("            （不想开窗口就跑命令行：开始导播.exe --engine）")
        print()
        if ff_bundled:
            print("  ★ 开箱即用：文件夹里已经带了 ffmpeg.exe，对方**不需要装任何东西**")
            print("    （包括「OBS 自带 Replay Buffer」这个省内存后端需要的 ffmpeg）。")
        else:
            print("  ⚠️ 这次**没有**带 ffmpeg（加了 --no-ffmpeg，或打包机上找不到它）：")
            print("     用「OBS 自带 Replay Buffer」后端的人要自己装 ffmpeg，或改用插件后端。")
        print("  注意：整个文件夹一起拷走才能用（别只拷 exe）。不想打 zip 加 --no-zip。")
    else:
        print(f"  成品: {out}   {size:.1f} MB")
        print()
        print("  第一次用：双击「先运行我-首次设置.cmd」")
        print("  之后每次：双击「开始导播.exe」打开设置窗口，点「启动引擎」开始导播")
        print()
        print("  单文件版拷到任何 Windows 电脑都能跑，不需要装 Python。")
        if ff_bundled:
            print("  ★ ffmpeg.exe 就在这个 exe 旁边 —— 一起拷走，省内存的 obs 后端才能用。")
        print("  如果它报 “Could not create temporary directory”，")
        print("  说明这台电脑的 %TEMP% 被限制/被杀软拦了，改用文件夹版即可。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

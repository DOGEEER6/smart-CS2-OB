#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 自动即时重放导播 (Tier 0)
================================

作用
----
监听 CS2 的 Game State Integration (GSI) 数据，在每一回合结束时判断"这回合值不值得回放"，
如果值得，就通过 obs-websocket 自动把 OBS 切到「即时回放」场景（由 Exeldro 的 Replay Source
插件播放内存里的最近 N 秒），播完再自动切回直播场景。

设计原则
--------
1. **只碰 OBS，不碰游戏客户端。** 不使用 netcon、不注入输入、不读游戏内存。
   因此在完美世界这类 VAC 服务器上是安全的，也不需要 `-insecure -tools`。
2. **失败要静默。** 任何异常都不能乱切场景。崩溃 = 什么都不做，保持当前画面。
3. **人永远优先。** 只要你手动切了场景，引擎立刻锁死让位（抄的是 Astra 的 lock 逻辑）。
4. **回放绝不盖住直播。** 回合一旦开打（phase 变 live），立即强制切回。

用法
----
    # 1) 探测模式：检查 OBS 连接、请求名、场景与回放源配置（先跑这个！）
    python replay_director.py --probe

    # 2) 空跑模式：连 OBS 但只打日志，不真的切场景
    python replay_director.py --dry-run

    # 3) 仿真模式：不需要 CS2、不需要 OBS，用伪造数据验证决策逻辑
    python replay_director.py --simulate

    # 4) 正式运行
    python replay_director.py --config config.json

需要: pip install websocket-client   (你的机器上已经装了)
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import http.server
import json
import math
import os
import queue
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid

def console_utf8():
    """让 Windows 控制台能正确显示中文。

    .cmd 启动器里有 `chcp 65001`，但**双击 exe** 时没有 —— 中文 Windows 的控制台
    默认是 GBK(936)，而我们把 stdout 设成了 UTF-8，结果就是一片乱码。
    这里等价于替用户执行一次 chcp 65001。
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001)
        k.SetConsoleCP(65001)
    except Exception:
        pass


console_utf8()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def base_dir():
    """本程序所在目录。

    打包成 exe 后 `__file__` 指向 PyInstaller 的临时解包目录，
    配置/口令必须跟着 **exe 自己** 走，否则每次启动口令都变、手机书签失效。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None


# ============================================================================
# 0. 配置
# ============================================================================

DEFAULTS = {
    # --- GSI 接收 ---
    "gsi_host": "127.0.0.1",
    "gsi_port": 23416,
    "gsi_path": "/cs2/input",

    # --- obs-websocket ---
    "obs_url": "ws://127.0.0.1:4455",
    "obs_password": "",

    # --- 场景名（必须和你的 OBS 完全一致）---
    "live_scene": "HUD",              # 直播场景
    "replay_scene": "即时回放",        # 回放场景
    "replay_item": "Replay Source",   # 回放场景里 Replay Source 那个源的名字
    "overlay_items": [],              # 可选：跟回放一起开关的其它源（比如"回放"角标）

    # ★ 关键：Replay Source 不会自动生成回放。滤镜只是在内存里滚动保留最近 N 秒，
    #   必须先触发它的 "Load replay" 热键，才能把缓存快照成一个可播放的回放。
    #   如果插件改名导致热键找不到，改成手动按键方案（见 README）。
    "load_replay_hotkey": "ReplaySource.Replay",

    # --- 回放时长估算（要和 Replay Source 插件里的设置对上）---
    "capture_seconds": 5.0,           # 插件里的 Duration（素材秒数）
    "speed": 0.7,                     # 插件里的 Speed Percentage / 100
    "return_margin": 1.0,             # 播完后额外的安全余量（秒）

    # --- 回放策略 ---
    "min_score": 25,                  # 戏剧性阈值（仅 auto_replay=true 时生效）
    "cooldown_rounds": 0,             # 两次回放之间至少间隔几个回合
    "max_hold": 15.0,                 # 硬上限：回放最多占多少秒

    # --- 副驾提示器（手机上看）---
    # 只读页面，绑在 0.0.0.0 上方便手机连。**这个端口不含任何控制端点**，
    # 同网段的人最多只能看到画面状态，不能触发切场景。
    "web_enabled": True,
    "web_host": "0.0.0.0",
    "web_port": 23417,
    # 手机页面上要不要显示玩家名字（默认不显示 —— 只显示按键更省眼睛）
    "show_names": False,

    # --- 自动切换视角（手机上有开关）---
    # 默认关闭，你在手机上点开才生效。它做的事就是"把提示器第一名的数字键按下去"。
    "auto_switch": False,
    "auto_min_score": 45,      # 第一名分数低于这个值就不切（预设会覆盖它）
    "auto_min_dwell": 3.0,     # 切过去至少待这么多秒（预设会覆盖它）
    "auto_switch_margin": 10,  # 新目标要比当前看的人高出这么多分才切（预设会覆盖它）
    # 灵敏度档位：fast / normal / calm，手机上可实时切换
    "auto_preset": "normal",
    # 对枪胜率加成：开=镜头偏向"预测能活下来的一方"，关=纯看谁在交火
    "auto_win_bonus": True,
    # ★ 安全阀：只有当 CS2 在前台时才发按键。
    #   否则合成的数字键会打进别的程序（OBS 里若绑了数字键就会乱切场景）。
    "auto_require_focus": True,
    "auto_focus_exe": "cs2.exe",

    # --- 自动回放总开关 ---
    # ★ 默认 false：完全手动。GSI 只用来做安全保护（不盖住回合）和统计，
    #   不再自动判断"该不该回放"。想恢复自动判断就改成 true。
    "auto_replay": False,

    # --- 手动键盘控制（唯一的工作方式：手动框选片段）---
    #   ←  左方向键   标记入点：记住"这一刻"
    #   →  右方向键   标记出点：把"入点 → 现在"这一段裁成一段素材（结尾就停在出点）
    #   小键盘6       保留片段：把当前这段素材另存成视频文件（赛后能剪出去发）
    #   小键盘Enter   切到回放场景播放；再按一次立刻切回直播
    # ★ 2026-10-06 用户决定：**去掉所有自动定格**（回合结束自动抓、击杀后延迟抓都删掉了）。
    #   素材完全由 ← / → 两个键框出来；引擎不会在你没按键的时候去碰回放缓冲。
    "manual_keys": True,
    "record_max_seconds": 10.0,       # 滚动缓冲/单段素材最长多少秒（会写进插件的 Duration）
    "key_debounce": 0.5,              # 同一按键的重复触发间隔（秒）
    "min_clip_seconds": 0.3,          # ← 到 → 至少隔这么久才算有效（防手抖双击）

    # ★ 2026-10-07 用户要求：**按键可以改**（适应不同键盘 —— 60% 键盘没有小键盘，
    #   可以把"播放"改成 F8、"保留片段"改成 F9）。键名写法见下面的 KEY_TABLE，
    #   也可以在图形界面 / --set-keys 里直接按一下你要的键，让程序自己填。
    #   每个动作可以绑多个键（列表），例如 "play": ["numpad_enter", "f8"]。
    "keys": {
        "mark_in": ["left"],
        "mark_out": ["right"],
        "save": ["numpad6"],
        "play": ["numpad_enter"],
    },
    # ★ 按键监听方式（2026-10-08 加）：
    #   "hook"（默认）= 全局低级键盘钩子 WH_KEYBOARD_LL。精确（能区分主/小键盘 Enter），
    #                   但低级钩子有个特性：**回调返回之前，全系统的键盘都在排队**。
    #                   引擎的钩子回调已经改成"只入队、不阻塞"（微秒级），正常机器没问题；
    #                   但如果机器本身在换页/被杀软拖住，仍可能让输入发顿。
    #   "poll"        = `GetAsyncKeyState` 轮询（key_poll_ms，默认 20ms）。
    #                   **完全不进入输入管线**，物理上不可能拖住键盘 —— 排查
    #                   "一开这软件整机就像死机"时，先切到这个模式做对照。
    #                   代价：认不出主/小键盘 Enter 的区别（要区分就把 play 改成 f8）。
    "key_mode": "hook",
    "key_poll_ms": 20,

    # ★ 2026-10-07 用户要求：**回放功能做成可开关，默认关闭**。
    #   关掉时：← / → / 小键盘6 / 小键盘Enter 四个键只提示、不动手，
    #   同时把插件的滚动缓冲摘掉（ReplaySource.Disable）—— 那份内存真的还回去。
    #   手机提示器 / 自动切观察位**不受影响**，照常工作。
    "replay_enabled": False,

    # ★ 内存模式 —— 决定 obs-replay-source 那几 GB 什么时候占着。
    #   "armed"（默认，省内存）：平时插件是 Disable 的（≈0 占用）；
    #        按 ← 才 Enable 开始攒帧，按 → 把这一段落取走后立刻再 Disable，
    #        内存里**只剩最近裁出来的这一段**。
    #        ⚠️ 代价：← 不能回溯了 —— 片段从你按 ← 那一刻开始（本来就推荐"打之前按 ←"）。
    #   "buffer"（旧行为）：插件一直开着，内存里滚动保留最近 record_max_seconds 秒，
    #        ← 可以标在已经过去的某一刻，但这份内存是**一直**占着的。
    #   两种模式的实测依据（插件源码 replay-source.c / replay.h）见 docs/技术笔记.md §1.14。
    "replay_mode": "armed",
    # armed 模式下：裁完片段是否立刻释放滚动缓冲（False = 留着，下次不用重新 Enable）
    "free_buffer_after_clip": True,
    # 关闭回放功能 / 切换模式时，是否把"已经取出来的那一段"也清掉（彻底还内存）
    "clear_replay_on_disable": True,
    # 启动时做内存体检：按真实画布/帧率/时长把数字算出来，并核对插件设置
    "memory_report": True,
    # ★ 内存护栏（2026-10-08 加，默认开）：按本机内存 + 真实画布算一遍，
    #   装不下就**自动把单段素材上限降下来**，连 2 秒都装不下就直接拒绝开回放。
    #   为什么必须有：32 GB 机器 + 4K60 画布 + 10 秒 = 两份 ≈39.8 GB →
    #   系统换页到整机失去响应、OBS 被拖死、只能长按电源键。
    #   想自己承担风险就设 false（不推荐）。
    "memory_guard": True,
    # ★ 资源哨兵（2026-10-08 加，默认开）：引擎运行期间每 ~2 秒看一眼可用内存，
    #   低于 low_mem_warn_pct 打警告，低于 low_mem_release_pct 就**主动把回放插件的
    #   滚动缓冲放掉**（先把我们占的几 GB 还回去），免得整机换页卡死、直播跟着卡。
    "resource_watch": True,
    "low_mem_warn_pct": 12.0,
    "low_mem_release_pct": 6.0,
    # 体检发现 Maximum replays > 1 / Capture internal frames = 开 时，自动写回正确值
    "memory_autofix": True,
    # ★ 提前多久切回直播场景。现在默认 **0**：改由插件在片子结束那一帧触发切回
    #   （见 preflight 里写的 next_scene）。只有你把 next_scene 清空、
    #   想让引擎自己掐时间时才需要设成 0.9 之类。
    "return_before_end": 0.0,
    # 手动模式默认不抢控制权：回放期间回合开打也不打断，导播自己按 Enter 收。
    # 自动模式下会自动设为 True。想强制让位就写 true。
    "interrupt_on_live": None,

    # --- 回放素材从哪来 ---
    # ★ 2026-10-06 用户决定：**没有自动取样**。素材只由 ← / → 手动框选产生
    #   （← 标入点；→ 标出点，并且**当场**把"入点 → 现在"这一段从滚动缓冲里裁出来）。
    #   所以这里只剩一个写给插件用的 Load Delay。
    "retrieve_delay_ms": 0,           # 同时写入插件的 "Load Delay"（一般保持 0）

    # --- ★ 包装转场（stinger）不许吃掉回放片头 ---
    # 你的 OBS 当前转场是「转场」= obs_stinger_transition，素材
    #   J:/比赛包装/01_Logo_transition_long.mov
    # 实测总长 **3.35 秒**（纯读文件头解析 mvhd 得来，不用装任何库）。
    # 切到回放场景的那一刻，stinger 就一直盖在节目画面上（1.0s 才真正切场景，
    # 3.35s 才放完），所以回放的**前 3.35 秒观众根本看不到** ——
    # 用户 2026-10-06 反馈："转场会覆盖掉前面一部分"。
    # 现在的做法：切场景后立刻把回放定格在第一帧，等这 3.35 秒包装放完再开播，
    # 片头一帧不丢（切场景→暂停的 RPC 大约 10~30ms，最多差 1~2 帧）。
    #   null  = 自动（读 OBS 当前转场；不是 stinger 就完全不等待）
    #   数字  = 强制等这么多秒（0 = 不等，回到旧行为）
    "replay_wrap_seconds": None,
    "wrap_pause_max_ms": 250,         # 暂停调用超过这么多 ms 就打警告（片头可能跑掉一点）

    # --- ★ 小键盘 6：保留本场回放片段（把素材另存成视频文件）---
    # 按一下 = 让 Replay Source 把"当前那段素材"写成一个文件，事后能剪出去发。
    # 存出来的是**入点之后**的内容（插件 replay_save() 会照旧应用 trim_front），
    # 和你在回放里看到的一致；没有裁过素材时按下什么都不会发生（插件自己会拦）。
    #   save_dir 留空 = 存到 OBS 的录制目录（GetRecordDirectory，你现在是 E:/Paris2024）
    "save_replay_key": True,
    "save_dir": "",
    "save_file_format": "回放_%CCYY-%MM-%DD_%hh.%mm.%ss",

    # ★ 存出来的文件有多大？（2026-10-07 加，用户问"为什么视频这么大"）
    #   插件只有两条编码通路（源码 replay-source.c:777-806，逐行核对）：
    #     非无损（默认）：.flv，H.264 **CRF 23 / preset veryfast** + AAC；
    #     无损（Lossless 开着）：.avi，**utvideo 无损** + pcm —— 体积是前者的几十倍
    #                            （4K60 十秒 1~2.5 GB）。
    #   引擎每次保存后会报「体积 + 实测码率」，并自动把 Lossless 纠回关。
    #   想进一步压小，把下面这个改成 remux 或 reencode（需要装了 ffmpeg）：
    #     "off"      = 什么都不做（默认）
    #     "remux"    = 只换容器 .flv → .mp4（-c copy，零损失、秒级完成）
    #     "reencode" = 用 libx264 CRF save_postprocess_crf 重编码压小（慢几十秒）
    "save_postprocess": "off",
    "save_postprocess_crf": 26,
    # reencode 之后是否保留原来的 .flv（默认删掉，只留压小的那份）
    "save_keep_original": False,

    # --- ★ 每次开播前自动往 CS2 控制台输入的指令（2026-10-07 加）---
    # 用户在设置窗口里填几条，引擎开播时替你"打开控制台 → 逐条敲进去 → 关掉"。
    #   "cs_console_commands": ["sv_cheats 1", "mp_freezetime 5"]
    # 机制和自动切观察位同一套：SendInput + 扫描码逐字符注入（不是注入游戏进程）。
    # 安全阀：默认**只有 CS2 在前台**才发键（否则会打进 OBS / 浏览器）。
    "cs_console_commands": [],          # 一行一条；空行和 // 开头的行会被忽略
    "cs_console_key": "`",              # 打开控制台的键（可改 f10 / backtick / numpad_enter…）
    "cs_console_trigger": "start",      # start=引擎起来后等到 CS2 在前台发一次（默认）
                                        # round1=每场第 1 个冻结时间发一次
                                        # manual=只手动（设置窗口按钮 / /control/console）
    "cs_console_delay_ms": 400,         # 打开控制台后等多久再开始打（控制台是异步渲染的）
    "cs_console_gap_ms": 120,           # 两条指令之间
    "cs_console_close": True,           # 发完自动把控制台关掉
    "cs_console_require_focus": True,   # 必须 CS2 在前台（强烈建议保持 True）
    "cs_console_timeout_s": 900,        # start 模式下最多等 CS2 到前台多少秒

    # ★ 回放后端（2026-10-08 加，用户要求"画质不损失把内存降下来"）：
    #   "plugin"（默认）= Exeldro obs-replay-source 插件：内存里放**未压缩帧**（BGRA 4 字节/像素）
    #                    → 1080p60 十秒两份 ≈ 9.95 GB；好处是能瞬间定格/任意入点真跳/倒放。
    #   "obs"          = **OBS 自带 Replay Buffer**：内存里放**编码后**的数据（几十~几百 MB），
    #                    按 → 时保存最近 N 秒的文件，再用 ffmpeg 按 (←,→) 做 **-c copy 零损失裁切**。
    #                    需要：OBS 里启用 Replay Buffer（时长 ≥ record_max_seconds）、
    #                    回放场景里有一个媒体源、以及本机有 ffmpeg。
    #                    ⚠️ 必须在真机上验证（这台开发机没装 OBS）。
    "replay_backend": "plugin",
    # obs 后端用：回放场景里那个**媒体源**（ffmpeg_source）的名字；缺了会自动创建
    "replay_media_item": "回放媒体源",
    # obs 后端的裁切方式：
    #   "copy"  = `-c copy`（**零损失、秒级**），但会**对齐到关键帧**（NVENC 默认 2 秒一个 I 帧，
    #             想让入点更准就把 OBS 的关键帧间隔设成 1 秒）
    #   "exact" = 对裁出来的那一小段做一次极短重编码（libx264 CRF trim_reencode_crf），
    #             入点精确到帧，多一代编码（CRF 18 肉眼无感）
    "trim_mode": "copy",
    "trim_reencode_crf": 18,
    # 按 SaveReplayBuffer 之后最多等多久（毫秒）让 OBS 把文件落盘
    "obs_buffer_wait_ms": 8000,
    # 裁完片段后要不要删掉 OBS 那份原始缓冲文件（OBS 每存一次就新写一个文件，
    # 不删的话磁盘会越攒越满：真机实测一次约 30 MB）。false = 留着原始文件。
    "obs_delete_buffer_after_trim": True,
    # ffmpeg 的路径（留空 = 自动找：PATH / 程序目录旁边 / winget / scoop / chocolatey …）
    "ffmpeg_exe": "",

    # --- 和 Astra 的协作（重要）---    # Astra 自己会按比赛阶段自动切场景（BP/直播/中场/图结束…）。为了不互相打架：
    #   require_live_scene : 只有当直播场景正好是 live_scene 时才插回放。
    #                        Astra 把画面切到"中场休息/数据看板"等场景时，引擎自动让位。
    #   lock_on_manual_scene : 是否把"任何非引擎发起的场景切换"都当成人工接管并锁定。
    #                        ⚠️ 用 Astra 的话必须保持 false，否则 Astra 每次阶段切换
    #                        都会把引擎永久锁死。人工接管请用 /control/lock。
    "require_live_scene": True,
    "lock_on_manual_scene": False,
    # ★ 人工接管的锁有有效期：一旦锁上，过了这么久就自动交还给引擎。
    #   为什么必须有：实测踩过 —— 回放中 Astra 按阶段把场景切到「数据看板」，
    #   引擎把它当成"人工接管"锁死，**整场比赛再没解锁**，
    #   于是后面每个回合你按 Enter 都只能播到很早以前那一段素材。
    #   0 = 永不过期（不建议）。
    "manual_lock_seconds": 45.0,

    # --- 其它 ---
    "dry_run": False,
    "verbose": True,
}

# 戏剧性评分权重（总分 100）
SCORE = {
    "kills_3plus": 45,   # 有人拿了 3 杀及以上
    "kills_2": 25,       # 有人拿了 2 杀
    "kills_1": 8,        # 有击杀
    "ace": 30,           # 额外：4 杀以上再加
    "sniper": 15,        # 击杀时持有狙击枪
    "clutch_won": 25,    # 1vX 残局获胜
    "defuse": 20,        # 拆包
    "plant": 12,         # 下包
    "last_man": 10,      # 最后一人存活到最后
}


# ---------------------------------------------------------------------------
# 配置读写（图形界面 / --bind / 手机页面改设置都走这里，保证只有一套规则）
# ---------------------------------------------------------------------------

CONFIG_PATH = None      # 本次运行实际读的那个 config.json（main() 里填）


def load_config(path=None):
    """读配置：DEFAULTS ← config.json ← CLI 覆盖。

    返回 (cfg, 实际使用的路径)。路径不存在就返回 (DEFAULTS 副本, 那个路径)，
    不会报错 —— 全新电脑上双击 exe 就是这个状态，接着会去拉首次设置向导。
    """
    cfg = dict(DEFAULTS)
    cfg_path = path or CONFIG_PATH
    if not cfg_path:
        cand = os.path.join(base_dir(), "config.json")
        if os.path.exists(cand):
            cfg_path = cand
    if cfg_path and os.path.exists(cfg_path):
        # ★ 用 utf-8-sig 读：记事本 / PowerShell 存出来的 config.json 可能带 BOM，
        #   带 BOM 的文件用纯 utf-8 读会直接 JSONDecodeError，整个引擎起不来。
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg, cfg_path


def save_config(cfg, path=None):
    """把配置写回 config.json（UTF-8、indent=2，和首次设置向导一致）。

    下划线开头的内部键（`_config_path` 之类）不落盘；写之前先写 .bak，
    免得写坏了连原来的配置都没了。
    """
    global CONFIG_PATH
    cfg_path = path or CONFIG_PATH or os.path.join(base_dir(), "config.json")
    data = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    try:
        if os.path.exists(cfg_path):
            shutil.copyfile(cfg_path, cfg_path + ".bak")
    except Exception:
        pass
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path)
    CONFIG_PATH = cfg_path
    return cfg_path


# ---------------------------------------------------------------------------
# 内存估算：obs-replay-source 到底占多少内存（纯计算，不连 OBS 也能算）
# ---------------------------------------------------------------------------
#
# 依据（插件源码 replay.h:24-38 + replay-source.c:1066-1180，2026-10-07 逐行核对）：
#   * 滚动缓冲在 **replay_filter** 滤镜里，存的是**未压缩**帧（画布宽×高×4 字节 BGRA）；
#   * `Load replay`（ReplaySource.Replay）把滤镜里的帧指针**搬**到已取出的那段
#     （circlebuf_pop_front → new_replay.video_frames），所以滤镜被掏空、从头再攒；
#   * 于是稳态是 **2 份**：正在回填的滚动缓冲 + 已取出的那一段；
#   * `replays`(Maximum replays，1~10，默认 1) 决定"已取出"能留几段 → 每多一段多一份；
#   * `ReplaySource.Disable` 会把滤镜摘掉 → 滚动那一份真的还回去；
#     `ReplaySource.Clear` 清掉所有已取出的段 → 另一份也还回去。
DEFAULT_CANVAS = (1920, 1080, 60.0)     # 拿不到 OBS 画布时的兜底


def estimate_replay_memory(width, height, fps, seconds, copies=2.0):
    """按画布尺寸算回放插件的内存占用。

    ⚠️ 用**十进制 GB**（1 GB = 1e9 字节）而不是 GiB —— 这样和
    `docs/技术笔记.md` / `双机导播方案.md` 里记的实测数字（1080p 两份 ≈ 10 GB、
    4K 两份 ≈ 40 GB、任务管理器里 obs64 私有 12.51 GB）对得上，不会让人以为算法错了。
    """
    width = int(width or DEFAULT_CANVAS[0])
    height = int(height or DEFAULT_CANVAS[1])
    fps = float(fps or DEFAULT_CANVAS[2]) or DEFAULT_CANVAS[2]
    seconds = max(0.0, float(seconds or 0.0))
    copies = max(0.0, float(copies))
    frame_mb = width * height * 4 / 1048576.0          # 每帧多少 MB（这个用 MiB）
    mb_per_sec = frame_mb * fps
    one_copy_gb = width * height * 4 * fps * seconds / 1e9
    steady_gb = one_copy_gb * copies
    hint = (f"{width}x{height}@{fps:g} | 单段 {seconds:.1f}s | {copies:g} 份 "
            f"≈ {steady_gb:.2f} GB")
    if copies >= 1.9:
        hint += "（滚动缓冲 + 已取出那段）"
    elif copies >= 0.9:
        hint += "（只留最近这一段）"
    else:
        hint += "（空闲，不占）"
    return {"frame_mb": round(frame_mb, 2), "mb_per_sec": round(mb_per_sec, 1),
            "one_copy_gb": round(one_copy_gb, 2), "steady_gb": round(steady_gb, 2),
            "copies": copies, "width": width, "height": height, "fps": fps,
            "seconds": seconds, "hint": hint}


def memory_estimate_from_cfg(cfg, width=None, height=None, fps=None):
    """用配置里的时长/模式算内存；拿不到 OBS 画布就用 DEFAULT_CANVAS。"""
    mode = str(cfg.get("replay_mode") or "armed").lower()
    seconds = float(cfg.get("record_max_seconds", 10.0) or 10.0)
    copies = 1.0 if mode == "armed" else 2.0
    d = estimate_replay_memory(width or DEFAULT_CANVAS[0], height or DEFAULT_CANVAS[1],
                               fps or DEFAULT_CANVAS[2], seconds, copies)
    if mode == "armed":
        d["hint"] += "；省内存模式：空闲时 ≈0，按 ← 之后才会涨到这么多"
        d["mode"] = "armed"
    else:
        d["hint"] += "；常驻缓冲模式：这个数字会一直占着"
        d["mode"] = "buffer"
    return d


# ---------------------------------------------------------------------------
# 内存护栏（2026-10-08 加）：别让回放插件把整机拖死
# ---------------------------------------------------------------------------
# 背景（真机取证，见 docs/技术笔记.md §11）：一台 **32 GB** 的机器，OBS 画布是 **4K60**，
# `record_max_seconds=10` → 插件要 `3840×2160×4×60×10 = 19.91 GB` 一份、两份 **39.81 GB**
# —— 比整机内存还大。结果是系统疯狂换页：OBS 自己卡成 "停止与 Windows 交互并已关闭"、
# 整机失去响应，只能长按电源键；事件日志是 `Kernel-Power 41` 且 `BugcheckCode=0`（没有蓝屏）
# + 没有崩溃转储。所以光"默认关掉回放功能"不够 —— 一旦用户打开，仍然会死，
# 必须在**启用回放/启动引擎**时按本机内存算一遍，装不下就自动降时长或直接拒绝。
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def system_memory_gb():
    """(总物理内存 GB, 可用物理内存 GB)。纯 ctypes，不依赖任何库。"""
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return (None, None)
        return (st.ullTotalPhys / 1e9, st.ullAvailPhys / 1e9)
    except Exception:
        return (None, None)


def obs_has_replay_source(obs):
    """OBS 里到底有没有装 obs-replay-source 插件？

    查两个地方（任意一个中就算有）：
      * `GetInputKindList` 里有 `replay_source`（插件注册的源类型）；
      * `GetInputList` 里的某个输入 `inputKind == "replay_source"`
        （★ 2026-10-09 加：真机上 `GetInputKindList` 在个别版本会返回空/失败，
         只看它会把"装了插件"误判成"没装" —— 于是向导自动切到 obs 后端，
         而用户又没装 ffmpeg，回放就整段死了）。
    """
    try:
        kinds = (obs.request("GetInputKindList", {"unversioned": False})
                 or {}).get("inputKinds") or []
        if "replay_source" in kinds:
            return True
    except Exception:
        pass
    try:
        inputs = (obs.request("GetInputList") or {}).get("inputs") or []
        for i in inputs:
            if (i.get("inputKind") or "") == "replay_source":
                return True
    except Exception:
        pass
    return False


def find_ffmpeg(cfg=None):
    """找一个可用的 ffmpeg（obs 后端裁片段用）。返回路径，找不到返回 ""。

    查找顺序：配置里的 `ffmpeg_exe` → PATH → **程序目录旁边** → 常见安装位置。

    ★ 为什么不能只认 PATH（2026-10-08 真机踩到）：`winget install Gyan.FFmpeg` 装完
      PATH 往往要新开一个终端才生效；如果引擎是从旧终端/双击启动的，`shutil.which()`
      找不到，obs 后端就会**直接拒绝启用**——用户明明装好了却被告知"没装 ffmpeg"。
      所以多找几个常见位置，并且支持**把 ffmpeg.exe 直接丢在程序目录旁边**
      （发给别人时最省事：解压出来把 ffmpeg.exe 放进去就行，不用改 PATH）。
    """
    cands = []
    try:
        c = str((cfg or {}).get("ffmpeg_exe") or "").strip()
        if c:
            cands.append(c)
    except Exception:
        pass
    try:
        w = shutil.which("ffmpeg")
        if w:
            cands.append(w)
    except Exception:
        pass
    home = os.path.expanduser("~")
    base = base_dir()
    cands += [
        os.path.join(base, "ffmpeg.exe"),
        os.path.join(base, "bin", "ffmpeg.exe"),
        os.path.join(base, "tools", "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(base, "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", "") or "", "Microsoft", "WinGet",
                     "Links", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramFiles", "") or "", "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramData", "") or "", "chocolatey", "bin",
                     "ffmpeg.exe"),
        os.path.join(home, "scoop", "shims", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except Exception:
            continue
    return ""


def canvas_from_obs_config():
    """从 OBS 自己的配置文件里读"基础(画布)分辨率 + FPS"（纯读文件，不用连 OBS）。

    为什么需要：插件缓冲是按**画布**算的（`画布宽×高×4×帧率×秒数`），而图形界面
    在引擎启动前拿不到 OBS 的画布 —— 但 `%APPDATA%\\obs-studio\\basic\\profiles\\*\\basic.ini`
    里就写着 `BaseCX/BaseCY/FPSCommon`。用它才能在设置窗口里**提前**给出真实估算，
    而不是傻乎乎按 1080p 算出一个偏小的数字（4K 时低估 4 倍，正是死机的根源）。
    读不到就返回 None，调用方退回 DEFAULT_CANVAS。
    """
    try:
        prof_dir = os.path.join(os.environ.get("APPDATA", ""),
                                "obs-studio", "basic", "profiles")
        if not os.path.isdir(prof_dir):
            return None
        best = None
        for name in sorted(os.listdir(prof_dir)):
            ini = os.path.join(prof_dir, name, "basic.ini")
            if not os.path.isfile(ini):
                continue
            try:
                with open(ini, encoding="utf-8-sig", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            def num(key, cast=int):
                m = re.search(rf"^{key}=([\d.]+)", txt, re.M)
                if not m:
                    return None
                try:
                    return cast(m.group(1))
                except Exception:
                    return None
            w, h, fps = num("BaseCX"), num("BaseCY"), num("FPSCommon", float)
            if w and h:
                return (w, h, fps if fps else DEFAULT_CANVAS[2])
    except Exception:
        pass
    return None


def obs_active_profile_ini():
    """读**当前正在用的** OBS 配置的 `basic.ini` 全文（读不到返回 ""）。

    为什么不用 `canvas_from_obs_config()` 那种"扫第一个"的笨办法：那个函数只是
    为了在**启动前**猜个画布，多点少点无所谓；而回放缓冲时长（`RecRBTime`）决定
    "能标多长的片段"，必须认准 OBS 里真正激活的那个配置 —— 名字就写在
    `%APPDATA%\\obs-studio\\user.ini` 的 `[Basic] ProfileDir`（老版本 `Profile`）里。
    """
    try:
        base = os.path.join(os.environ.get("APPDATA", ""), "obs-studio")
        name = ""
        user_ini = os.path.join(base, "user.ini")
        if os.path.isfile(user_ini):
            with open(user_ini, encoding="utf-8-sig", errors="replace") as f:
                txt = f.read()
            m = re.search(r"^ProfileDir=(.+)$", txt, re.M) or \
                re.search(r"^Profile=(.+)$", txt, re.M)
            if m:
                name = m.group(1).strip()
        if name:
            ini = os.path.join(base, "basic", "profiles", name, "basic.ini")
            if os.path.isfile(ini):
                with open(ini, encoding="utf-8-sig", errors="replace") as f:
                    return f.read()
        # 退回"扫第一个有 [SimpleOutput]/[AdvOut] 的配置"
        prof_dir = os.path.join(base, "basic", "profiles")
        for n in sorted(os.listdir(prof_dir)) if os.path.isdir(prof_dir) else []:
            ini = os.path.join(prof_dir, n, "basic.ini")
            if os.path.isfile(ini):
                with open(ini, encoding="utf-8-sig", errors="replace") as f:
                    txt = f.read()
                if "RecRBTime=" in txt:
                    return txt
    except Exception:
        pass
    return ""


def obs_replay_buffer_info():
    """读 OBS 自己的"回放缓冲"设置：`{"enabled": bool, "seconds": float|None}`。

    ★ 真机教训（2026-10-08，OBS 32.x）：obs-websocket 的
      `GetOutputSettings("ReplayBuffer")` 在真机上**返回 `{}`**（什么设置都读不到），
      光靠它没法告诉用户"你最多能标多长"。而同一份设置就明明白白写在
      `basic.ini` 里：简单输出模式在 `[SimpleOutput]`、高级输出模式在 `[AdvOut]`，
      键名都是 `RecRB`（开关）和 `RecRBTime`（秒）。哪个段生效由 `[Output] Mode=` 决定。
    """
    txt = obs_active_profile_ini()
    if not txt:
        return {"enabled": None, "seconds": None}
    # 按段切出来（ini 里同名键在两个段都可能有，必须认段）
    sections, cur = {}, ""
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
            sections.setdefault(cur, {})
        elif "=" in s and cur:
            k, v = s.split("=", 1)
            sections[cur][k.strip()] = v.strip()
    mode = (sections.get("Output", {}).get("Mode") or "Simple").lower()
    sec = sections.get("AdvOut" if mode.startswith("adv") else "SimpleOutput", {})
    enabled = None
    if "RecRB" in sec:
        enabled = sec["RecRB"].lower() in ("true", "1", "yes")
    seconds = None
    try:
        if "RecRBTime" in sec:
            seconds = float(sec["RecRBTime"])
    except Exception:
        seconds = None
    if seconds is None:                      # 段里没有就全局找一个兜底
        m = re.search(r"^RecRBTime=([\d.]+)", txt, re.M)
        if m:
            seconds = float(m.group(1))
    return {"enabled": enabled, "seconds": seconds}


def disk_free_gb(path):
    """某个路径所在盘还剩多少 GB（读不到返回 None）。"""
    try:
        if not path:
            return None
        drive = os.path.splitdrive(os.path.abspath(path))[0] or "C:"
        return shutil.disk_usage(drive + os.sep).free / 1e9
    except Exception:
        return None


def memory_budget_gb(total_gb, avail_gb):
    """回放缓冲允许吃掉多少内存：总内存的 30%，且不超过当前可用内存的 60%。"""
    if not total_gb:
        return None
    cap = float(total_gb) * 0.30
    if avail_gb:
        cap = min(cap, max(0.5, float(avail_gb) * 0.60))
    return cap


def translate_obs_time_format(fmt):
    """把 OBS 的文件名时间格式翻成 Python `strftime` 格式。

    插件里 `save_file_format` 用的是 OBS 风格的 `回放_%CCYY-%MM-%DD_%hh.%mm.%ss`，
    而 `obs` 后端是我们自己用 Python 命名文件，所以要翻译一遍（顺序很重要：
    先长后短，否则 `%CCYY` 会被 `%C` 之类误伤）。
    """
    pairs = (("%CCYY", "%Y"), ("%YY", "%y"), ("%MM", "%m"), ("%DD", "%d"),
             ("%hh", "%H"), ("%mm", "%M"), ("%ss", "%S"))
    out = str(fmt or "")
    for a, b in pairs:
        out = out.replace(a, b)
    return out


def trim_plan(file_seconds, elapsed, min_seconds=0.1):
    """算"从保存下来的缓冲文件里裁哪一段"。

    `obs` 后端拿到的文件是 OBS 回放缓冲保存出来的**最近 file_seconds 秒**，
    文件尾巴 ≈ 按 `→` 的那一刻；我们要的是"入点（按 ← 的那一刻）→ 现在"。

    返回 `(start, length, rolled)`：
      * `start`  —— 从文件开头跳过多少秒
      * `length` —— 要多少秒
      * `rolled` —— True 表示入点已经滚出缓冲（只能给整段）
    """
    try:
        fsec = max(0.0, float(file_seconds or 0.0))
        el = max(0.0, float(elapsed or 0.0))
    except Exception:
        return (0.0, 0.0, False)
    if el <= 0:
        return (0.0, fsec, False)
    if el > fsec + 0.2:                 # 入点被滚掉了
        return (0.0, fsec, True)
    start = max(0.0, fsec - el)
    length = min(el, fsec)
    if length < min_seconds:
        length = min(fsec, min_seconds)
    return (round(start, 3), round(length, 3), False)


def resource_pressure(avail_gb, total_gb, warn_pct=12.0, release_pct=6.0):
    """按"可用内存占比"判断当前压力：`"ok"` / `"warn"` / `"release"`。

    2026-10-08 加：用户报"正在直播时整机卡死、直播间也卡死" —— 这类"全都卡住"
    基本就是系统级资源耗尽（换页风暴），而**受害者是整台机器**，不只是我们。
    所以引擎要当"哨兵"：
      * `warn`    → 打一条带数字的警告（谁在吃内存、建议怎么降）；
      * `release` → **主动把回放插件的滚动缓冲放掉**（`ReplaySource.Disable`），
                    先把我们自己占的那几 GB 还回去，别等系统崩。
    """
    if not total_gb or total_gb <= 0 or avail_gb is None:
        return "ok"
    pct = float(avail_gb) / float(total_gb) * 100.0
    if pct <= float(release_pct):
        return "release"
    if pct <= float(warn_pct):
        return "warn"
    return "ok"


def plan_replay_seconds(width, height, fps, want_seconds, total_gb, avail_gb,
                        copies=2.0, min_seconds=2.0, step=0.5):
    """按内存给一个**安全时长**。

    返回 `(safe_seconds, note)`：
      * `safe_seconds == want_seconds` → 没问题；
      * 更小 → 已降到这个值（note 是给人看的说明）；
      * `None` → 这台机器**跑不了回放**（连 min_seconds 都装不下）。
    `copies` 用 2：裁完片段那一瞬间"滚动缓冲 + 已取出那段"是同时在的，要按峰值算。
    """
    budget = memory_budget_gb(total_gb, avail_gb)
    if budget is None:
        return (want_seconds, "")      # 读不到内存就别拦（不能把功能拦死）

    def need(sec):
        return (float(width) * float(height) * 4 * float(fps) * float(sec)) / 1e9 * copies

    want_need = need(want_seconds)
    if want_need <= budget:
        return (want_seconds, "")
    s = float(want_seconds)
    while s > min_seconds and need(s) > budget:
        s = round(s - step, 2)
    if need(s) > budget:
        return (None, f"连 {min_seconds:.0f} 秒都要 {need(min_seconds):.2f} GB，"
                      f"超过安全预算 {budget:.2f} GB")
    return (s, f"{float(want_seconds):.1f} 秒需要 {want_need:.2f} GB，"
               f"超过安全预算 {budget:.2f} GB")


LOG_FILE = None
_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            with _log_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def logv(cfg, msg: str) -> None:
    if cfg.get("verbose"):
        log(msg)


# ============================================================================
# 1. GSI 接收器
# ============================================================================

class GsiHandler(http.server.BaseHTTPRequestHandler):
    """接收 CS2 推来的 GSI POST，同时提供几个控制端点。"""

    app = None  # 由外部注入

    def log_message(self, *args):  # 静音默认访问日志
        pass

    # ---------- 控制端点 ----------
    def do_GET(self):
        app = self.app
        path = urllib.parse.urlparse(self.path).path
        if path == "/control/status":
            body = json.dumps(app.status(), ensure_ascii=False, indent=2).encode("utf-8")
            code = 200
        elif path == "/control/unlock":
            app.unlock("http")
            body = b'{"ok":true,"locked":false}'
            code = 200
        elif path == "/control/lock":
            app.lock("http")
            body = b'{"ok":true,"locked":true}'
            code = 200
        elif path == "/control/replay":
            ok = app.manual_replay()
            body = json.dumps({"ok": ok}).encode("utf-8")
            code = 200 if ok else 409
        elif path in ("/control/mark_in", "/control/record_start"):
            body = json.dumps({"ok": app.mark_in()}).encode("utf-8")
            code = 200
        elif path in ("/control/mark_out", "/control/record_stop"):
            body = json.dumps({"ok": app.mark_out()}).encode("utf-8")
            code = 200
        elif path == "/control/console":
            # ★ 2026-10-07：手动发一次 CS 控制台指令（设置窗口的「现在发送一次」也走这里）
            body = json.dumps({"ok": app.send_console("HTTP 手动")}).encode("utf-8")
            code = 200
        elif path == "/control/save":
            # ★ 2026-10-08：留档当前片段（等于按「保留片段」键）—— 手机上/浏览器里
            #   也能留档，不用非得按键盘。
            body = json.dumps({"ok": app.save_clip()}).encode("utf-8")
            code = 200
        else:
            body = (b'{"error":"use /control/status | /lock | /unlock | /replay'
                    b' | /mark_in | /mark_out | /console | /save"}')
            code = 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        app = self.app
        path = urllib.parse.urlparse(self.path).path
        if path != app.cfg["gsi_path"]:
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            # 先回包，让 CS2 尽快结束这次请求（GSI 对延迟敏感）
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            if raw:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                app.on_gsi(payload)
        except Exception:
            log("!! 处理 GSI 数据包出错:\n" + traceback.format_exc())


class GsiServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一端口（会静默抢包），
    # 所以这里必须关掉，让重复启动直接报错，而不是"莫名连上旧实例"。
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 手机端断开 SSE / 浏览器换页时会抛 ConnectionResetError，
        # 那属于正常现象，不用打一堆堆栈吓人。
        if sys.exc_info()[0] is ConnectionResetError:
            return
        super().handle_error(request, client_address)


def port_in_use(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


# ============================================================================
# 2. 比赛状态跟踪 / 回合结束检测 / 戏剧性评分
# ============================================================================

class RoundStats:
    """一个回合的统计。"""

    def __init__(self, round_no: int):
        self.round_no = round_no
        self.final_kills = {}        # steamid -> 本回合击杀数（锁存到的最大值）
        self.final_dmg = {}          # steamid -> 本回合伤害
        self.names = {}              # steamid -> 名字
        self.weapons_at_kill = {}    # steamid -> set(武器名)，击杀发生那一刻手里有什么
        self.kills_total = 0
        self.had_plant = False
        self.had_defuse = False
        self.lowest_alive = {}       # team -> 本回合出现过的最低存活人数
        self.win_team = None
        self.phases_seen = set()

    # --- 评分 ---
    def score(self) -> tuple[int, list[str]]:
        s = 0
        reasons = []
        mx = max(self.final_kills.values()) if self.final_kills else 0

        if mx >= 3:
            s += SCORE["kills_3plus"]; reasons.append(f"{mx}杀")
        elif mx == 2:
            s += SCORE["kills_2"]; reasons.append("2杀")
        elif mx == 1:
            s += SCORE["kills_1"]; reasons.append("1杀")

        if mx >= 4:
            s += SCORE["ace"]; reasons.append("多杀秀")

        if any(("weapon_awp" in w or "weapon_ssg08" in w) for w in self.weapons_at_kill.values()):
            s += SCORE["sniper"]; reasons.append("狙击")

        # 残局：获胜方曾经只剩 1 人
        if self.win_team and self.lowest_alive.get(self.win_team, 5) == 1:
            s += SCORE["clutch_won"]; reasons.append("残局翻盘")

        if self.had_defuse:
            s += SCORE["defuse"]; reasons.append("拆包")
        elif self.had_plant:
            s += SCORE["plant"]; reasons.append("下包")

        return s, reasons

    def summary(self) -> str:
        top = sorted(self.final_kills.items(), key=lambda kv: -kv[1])[:3]
        names = ", ".join(f"{self.names.get(k, k[:8])}×{v}" for k, v in top if v)
        return names or "无击杀"


class MatchState:
    """
    跟踪比赛状态，检测"回合结束"。

    ⚠️ 这里的关键算法来自 Astra Director 的生产代码注释（它踩过的坑）：
      * GSI 在**回合开始时**会把 `state.round_kills` 清零，所以必须**每个数据包都锁存一次**，
        回合号递增前的最后一包才是刚结束回合的最终击杀数。
      * `phase_countdowns` 和 `map.round` 来自**不同的子系统**，到达顺序不保证
        （freezetime 先到 / 回合号先到 都可能），所以必须用**水平触发**而不是边沿触发，
        否则会出现"永远不触发"。
    """

    def __init__(self, on_round_end, on_kill=None):
        self.on_round_end = on_round_end
        self.on_kill_cb = on_kill
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.live_round = None
        self.live = None
        self.snap = None          # 刚结束回合的快照
        self.fired_rounds = set()
        self.phase = "unknown"
        self.phase_ends_in = None
        self.map_name = None
        self.last_packet_at = 0.0
        self.dead_windows = []        # 观察到的"回合结束→下一回合开打"窗口时长
        self._pcache = {}             # steamid -> {state, match_stats, weapons, team, name}
        self._pseen = {}              # steamid -> 最后一次出现的时刻
        self._dead_started_at = None
        self._last_emit_at = 0.0      # 上次"回合结束"上报的时刻（防重复触发）

    # ------------------------------------------------------------------
    def on_gsi(self, g: dict):
        with self.lock:
            self._on_gsi(g)

    def _on_gsi(self, g: dict):
        now = time.time()
        self.last_packet_at = now

        # --- 地图变化 / 回合号回退 → 整个重置 ---
        m = (g.get("map") or {}).get("name")
        if m and m != self.map_name:
            logv(self.cfg_ref, f"地图变化: {self.map_name} -> {m}，状态重置")
            self.map_name = m
            self.live_round = None
            self.live = None
            self.snap = None
            self.fired_rounds.clear()
            self._pcache.clear()
            self._pseen.clear()

        round_no = (g.get("map") or {}).get("round")
        if not isinstance(round_no, int):
            return
        if self.live_round is not None and round_no < self.live_round:
            logv(self.cfg_ref, "回合号回退（比赛重启？），状态重置")
            self.live_round = None
            self.live = None
            self.snap = None
            self.fired_rounds.clear()
            self._pcache.clear()
            self._pseen.clear()

        # --- 合并 allplayers ---
        # ⚠️ CS2 会发"部分包"：某些包里 allplayers[sid] 只有 id/team，
        #    没有 state / match_stats / weapons。必须像 Astra 的 mergeGsi 那样
        #    把上一包的值保留下来，否则会把活着的选手误判成死亡（假残局）。
        for sid, p in (g.get("allplayers") or {}).items():
            if not isinstance(p, dict):
                continue
            c = self._pcache.setdefault(sid, {})
            if p.get("state"):
                c["state"] = p["state"]
            if p.get("match_stats"):
                c["match_stats"] = p["match_stats"]
            if p.get("weapons"):
                c["weapons"] = p["weapons"]
            if p.get("team"):
                c["team"] = p["team"]
            if p.get("name"):
                c["name"] = p["name"]
            self._pseen[sid] = now
        # 清掉长时间没出现的选手（断线等）
        for sid in [s for s, t in self._pseen.items() if now - t > 10]:
            self._pcache.pop(sid, None)
            self._pseen.pop(sid, None)

        # --- 回合号递增：上一回合数据定型 ---
        if self.live_round is None:
            self.live_round = round_no
        if round_no != self.live_round:
            if round_no > self.live_round:
                self.snap = self.live
            self.live_round = round_no
            self.live = RoundStats(round_no)
        if self.live is None:
            self.live = RoundStats(round_no)

        # --- 每个包都锁存（关键：state.round_kills 会在回合开始时被清零）---
        for sid, c in self._pcache.items():
            st = c.get("state") or {}
            if "round_kills" in st:
                k = int(st.get("round_kills") or 0)
                prev = self.live.final_kills.get(sid, 0)
                if k > prev:
                    ws = {str(w.get("name")) for w in (c.get("weapons") or {}).values()
                          if isinstance(w, dict) and w.get("name")}
                    self.live.weapons_at_kill[sid] = ws
                    self.live.final_kills[sid] = k
                    self.live.kills_total += (k - prev)
                    if self.on_kill_cb:
                        try:
                            self.on_kill_cb(sid, k, round_no, c.get("name") or sid)
                        except Exception:
                            log("!! on_kill 回调出错:\n" + traceback.format_exc())
            if "round_totaldmg" in st:
                self.live.final_dmg[sid] = max(self.live.final_dmg.get(sid, 0),
                                               int(st.get("round_totaldmg") or 0))
            if c.get("name"):
                self.live.names[sid] = c["name"]

        # --- 存活人数（只统计"健康值已知"的选手，避免部分包造成误判）---
        alive = {"CT": 0, "T": 0}
        for sid, c in self._pcache.items():
            st = c.get("state") or {}
            if "health" not in st:
                continue
            team = c.get("team")
            if team in alive and int(st.get("health") or 0) > 0:
                alive[team] += 1
        for t, n in alive.items():
            if n > 0:
                lo = self.live.lowest_alive.get(t)
                self.live.lowest_alive[t] = n if lo is None else min(lo, n)

        # --- 相位：以 phase_countdowns.phase 为准（Astra 的生产做法），round.phase 兜底 ---
        pc = g.get("phase_countdowns") or {}
        rnd = g.get("round") or {}
        phase = pc.get("phase") or rnd.get("phase")
        if phase:
            self.live.phases_seen.add(phase)
            if phase == "bomb" or (rnd.get("bomb") and phase in ("live", "defuse")):
                self.live.had_plant = True
            if phase == "defuse":
                self.live.had_defuse = True
            if self.phase != phase:
                self._on_phase_change(self.phase, phase, round_no)
            self.phase = phase
        self.phase_ends_in = pc.get("phase_ends_in")
        if rnd.get("win_team"):
            self.live.win_team = rnd["win_team"]

        # --- 触发检查（水平触发 + 防重）---
        self._check_trigger(round_no, phase)

    def _on_phase_change(self, old, new, round_no):
        logv(self.cfg_ref, f"  phase: {old} -> {new} (round {round_no})")
        # "死时间窗口" = 从回合结束(over) 到 下一回合真正开打(live)
        # 这个窗口才是回放能用的全部时间（含 over + freezetime + 买枪）
        if new == "over" and self._dead_started_at is None:
            self._dead_started_at = time.time()
        if new == "live" and self._dead_started_at is not None:
            dur = time.time() - self._dead_started_at
            self._dead_started_at = None
            self.dead_windows.append(dur)
            avg = sum(self.dead_windows) / len(self.dead_windows)
            log(f"本回合结束→开打的窗口 {dur:.1f}s（最近 {len(self.dead_windows)} 次平均 {avg:.1f}s）")

    def dead_window_avg(self):
        if not self.dead_windows:
            return None
        return sum(self.dead_windows) / len(self.dead_windows)

    def _check_trigger(self, round_no, phase):
        """
        水平触发：条件成立就报，用 fired_rounds 防重。

        ⚠️ 这里踩过一个坑，务必别改回去：
        兜底路径检查的键必须和写入的键**是同一个回合号**。
        早期版本写成 `fired = round_no in fired_rounds`，但兜底写入的是
        `snap.round_no`（上一个回合）。结果冻结时间里**每个 GSI 数据包**
        （约 10 Hz × 20 秒 = 200 次）都会重复触发同一个回合，
        自动模式下会一回合放好几次回放，而且每次都在"此刻"重新快照，
        抓到的画面越来越偏向回合收尾。实测症状：379 条回合结束只对应 6 个真实回合。
        """
        if self.snap is not None and self.snap.round_no not in self.fired_rounds:
            sr = self.snap.round_no
            # 兜底：已经进入下一回合的 freezetime，但 over 没抓到（丢包等）
            if phase == "freezetime" and round_no == sr + 1:
                self._mark_fired(sr, "freezetime-fallback")
                return
        # 主路径：phase 变成 over —— 回合刚结束，win_team 已知
        if phase == "over" and round_no not in self.fired_rounds:
            self._mark_fired(round_no, "over")
            return

    def _mark_fired(self, round_no, how):
        self.fired_rounds.add(round_no)
        # 双保险：同一时间窗内绝不放行第二次（防任何未知的重复触发）
        now = time.time()
        if now - self._last_emit_at < 2.0:
            logv(self.cfg_ref, f"  （回合 {round_no} 的重复触发被时间窗拦掉）")
            return
        self._last_emit_at = now
        self._emit_round_end(round_no, how)

    def _emit_round_end(self, round_no, how):
        stats = self.live if (self.live and self.live.round_no == round_no) else self.snap
        if stats is None:
            return
        sc, reasons = stats.score()
        log(f"▶ 回合 {round_no} 结束 [{how}]  比分方: {stats.win_team}  "
            f"击杀: {stats.summary()}  评分 {sc} ({', '.join(reasons) or '平淡'})")
        try:
            self.on_round_end(round_no, stats, sc, reasons)
        except Exception:
            log("!! on_round_end 出错:\n" + traceback.format_exc())


# ============================================================================
# 3. obs-websocket v5 客户端
# ============================================================================

# obs-websocket 的媒体控播动作名（TriggerMediaInputAction 的 mediaAction）。
# ★ 为什么非得用媒体接口而不是热键（2026-10-06 真机实测的坑）：
#   插件的 `ReplaySource.Replay` 是 "Load replay"（replay-source.c:1233 replay_hotkey
#   → replay_retrieve），按下去会**重新从滚动缓冲取一段**；拿它当"继续播放"用，
#   素材尾巴就变成"按下的那一刻"，播出来根本不是用户框的那一段
#   （用户原话："你那截的啥？根本就不是我标记的那一段"）。
#   媒体接口 PLAY → 插件 replay_play_pause(data, false)（:722-757）才是
#   "从 pause_timestamp 处继续"，既不重新取素材、也不动 start_delay。
MEDIA_ACTION_PLAY = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"
MEDIA_ACTION_PAUSE = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE"
MEDIA_ACTION_RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"


class ObsClient:
    """最小实现的 obs-websocket v5 客户端（op 0/1/2/5/6/7）。"""

    def __init__(self, url, password, on_event=None, timeout=5.0):
        if websocket is None:
            raise RuntimeError("缺少 websocket-client，请先 pip install websocket-client")
        self.url = url
        self.password = password or ""
        self.on_event = on_event
        self._ws = None
        self._reqs = {}
        self._lock = threading.Lock()
        self._running = False
        self._reader = None
        # ★ 事件不能直接在接收线程里处理！
        #   接收线程正在等 request 的响应；如果在事件回调里再发一个 request，
        #   响应要靠同一个线程来收 → 死锁 → 请求超时。
        #   （实测症状：`SetSceneItemEnabled` / `GetSceneList` 超时，
        #     紧接着自动切换也失效。）所以事件走队列 + 独立线程。
        self._events = queue.Queue()
        self._evt_thread = None
        self.identified = False

    # ---------------- 连接 ----------------
    def connect(self, timeout=6.0):
        self._ws = websocket.create_connection(self.url, timeout=timeout)
        self._ws.settimeout(0.5)
        hello = json.loads(self._ws.recv())
        if hello.get("op") != 0:
            raise RuntimeError(f"预期 Hello(op 0)，收到 {hello.get('op')}")
        d = hello.get("d") or {}
        ident = {"rpcVersion": 1, "eventSubscriptions": 5}  # 5 = General | Scenes
        auth = d.get("authentication")
        if auth:
            if not self.password:
                raise RuntimeError("OBS 要求密码，但配置里 obs_password 是空的")
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self._ws.send(json.dumps({"op": 1, "d": ident}))

        # 等 Identified (op 2)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self._ws.recv())
            if msg.get("op") == 2:
                self.identified = True
                break
            if msg.get("op") == 0:
                continue
        if not self.identified:
            raise RuntimeError("obs-websocket 握手超时（密码错了？）")

        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._evt_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._evt_thread.start()
        return True

    def _event_loop(self):
        """在独立线程里跑事件回调，避免和 request/response 抢同一个线程。"""
        while self._running:
            try:
                item = self._events.get(timeout=0.3)
            except queue.Empty:
                continue
            if item is None:
                return
            et, ed = item
            if self.on_event:
                try:
                    self.on_event(et, ed)
                except Exception:
                    log("!! 事件回调出错:\n" + traceback.format_exc())

    def close(self):
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ---------------- 收发 ----------------
    def _read_loop(self):
        while self._running:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                if self._running:
                    log("!! obs-websocket 读循环断开")
                self._running = False
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            op = msg.get("op")
            d = msg.get("d") or {}
            if op == 7:  # RequestResponse
                rid = d.get("requestId")
                with self._lock:
                    slot = self._reqs.pop(rid, None)
                if slot:
                    slot["result"] = d
                    slot["ev"].set()
            elif op == 5:  # Event
                if self.on_event:
                    self._events.put((d.get("eventType"), d.get("eventData") or {}))
            elif op == 6:
                pass

    def request(self, req_type, data=None, timeout=5.0):
        if not self.identified:
            raise RuntimeError("OBS 未连接")
        rid = str(uuid.uuid4())
        slot = {"ev": threading.Event(), "result": None}
        with self._lock:
            self._reqs[rid] = slot
        self._ws.send(json.dumps({
            "op": 6, "d": {"requestType": req_type, "requestId": rid, "requestData": data or {}}
        }))
        if not slot["ev"].wait(timeout):
            with self._lock:
                self._reqs.pop(rid, None)
            raise TimeoutError(f"OBS 请求超时: {req_type}")
        resp = slot["result"] or {}
        st = resp.get("requestStatus") or {}
        if not st.get("result"):
            raise RuntimeError(f"OBS 请求失败 {req_type}: {st.get('comment') or st}")
        return resp.get("responseData") or {}

    # ---------------- 便捷封装 ----------------
    def scene_list(self):
        d = self.request("GetSceneList")
        scenes = [s["sceneName"] for s in (d.get("scenes") or [])]
        return scenes, d.get("currentProgramSceneName")

    def scene_items(self, scene):
        d = self.request("GetSceneItemList", {"sceneName": scene})
        return d.get("sceneItems") or []

    def set_scene(self, scene):
        return self.request("SetCurrentProgramScene", {"sceneName": scene})

    def set_item_enabled(self, scene, item_id, enabled):
        return self.request("SetSceneItemEnabled",
                            {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled})

    def input_settings(self, name):
        d = self.request("GetInputSettings", {"inputName": name})
        return d.get("inputKind"), (d.get("inputSettings") or {})

    def trigger_media_action(self, input_name, action):
        """控播动作（obs-websocket TriggerMediaInputAction）。

        这是"续播 / 定格"唯一正确的通道：走的是插件的 media_play_pause 等回调，
        **不会**像 `ReplaySource.Replay` 那样重新从滚动缓冲取一段素材。
        """
        return self.request("TriggerMediaInputAction",
                            {"inputName": input_name, "mediaAction": action})

    def media_input_status(self, input_name):
        """读媒体源状态（mediaState / mediaDuration / mediaCursor）。"""
        return self.request("GetMediaInputStatus", {"inputName": input_name})

    def video_settings(self):
        """画布/帧率 —— 内存体检要用它算"一帧多少 MB"。"""
        return self.request("GetVideoSettings") or {}

    def input_list(self):
        """所有输入源（用来数 replay_source 实例 —— 每个实例都自己攒一份缓冲）。"""
        return (self.request("GetInputList") or {}).get("inputs") or []

    # ---------------- OBS 自带 Replay Buffer（obs 后端用）----------------
    # API 依据：obs-websocket v5 协议（docs/generated/protocol.json，2026-10-08 核对）
    #   GetReplayBufferStatus → outputActive
    #   StartReplayBuffer / StopReplayBuffer / ToggleReplayBuffer → outputActive
    #   SaveReplayBuffer（无入参）→ 保存路径靠 GetLastReplayBufferReplay.savedReplayPath
    #                              或 ReplayBufferSaved 事件（Outputs 订阅）
    def replay_buffer_active(self):
        try:
            return bool((self.request("GetReplayBufferStatus") or {}).get("outputActive"))
        except Exception:
            return False

    def start_replay_buffer(self):
        return self.request("StartReplayBuffer") or {}

    def stop_replay_buffer(self):
        return self.request("StopReplayBuffer") or {}

    def save_replay_buffer(self):
        return self.request("SaveReplayBuffer") or {}

    def last_replay_buffer_path(self):
        return str((self.request("GetLastReplayBufferReplay") or {}).get("savedReplayPath") or "")

    def output_settings(self, output_name):
        """读输出设置（回放缓冲的时长等）—— 拿不到就返回 {}。"""
        try:
            return (self.request("GetOutputSettings", {"outputName": output_name})
                    or {}).get("outputSettings") or {}
        except Exception:
            return {}

    def set_output_settings(self, output_name, settings):
        return self.request("SetOutputSettings",
                            {"outputName": output_name, "outputSettings": settings}) or {}

    def create_input(self, scene, input_name, input_kind, settings=None):
        return self.request("CreateInput", {"sceneName": scene, "inputName": input_name,
                                           "inputKind": input_kind,
                                           "inputSettings": settings or {},
                                           "sceneItemEnabled": False}) or {}


class NullObsClient:
    """仿真 / 空跑用的假 OBS。"""

    def __init__(self, scenes=None, items=None):
        self.scenes = scenes or ["HUD", "即时回放", "bp"]
        self.current = self.scenes[0]
        self.items = items or {self.scenes[1]: [{"sourceName": "Replay Source", "sceneItemId": 2,
                                                 "sceneItemEnabled": False}]}
        self.calls = []

    def connect(self, timeout=6.0):
        return True

    def close(self):
        pass

    def scene_list(self):
        return list(self.scenes), self.current

    def scene_items(self, scene):
        return self.items.get(scene, [])

    def set_scene(self, scene):
        self.calls.append(("SetCurrentProgramScene", scene))
        log(f"    [OBS] 切场景 -> {scene}")
        self.current = scene
        return {}

    def set_item_enabled(self, scene, item_id, enabled):
        self.calls.append(("SetSceneItemEnabled", scene, item_id, enabled))
        log(f"    [OBS] {scene} 场景项 {item_id} -> {'显示' if enabled else '隐藏'}")
        for it in self.items.get(scene, []):
            if it["sceneItemId"] == item_id:
                it["sceneItemEnabled"] = enabled
        return {}

    def trigger_media_action(self, input_name, action):
        self.calls.append(("TriggerMediaInputAction", input_name, action))
        log(f"    [OBS] 媒体动作 {action} @ {input_name}")
        return {}

    def media_input_status(self, input_name):
        self.calls.append(("GetMediaInputStatus", input_name))
        # 仿真里假装"回放源刚显示、正在播"（真机上 visibility_action=Restart 就是这个状态），
        # 这样定格 / 续播两条通路在仿真里都会被走到。
        return {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaDuration": 10000, "mediaCursor": 0}

    def input_settings(self, name):
        return "replay_source", {"source": "游戏采集", "duration": 5.0, "speed": 0.7,
                                "replays": 1, "internal_frames": False,
                                "start_delay": 0}

    def video_settings(self):
        # 仿真里用一份 1080p60 的画布（内存体检照样能算出数字来）
        return {"baseWidth": 1920, "baseHeight": 1080, "outputWidth": 1920,
                "outputHeight": 1080, "fpsNumerator": 60, "fpsDenominator": 1}

    def input_list(self):
        return [{"inputName": "Replay Source", "inputKind": "replay_source"},
                {"inputName": "回放媒体源", "inputKind": "ffmpeg_source"}]

    # ---------------- 仿真：OBS 自带 Replay Buffer（obs 后端）----------------
    def replay_buffer_active(self):
        self.calls.append(("GetReplayBufferStatus", None))
        return bool(getattr(self, "rb_active", False))

    def start_replay_buffer(self):
        self.calls.append(("StartReplayBuffer", None))
        if not getattr(self, "rb_configured", True):
            raise RuntimeError("OBS 请求失败 StartReplayBuffer: "
                               "Replay buffer is not configured")
        self.rb_active = True
        log("    [OBS] Replay Buffer 已启动（仿真）")
        return {"outputActive": True}

    def stop_replay_buffer(self):
        self.calls.append(("StopReplayBuffer", None))
        self.rb_active = False
        log("    [OBS] Replay Buffer 已停止（仿真）")
        return {"outputActive": False}

    def save_replay_buffer(self):
        self.calls.append(("SaveReplayBuffer", None))
        # 仿真：写一个真文件出来（内容随便），路径按 OBS 的命名风格
        import tempfile
        d = getattr(self, "sim_buffer_dir", None) or tempfile.gettempdir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "Replay 2026-10-08 23-00-00.flv")
        with open(path, "wb") as f:
            f.write(b"\0" * 4096)
        self.last_rb_path = path
        log(f"    [OBS] 已保存回放缓冲（仿真）→ {path}")
        return {}

    def last_replay_buffer_path(self):
        self.calls.append(("GetLastReplayBufferReplay", None))
        return getattr(self, "last_rb_path", "")

    def output_settings(self, output_name):
        self.calls.append(("GetOutputSettings", output_name))
        if output_name == "ReplayBuffer":
            return {"duration": int(self.rb_seconds * 1000) if getattr(self, "rb_seconds", None)
                    else 20000}
        return {}

    def create_input(self, scene, input_name, input_kind, settings=None):
        self.calls.append(("CreateInput", scene, input_name, input_kind))
        log(f"    [OBS] 创建输入 {input_name}({input_kind}) @ {scene}（仿真）")
        return {"sceneItemId": 99}

    def request(self, req_type, data=None, timeout=5.0):
        self.calls.append((req_type, data))
        if req_type == "GetSourceActive":
            return {"videoShowing": True, "videoActive": True}
        if req_type == "GetCurrentSceneTransition":
            # 仿真里当"没有转场要等"处理：transitionDuration=0 → 包装等待 0 秒，
            # 于是仿真时序和以前逐项一致（真机上这个值来自 stinger 素材时长）。
            return {"transitionKind": "obs_stinger_transition",
                    "transitionFixed": True,
                    "transitionDuration": 0,
                    "transitionSettings": {"path": "C:/Temp/sim_stinger.mov"}}
        if req_type == "GetRecordDirectory":
            return {"recordDirectory": "C:/Temp/sim_records"}
        if req_type == "GetVideoSettings":
            return self.video_settings()
        if req_type == "GetInputList":
            return {"inputs": self.input_list()}
        if req_type == "GetInputSettings":
            return self.input_settings((data or {}).get("inputName") or "")[1]
        if req_type == "TriggerHotkeyByName":
            log(f"    [OBS] 触发热键 {data.get('hotkeyName')} @ {data.get('contextName')}")
        return {}


# ============================================================================
# 4. 重放编排器
# ============================================================================

class _SkipPluginSection(Exception):
    """内部信号：当前用的是 obs 后端，跳过"只对插件有意义"的 preflight 段。

    为什么不直接把这些段搬进单独方法：那些段又长又脆（涉及 start_delay / next_scene /
    Enable 这些踩过坑的地方），搬动容易改坏；用一个信号异常在原地跳过，风险最小。
    """


class ObsBufferBackend:
    """回放后端②：**OBS 自带 Replay Buffer**（编码后保存，内存几十~几百 MB）。

    为什么要有它（用户原话："视频画质不损失的情况下能降下来内存占用吗？相机 4k 拍十秒
    也不会几个 G"）：插件后端把**未压缩帧**放内存里（BGRA 4 字节/像素 → 1080p60 十秒
    两份 ≈ 9.95 GB）；OBS 自带的 Replay Buffer 放的是**编码后**的数据（NVENC 几十 Mbps
    → 同样条件几十 MB），代价只是"多一代编码"，而**裁切用 `-c copy` 是零损失**。

    流程：
        按 ←   → StartReplayBuffer（armed 模式；常驻模式在准备阶段就开着）
        按 →   → SaveReplayBuffer → 等文件落盘 → 用 ffmpeg 按 (←,→) 裁成一小段
        播放   → 把裁好的文件喂给回放场景里的**媒体源**，切场景播放（定格/续播逻辑同插件）
        小键盘6 → 把这段文件另存到 save_dir（我们自己的文件名格式）

    依赖：
      * OBS 里必须**启用 Replay Buffer**（设置 → 输出 → 回放缓冲：勾选 + 时长 ≥
        `record_max_seconds` + 编码器/码率给足）；没启用时 `StartReplayBuffer` 会报错，
        这里会把 OBS 的原话打出来并告诉你去哪开。
      * 本机要有 **ffmpeg**（裁剪用）。没有就不让启用这个后端（会自动退回插件后端）。
      * 回放场景里要有一个**媒体源**（`ffmpeg_source`，默认叫「回放媒体源」），缺了会自动建。

    ⚠️ 这套**必须在真机上验证**（开发机没装 OBS）：本项目只保证接口按
    obs-websocket v5 协议写（`GetReplayBufferStatus/Start/Stop/SaveReplayBuffer/
    GetLastReplayBufferReplay/GetOutputSettings/TriggerMediaInputAction`，2026-10-08 核对）。
    """

    name = "obs"

    def __init__(self, director):
        self.d = director
        self.cfg = director.cfg
        self.obs = director.obs
        self.media_item = str(self.cfg.get("replay_media_item") or "回放媒体源")
        self.media_item_id = None
        self.ready = False
        self.clip_path = ""          # 当前这段裁好的文件
        self.clip_seconds = 0.0
        self._prev_clip = ""         # 上一次裁的那份工作副本（下次裁完就删，免得堆磁盘）

    # ---------------- 内存账（编码后，MB 级）----------------
    def memory_mb(self):
        """按码率估内存：`码率(Mbps) × 秒 / 8`，再乘 2（缓冲 + 保存中的一份）。"""
        mbps = float(self.cfg.get("obs_buffer_mbps", 40.0) or 40.0)
        sec = float(self.cfg.get("record_max_seconds", 10.0) or 10.0)
        return mbps * sec / 8.0 * 2.0

    # ---------------- 准备 ----------------
    def prepare(self):
        """preflight：找/建媒体源，检查 ffmpeg 与 Replay Buffer 是否可用。"""
        cfg, obs = self.cfg, self.obs
        # 1) ffmpeg：裁切必需
        ff = find_ffmpeg(cfg)
        if not ff:
            log("   ❌ obs 后端需要 ffmpeg 来裁片段（`-c copy` 零损失），但这台机器上没有。")
            log("      装一个：winget install Gyan.FFmpeg   或到 ffmpeg.org 下 zip 后把 bin 加进 PATH；")
            log("      也可以直接把 ffmpeg.exe 放到本程序目录旁边（最省事，不用改 PATH）。")
            log("      （如果 OBS 里装了 Replay Source 插件，引擎会自动退回插件后端接着用。）")
            return False
        self.ffmpeg = ff
        log(f"   ✅ ffmpeg: {ff}")

        # 2) 回放场景里要有媒体源（缺了就建一个）
        try:
            items = obs.scene_items(cfg["replay_scene"])
        except Exception as e:
            log(f"   ❌ 读回放场景失败：{e}")
            return False
        for it in items:
            if it.get("sourceName") == self.media_item:
                self.media_item_id = it.get("sceneItemId")
                break
        if self.media_item_id is None:
            log(f"   回放场景里没有媒体源「{self.media_item}」→ 自动创建一个（ffmpeg_source）")
            try:
                r = obs.create_input(cfg["replay_scene"], self.media_item, "ffmpeg_source",
                                     {"is_local_file": True, "looping": False,
                                      "close_when_inactive": False,
                                      "restart_on_activate": False,
                                      "clear_on_media_end": False})
                self.media_item_id = r.get("sceneItemId")
            except Exception as e:
                log(f"   ❌ 创建媒体源失败：{e}")
                log(f"      手动建：回放场景 → 添加「媒体源」，命名成「{self.media_item}」")
                return False
        try:
            # 播放速度跟插件后端保持一致（0.7 倍慢放）
            obs.request("SetInputSettings",
                        {"inputName": self.media_item,
                         "inputSettings": {"speed_percent": round(float(cfg.get("speed", 0.7)) * 100, 2),
                                           "looping": False, "close_when_inactive": False},
                         "overwrite": False})
        except Exception as e:
            logv(cfg, f"   （设置媒体源参数失败，忽略: {e}）")

        # 3) Replay Buffer 可用性 + 时长
        try:
            settings = obs.output_settings("ReplayBuffer") or {}
        except Exception:
            settings = {}
        buf_sec = None
        for k in ("duration", "RecRBTime", "rec_rb_time", "time"):
            if settings.get(k):
                try:
                    v = float(settings[k])
                    buf_sec = v / 1000.0 if v > 1000 else v     # OBS 有的版本给毫秒
                    break
                except Exception:
                    pass
        # ★ 真机教训（2026-10-08）：OBS 32.x 上 `GetOutputSettings("ReplayBuffer")`
        #   返回 `{}`，读不到时长 —— 退回读 OBS 配置文件（一样准，还不用等 OBS）。
        info = obs_replay_buffer_info()
        if not buf_sec and info.get("seconds"):
            buf_sec = info["seconds"]
            logv(cfg, f"   （回放缓冲时长来自 OBS 配置文件：{buf_sec:.0f} 秒）")
        cfg["obs_buffer_seconds"] = buf_sec
        want = float(cfg.get("record_max_seconds", 10.0) or 10.0)
        if info.get("enabled") is False:
            log("   ⚠️ OBS 里「回放缓冲」是**关着**的（basic.ini: RecRB=false）——"
                "按入点键时 StartReplayBuffer 会被 OBS 拒绝。")
            log("      去 OBS → 设置 → 输出 → 输出模式「简单」→ 勾「启用回放缓冲」，"
                "然后**重启一次 OBS**（回放缓冲的输出是启动时创建的）。")
        if buf_sec:
            log(f"   OBS 回放缓冲时长：{buf_sec:.0f} 秒"
                f"{'' if buf_sec >= want else f'  ⚠️ 小于单段上限 {want:.0f} 秒 → 片段会被截短，'
                   f'请在 OBS → 设置 → 输出 → 回放缓冲里把时长调大'}")
        else:
            log("   （读不到回放缓冲时长；请确认 OBS → 设置 → 输出 → 回放缓冲 里时长 ≥ "
                f"{want:.0f} 秒）")
        if not obs.replay_buffer_active():
            if str(cfg.get("replay_mode") or "armed").lower() == "armed":
                log("   （回放缓冲当前没在跑 —— 省内存模式下按入点键才会启动它）")
            else:
                log("   （回放缓冲当前没在跑 —— 常驻缓冲模式马上会启动它）")
        self.ready = True
        log(f"   ✅ obs 后端就绪：内存 ≈ {self.memory_mb():.0f} MB"
            f"（按 {self.cfg.get('obs_buffer_mbps', 40)} Mbps 估；插件后端同条件是 GB 级）")
        # ★ 真机教训（2026-10-08）：**常驻缓冲模式**下要把 OBS 的回放缓冲现在就开起来。
        #   插件后端在 buffer 模式是 preflight 里 Enable 一次、插件自己一直攒；
        #   obs 后端的对应动作就是 StartReplayBuffer —— 少了这一步，按 → 时
        #   SaveReplayBuffer 会直接回 501，一整段素材白打（真机实测）。
        if (str(cfg.get("replay_mode") or "armed").lower() != "armed"
                and cfg.get("replay_enabled")):
            self.arm("常驻缓冲模式（启动时就开）")
        return True

    # ---------------- 采集开关 ----------------
    def arm(self, reason=""):
        if not self.ready:
            return False
        if self.obs.replay_buffer_active():
            logv(self.cfg, f"   （回放缓冲已经在跑了 —— 可能是 OBS 用 --startreplaybuffer 起的，"
                           f"直接用它：{reason or 'obs 后端'}）")
            return False
        try:
            self.obs.start_replay_buffer()
            log(f"   ▶️ 已启动 OBS 回放缓冲（{reason or 'obs 后端'}）——"
                f"内存 ≈ {self.memory_mb():.0f} MB，不是 GB 级")
            return True
        except Exception as e:
            log(f"   ❌ 启动 OBS 回放缓冲失败：{e}")
            log("      → 请到 OBS → 设置 → 输出 → **回放缓冲**：勾选「启用回放缓冲」，"
                "时长调成 ≥ "
                f"{float(self.cfg.get('record_max_seconds', 10.0)):.0f} 秒，编码器选 NVENC，"
                "码率给足（比如 40 Mbps）。")
            # ★ 真机教训（2026-10-08）：`RecRB=true` 写在配置里还不够 —— 回放缓冲这个
            #   输出是 **OBS 启动时**按设置创建的，所以在设置里勾完之后**必须重启一次
            #   OBS**，否则 StartReplayBuffer 一直回 "Replay buffer is not available"。
            log("      ⚠️ 刚在设置里勾的回放缓冲，**要重启一次 OBS 才生效**"
                "（这个输出是启动时创建的）—— 报错里带 Replay buffer is not available "
                "基本都是这个原因。")
            return False

    def release(self, reason=""):
        if not self.ready or not self.obs.replay_buffer_active():
            return False
        try:
            self.obs.stop_replay_buffer()
            log(f"   ⏏  已停止 OBS 回放缓冲（{reason or 'obs 后端'}）——"
                f"省下约 {self.memory_mb():.0f} MB")
            return True
        except Exception as e:
            log(f"   ⚠️ 停止回放缓冲失败：{e}")
            return False

    # ---------------- 裁片段 ----------------
    def capture_clip(self, mark_at):
        """按 SaveReplayBuffer → 等落盘 → ffmpeg 裁成 (←,→) 那一段。

        返回 `(path, seconds)`；失败返回 `(None, 0.0)`。
        """
        cfg, obs = self.cfg, self.obs
        elapsed = max(0.0, time.time() - float(mark_at or 0.0))
        prev = ""
        try:
            prev = obs.last_replay_buffer_path()
        except Exception:
            pass
        try:
            obs.save_replay_buffer()
        except Exception as e:
            log(f"   ❌ 保存回放缓冲失败：{e}")
            log("      → OBS → 设置 → 输出 → 回放缓冲 里确认已勾选启用；"
                "或者先按一下入点键让它跑起来。")
            return (None, 0.0)
        log(f"   💾 已让 OBS 保存最近的回放缓冲（入点到现在 {elapsed:.1f} 秒），等落盘…")

        deadline = time.time() + max(2.0, float(cfg.get("obs_buffer_wait_ms", 8000)) / 1000.0)
        path = ""
        while time.time() < deadline:
            time.sleep(0.25)
            try:
                p = obs.last_replay_buffer_path()
            except Exception:
                p = ""
            if p and p != prev and os.path.exists(p):
                path = p
                break
        if not path:
            log("   ⚠️ 没等到保存好的文件（OBS 没回路径）—— 这一按没有可存的素材？")
            return (None, 0.0)
        size, done = self.d._wait_file_done(path, 0.0, timeout_extra=6.0)
        log(f"   ✔ 缓冲文件：{path}（{size / 1e6:.1f} MB"
            f"{'' if done else '，可能还在写'}）")

        # 裁成"入点 → 现在"
        fsec = 0.0
        try:
            fsec = self._probe_seconds(path)
        except Exception:
            fsec = 0.0
        if not fsec:
            fsec = min(elapsed, float(cfg.get("record_max_seconds", 10.0) or 10.0))
            log(f"   （读不出文件时长，按 {fsec:.1f} 秒算）")
        start, length, rolled = trim_plan(fsec, elapsed)
        if rolled:
            log(f"   ⚠️ 入点已经滚出缓冲了（缓冲里只有 {fsec:.1f} 秒，"
                f"入点到出点隔了 {elapsed:.1f} 秒）→ 这一段从头播")
        out = self._trim(path, start, length)
        if not out:
            log("   ⚠️ 裁切失败，退回用整段缓冲文件")
            self.clip_path, self.clip_seconds = path, fsec
            return (path, fsec)
        # ★ 真机教训（2026-10-08，实测于 OBS 32.x + NVENC 2 秒 GOP）：
        #   `-c copy` **不能按帧裁** —— ffmpeg 会把起点往前对齐到最近的关键帧，
        #   而 `-t` 是照**入点时间轴**数的，于是裁出来的文件比要的**长**：
        #   实测 `-ss 2 -t 8` 得到 10.03 秒（多出的 2 秒就是往回对齐的那一个 GOP）。
        #   内容上没坏（多出来的是入点**之前**的画面，结尾仍精确停在出点），
        #   但如果还按"要了 8 秒"去算播放时长，回放就会**提前 2 秒切走、把结尾砍掉**。
        #   所以这里一律以**文件实际时长**为准。
        real = 0.0
        try:
            real = self._probe_seconds(out)
        except Exception:
            real = 0.0
        if not real:
            real = length
        self.clip_path, self.clip_seconds = out, real
        extra = real - length
        log(f"   ✔ 片段已裁好（实际 {real:.1f} 秒，从 {start:.1f} 秒处开始；"
            f"{'零损失 copy' if self._trim_mode() == 'copy' else '精确重编码'}）")
        if extra > 0.15:
            log(f"      （比要的 {length:.1f} 秒多了 {extra:.1f} 秒：copy 模式会对齐到关键帧，"
                f"多出来的是入点**之前**的画面，结尾仍然停在出点）")
        # ★ 真机教训（2026-10-08）：OBS 每按一次 SaveReplayBuffer 就**新写一个文件**
        #   （我们裁完的那个 `_clip.mp4` 是它的副本），留着它们磁盘会越攒越满
        #   （真机实测一次 30 MB；一场比赛按 50 次就是 1.5 GB）。裁切成功了就把它删掉。
        if cfg.get("obs_delete_buffer_after_trim", True) and path != out:
            try:
                os.remove(path)
                log(f"   🧹 已删掉 OBS 那份原始缓冲文件（省 {os.path.getsize(out) / 1e6:.1f} MB 磁盘；"
                    f"想留着就把 config.json 的 obs_delete_buffer_after_trim 改成 false）")
            except Exception as e:
                logv(cfg, f"   （删原始缓冲文件失败，忽略: {e}）")
        # 上一次裁的片段也删掉：它只是一份"待播/待留档"的工作副本，
        # 不删的话 OBS 的录像目录里会一个片段一个文件地堆下去（同样吃磁盘）。
        # 想留档的请在播放前后按一下「保留片段」键（那是 copy 到 save_dir，不受影响）。
        old = self._prev_clip
        if old and old != out and os.path.exists(old):
            try:
                os.remove(old)
                logv(cfg, f"   🧹 已删掉上一次的工作片段（{os.path.basename(old)}）")
            except Exception:
                pass
        self._prev_clip = out
        return (out, real)

    def _trim_mode(self):
        m = str(self.cfg.get("trim_mode") or "copy").lower()
        return m if m in ("copy", "exact") else "copy"

    def _probe_seconds(self, path):
        """用 ffmpeg 读时长（不装 ffprobe 也能读：解析 stderr 的 Duration）。"""
        try:
            r = subprocess.run([self.ffmpeg, "-hide_banner", "-i", path],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            pass
        return 0.0

    def _trim(self, src, start, length):
        """裁切：默认 `-c copy`（零损失、秒级）；`exact` 模式重编码这一小段。"""
        if length <= 0.05:
            return ""
        base, _ext = os.path.splitext(src)
        out = base + "_clip.mp4"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}"]
        if self._trim_mode() == "exact":
            crf = self.cfg.get("trim_reencode_crf", 18)
            cmd += ["-c:v", "libx264", "-crf", f"{crf}", "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "160k"]
        else:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        cmd += ["-movflags", "+faststart", out]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=600)
        except Exception as e:
            log(f"   ⚠️ 裁切失败（{e}）")
            return ""
        if r.returncode != 0 or not os.path.exists(out):
            log(f"   ⚠️ 裁切失败（ffmpeg 退出码 {r.returncode}）："
                f"{(r.stderr or '').strip()[:200]}")
            return ""
        log(f"   ✂️ 裁切完成（{time.time() - t0:.1f} 秒，{os.path.getsize(out) / 1e6:.1f} MB）")
        return out

    # ---------------- 播放 ----------------
    def play_clip(self, path=None):
        """把裁好的文件喂给媒体源并在回放场景播放；返回预计播放秒数。"""
        cfg, obs = self.cfg, self.obs
        path = path or self.clip_path
        if not path or not os.path.exists(path):
            log("   ❌ 没有可播的片段文件（先在回放功能里按 ← 再按 →）")
            return 0.0
        try:
            obs.request("SetInputSettings",
                        {"inputName": self.media_item,
                         "inputSettings": {"is_local_file": True, "local_file": path,
                                           "looping": False, "close_when_inactive": False,
                                           "restart_on_activate": False,
                                           "clear_on_media_end": False},
                         "overwrite": False})
        except Exception as e:
            log(f"   ⚠️ 给媒体源设置文件失败：{e}")
        if self.media_item_id is not None:
            obs.set_item_enabled(cfg["replay_scene"], self.media_item_id, True)
        try:
            obs.trigger_media_action(self.media_item, MEDIA_ACTION_RESTART)
        except Exception as e:
            logv(cfg, f"   （RESTART 失败，试 PLAY: {e}）")
            obs.trigger_media_action(self.media_item, MEDIA_ACTION_PLAY)
        speed = max(0.05, float(cfg.get("speed", 0.7) or 0.7))
        hold = self.clip_seconds / speed + 0.6
        log(f"   ▶️ 媒体源开始播放：{os.path.basename(path)}"
            f"（{self.clip_seconds:.1f} 秒 @ {speed * 100:.0f}% → 约 {hold:.1f} 秒）")
        return hold

    def hide(self):
        if self.media_item_id is not None:
            try:
                self.obs.set_item_enabled(self.cfg["replay_scene"], self.media_item_id, False)
            except Exception:
                pass

    def media_state(self):
        try:
            return str((self.obs.media_input_status(self.media_item) or {})
                       .get("mediaState") or "")
        except Exception:
            return ""

    # ---------------- 留档 ----------------
    def archive_clip(self, path=None):
        """小键盘 6：把裁好的文件另存到 save_dir（用我们自己的文件名格式）。"""
        cfg = self.cfg
        path = path or self.clip_path
        if not path or not os.path.exists(path):
            log("   ❌ 手里还没有裁好的片段（先按 ← 再按 →）")
            return ""
        want_dir = str(cfg.get("save_dir") or "").strip()
        if not want_dir:
            try:
                want_dir = str((self.obs.request("GetRecordDirectory") or {})
                               .get("recordDirectory") or "")
            except Exception:
                want_dir = ""
        if not want_dir:
            want_dir = os.path.dirname(path)
        try:
            os.makedirs(want_dir, exist_ok=True)
        except Exception:
            pass
        ext = os.path.splitext(path)[1] or ".mp4"
        fmt = translate_obs_time_format(
            cfg.get("save_file_format") or "回放_%CCYY-%MM-%DD_%hh.%mm.%ss")
        try:
            name = time.strftime(fmt) + ext
        except Exception:
            name = "回放_" + time.strftime("%Y-%m-%d_%H.%M.%S") + ext
        dst = os.path.join(want_dir, name)
        try:
            shutil.copy2(path, dst)
        except Exception as e:
            log(f"   ❌ 另存失败：{e}")
            return ""
        log(f"   ✔ 已留档：{dst}（{os.path.getsize(dst) / 1e6:.1f} MB）")
        return dst


class ReplayDirector:
    def __init__(self, cfg, obs, simulate=False):
        self.cfg = cfg
        self.obs = obs
        self.simulate = simulate
        self.state_ref = None            # 由 App 注入，用于读取实测的"死时间窗口"
        self.auto_enabled = True         # False = 只响应手动 /control/replay，不自动触发
        # 定格与播放互斥：防止"定格还在读日志、播放就开始了"，那会用旧的素材长度
        # 算播放时长，片子被腰斩（实测到过：素材 12 秒却只播 7 秒）。
        self._capture_lock = threading.RLock()
        self.lock_state = threading.Lock()
        self.locked = False
        self.locked_reason = ""
        self.replay_active = False
        self.replay_until = 0.0
        self.replay_hard_deadline = 0.0
        self._started_at = 0.0
        self._phase_at_start = None
        self.replay_item_id = None
        self.overlay_item_ids = []
        self.video_source_name = ""      # 回放源绑定的游戏采集源名（preflight 里填）
        self._last_load_replay = 0.0
        self._snapshot_ok = False        # 手里有没有一段裁好的素材（按 → 标出点时产生）
        self._last_snapshot_at = 0.0     # 上次裁素材的时刻（= 缓冲区被掏空、重新开始累积的时刻）
        self.mark_in_at = 0.0            # ★ 按 ← 标下的入点时刻（0 = 还没标）
        self.last_clip_seconds = None    # 最近一次裁出来的**可播**长度（已扣掉裁掉的开头）
        self.last_clip_raw_seconds = None  # 插件报的原始长度（未裁）
        self.last_clip_trim = 0.0        # 这一段裁掉了开头多少秒（= 入点之前的那些）
        self._last_clip_end_at = 0.0     # 裁素材那一刻（= 素材的结尾时刻）
        self._current_scene = None       # 本地镜像的当前节目场景，避免频繁查询 OBS
        self.last_replay_round = None
        self.last_completed_round = None  # 最近打完的回合号（判断手里的素材是不是这一回合的）
        self._start_delay_checked = False  # 是否已经验证过 StartDelay 能写进插件
        # ★ 回放功能总开关 / 内存模式（2026-10-07 加；默认关、默认省内存）
        self.replay_enabled = bool(cfg.get("replay_enabled", False))
        self.replay_mode = str(cfg.get("replay_mode") or "armed").lower()
        if self.replay_mode not in ("armed", "buffer"):
            self.replay_mode = "armed"
        self._capture_armed_at = 0.0     # armed 模式：上次让插件开始攒帧的时刻
        self._capture_disabled = None    # None=还不知道；True=插件当前是 Disable 状态
        self._memory_info = {}           # 最近一次内存体检的原始数据
        self._watch_tick = 0             # 资源哨兵的计数器（ticker 每 50ms +1）
        self._media_poll_at = 0.0        # obs 后端：上次问"媒体播完了吗"的时刻
        self._mem_alert = ""             # 上次的内存警告级别（ok/warn/release）
        self._mem_alert_at = 0.0
        self._config_path = CONFIG_PATH
        # ★ 回放后端：plugin（默认，插件内存缓冲）/ obs（OBS 自带 Replay Buffer，编码后）
        self.backend_name = str(cfg.get("replay_backend") or "plugin").lower()
        if self.backend_name not in ("plugin", "obs"):
            self.backend_name = "plugin"
        self.backend = ObsBufferBackend(self) if self.backend_name == "obs" else None
        self.obs_backend_ready = False
        # 人工接管锁的自动过期
        self._locked_at = 0.0
        # 进回放之前人在哪个场景（回放结束后切回这里，而不是死板地切回 live_scene）
        self._scene_before_replay = None
        # ★ 包装转场（stinger）等待：切场景后先把回放定格在第一帧，等包装放完再开播，
        #   不然回放的前 ~3.35 秒会被包装画面盖掉（用户 2026-10-06 反馈）。
        self._wrap_resume_at = 0.0       # >0 = 正在等包装放完（到点由 ticker 恢复播放）
        self._wrap_cache = None          # 自动量出来的包装时长（只量一次）
        self.expected_hold = 0.0
        self.manual_replay_pending = False
        self._expected_scene = None      # 兼容旧逻辑
        # 我们自己会切到的场景集合 + 有效期。在这个窗口内，落到这两个场景上的
        # CurrentProgramSceneChanged 事件都认为是我们自己发的，不当作人工接管。
        # （实测：OBS 有时会对同一个场景重复发事件，一次性消费的旧写法会误锁。）
        self._self_scenes = {cfg.get("live_scene"), cfg.get("replay_scene")}
        self._self_scene_until = 0.0
        self._stop = False
        self.counters = {"rounds": 0, "replays": 0, "skipped": 0, "interrupted": 0}

    # ---------------- 启动检查 ----------------
    def _plugin_backend_available(self):
        """插件后端能不能用：回放场景里有那个回放源，或者 OBS 里装了 `replay_source`。

        用来在"选的后端不可用"时判断能不能自动退回插件后端（2026-10-09 真机反馈）。
        """
        if self.replay_item_id is not None:
            return True
        return obs_has_replay_source(self.obs)

    def _obs_backend_available(self):
        """obs 后端能不能用：有 ffmpeg + OBS 里启用了回放缓冲。"""
        try:
            if not find_ffmpeg(self.cfg):
                return False
            info = obs_replay_buffer_info()
            return bool(info.get("enabled") is True and info.get("seconds"))
        except Exception:
            return False

    def preflight(self):
        cfg = self.cfg
        scenes, current = self.obs.scene_list()
        self._current_scene = current
        log(f"OBS 场景 ({len(scenes)}): {', '.join(scenes)}")
        log(f"OBS 当前直播场景: {current}")

        # 补全可能不存在的场景
        for key, label in (("live_scene", "直播场景"), ("replay_scene", "回放场景")):
            name = cfg[key]
            if name not in scenes:
                log(f"❌ 配置里的{label}「{name}」在 OBS 里找不到！请核对 config 或 OBS 场景名。")
                return False

        # 解析回放场景里的 Replay Source 场景项 id
        items = self.obs.scene_items(cfg["replay_scene"])
        log(f"回放场景「{cfg['replay_scene']}」共 {len(items)} 个场景项:")
        for it in items:
            mark = ""
            if it.get("sourceName") == cfg["replay_item"]:
                mark = "   <== 这就是回放源（plugin 后端用）"
                self.replay_item_id = it.get("sceneItemId")
            if it.get("sourceName") == str(cfg.get("replay_media_item") or ""):
                mark = "   <== 这是媒体源（obs 后端用）"
            log(f"    - {it.get('sourceName')}  (id={it.get('sceneItemId')}, "
                f"{'可见' if it.get('sceneItemEnabled') else '隐藏'}){mark}")
        if self.backend_name == "obs":
            # obs 后端不用插件的回放源；它要的是回放场景里的**媒体源**
            ready = bool(self.backend.prepare())
            if not ready and self._plugin_backend_available():
                # ★ 2026-10-09 真机反馈（用户机器）：没装 ffmpeg → obs 后端直接拒绝启用 →
                #   整个"回放功能"死了；可他机器上**插件是装着的**，明明能用。
                #   所以这里自动退回插件后端，只改本次运行的配置（不偷偷改他的 config.json）。
                log("   ↩ 自动退回「插件后端」：OBS 里有 Replay Source，回放照常能用。")
                log("      想用 obs 后端的话：装好 ffmpeg（或把 ffmpeg.exe 放到本程序目录旁边），"
                    "再到设置窗口 ① 页把「回放后端」切回 OBS 自带 Replay Buffer。")
                self.backend_name = "plugin"
                self.cfg["replay_backend"] = "plugin"
                self.backend = None
                self.obs_backend_ready = False
            else:
                self.obs_backend_ready = ready
                if not ready:
                    log("   ⚠️ obs 后端没准备好，而且插件后端也用不了 —— 回放暂时不可用"
                        "（自动切镜头和手机提示器不受影响）。")
        elif self.replay_item_id is None:
            log(f"❌ 回放场景里找不到名为「{cfg['replay_item']}」的源，请核对名字。")
            if self._obs_backend_available():
                log("   ↩ 不过这台机器上 obs 后端的条件看着是齐的 —— 可以在设置窗口 ① 页"
                    "把「回放后端」换成 OBS 自带 Replay Buffer（不需要那个插件）。")
            return False

        # 角标
        self.overlay_item_ids = []
        for name in cfg.get("overlay_items") or []:
            found = [it for it in items if it.get("sourceName") == name]
            if found:
                self.overlay_item_ids.append((name, found[0].get("sceneItemId")))
            else:
                log(f"⚠️  角标源「{name}」在回放场景里找不到，忽略。")

        # ⚠️ 关键安全检查：回放场景里必须有游戏采集源，否则回放源会失去输入（黑屏）
        src_names = [it.get("sourceName") for it in items]
        try:
            if self.backend_name != "plugin":
                raise _SkipPluginSection()      # obs 后端不碰插件的回放源
            kind, settings = self.obs.input_settings(cfg["replay_item"])
            video_src = settings.get("source") or ""
            self.video_source_name = video_src
            log(f"回放源类型={kind}  绑定的 Video Source = 「{video_src}」")
            if not video_src:
                log("❌ Replay Source 的 Video Source 是空的！请先在 OBS 里绑定成游戏采集源。")
                return False
            if video_src not in src_names:
                # 实测结论（2026-10-05）：这**不是**致命的。
                # 帧是缓存在内存里的，切到回放场景时缓存里已经有最近 N 秒，
                # 所以回放照常放得出来（实测截图 876KB 真实画面）。
                # 唯一影响：停在回放场景期间滤镜停止累积新帧，连放两次第二次会偏短。
                log(f"   ℹ️  「{video_src}」不在回放场景里。这不影响单次回放"
                    f"（帧已在内存缓存里），但停在回放场景时滤镜会停止累积新帧。")
                log("      建议（可选）：把「游戏采集」作为现有源加进回放场景、放最底层，")
                log("      这样连放两次也不会出现第二次片段偏短。")
            else:
                log(f"✅ 回放场景里含游戏采集源「{video_src}」，回放时不会掉输入。")
            dur = settings.get("duration")
            spd = settings.get("speed")
            if dur or spd:
                log(f"    （插件里的 Duration={dur}, Speed={spd}；请确认和配置里的 "
                    f"capture_seconds={cfg['capture_seconds']} / speed={cfg['speed']} 一致）")
        except _SkipPluginSection:
            log("   （obs 后端：跳过插件的「Video Source」检查）")
        except Exception as e:
            log(f"⚠️  读回放源设置失败（不影响运行）: {e}")

        # ★ 包装转场（stinger）会盖住回放片头：启动时报一次它有多长（只报一次）。
        _ = self._wrap_seconds()
        if self.cfg.get("replay_wrap_seconds") is not None:
            try:
                log(f"   （包装等待按配置写死 {float(self.cfg['replay_wrap_seconds']):.2f} 秒，"
                    f"不再自己量素材；想让它自己量就把 config.json 里的 "
                    f"replay_wrap_seconds 设成 null）")
            except Exception:
                pass

        # ★ 2026-10-06：转场放完怎么"续播"——必须走媒体接口，不能按 ReplaySource.Replay
        #   （那是插件的 Load replay，会重新取一段素材，播出来不是用户框的那段）。
        #   这里先确认接口在；不在就明确告诉用户会退回 Restart（内容仍对，只是不吃定格）。
        try:
            avail = set((self.obs.request("GetVersion") or {}).get("availableRequests") or [])
            if "TriggerMediaInputAction" in avail:
                log("   ✅ 媒体接口可用：转场放完从定格那一帧续播，不会重新取素材")
            elif avail:
                log("   ⚠️  这个 obs-websocket 没有 TriggerMediaInputAction —— 转场续播会退回"
                    "插件 Restart 热键（同一段素材从头播，内容仍然是你框的那一段）")
        except Exception as e:
            logv(cfg, f"   （核对媒体接口失败，忽略: {e}）")

        # ★ 小键盘 6 = 保留片段：把"存到哪儿 / 文件名格式"写进插件。
        #   插件源码 :3070 `obs_properties_add_path(... OBS_PATH_DIRECTORY ...)` 的
        #   directory 默认是**未设置**的 → 直接按保存会生成相对路径，很可能落到
        #   OBS 工作目录里去，所以必须显式给一个目录。
        if cfg.get("save_replay_key", True):
            try:
                if self.backend_name != "plugin":
                    raise _SkipPluginSection()   # obs 后端自己命名/另存文件
                want_dir = str(cfg.get("save_dir") or "").strip()
                src = "config.save_dir"
                if not want_dir:
                    try:
                        want_dir = str((self.obs.request("GetRecordDirectory")
                                        or {}).get("recordDirectory") or "")
                        src = "OBS 录制目录"
                    except Exception as e:
                        log(f"   （读 OBS 录制目录失败: {e}）")
                if want_dir and cfg.get("save_dir"):
                    try:
                        os.makedirs(want_dir, exist_ok=True)   # 只有你明确指定才建目录
                    except Exception as e:
                        log(f"   ⚠️ 建目录「{want_dir}」失败: {e}")
                want_fmt = str(cfg.get("save_file_format")
                               or "回放_%CCYY-%MM-%DD_%hh.%mm.%ss")
                _, s3 = self.obs.input_settings(cfg["replay_item"])
                patch = {}
                if int(s3.get("lossless") or 0):
                    # 无损模式导出的是 .avi，体积是 H.264 的几十倍，默认关掉
                    patch["lossless"] = False
                if want_dir and str(s3.get("directory") or "") != want_dir:
                    patch["directory"] = want_dir
                if str(s3.get("file_format") or "") != want_fmt:
                    patch["file_format"] = want_fmt
                if patch:
                    self.obs.request("SetInputSettings",
                                     {"inputName": cfg["replay_item"],
                                      "inputSettings": patch, "overwrite": False})
                    log(f"   『小键盘6=保留片段』就绪：存到 {want_dir or '(插件默认位置)'}"
                        f"（来自{src}），文件名 {want_fmt}")
                else:
                    log(f"   『小键盘6=保留片段』就绪：存到 {want_dir or '(插件默认位置)'}")
            except _SkipPluginSection:
                log("   （obs 后端：『小键盘6=保留片段』由引擎自己另存文件，不写插件参数）")
            except Exception as e:
                log(f"   （设置保留片段失败，小键盘 6 仍可用但可能存到默认位置: {e}）")

        self.expected_hold = min(
            float(cfg.get("record_max_seconds", 10.0)) / max(cfg["speed"], 0.05)
            - float(cfg.get("return_before_end", 0.0)) + 1.0,
            cfg["max_hold"])
        log(f"预计单次回放最长占用 {self.expected_hold:.1f}s"
            f"（= 素材上限 {cfg.get('record_max_seconds')} 秒 / {cfg['speed']*100:.0f}% 速度）")

        # ★ 内存护栏：必须在把 duration 写进插件**之前**跑 ——
        #   它会按本机内存 + 真实画布决定"这台机器最多能攒几秒"。
        self._apply_memory_guard()

        # 把"单段素材上限"和速度写进插件（config 变成唯一事实来源，不用手动同步两处）
        want_ms = int(float(cfg.get("record_max_seconds", 10.0)) * 1000)
        want_spd = round(float(cfg.get("speed", 0.7)) * 100.0, 2)
        try:
            if self.backend_name != "plugin":
                raise _SkipPluginSection()   # obs 后端不写插件的 duration/next_scene/缓冲开关
            _, s = self.obs.input_settings(cfg["replay_item"])
            patch = {}
            if int(s.get("duration") or 0) != want_ms:
                patch["duration"] = want_ms
            if abs(float(s.get("speed_percent") or 100.0) - want_spd) > 0.5:
                patch["speed_percent"] = want_spd
            if patch:
                self.obs.request("SetInputSettings",
                                 {"inputName": cfg["replay_item"],
                                  "inputSettings": patch, "overwrite": False})
                log(f"   已同步 Replay Source 设置: {patch}")

            # ★ 让**插件自己**在片子播完的同一帧切回直播场景。
            #   这比引擎掐时间准得多：引擎不需要知道素材到底多长，
            #   也就不会出现"以为 5.5 秒、实际 12 秒 → 播一半被砍掉"。
            #   （End Action 保持 Pause after single：定格和切场景同一帧发生，
            #     转场会把这一刻盖住，不会有"定格一下才切"的顿感。）
            try:
                _, s2 = self.obs.input_settings(cfg["replay_item"])
                ns = s2.get("next_scene")
                if ns != cfg["live_scene"]:
                    self.obs.request("SetInputSettings",
                                     {"inputName": cfg["replay_item"],
                                      "inputSettings": {"next_scene": cfg["live_scene"]},
                                      "overwrite": False})
                    log(f"   已设置「播完自动切回」→ {cfg['live_scene']}"
                        f"（由插件在片子结束的同一帧触发，比引擎掐时间准）")
                else:
                    log(f"   「播完自动切回」= {cfg['live_scene']} ✅")
            except Exception as e:
                log(f"   （设置 next_scene 失败，会用引擎计时兜底: {e}）")

            log(f"   单段素材上限 {want_ms/1000:.0f} 秒，回放速度 {want_spd:.0f}%"
                f" → 播放约 {want_ms/1000/max(want_spd/100,0.05):.1f} 秒")
        except _SkipPluginSection:
            log(f"   （obs 后端：单段上限 {want_ms/1000:.0f} 秒 / 速度 {want_spd:.0f}%"
                f" → 由 OBS 回放缓冲的时长决定，请在 OBS 里设成 ≥ {want_ms/1000:.0f} 秒）")
        except Exception as e:
            log(f"   （同步 Duration/Speed 失败，不影响使用: {e}）")

        # ★ 2026-10-07：内存体检 + 回放开关落地。
        #   老版本的日志写的是"4K 10 秒约 7.5 GB / 1080p 约 1.9 GB"，**差了 5 倍**
        #   （实测：4K 两份 ≈ 40 GB，1080p 两份 ≈ 10 GB）—— 就是这句误导让人以为是内存泄漏。
        #   现在按画布 × 帧率 × 时长 × 份数**算真的数字**，并核对插件的内存相关设置。
        if cfg.get("memory_report", True):
            self._memory_audit()

        # 插件当前该开还是该关：只有"回放功能开着 + 常驻缓冲模式"才需要一直攒帧。
        if not self.replay_enabled:
            self.release_capture("回放功能默认是关的（省内存）")
            if cfg.get("clear_replay_on_disable", True):
                self.clear_replays("回放功能默认是关的")
            log("   🎛  回放功能：关闭（默认）—— 四个回放键只提示不动手；")
            log("       想用就在设置窗口 / 手机页面上打开，或把 config.json 的 "
                "replay_enabled 改成 true。")
        elif self.replay_mode == "armed":
            # 省内存模式：启动时不攒帧，等用户按 ← 再 Enable（那条通路上会打日志）。
            self.release_capture("省内存模式：等按 ← 再开始攒帧")
            log("   🎛  回放功能：开启（省内存模式）—— 按【←】标入点的那一刻才开始攒帧。")
        else:
            # 常驻缓冲模式：先冲一次旧缓冲（改了 Duration 之后滤镜里可能还留着旧上限的帧，
            # 实测：配置 10 秒但第一次裁出来是 12 秒），然后一直攒着。
            try:
                self._hk("ReplaySource.Enable")
                self._capture_disabled = False
                self._capture_armed_at = time.time()
                self._hk("ReplaySource.Replay")
                log("   🎛  回放功能：开启（常驻缓冲模式）—— 已冲掉启动前的旧缓冲，"
                    "现在开始一直攒着最近 "
                    f"{float(cfg.get('record_max_seconds', 10.0)):.0f} 秒。")
            except Exception as e:
                log(f"   （冲旧缓冲失败，不影响使用: {e}）")
        if self.backend_name == "obs":
            return bool(self.obs_backend_ready)
        return True if self.replay_item_id is not None else False

    # ---------------- 内存护栏 ----------------
    def _apply_memory_guard(self):
        """按本机内存 + 真实画布，决定"这台机器最多能攒几秒"，装不下就降/就拒。

        为什么必须有（真机取证，2026-10-08）：32 GB 机器 + 4K60 画布 + 10 秒
        = 插件要两份 ≈39.8 GB → 系统换页到整机失去响应、OBS 被拖死、
        只能长按电源键（事件日志 `Kernel-Power 41 / BugcheckCode=0`、无转储）。
        返回 True 表示回放仍然可用；False 表示这台机器跑不了、已把回放功能关掉。
        """
        cfg = self.cfg
        if not cfg.get("memory_guard", True):
            log("   🧠 内存护栏：已按配置关闭（memory_guard=false）—— 自己不拦，出事自负")
            return True
        if self.simulate:
            logv(cfg, "   （仿真模式：内存护栏跳过，改用纯函数单独回归）")
            return True
        if self.backend_name == "obs":
            # obs 后端放的是**编码后**的数据（几十~几百 MB），不需要按未压缩帧那套拦；
            # 只提醒"OBS 回放缓冲时长够不够"，以及磁盘空间。
            try:
                mb = self.backend.memory_mb()
            except Exception:
                mb = 0.0
            buf = cfg.get("obs_buffer_seconds")
            want = float(cfg.get("record_max_seconds", 10.0) or 10.0)
            log(f"   🧠 内存护栏（obs 后端）：编码后缓冲 ≈ {mb:.0f} MB，"
                f"不需要按未压缩帧限制时长")
            if buf and float(buf) + 0.5 < want:
                log(f"      ⚠️ OBS 回放缓冲只有 {float(buf):.0f} 秒，小于单段上限 {want:.0f} 秒"
                    f" → 片段会被截到 {float(buf):.0f} 秒；"
                    f"请在 OBS → 设置 → 输出 → 回放缓冲里把它调大。")
            if not self._check_disk_space():
                self.replay_enabled = False
                cfg["replay_enabled"] = False
                return False
            return True
        info = self.memory_info()
        total, avail = system_memory_gb()
        if not total:
            logv(cfg, "   （读不到本机内存，内存护栏跳过）")
            return True
        want = float(cfg.get("record_max_seconds", 10.0) or 10.0)
        safe, note = plan_replay_seconds(info["width"], info["height"], info["fps"],
                                         want, total, avail, copies=2.0)
        budget = memory_budget_gb(total, avail)
        judge = ("没问题" if safe == want else
                 ("装不下" if safe is None else f"降到 {safe:.1f} 秒"))
        log("")
        log(f"   🧠 内存护栏：本机 {total:.1f} GB（当前可用 {avail:.1f} GB），"
            f"回放安全预算 ≈ {budget:.2f} GB → {judge}")
        if safe == want:
            return True
        high_res = info["width"] >= 2560 or info["height"] >= 1440
        log(f"      ⚠️ 当前设置：画布 {info['width']}x{info['height']}@{info['fps']:g}"
            f"，单段 {want:.1f} 秒 → 峰值要两份 ≈ "
            f"{info['width'] * info['height'] * 4 * info['fps'] * want / 1e9 * 2:.2f} GB")
        log(f"      {note}")
        if high_res:
            log("      ★ 根治办法：把 OBS 的**基础(画布)分辨率降到 1920x1080** ——")
            log("        插件缓冲是按画布算的，4K→1080p 直接少 4 倍内存；")
            log("        观众本来就看 1080p，画质没有任何变化。一键工具：")
            log("          python tools\\rescale_canvas.py --apply        （不用关 OBS，可回滚）")
        if safe is not None and high_res and safe < 4.0:
            # 4K 画布下能安全攒的秒数不到 4 秒 —— 这种"回放"没有使用价值，
            # 与其让用户拿到 2 秒的片子，不如直接说清楚"先降画布"。
            log(f"      ❌ 高分辨率画布下最多只能安全攒 {safe:.1f} 秒，回放没有使用价值 →")
            log("         按**拒绝开启**处理：先降到 1080p 画布（上面那条命令），再打开回放功能。")
            safe = None
        if safe is None:
            self.replay_enabled = False
            cfg["replay_enabled"] = False
            log("      ❌ 这台机器**跑不了回放**：已把回放功能关掉（提示器与自动切视角照常工作）。")
            log("         想用回放：先降画布/帧率，或在 config.json 里把 record_max_seconds 调小。")
            try:
                self.release_capture("内存护栏：装不下")
            except Exception:
                pass
            return False
        # 磁盘也要看一眼（页面文件在系统盘，写满了同样会"整机不可用"）
        if not self._check_disk_space():
            self.replay_enabled = False
            cfg["replay_enabled"] = False
            try:
                self.release_capture("磁盘护栏：系统盘快满")
            except Exception:
                pass
            return False
        log(f"      → 已自动把单段素材上限从 {want:.1f} 秒降到 **{safe:.1f} 秒**"
            f"（≈{info['width'] * info['height'] * 4 * info['fps'] * safe / 1e9 * 2:.2f} GB 峰值）")
        log("        不想让它自动降：config.json 里把 memory_guard 设成 false（不建议）")
        cfg["record_max_seconds"] = safe
        if not cfg.get("dry_run"):
            try:
                self.persist_config()
            except Exception:
                pass
        return True

    def _check_disk_space(self):
        """磁盘写满也会"整机不可用"：页面文件在系统盘，回放留档也往盘上写。

        返回 True = 还行；False = 系统盘快满了（拒绝开回放）。
        """
        cfg = self.cfg
        sys_drive = os.environ.get("SystemDrive", "C:") + os.sep
        free_sys = disk_free_gb(sys_drive)
        save_dir = str(cfg.get("save_dir") or "").strip()
        free_save = disk_free_gb(save_dir) if save_dir else None
        os.makedirs  # noqa: B018  （只是提示：这里不建目录）
        if free_sys is None and free_save is None:
            return True
        if free_sys is not None:
            if free_sys < 2.0:
                log(f"   ❌ 磁盘护栏：系统盘 {sys_drive} 只剩 {free_sys:.1f} GB ——"
                    f" 页面文件都写不进去，整机会卡死/报错。先清理磁盘再开回放。")
                return False
            if free_sys < 8.0:
                log(f"   ⚠️ 磁盘护栏：系统盘 {sys_drive} 只剩 {free_sys:.1f} GB，"
                    f"页面交换会更吃力（机器内存吃紧时尤其明显）——建议先清一点空间。")
        if free_save is not None and free_save < 5.0:
            log(f"   ⚠️ 磁盘护栏：留档目录所在盘只剩 {free_save:.1f} GB，"
                f"按小键盘 6 存片段可能写到没空间（一段 10 秒 ≈ 20~60 MB，但会累计）。")
        return True

    # ---------------- 内存体检 ----------------
    def _memory_audit(self):
        """把回放插件的内存真相打出来，并核对几个会让内存翻倍的设置。

        为什么要做：用户 2026-10-07 反馈"每次用这个插件都占 10 G 内存，是不是缺陷"。
        查下来不是泄漏，是插件的设计（未压缩帧滚动缓冲）+ 几个**会静默翻倍**的设置：
          * 插件设置 `replays`(Maximum replays) —— 每多留一段就多一整份缓冲（1~10）；
          * 插件设置 `internal_frames`(Capture internal frames) —— 换成内部帧，通常是多一份；
          * 场景里有**多个** replay_source 实例 —— 每个都自己攒一份。
        这里把数字算出来、把这几个坑指出来，并按配置自动写回安全值。
        """
        cfg = self.cfg
        if self.backend_name == "obs":
            # obs 后端：内存是**编码后**的，没有"未压缩帧翻倍"这些坑，报个简短结论就行
            mbps = float(cfg.get("obs_buffer_mbps", 40.0) or 40.0)
            secs = float(cfg.get("record_max_seconds", 10.0) or 10.0)
            mb = mbps * secs / 8.0 * 2.0
            # ★ 2026-10-09：obs-websocket 读不到时长时（真机就是这样）要退回读 OBS 配置文件，
            #   否则这里永远显示"(读不到)" —— 用户机器上明明设了 20 秒。
            buf = cfg.get("obs_buffer_seconds")
            if not buf:
                try:
                    buf = obs_replay_buffer_info().get("seconds")
                except Exception:
                    buf = None
                if buf:
                    cfg["obs_buffer_seconds"] = buf
            log("")
            log("   🧠 内存体检（obs 后端：OBS 自带 Replay Buffer）")
            log(f"      编码后缓冲 ≈ {mb:.0f} MB（按 {mbps:.0f} Mbps × {secs:.0f} 秒 × 2 份估）")
            log(f"      OBS 回放缓冲时长：{'%.0f 秒' % float(buf) if buf else '(读不到)'}"
                f"（必须 ≥ 单段上限 {secs:.0f} 秒）")
            log("      ★ 这条路径**不需要**按未压缩帧那套限制时长：4K 也只是内存里放编码数据，")
            log("        片段是「保存缓冲文件 + ffmpeg 按 ←/→ 裁切」出来的（裁切零损失）。")
            log("")
            return
        try:
            _, s = self.obs.input_settings(cfg["replay_item"])
        except Exception as e:
            logv(cfg, f"   （内存体检读插件设置失败: {e}）")
            s = {}

        info = self.memory_info()
        log("")
        log("   🧠 内存体检（回放插件占多少）")
        log(f"      画布/帧率：{info['width']}x{info['height']} @ {info['fps']:g}fps"
            f"（每帧 {info['frame_mb']:.1f} MB，每秒 {info['mb_per_sec']:.0f} MB）")
        log(f"      单段素材上限：{info['seconds']:.1f} 秒 → 一份 ≈ "
            f"{info['one_copy_gb']:.2f} GB")
        log(f"      当前模式：{'省内存（armed）' if self.replay_mode == 'armed' else '常驻缓冲（buffer）'}"
            f" → 稳态 {info['copies']:g} 份 ≈ {info['steady_gb']:.2f} GB")
        log(f"      「Maximum replays」= {s.get('replays', '(读不到)')}"
            f"（每多 1 段就多一份 ≈ {info['one_copy_gb']:.2f} GB）")
        log(f"      「Capture internal frames」= {s.get('internal_frames', '(读不到)')}"
            f"（开着通常再多一份）")
        log(f"      插件 Duration = {s.get('duration', '(读不到)')} ms"
            f"（引擎会按 record_max_seconds 同步）")

        # 场景里有几个 replay_source？每个实例都自己攒一份，这是最容易翻倍的地方。
        try:
            kinds = [str((i or {}).get("inputKind") or "") for i in (self.obs.input_list() or [])]
            n_rs = sum(1 for k in kinds if k == "replay_source")
            n_flt = sum(1 for k in kinds if k.startswith("replay_filter"))
            log(f"      replay_source 实例：{n_rs} 个"
                f"{'（正常，1 个就够）' if n_rs <= 1 else '　⚠️ 每多一个实例就多攒一份，建议只留一个'}")
            if n_flt:
                log(f"      另外还有 {n_flt} 个独立的 replay_filter 输入（也会各占一份）")
        except Exception as e:
            logv(cfg, f"      （数实例失败: {e}）")

        if not self.replay_enabled:
            log("      当前回放功能是**关闭**的：启动后已经 Disable，滚动缓冲不占内存。")
        elif self.replay_mode == "armed":
            log("      省内存模式：空闲时 ≈0；按 ← 之后会涨到 "
                f"≈{info['one_copy_gb'] * 2:.2f} GB（攒帧 + 已取出那段），"
                "裁完立即释放滚动那份。")
        log("      依据：插件把最近 N 秒的**未压缩**帧放内存里（画布宽×高×4 字节×帧率），"
            "不是内存泄漏。")
        log("      想更省：record_max_seconds 调小（10→5 秒省一半），或降到 720p/30fps。")

        patch = {}
        try:
            if int(s.get("replays") or 1) != 1:
                patch["replays"] = 1
                log(f"      ⚠️ Maximum replays = {s.get('replays')}："
                    f"这会多占 {info['one_copy_gb'] * (int(s.get('replays') or 1) - 1):.2f} GB"
                    f" → 建议改回 1")
            if s.get("internal_frames"):
                patch["internal_frames"] = False
                log("      ⚠️ Capture internal frames 是开着的：通常会让内存再多一份 → 建议关掉")
        except Exception:
            pass
        if patch:
            if cfg.get("memory_autofix", True):
                try:
                    self.obs.request("SetInputSettings",
                                     {"inputName": cfg["replay_item"],
                                      "inputSettings": patch, "overwrite": False})
                    log(f"      ✔ 已自动写回安全值：{patch}（想自己留着就把 config.json 的 "
                        "memory_autofix 设成 false）")
                except Exception as e:
                    log(f"      ⚠️ 自动写回失败: {e}")
            else:
                log(f"      （memory_autofix=false，没有自动改。建议在 OBS 里改成 {patch}）")
        log("")


    # ---------------- 击杀事件：计划快照 ----------------
    def on_kill(self, sid, kill_count, round_no, name):
        """检测到击杀 —— 只打一行日志。

        ★ 2026-10-06 用户决定：**去掉所有自动定格**（回合结束自动抓、击杀后延迟抓都删了）。
        所以这里不再排任何定时任务，素材只由 ← / → 两个键框出来。
        """
        cfg = self.cfg
        if kill_count and int(kill_count) >= 1:
            logv(cfg, f"   [击杀] {name or sid[:8]} 第 {kill_count} 杀")

    # ---------------- 人工接管 / 锁 ----------------
    def lock(self, reason):
        with self.lock_state:
            if not self.locked:
                log(f"🔒 已锁定自动导播（原因：{reason}）—— 手动接管优先")
            self.locked = True
            self.locked_reason = reason
            self._locked_at = time.time()

    def unlock(self, reason):
        with self.lock_state:
            if self.locked:
                log(f"🔓 已解锁自动导播（原因：{reason}）")
            self.locked = False
            self.locked_reason = ""
            self._locked_at = 0.0

    def _return_scene(self):
        """回放结束后该回到哪个场景。

        默认 live_scene；但如果是你（或 Astra）在「数据看板」之类的场景上按了
        播放键，就在播完之后切回那个场景，而不是硬切回 HUD
        —— 硬切会让画面在赛间乱跳一下。
        """
        return self._scene_before_replay or self.cfg["live_scene"]

    def _set_plugin_next_scene(self, scene):
        """把"播完自动切到哪个场景"写进插件（负值/空值会让插件不切）。"""
        try:
            _, s = self.obs.input_settings(self.cfg["replay_item"])
            if s.get("next_scene") == scene:
                return
            self.obs.request("SetInputSettings",
                             {"inputName": self.cfg["replay_item"],
                              "inputSettings": {"next_scene": scene},
                              "overlay": True, "overwrite": False})
        except Exception as e:
            logv(self.cfg, f"   （设置播完切回场景失败，用引擎计时兜底: {e}）")

    def on_obs_event(self, event_type, data):
        """
        场景变化事件。

        和 Astra 的协作规则（重要）：
          * 引擎自己切的场景 → 忽略。
          * **回放进行中**被切走 → 这是明确的人工接管，立即结束回放并锁定。
          * 其它场景变化（Astra 按阶段自动切、导播自己切）→ 默认**不改锁状态**。
            因为 Astra 每个阶段边界都会切场景，若一律锁定，引擎会被永久锁死。
            想回到"任何人工切换都锁定"的严格模式，把配置里的
            lock_on_manual_scene 改成 true（不适合和 Astra 同时用）。
        """
        if event_type != "CurrentProgramSceneChanged":
            return
        scene = data.get("sceneName")
        prev = self._current_scene
        self._current_scene = scene
        now = time.time()
        # ★ 插件按 next_scene 自己切回来了（片子播完的那一帧）——这是我们要的收尾方式。
        #   识别方式：从「即时回放」回到**进回放之前那个场景**的转变，而且不是我们发起的
        #   （我们自己切的时候 replay_active 已经先置 False 了，所以不会误判）。
        #   注意这里不能写死 live_scene：你可能是在「数据看板」上按的播放键。
        if (self.replay_active and prev == self.cfg["replay_scene"]
                and scene == self._return_scene()):
            self._end_replay(f"素材播完（插件按 next_scene 切回「{scene}」）")
            return
        if scene in self._self_scenes and now <= self._self_scene_until:
            logv(self.cfg, f"   （场景事件 {scene} 由引擎自己触发，忽略）")
            return
        # 回放中被人切走 —— 无疑义的人工接管
        if self.replay_active and scene != self.cfg["replay_scene"]:
            self.lock(f"回放中被切到「{scene}」")
            # ★ 不要再切一次场景！人家（你或者 Astra）已经把画面切到别处了，
            #   我们再切回 HUD 就是两个程序互相抢画面。只把回放源收起来就好。
            self._end_replay("人工接管", switch_back=False)
            return
        if self.cfg.get("lock_on_manual_scene"):
            self.lock(f"检测到切到「{scene}」")
        else:
            logv(self.cfg, f"   （场景切到「{scene}」——不是回放中断，不锁定；"
                           f"要手动接管请访问 /control/lock）")

    def _switch_program(self, scene):
        # 有效期覆盖整次回放 + 余量，期间落在这两个场景上的事件都算自己的
        self._self_scene_until = time.time() + self.expected_hold + 8.0
        self._expected_scene = (scene, time.time() + 5.0)
        # ★ 凡是**我们自己**切过去的场景，都记成"自己的场景"。
        #   否则"播完切回数据看板"这个动作会落进"回放中被人切走"分支 → 误锁自己。
        self._self_scenes.add(scene)
        self._current_scene = scene
        log(f"   [OBS] 切到场景「{scene}」")
        self.obs.set_scene(scene)

    # ---------------- 回合结束回调 ----------------
    def on_round_end(self, round_no, stats, score, reasons):
        cfg = self.cfg
        self.counters["rounds"] += 1
        self.last_completed_round = round_no

        # ★ 2026-10-06 用户决定：**去掉所有自动定格**。
        #   回合结束不再自动抓素材；素材完全由 ← / → 两个键框出来
        #   （← 标入点 → 打完了按 → 当场把"入点 → 现在"裁成一段）。
        #   如果你想每回合都自动播一遍刚打的片段，就把 auto_replay 打开
        #   —— 但它现在只会播**你手动裁好的那一段**，不会再自己去碰缓冲。
        log(f"   本回合评分 {score}（{', '.join(reasons) or '平淡'}）——仅供参考"
            + ("　（想回放：按 ← 标入点 → 按 → 标出点 → 小键盘 Enter 播放）"
               if not cfg.get("auto_replay", False) else ""))

        # 默认手动模式：不自动判断、不自动切画面。
        if not cfg.get("auto_replay", False):
            self.counters["skipped"] += 1
            return

        if not self.auto_enabled:
            self.counters["skipped"] += 1
            log(f"   → 不自动回放（当前是 --manual-only 手动模式，引擎只记录决策）"
                f"{'，如果自动跑会回放' if score >= cfg['min_score'] else ''}")
            return
        if self.locked:
            self.counters["skipped"] += 1
            log(f"   → 跳过（已锁定：{self.locked_reason}）")
            return
        if self.replay_active:
            self.counters["skipped"] += 1
            log("   → 跳过（上一次回放还没结束）")
            return
        if score < cfg["min_score"]:
            self.counters["skipped"] += 1
            log(f"   → 跳过（评分 {score} < 阈值 {cfg['min_score']}）")
            return

        # 和 Astra 的分工：只有当我们正好在直播场景上时才插回放。
        # Astra 把画面切到"中场休息/数据看板/地图"等场景时，引擎自动让位，不抢场景。
        if cfg.get("require_live_scene", True):
            cur = self._current_scene
            if cur is None:
                try:
                    cur = self.obs.scene_list()[1]
                    self._current_scene = cur
                except Exception:
                    cur = None
            if cur != cfg["live_scene"]:
                self.counters["skipped"] += 1
                log(f"   → 跳过（当前在「{cur}」而不是直播场景「{cfg['live_scene']}」"
                    f"—— 阶段切换交给 Astra，引擎不抢）")
                return
        if self.last_replay_round is not None:
            gap = round_no - self.last_replay_round
            if gap <= cfg["cooldown_rounds"]:
                self.counters["skipped"] += 1
                log(f"   → 跳过（冷却中，距上次回放 {gap} 回合）")
                return

        self._start_replay(f"回合 {round_no}: {', '.join(reasons)}", round_no)

    def manual_replay(self):
        if self.replay_active:
            log("手动回放被拒绝：已经在回放中")
            return False
        self._start_replay("手动触发", None)
        return True

    # ---------------- 回放生命周期 ----------------
    def _start_replay(self, why, round_no=None, hold_seconds=None):
        """
        把"选哪段素材 + 算播放时长 + 真正开播"整段放进 _capture_lock。

        ⚠️ 为什么必须这样：
          裁素材（_load_replay）要读 OBS 日志才能知道素材多长，要花 0.5~1 秒。
          如果这期间另一条线程（你按 Enter）开始播放，它会读到**上一次的旧长度**，
          于是按旧长度算播放时长 → 素材 12 秒却只播 7 秒，精彩的部分被砍掉。
          （这是实测到的真实故障，不是你操作问题。）
        """
        with self._capture_lock:
            # --- 1. 用哪段素材：只有按 ← / → 手动裁出来的那一段 ---
            #   ★ 2026-10-06 用户决定：引擎**不再按需定格**。手里没有裁好的片段就
            #     明确让你先按 ← / →，而不是偷偷去抓一段你没框过的画面。
            if not self._snapshot_ok:
                log("   ⚠️ 还没有裁好的片段 —— 先按【←】标入点，打完了按【→】标出点，"
                    "再按【小键盘 Enter】播放")
                return
            age = time.time() - self._last_snapshot_at
            clip_len = self.last_clip_seconds
            log(f"   ✔ 播放已裁好的片段（{age:.0f} 秒前裁的"
                + (f"，{clip_len:.1f} 秒" if clip_len else "") + "）")
            try:
                if self.backend_name == "plugin":
                    self._hk("ReplaySource.Last")
                    log("   ✔ 已选中最新一段素材")
                # obs 后端不用选段：手里就是刚裁好的那个文件
            except Exception as e:
                logv(self.cfg, f"   （ReplaySource.Last 失败，忽略: {e}）")

            # --- 2. 用**锁内读到的最新长度**算播放时长 ---
            clip = hold_seconds if hold_seconds is not None else self.last_clip_seconds
            hold = self.expected_hold
            if not hold:
                # preflight 没跑成（很少见）→ 自己按配置算一个，
                # 绝不能留 0 秒：那样 ticker 会在 5 秒后就把长回放掐掉。
                hold = min(float(self.cfg["record_max_seconds"]) / max(self.cfg["speed"], 0.05),
                           self.cfg["max_hold"])
            if clip:
                play = clip / max(self.cfg["speed"], 0.05)
                back = float(self.cfg.get("return_before_end", 0.0))
                back = min(back, max(0.0, play * 0.35))
                hold = max(0.6, play - back)
                log(f"   本次素材 {clip:.2f} 秒 @ {self.cfg['speed']*100:.0f}% 速度"
                    f" → 播 {play:.1f} 秒"
                    + (f"，在剩 {back:.1f} 秒时切回" if back > 0.05 else
                       ("，播完由引擎切回" if self.backend_name == "obs" else "，播完由插件切回")))
            else:
                # ★ 长度未知时**不要往短了猜** —— 猜短了片子会被腰斩（实测过）。
                #   按素材上限给足时间，精确收尾交给插件的 next_scene。
                log(f"   素材长度未知 → 按上限给足 {hold:.1f} 秒，由插件精确收尾")

            self.replay_active = True
            self._started_at = time.time()
            self._phase_at_start = self.state_ref.phase if self.state_ref else None
            # ★ 包装转场（stinger）会盖住回放的片头 —— 这段时间播放窗口也要往后推。
            wrap = self._wrap_seconds()
            # obs 后端：把裁好的文件喂给媒体源并开播（返回实际要播多久）
            obs_hold = None
            if self.backend_name == "obs":
                obs_hold = self.backend.play_clip()
                if not obs_hold:
                    self.replay_active = False
                    log("   ⚠️ obs 后端播放失败，取消这次回放")
                    return
                hold = obs_hold
            # 播放窗口 += 5 秒安全垫：正常情况下插件会在片子结束那一帧就切回去，
            # 这个是"插件没生效"时的兜底。
            self.replay_until = time.time() + wrap + hold + 5.0
            self.replay_hard_deadline = self.replay_until + 10.0
            self.last_replay_round = round_no if round_no is not None else self.last_replay_round
            self.counters["replays"] += 1
            log(f"🎬 开始回放 —— {why}")

        # 时长体检：把预计占用时间和实测的"死时间窗口"比一比（只提醒前 3 次，避免刷屏）
        if self.state_ref is not None and self.counters.get("hold_warnings", 0) < 3:
            win = self.state_ref.dead_window_avg()
            if win is not None and self.expected_hold > win:
                self.counters["hold_warnings"] = self.counters.get("hold_warnings", 0) + 1
                log(f"   ⚠️  预计占用 {self.expected_hold:.1f}s > 实测可用窗口 {win:.1f}s，"
                    f"可能会盖住下一回合的开局。")
                log(f"       建议：把 Replay Source 的 Duration 降到约 "
                    f"{max(1.0, (win - self.cfg['return_margin']) * self.cfg['speed']):.1f}s，"
                    f"或把速度提到 {min(1.0, self.cfg['capture_seconds'] / max(win - self.cfg['return_margin'], 0.5)):.2f}，"
                    f"或把 min_score 调高、少放几个回放。")

        try:
            # ★ 记下"按播放键时人在哪个场景"，播完切回**这里**（不一定非得是 HUD）。
            #   你/ Astra 把画面放在「数据看板」时按播放，播完硬切回 HUD 会很突兀。
            try:
                cur = self.obs.scene_list()[1]
            except Exception:
                cur = self._current_scene
            if cur and cur != self.cfg["replay_scene"]:
                self._scene_before_replay = cur
            target = self._return_scene()
            # 让插件在片子结束那一帧切回去（引擎掐时间只是兜底）
            if self.backend_name == "plugin":
                self._set_plugin_next_scene(target)
            if target != self.cfg["live_scene"]:
                log(f"   [OBS] 播完会切回「{target}」（你是在这个场景上按的播放）")
            # 切场景 + 显示回放源。Visibility Action=Restart 会让它从头开始播。
            if cur != self.cfg["replay_scene"]:
                self._switch_program(self.cfg["replay_scene"])
            else:
                self._current_scene = self.cfg["replay_scene"]
            if self.backend_name == "plugin":
                self.obs.set_item_enabled(self.cfg["replay_scene"], self.replay_item_id, True)
            for name, iid in self.overlay_item_ids:
                self.obs.set_item_enabled(self.cfg["replay_scene"], iid, True)
            # ★ 立刻把回放定格在第一帧，等包装转场放完再开播（片头一帧不丢）。
            #   插件行为（replay-source.c:1373）：`restart` 处理时
            #   `pause_timestamp = c->play ? 0 : os_timestamp` —— 所以"显示后马上暂停"
            #   通常正好停在片段第一帧；就算晚了几十毫秒，也只差 1~2 帧。
            #   暂停是**切换式**的（:722-757），这里必须是 RESTART/播放中的状态才安全，
            #   而 set_item_enabled(True) 触发 visibility_action=Restart 正是这个状态。
            self._wrap_resume_at = 0.0
            if wrap > 0.05:
                used_ms = self._freeze_first_frame()
                self._wrap_resume_at = time.time() + wrap
                log(f"   🎁 包装转场 {wrap:.2f} 秒：回放先定格在第一帧"
                    f"（暂停耗时 {used_ms:.0f}ms），{wrap:.2f} 秒后自动续播")
                lim = float(self.cfg.get("wrap_pause_max_ms", 250) or 250)
                if used_ms > lim:
                    log(f"      ⚠️ 暂停调用用了 {used_ms:.0f}ms（>{lim:.0f}ms），"
                        f"片头可能已经跑掉约 {used_ms/1000:.2f} 秒")
        except Exception:
            log("!! 启动回放失败:\n" + traceback.format_exc())
            self._end_replay("启动失败")

    # ------------------------------------------------------------------
    def _hk(self, name):
        """触发 Replay Source 的源级热键（必须带 contextName）。"""
        self.obs.request("TriggerHotkeyByName",
                         {"hotkeyName": name, "contextName": self.cfg["replay_item"]})

    # ------------------------------------------------------------------
    def _active_input(self):
        """当前后端"正在播的那个输入源"名字（插件=回放源；obs 后端=媒体源）。"""
        if self.backend_name == "obs":
            return str(self.cfg.get("replay_media_item") or "")
        return str(self.cfg.get("replay_item") or "")

    def _media_state(self):
        """读回放源/媒体源的媒体状态；读不到返回 None（老版本 obs-websocket 或请求失败）。"""
        try:
            d = self.obs.media_input_status(self._active_input())
            return d.get("mediaState")
        except Exception as e:
            logv(self.cfg, f"   （读媒体状态失败，忽略: {e}）")
            return None

    def _wait_media_state(self, want, timeout=0.5):
        """轮询等媒体状态变成 want 里的某一个；超时返回最后读到的状态（读不到返回 None）。"""
        st = None
        deadline = time.time() + max(0.0, timeout)
        while True:
            st = self._media_state()
            if st is None or st in want or time.time() >= deadline:
                return st
            time.sleep(0.05)

    def _freeze_first_frame(self):
        """显示回放源之后，让它停在片段第一帧上（等包装转场放完再续播）。

        插件语义（replay-source.c）：
          * `replay_source_active`（:637-655）：源变可见时 visibility_action=Restart(0)
            → `play = true; restart = true` —— 一露头就开始播。
          * `ReplaySource.Pause` 热键 = `replay_play_pause(data, true)`（:768-775）
            是**切换式**的（:722-757）：正在播 → 暂停；已经暂停 → 开播。
        所以这里先读一眼状态，确认它确实在播再按 Pause（盲按有可能反而把它播起来）；
        读不到状态（老 OBS）就按老办法直接按一下。
        """
        st = self._wait_media_state(("OBS_MEDIA_STATE_PLAYING",), timeout=0.5)
        t0 = time.time()
        if st is None or st == "OBS_MEDIA_STATE_PLAYING":
            try:
                if self.backend_name == "obs":
                    # 媒体源直接用媒体接口暂停（就是"定格在第一帧"）
                    self.obs.trigger_media_action(self._active_input(), MEDIA_ACTION_PAUSE)
                else:
                    self._hk("ReplaySource.Pause")
            except Exception as e:
                log(f"   ⚠️ 定格第一帧失败（片头可能被转场盖住）: {e}")
                return 0.0
        else:
            logv(self.cfg, f"   （回放源现在已经是 {st}，不用按 Pause）")
        return (time.time() - t0) * 1000.0

    def _resume_playback(self):
        """包装转场放完了 → 让回放**从定格那一帧继续播**。

        ⚠️ 2026-10-06 真机实测修正（用户："你那截的啥？根本就不是我标记的那一段"）：
        以前这里按的是 `ReplaySource.Replay`，那是插件的 "Load replay"
        （replay-source.c:1233 `replay_hotkey` → `replay_retrieve`）——**会重新从滚动缓冲
        取一段**，而 `start_delay` 还是原来那个值，于是素材尾巴变成"按下的那一刻"，
        播出来整段后移（实测 21:43 那次：标记的是 21:43:12~14，播出来的是 21:43:44~46）。
        正确通道是媒体的 PLAY：插件 `replay_play_pause(data, false)` 会把
        `start_timestamp` 按暂停时长补回去 → 接着定格那一帧往下播，不重新取素材。
        """
        try:
            self.obs.trigger_media_action(self._active_input(), MEDIA_ACTION_PLAY)
            return True
        except Exception as e:
            if self.backend_name == "obs":
                log(f"   ⚠️ 媒体接口 PLAY 失败（{e}）—— obs 后端没有插件热键可退，"
                    f"改用 RESTART 从头播这一段")
                try:
                    self.obs.trigger_media_action(self._active_input(), MEDIA_ACTION_RESTART)
                    return True
                except Exception as e2:
                    log(f"!! 续播失败: {e2}")
                    return False
            # 老 obs-websocket 没有媒体接口时的退路：Restart 是"同一段素材从头播"，
            # 也**不会**重新取素材，内容仍然是你框的那一段（只是不吃定格那一下）。
            log(f"   ⚠️ 媒体接口 PLAY 失败（{e}）→ 改用插件 Restart（同一段素材从头播）")
            try:
                self._hk("ReplaySource.Restart")
                return True
            except Exception:
                log("!! 恢复回放播放失败:\n" + traceback.format_exc())
                return False

    # ------------------------------------------------------------------
    def _detect_wrap(self):
        """问 OBS 当前转场：是 stinger 就量出它的时长（= 会盖住回放片头多久）。

        实测（本机 OBS 32.2.2）：
          * `GetCurrentSceneTransition` 返回
            {transitionName:"转场", transitionKind:"obs_stinger_transition",
             transitionFixed:true, transitionDuration:null,
             transitionSettings:{path:"J:/比赛包装/01_Logo_transition_long.mov",
                                 transition_point:1000, preload:true}}
          * stinger 的时长 API **问不出来**（transitionFixed=true → Duration 是 null），
            所以直接读那个 .mov 的文件头量（见 mov_duration）。
          * 普通转场（淡入淡出/剪切）不需要等待 —— 它们不会挡住进来的画面。
        """
        try:
            d = self.obs.request("GetCurrentSceneTransition") or {}
        except Exception as e:
            logv(self.cfg, f"   （读当前转场失败，不做包装等待: {e}）")
            return 0.0
        kind = str(d.get("transitionKind") or "")
        name = str(d.get("transitionName") or "")
        if "stinger" not in kind.lower():
            log(f"   当前转场「{name}」不是 stinger 包装转场 → 回放不需要等转场")
            return 0.0
        st = d.get("transitionSettings") or {}
        path = str(st.get("path") or "")
        dur = mov_duration(path) if path else None
        if dur:
            base = os.path.basename(path)
            vis = stinger_visible_seconds(path, dur, st.get("transition_point"))
            if vis is not None and vis < dur - 0.08:
                log(f"   当前转场「{name}」是 stinger，素材 {base} 时长 {dur:.2f} 秒，"
                    f"但动画在 {vis:.2f} 秒就放完了（最后 {dur - vis:.2f} 秒是全透明帧，"
                    f"那段时间观众看到的就是定格的回放）→ 回放只等 {vis:.2f} 秒就开播")
                return float(vis)
            log(f"   当前转场「{name}」是 stinger，素材 {base} "
                f"实测 {dur:.2f} 秒 → 回放片头会等它放完才开播")
            return dur
        # 量不出来时退一步：有些导出会把时长塞进 transitionDuration
        ms = None
        try:
            ms = d.get("transitionDuration")
        except Exception:
            ms = None
        if ms:
            log(f"   当前转场「{name}」是 stinger，量不出素材时长，"
                f"按 OBS 报的 {float(ms)/1000:.2f} 秒等")
            return float(ms) / 1000.0
        if ms == 0 or not path:
            # 仿真 / 还没配 stinger 素材：OBS 说时长是 0，那就没什么可等的
            logv(self.cfg, f"   当前转场「{name}」是 stinger，但时长报 0 → 不做等待")
            return 0.0
        log(f"   ⚠️ 当前转场「{name}」是 stinger，但量不出素材时长"
            f"（path={path}）→ 不做等待；要手动指定就设 replay_wrap_seconds")
        return 0.0

    def _wrap_seconds(self):
        """这次回放要等多久才开播（0 = 立刻播，和以前一样）。"""
        v = self.cfg.get("replay_wrap_seconds")
        if v is not None:
            try:
                return max(0.0, float(v))
            except Exception:
                return 0.0
        if self._wrap_cache is None:
            self._wrap_cache = self._detect_wrap()
        return self._wrap_cache

    # ------------------------------------------------------------------
    def save_replay(self):
        """小键盘 6：把当前那段素材另存成一个视频文件。

        插件源码依据（obs-replay-source 1.8.1 `replay-source.c`）：
          * 热键 `ReplaySource.Save`（:1249-1260）只把 saving_status 置为 STARTING，
            真正写盘在渲染线程的 `replay_save()`（:914-1056）里做。
          * `:916-919` 有个守卫：`video_frame_count == 0` 就直接返回 ——
            **还没裁过素材时按一下什么都不会发生**（安全，不会崩，也不会留半个文件）。
          * `:925` 存的是 `current_replay`（也就是你按 Enter 会播的那一段），
            `:1024-1034` 会照旧应用 trim_front → 存出来的和回放里看到的一样
            （所以留档的视频也是"从你按 ← 那个入点开始"的）。
          * `:1010` 会往 OBS 日志写 `[replay_source: 'Replay Source'] start saving '<文件>'`，
            所以这里用"字节偏移读日志新行"的办法把完整路径回显给你。
        """
        if self.backend_name == "obs":
            # obs 后端：手里那个裁好的文件就是留档，引擎自己另存一份到 save_dir
            log("💾 【小键盘 6 保留片段】obs 后端：把裁好的文件另存一份")
            p = self.backend.archive_clip()
            if p:
                self._report_save_size(p, self.last_clip_seconds)
            else:
                log("   ⚠️ 没有可另存的片段 —— 先按【←】标入点、【→】标出点裁一段。")
            return
        try:
            mark = obs_log_mark()
            self._hk("ReplaySource.Save")
        except Exception as e:
            log(f"❌ 【小键盘 6 保留片段】失败: {e}")
            return
        log("💾 【小键盘 6 保留片段】已让 OBS 把这段素材写成文件")
        if not self._snapshot_ok:
            log("   ℹ️ 这次会话还没裁过素材（没按过 →）。缓冲是插件自己在持续累积的，"
                "所以通常照样能存（实测：没裁过也能存出 8.5MB 的片段）——")
            log("      下面没出现「✔ 已开始写盘」才是真没素材。")
        threading.Thread(target=self._report_save, args=(mark,), daemon=True).start()

    def _report_save(self, mark):
        """等 OBS 日志里出现 `start saving '<文件>'`，把完整路径打出来。

        2026-10-07 追加：**体积体检 +（可选）后处理**。用户问"为什么视频这么大？
        相机 10 秒 4K 也没这么大啊，编码有问题吧" —— 插件其实只有两条编码通路
        （源码 `replay-source.c:777-806`，逐行核对过）：

          * **非无损（默认，引擎会强制）**：`ffmpeg_muxer` 输出 **`.flv`**，
            视频 `obs_x264` **CRF 23 / preset veryfast / profile high**，
            音频 `ffmpeg_aac`。CRF 是"保质量不保码率"，4K60 复杂画面典型
            十几到几十 Mbps —— 和手机 4K60（≈50 Mbps）同量级。
          * **无损（Lossless 开着）**：`ffmpeg_output` + **utvideo**（无损帧内）
            + `pcm_s16le`→ **`.avi`**。4K60 大约 100~250 MB/s，
            10 秒就是 **1~2.5 GB**，比手机视频大几十倍。

        所以："文件是不是 `.avi`" 基本就能判定是不是踩了 lossless（这里会自动纠回），
        而 `.flv` 的话会把实测码率算出来跟手机对比，让人自己判断。
        """
        for _ in range(40):          # 最多等 ~12 秒
            time.sleep(0.3)
            for l in obs_log_since(mark, limit=200):
                m = re.search(r"start saving '([^']+)'", l)
                if m:
                    path = m.group(1)
                    n = self.last_clip_seconds
                    log(f"   ✔ 已开始写盘：{path}")
                    if n:
                        log(f"     （这段约 {n:.1f} 秒，写完要等同样长的时间，"
                            f"期间别关 OBS）")
                    threading.Thread(target=self._postprocess_save,
                                     args=(path, n), daemon=True).start()
                    return
        log("   ⚠️ 没在 OBS 日志里看到 'start saving' —— 这一按没有可存的素材。")
        log("      缓冲是插件自己在累积的，只有 OBS 刚启动 / 刚 Enable 过缓冲时才会是空的：")
        log("      先按【←】标入点、【→】标出点裁一段出来，再按小键盘 6 保留。")

    # ------------------------------------------------------------------
    # 保存后的体积体检（+ 可选后处理）
    # ------------------------------------------------------------------
    def _wait_file_done(self, path, seconds, timeout_extra=8.0):
        """等文件写完：大小连续两次采样不变就算完（写盘是实时的）。"""
        try:
            wait_max = max(5.0, float(seconds or 0.0) + timeout_extra)
        except Exception:
            wait_max = 20.0
        t0 = time.time()
        last, stable, done = -1, 0, False
        while time.time() - t0 < wait_max:
            time.sleep(0.5)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > 0 and size == last:
                stable += 1
                if stable >= 2:
                    done = True
                    break
            else:
                stable = 0
            last = size
        try:
            return os.path.getsize(path), done
        except OSError:
            return 0, False

    def _report_save_size(self, path, seconds):
        """把"这个文件多大、多少 Mbps、和手机比怎么样"打出来。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        ext = os.path.splitext(path)[1].lower()
        mb = size / 1e6
        mbps = (size * 8 / 1e6 / seconds) if seconds and seconds > 0.1 else None
        log(f"   📦 文件体积：{mb:.1f} MB" + (f"（{seconds:.1f} 秒）" if seconds else ""))
        if ext == ".avi":
            log(f"   ❌ 这是 **.avi 无损（utvideo）** 写的 —— 体积是 H.264 的几十倍！")
            log("      原因：Replay Source 插件里的「Lossless」被打开了。")
            log("      修法：设置窗口 / OBS 里把 Lossless 关掉（引擎每次启动也会自动纠回），"
                "关掉后同样一段通常只有几 MB ~ 几十 MB。")
            self._fix_lossless()
            return size
        if mbps:
            # 手机 4K60 大概 50 Mbps 上下，4K30 约 25 Mbps —— 给一个能直接对比的参照
            who = ("OBS 自己的录像编码器（你设的码率/画质）"
                   if self.backend_name == "obs" else "H.264 CRF23 veryfast，插件写死的参数")
            log(f"   📊 码率约 {mbps:.1f} Mbps（{who}）")
            if mbps > 80:
                log("      比手机 4K60（≈50 Mbps）还高：CRF 是「保质量不保码率」，"
                    "画布越大、画面越乱就越大。")
                log("      想小一点：把 record_max_seconds 调小（片段短了自然小）、"
                    "画布降到 1080p，或把 config.json 的 save_postprocess 打开（见下）。")
            else:
                log("      参照：手机 4K60 视频约 50 Mbps、4K30 约 25 Mbps —— 这个量级是正常的。")
        keep = self.cfg.get("save_postprocess") or "off"
        if keep != "off":
            ff = shutil.which("ffmpeg")
            if not ff:
                log(f"   ⚠️ save_postprocess={keep} 但没找到 ffmpeg —— 跳过（原文件照旧可用）")
            else:
                log(f"   🛠 后处理（save_postprocess={keep}）：{os.path.basename(path)}")
        return size

    def _fix_lossless(self):
        """把插件里的 Lossless 关掉（它是 .avi 巨大的唯一原因）。"""
        try:
            _, s = self.obs.input_settings(self.cfg["replay_item"])
            if int(s.get("lossless") or 0):
                self.obs.request("SetInputSettings",
                                 {"inputName": self.cfg["replay_item"],
                                  "inputSettings": {"lossless": False}, "overwrite": False})
                log("      ✔ 已把插件的 Lossless 关掉（下一次保存就是 .flv H.264）")
        except Exception as e:
            log(f"      （自动关 Lossless 失败：{e}；请在 OBS 里手动关）")

    def _postprocess_save(self, path, seconds):
        """保存完成后的体积体检 +（可选）后处理。

        `save_postprocess`：
          * `off`（默认）—— 什么都不做，只报体积/码率；
          * `remux`    —— 只换容器：`.flv` → `.mp4`（`-c copy`，零质量损失、秒级完成）；
          * `reencode` —— 用 libx264 CRF `save_postprocess_crf`（默认 26）重编码压小，
                         慢一些；原来的 .flv 按 `save_keep_original` 决定留不留。
        """
        size, done = self._wait_file_done(path, seconds)
        if not size:
            return
        if not done:
            log(f"   （{os.path.basename(path)} 还在写，跳过体积体检）")
            return
        self._report_save_size(path, seconds)

        mode = str(self.cfg.get("save_postprocess") or "off").lower()
        if mode == "off":
            return
        ff = shutil.which("ffmpeg")
        if not ff:
            return
        base = os.path.splitext(path)[0]
        if mode == "remux":
            out = base + ".mp4"
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                   "-c", "copy", "-movflags", "+faststart", out]
        elif mode == "reencode":
            try:
                crf = float(self.cfg.get("save_postprocess_crf", 26) or 26)
            except Exception:
                crf = 26.0
            out = base + "_small.mp4"
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                   "-c:v", "libx264", "-crf", f"{crf:g}", "-preset", "veryfast",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
        else:
            log(f"   ⚠️ save_postprocess={mode} 不认识（只能是 off / remux / reencode）")
            return
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
        except Exception as e:
            log(f"   ⚠️ 后处理失败（{e}）—— 原文件没动，照旧可用")
            return
        if r.returncode != 0 or not os.path.exists(out):
            log(f"   ⚠️ 后处理失败（ffmpeg 退出码 {r.returncode}）："
                f"{(r.stderr or '').strip()[:200]}")
            log("      原文件没动，照旧可用。")
            return
        try:
            new_mb = os.path.getsize(out) / 1e6
        except OSError:
            new_mb = 0
        log(f"   ✅ 后处理完成（{time.time() - t0:.1f} 秒）：{os.path.basename(out)}"
            f"（{new_mb:.1f} MB）")
        if mode == "reencode" and not self.cfg.get("save_keep_original", False):
            try:
                os.remove(path)
                log(f"      （已删掉原来的 {os.path.basename(path)}；"
                    f"想留着就把 config.json 的 save_keep_original 设成 true）")
            except OSError as e:
                log(f"      （原文件删除失败：{e}）")

    # ------------------------------------------------------------------
    # 回放功能开关 / 内存模式（2026-10-07）
    # ------------------------------------------------------------------
    # 插件侧依据（replay-source.c，2026-10-07 逐行核对，结论写进 docs/技术笔记.md §1.14）：
    #   * `ReplaySource.Disable` 热键 → `c->disabled = true; obs_source_update()` →
    #     把 `replay_filter` 从采集源上摘掉（:2012-2044）→ **滚动缓冲那几 GB 真的还回去**；
    #   * `ReplaySource.Enable` 重新挂上滤镜、缓冲从零开始累积（:2092-2114）；
    #   * `Load replay` 把滤镜里的帧**搬**到"已取出的那一段"（:1108-1112），
    #     所以取出之后滤镜是空的、重新累积 —— 稳态才是两份。
    #   * `ReplaySource.Clear` 清掉所有已取出的段并释放帧（:1610-1630）。
    def memory_info(self, canvas=None):
        """算出这台机器上回放插件到底占多少内存（能读 OBS 就读真画布）。

        ⚠️ 画布只查一次并缓存 —— 手机页面每个 GSI 包都会调这里（10Hz），
           不能每个包都去问一次 OBS。
        """
        cfg = self.cfg
        w, h, fps = (canvas or (None, None, None))
        if not w or not h or not fps:
            cached = getattr(self, "_canvas_cache", None)
            if cached:
                w, h, fps = cached
            else:
                try:
                    vs = self.obs.video_settings() or {}
                    w = vs.get("baseWidth") or vs.get("outputWidth") or DEFAULT_CANVAS[0]
                    h = vs.get("baseHeight") or vs.get("outputHeight") or DEFAULT_CANVAS[1]
                    num, den = vs.get("fpsNumerator"), vs.get("fpsDenominator")
                    fps = (float(num) / float(den)) if num and den else DEFAULT_CANVAS[2]
                except Exception:
                    w, h, fps = DEFAULT_CANVAS
                self._canvas_cache = (w, h, fps)
        seconds = float(cfg.get("record_max_seconds", 10.0) or 10.0)
        copies = 1.0 if self.replay_mode == "armed" else 2.0
        info = estimate_replay_memory(w, h, fps, seconds, copies)
        info["mode"] = self.replay_mode
        info["enabled"] = bool(self.replay_enabled)
        info["armed"] = bool(self._capture_armed_at and self._capture_disabled is False)
        self._memory_info = info
        return info

    def _log_memory(self, prefix="   "):
        info = self.memory_info()
        if self.replay_mode == "armed":
            now_txt = (f"当前正在攒帧（约 {info['one_copy_gb']:.2f} GB + 已取出那份）"
                       if info["armed"] else "当前空闲（≈0）")
            log(f"{prefix}🧠 内存：{info['hint']}")
            log(f"{prefix}   省内存模式（armed）：按 ← 才开始攒帧，裁完立即释放 → {now_txt}")
        else:
            log(f"{prefix}🧠 内存：{info['hint']}（常驻缓冲模式：这个数字一直占着）")
        return info

    def arm_capture(self, reason=""):
        """让**当前后端**现在开始攒（插件 `Enable` / OBS `StartReplayBuffer`）。

        ⚠️ 插件后端只有 armed 模式才这么干（buffer 模式是 preflight 里一次性 Enable
           之后插件自己一直攒）；**obs 后端两种模式都必须走 StartReplayBuffer** ——
           真机踩过：buffer 模式下这里被 `replay_mode != "armed"` 挡掉，导致按 → 时
           `SaveReplayBuffer` 直接报 501，一整段素材废掉。所以 obs 后端要在模式判断**之前**处理。
        """
        if self.backend_name == "obs":
            if not self.obs_backend_ready:
                return False
            self._capture_armed_at = time.time()
            return self.backend.arm(reason)
        if self.replay_mode != "armed":
            return False
        if self._capture_disabled is False:      # 已经在攒，不用重复 Enable
            return False
        try:
            self._hk("ReplaySource.Enable")
            self._capture_disabled = False
            self._capture_armed_at = time.time()
            dur = float(self.cfg.get("record_max_seconds", 10.0) or 10.0)
            log(f"   ▶️ 插件开始攒帧（{reason or '省内存模式'}）—— "
                f"片段从这一刻开始，最多能攒 {dur:.0f} 秒")
            return True
        except Exception as e:
            log(f"   ⚠️ 让插件开始攒帧失败（{e}）—— 这次可能裁不出东西")
            return False

    def release_capture(self, reason=""):
        """省内存模式：把滚动缓冲摘掉（Disable），那几 GB 真的还回去。

        ⚠️ 只动"滚动缓冲"，**不动**已经取出来的那一段 —— 所以裁完立刻释放，
           手里那段照样能播、能按小键盘 6 存盘。
        """
        if self.backend_name == "obs":
            if not self.obs_backend_ready:
                return False
            self._capture_armed_at = 0.0
            return self.backend.release(reason)
        if self._capture_disabled is True:
            return False
        info = self.memory_info()
        try:
            self._hk("ReplaySource.Disable")
            self._capture_disabled = True
            self._capture_armed_at = 0.0
            log(f"   ⏏  已收起滚动缓冲（{reason or '省内存模式'}）—— "
                f"约省下 {info['one_copy_gb']:.2f} GB，手里那段片段不受影响")
            return True
        except Exception as e:
            log(f"   ⚠️ 收起滚动缓冲失败（{e}）")
            return False

    def clear_replays(self, reason=""):
        """清掉内存里"已取出的片段"—— 彻底还内存，但就不能再播/存了。"""
        if self.backend_name == "obs":
            # obs 后端没有"内存里的片段"，只有磁盘上那个裁好的文件
            self._snapshot_ok = False
            try:
                self.backend.clip_path = ""
                self.backend.clip_seconds = 0.0
            except Exception:
                pass
            log(f"   🧹 已清掉手里的片段引用（{reason or '释放内存'}）"
                f"（obs 后端：文件还在磁盘上，没有删）")
            return True
        info = self.memory_info()
        try:
            self._hk("ReplaySource.Clear")
            self._snapshot_ok = False
            self.last_clip_seconds = None
            self.last_clip_raw_seconds = None
            log(f"   🧹 已清掉内存里保留的片段（{reason or '释放内存'}）—— "
                f"约省下 {info['one_copy_gb']:.2f} GB")
            return True
        except Exception as e:
            log(f"   ⚠️ 清片段失败（{e}）")
            return False

    def set_replay_enabled(self, on, persist=True, reason=""):
        """回放功能总开关（默认关）。关掉时顺便把插件内存还回去。

        关掉**不影响**手机提示器与自动切观察位 —— 那两件事和回放是并列的。
        """
        on = bool(on)
        if on and not self.replay_enabled:
            # ★ 打开回放前先过内存护栏：这台机器装不下就直接不开
            #   （手机页面/图形界面随时能开，不能只靠启动时那一次检查）
            if not self._apply_memory_guard():
                log("🎛  回放功能没有打开：内存护栏判定这台机器装不下回放缓冲。")
                if persist:
                    self.persist_config()
                return self.replay_enabled
        changed = (on != self.replay_enabled)
        self.replay_enabled = on
        self.cfg["replay_enabled"] = on
        if not on:
            if self.replay_active:
                self._end_replay("回放功能已关闭")
            # obs 后端：关回放就把 OBS 的回放缓冲停掉（几十~几百 MB，顺手还回去）；
            # 插件后端仍然是"只有 armed 模式才需要显式 Disable"。
            if self.replay_mode == "armed" or self.backend_name == "obs":
                self.release_capture("回放功能已关闭")
            if self.cfg.get("clear_replay_on_disable", True):
                self.clear_replays("回放功能已关闭")
            if changed:
                log("🎛  回放功能 → 关闭：← / → / 保留片段 / 播放 四个键不再动手；")
                log("    手机提示器与自动切观察位不受影响，照常工作。")
        else:
            if self.replay_mode == "armed":
                if self._capture_disabled is False:
                    self.release_capture("切到省内存模式")
                log("🎛  回放功能 → 开启（省内存模式）：按【←】标入点的那一刻才开始攒帧。")
            else:
                log("🎛  回放功能 → 开启（常驻缓冲模式）："
                    + ("OBS 的回放缓冲一直开着，攒着最近 "
                       if self.backend_name == "obs" else "插件一直攒着最近 ")
                    + f"{float(self.cfg.get('record_max_seconds', 10.0)):.0f} 秒。")
                self.arm_capture("回放功能开启")
        if persist:
            self.persist_config()
        return self.replay_enabled

    def set_replay_mode(self, mode, persist=True):
        """切换内存模式：armed（省内存，默认）/ buffer（常驻滚动缓冲，可回溯）。"""
        mode = str(mode or "").lower()
        if mode not in ("armed", "buffer"):
            return self.replay_mode
        if mode == self.replay_mode:
            return self.replay_mode
        self.replay_mode = mode
        self.cfg["replay_mode"] = mode
        if mode == "armed":
            # 切回省内存：立刻把常驻的那份缓冲摘掉（没在攒帧就不会有事）
            if not self.replay_active:
                self.release_capture("切到省内存模式")
            log("🧠 内存模式 → 省内存：平时不占内存；按 ← 才开始攒帧，"
                "裁完立刻释放，内存里只留最近这一段。")
            log("    ⚠️ 注意：这个模式下 ← 不能往回标了 —— 片段从你按 ← 那一刻开始。")
        else:
            self.arm_capture("切到常驻缓冲模式")
            log("🧠 内存模式 → 常驻缓冲："
                + ("OBS 的回放缓冲一直开着，攒着最近 "
                   if self.backend_name == "obs" else "插件一直攒着最近 ")
                + f"{float(self.cfg.get('record_max_seconds', 10.0)):.0f} 秒，"
                "← 可以标在已经过去的某一刻（代价是这份内存一直占着）。")
        self._log_memory()
        if persist:
            self.persist_config()
        return self.replay_mode

    def persist_config(self):
        """把当前配置写回 config.json（手机页面上改的开关要能记住）。"""
        path = self._config_path or CONFIG_PATH
        if not path:
            return None
        try:
            saved = save_config(self.cfg, path)
            self._config_path = saved
            logv(self.cfg, f"   （已把设置写回 {saved}）")
            return saved
        except Exception as e:
            log(f"   ⚠️ 写回配置失败: {e}")
            return None


    # ------------------------------------------------------------------
    # 素材入点：由 ← 手动标出来，不再是"固定秒数 / 识别击杀"
    # ------------------------------------------------------------------
    def clip_skip_from_mark(self, mark_at):
        """
        算"素材入点该跳过开头多少秒" —— 入点就是导播按【←】的那一刻。

        ★ 2026-10-06 用户决定：素材完全由 ← / → 手动框选。
          `←` 只记一个时刻；真正裁素材发生在按 `→` 的时候，所以这一步要回答：
          "从按 ← 到现在，缓冲里还剩多少、要裁掉多少开头？"

        两种模式（2026-10-07 加，见 cfg["replay_mode"]）：

        **buffer（常驻滚动缓冲，旧行为）**：缓冲里最多 `record_max_seconds` 秒、
        边打边滚，每次裁素材（Load replay）会把它掏空重新累积。于是：
            avail = min(record_max_seconds, now - 上次裁素材的时刻)
            skip  = avail - elapsed          # 入点已经位于缓冲内的这个位置
        入点滚出去了（elapsed > avail）→ 打警告返回 0.0（整段播）。

        **armed（省内存，默认）**：插件是**按 ← 那一刻才 Enable 开始攒帧**的，
        所以缓冲起点就是入点，正常情况下 skip = 0；只有"攒过头"（按 → 太晚、
        超过缓冲上限，开头被覆盖）时才裁掉被覆盖的那一段：
            skip = max(0, elapsed - record_max_seconds)
        """
        cfg = self.cfg
        dur = max(1.0, float(cfg.get("record_max_seconds", 10.0)))
        if self.backend_name == "obs":
            # ★ 真机教训（2026-10-08）：obs 后端的缓冲是 **OBS 自己的回放缓冲**
            #   （时长 = 设置里的「回放缓冲时长」），跟 `record_max_seconds`
            #   （插件的单段上限）没关系。原来这里照抄插件的上限打比方，真机上就出现了
            #   "攒了 12 秒、超过上限 10 秒、从 2.0 秒处开始播"这种**假警告** ——
            #   实际文件 11.9 秒一点没丢，用户只会被吓一跳。
            dur = float(cfg.get("obs_buffer_seconds")
                        or obs_replay_buffer_info().get("seconds")
                        or 20.0)
        now = time.time()
        elapsed = max(0.0, now - float(mark_at or 0.0))
        if self.replay_mode == "armed":
            if elapsed > dur + 0.2:
                over = elapsed - dur
                if self.backend_name == "obs":
                    log(f"   ✂️ 这一段攒了 {elapsed:.1f} 秒、超过 OBS 回放缓冲的 {dur:.0f} 秒 → "
                        f"开头约 {over:.1f} 秒可能已经被滚动覆盖（以文件里实际的帧为准）")
                else:
                    log(f"   ✂️ 这一段攒了 {elapsed:.1f} 秒、超过缓冲上限 {dur:.0f} 秒 → "
                        f"开头 {over:.1f} 秒已经被滚动覆盖，从 {over:.1f} 秒处开始播")
                return over
            return 0.0
        since_last = now - self._last_snapshot_at if self._last_snapshot_at else dur
        avail = min(dur, max(0.0, since_last))
        if elapsed > avail + 0.2:
            log(f"   ⚠️ 入点已经滚出缓冲了（缓冲里只有 {avail:.1f} 秒，"
                f"入点到出点隔了 {elapsed:.1f} 秒）→ 这一段从头播")
            log(f"      下次注意：按完【←】要在 {dur:.0f} 秒内按【→】"
                f"（缓冲最多留 {dur:.0f} 秒）。")
            return 0.0
        return max(0.0, avail - elapsed)


    def mark_window(self):
        """
        给手机提示器用：**现在这个【←】入点还剩多少秒可以按【→】**。

        2026-10-06 真机教训（用户："截取的片段根本不是啊…莫名其妙的一段"）：
        他 21:57:06 按 ←、21:57:19 才按 →（隔了 12.4 秒），入点早就滚出 10 秒滚动缓冲，
        引擎只能退化成"整段缓冲从头播" → 拿到的画面起点比他标的晚了 3 秒、
        而且回合最后两个击杀（21:57:21/23）还没发生 → 看起来就是"莫名其妙的一段"。
        电脑上的日志虽然写了警告，但人在打游戏时不会去看日志，所以要把
        **"还能等几秒"直接顶到手机上**。

        公式和 `clip_skip_from_mark()` 完全一致（缓冲边打边滚，裁一次就掏空重攒）：
            avail = min(record_max_seconds, now - 上次裁素材的时刻)
            left  = avail - (now - 入点时刻)
        left <= 0 → 入点已经滚出缓冲，再按 → 只能拿到最近 avail 秒。
        """
        cfg = self.cfg
        dur = max(1.0, float(cfg.get("record_max_seconds", 10.0)))
        now = time.time()
        if self.replay_mode == "armed":
            # 省内存模式：缓冲起点 = 按 ← 那一刻（Enable 的时刻），所以
            # "还剩几秒能按 →" = 缓冲上限 - 已经过去的时间（超过就开始覆盖开头）。
            since_arm = (now - self._capture_armed_at) if self._capture_armed_at else 0.0
            avail = min(dur, max(0.0, since_arm))
        else:
            since_last = now - self._last_snapshot_at if self._last_snapshot_at else dur
            avail = min(dur, max(0.0, since_last))
        out = {
            "bufMax": round(dur, 1),
            "avail": round(avail, 1),
            "mode": self.replay_mode,
            "enabled": bool(self.replay_enabled),
            "clipReady": bool(self._snapshot_ok),
            "clipAgo": (round(now - self._last_snapshot_at, 1)
                        if self._last_snapshot_at else None),
            "lastClipSec": (round(float(self.last_clip_seconds), 1)
                            if getattr(self, "last_clip_seconds", None) else None),
            "replayActive": bool(self.replay_active),
            "markAgo": None, "markLeft": None, "expired": False,
        }
        if self.mark_in_at:
            elapsed = max(0.0, now - float(self.mark_in_at))
            out["markAgo"] = round(elapsed, 1)
            if self.replay_mode == "armed":
                out["markLeft"] = round(dur - elapsed, 1)
                out["expired"] = elapsed > dur + 0.2
            else:
                out["markLeft"] = round(avail - elapsed, 1)
                out["expired"] = elapsed > avail + 0.2
            out["clipMax"] = round(dur, 1)
        elif self.replay_mode == "armed" and not self._capture_armed_at:
            # 还没按 ←：提示"按 ← 才开始攒帧"
            out["idle"] = True
        return out

    def _apply_start_delay(self, skip):
        """
        把"跳过素材开头 skip 秒"写进 Replay Source 的 StartDelay。

        插件源码依据（obs-replay-source `replay-source.c`）：
          * `replay_retrieve` :1156-1173 —— start_delay 为**负**时
            `new_replay.trim_front = context->start_delay * -1`，即从片段开头裁掉这么多纳秒；
            若 `|trim_front| >= 片段总长`，这个分支不成立 → trim_front 保持 0 → 原样从头播
            （所以算过头了也不会把片子裁没，最多是没裁）。
          * `replay_restart_at_begin` :1382-1395 —— 播放时直接 seek 到
            `first_frame_timestamp + trim_front`，是**真跳**，不是快进。
          * `replay_source_update` :2076 只设 `context->start_delay`；只有 duration /
            sound_trigger / audio_threshold 变化才会 `obs_source_update(filter)`，
            所以随时改 StartDelay 都安全，**不会把攒好的滚动缓冲冲掉**。
        """
        cfg = self.cfg
        try:
            ms = -int(round(max(0.0, skip) * 1000))
            _, s = self.obs.input_settings(cfg["replay_item"])
            if int(s.get("start_delay", 0) or 0) == ms:
                return
            self.obs.request("SetInputSettings",
                             {"inputName": cfg["replay_item"],
                              "inputSettings": {"start_delay": ms},
                              "overlay": True, "overwrite": False})
            # 第一次写的时候自检一次：写进去 → 读回来 → 比对，把结论打进日志。
            # 这样"插件到底认不认这个键"在第一次开播就能看出来，不用猜。
            if not self._start_delay_checked:
                self._start_delay_checked = True
                _, s2 = self.obs.input_settings(cfg["replay_item"])
                got = int(s2.get("start_delay", 0) or 0)
                if got == ms:
                    log(f"   ✔ 入点通路自检通过：StartDelay={ms}ms（插件已确认）")
                else:
                    log(f"   ⚠️ 入点通路自检失败：想写 StartDelay={ms}，读回 {got}"
                        f" → 这次会从素材片头整段播（插件不认这个键）")
        except Exception as e:
            logv(cfg, f"   （设置入点失败，这次从片头播: {e}）")

    def _load_replay(self, round_no=None, skip=0.0):
        """
        触发 Replay Source 的 "Load replay" 热键，把回放滤镜缓存的最近 N 秒
        快照成一段可播放的回放（= 按 → 标出点时"当场把入点到现在裁出来"）。

        `skip` = 从这段素材开头再裁掉多少秒（入点之前那些），由
        `clip_skip_from_mark()` 算好传进来；默认 0 = 整段播。

        实测结论（2026-10-05 在本机 OBS 32.2.2 + replay-source 上验证）：
          * obs-websocket 的 TriggerHotkeyByName **可以**触发源级热键，
            但必须带 contextName = 回放源的名字。
          * 触发后 OBS 日志会出现 `replay added of X seconds`，用**字节偏移**读新行，
            不能用"文本差异"判断（同样的长度文本完全一样，会认错）。
          * ⚠️ 每次快照会"吃掉"滤镜缓冲：紧接着再按一次，只会有很短的一小段。

        整个函数持 self._capture_lock —— 防止"定格还没读完日志，播放就开始了"
        这种竞态（那会导致用旧的素材长度算播放时长，片子被腰斩）。
        """
        with self._capture_lock:
            return self._load_replay_locked(round_no, skip)

    def _load_replay_locked(self, round_no=None, skip=0.0):
        """按当前后端裁片段：plugin = 插件快照 + StartDelay；obs = 保存缓冲 + ffmpeg 裁。"""
        if self.backend_name == "obs":
            return self._load_replay_obs(round_no)
        cfg = self.cfg
        name = cfg.get("load_replay_hotkey") or ""
        if not name:
            return
        # 限流：两次 Load replay 之间至少要隔着 record_max_seconds，否则第二次是残缺的
        gap = time.time() - self._last_load_replay
        need = max(1.0, float(cfg.get("record_max_seconds", 10.0)))
        if self._last_load_replay and gap < need and self.replay_mode != "armed":
            # 省内存模式下"短"是正常的（缓冲从按 ← 那一刻才开始攒），不吓唬用户
            log(f"   ⚠️  距上次裁片只有 {gap:.1f}s（需要 {need:.1f}s），"
                f"这次素材会比上限短（{gap:.1f} 秒左右）。")
        try:
            mark = obs_log_mark()
            # ★ 入点：由 ← 标出来，skip = "入点之前那几秒"。在触发 Load replay
            #   **之前**写进插件的 StartDelay（负值 = trim_front，真裁掉）。
            skip = max(0.0, float(skip or 0.0))
            if skip > 0.05:
                log(f"   ✂️ 从入点开始播（跳过素材开头 {skip:.1f} 秒）")
            else:
                log("   ✂️ 不裁开头（整段缓冲都播）")
            self._apply_start_delay(skip)
            self.obs.request("TriggerHotkeyByName",
                             {"hotkeyName": name, "contextName": cfg["replay_item"]})
            self._last_load_replay = time.time()
            self._last_clip_end_at = self._last_load_replay
            self._last_snapshot_at = self._last_load_replay
            self._snapshot_ok = True
            self.counters["snapshots"] = self.counters.get("snapshots", 0) + 1
            # 从 OBS 日志读**真实**的素材长度，不靠按键时间猜。
            # 用字节偏移读新行（不能用文本差异，见 obs_log_since 的注释）。
            true_len = None
            for _ in range(12):
                time.sleep(0.08)
                for l in obs_log_since(mark):
                    m = re.search(r"replay added of ([\d.]+) seconds", l)
                    if m:
                        true_len = float(m.group(1))
                if true_len is not None:
                    break
            if true_len is not None:
                self.last_clip_raw_seconds = true_len
                # 插件只在 |trim_front| < 片段总长时才真裁，否则原样从头播 → 如实记录。
                applied = skip if 0.05 < skip < true_len else 0.0
                self.last_clip_trim = applied
                playable = max(0.0, true_len - applied)
                self.last_clip_seconds = playable
                want = float(cfg.get("record_max_seconds", 10.0))
                if applied > 0.05:
                    log(f"   ✔ 素材已裁好（OBS 实测 {true_len:.2f} 秒，"
                        f"从入点开始跳过开头 {applied:.1f} 秒 → 可播 {playable:.2f} 秒）")
                else:
                    log(f"   ✔ 素材已裁好（OBS 实测 {true_len:.2f} 秒，整段播）")
                    if skip > 0.05:
                        log(f"      （本来想从 {skip:.1f} 秒处的入点开始，但片子只有 "
                            f"{true_len:.2f} 秒，比入点还短 → 从头播）")
                if true_len < 1.0:
                    log(f"   ❌ 太短了！只有 {true_len:.2f} 秒，几乎看不到东西。")
                    log("      常见原因：")
                    log("        1) 裁片前 OBS 不在 HUD 场景 → 游戏采集没在渲染，缓冲是空的")
                    log("        2) 游戏窗口被最小化 / 游戏没在出画面")
                    log("        3) 刚刚裁过一次，缓冲才刚开始重新累积")
                elif true_len < want * 0.6:
                    log(f"   ⚠️ 比上限 {want:.0f} 秒短不少。如果这不是你刚裁过一次，")
                    log("      检查一下游戏采集是否一直在出画面（别切走 HUD 场景）。")
            else:
                self.last_clip_seconds = None
                self.last_clip_raw_seconds = None
                self.last_clip_trim = 0.0
                log(f"   ✔ 已触发 Load replay（{name} @ {cfg['replay_item']}）")
        except Exception as e:
            log(f"   ⚠️  触发 Load replay 失败: {e}")
            log("      回放里会是空的。可用 --test-load-replay 单独排查。")

    def _load_replay_obs(self, round_no=None):
        """obs 后端"裁片段"：保存 OBS 回放缓冲 → 用 ffmpeg 裁出 (←,→) 那一段。

        和插件后端的区别：插件是内存里搬指针（瞬时），这里是**落盘 + 裁剪**（几百毫秒）。
        入点由 `trim_plan()` 用"文件时长 - 入点到现在"算出来，所以语义和插件一致：
        播出来的就是从你按 ← 那一刻开始的。
        """
        cfg = self.cfg
        mark = self.mark_in_at
        t0 = time.time()
        path, secs = self.backend.capture_clip(mark)
        self._last_load_replay = time.time()
        self._last_clip_end_at = self._last_load_replay
        self._last_snapshot_at = self._last_load_replay
        if path and secs > 0:
            self._snapshot_ok = True
            self.last_clip_seconds = float(secs)
            self.last_clip_raw_seconds = float(secs)
            self.last_clip_trim = 0.0
            self.counters["snapshots"] = self.counters.get("snapshots", 0) + 1
            log(f"   ✔ 素材已裁好（obs 后端：{secs:.1f} 秒，用时 {time.time() - t0:.1f} 秒）")
            if secs < 1.0:
                log("   ❌ 太短了！确认 OBS 的回放缓冲已经跑起来（省内存模式下要先按 ←）。")
        else:
            self._snapshot_ok = False
            self.last_clip_seconds = None
            self.last_clip_raw_seconds = None
            log("   ⚠️ 这一按没拿到素材（obs 后端：缓冲没跑起来 / 没等到保存好的文件）")

    def _end_replay(self, why, switch_back=True):
        if not self.replay_active:
            return
        self.replay_active = False
        # 还在等包装转场就取消掉 —— 别让"待恢复播放"在切回直播后又把回放放起来
        self._wrap_resume_at = 0.0
        log(f"⏹  结束回放（{why}）")
        # ★ 顺序很重要：**先切场景，再隐藏源**。
        #   反过来的话，源先消失、画面会闪一下空帧，看起来就是"卡顿"。
        #   先切场景时，转场会把慢放画面盖住，回放源在被隐藏时已经不在节目输出里了。
        #   切回去的目标是"按播放键时所在的那个场景"（见 _return_scene），
        #   不一定非得是 HUD —— 你可能是在「数据看板」上按的播放键。
        target = self._return_scene()
        try:
            if not switch_back:
                log(f"   [OBS] 画面已经被切到「{self.obs.scene_list()[1]}」，不再抢回来")
            elif self.obs.scene_list()[1] != target:
                self._switch_program(target)
            else:
                log(f"   [OBS] 已经在「{target}」")
        except Exception:
            log("!! 切回直播场景失败:\n" + traceback.format_exc())
        try:
            if self.backend_name == "obs":
                self.backend.hide()
                log("   [OBS] 已隐藏回放媒体源")
            else:
                self.obs.set_item_enabled(self.cfg["replay_scene"], self.replay_item_id, False)
            for name, iid in self.overlay_item_ids:
                self.obs.set_item_enabled(self.cfg["replay_scene"], iid, False)
            if self.backend_name != "obs":
                log("   [OBS] 已隐藏回放源")
        except Exception:
            log("!! 隐藏回放源失败:\n" + traceback.format_exc())

    # ---------------- 时钟线程 ----------------
    def ticker(self):
        while not self._stop:
            time.sleep(0.05)
            # ★ 2026-10-06：这里原来有个"到点自动定格素材"的分支，已按用户要求整段删掉。
            #   现在素材只由 ← / → 手动框选产生。
            # ★ 包装转场放完了 → 让回放真正开始播（见 _start_replay 里的定格第一帧）
            self._resume_after_wrap()
            if self.replay_active and time.time() >= self.replay_until:
                self._end_replay("播放完成")
            elif self.replay_active and time.time() >= self.replay_hard_deadline:
                self.counters["interrupted"] += 1
                self._end_replay("超过硬上限")
            elif (self.replay_active and self.backend_name == "obs"
                  and not self._wrap_resume_at):
                # 素材播完立刻切回（不然观众要盯着最后一帧等 5 秒兜底）
                self._end_when_media_ends()

            # ★ 人工接管的锁必须有自动过期。
            #   实测踩过的大坑：回放期间 Astra 按比赛阶段把场景切到「数据看板」，
            #   引擎判定"回放中被人抢走画面" → 上锁"手动接管优先"，而**解开的唯一途径
            #   是你手动按一下方向键**。结果整场后面每个回合都被跳过定格，按 Enter
            #   只能反复播很早以前那一段素材。现在到点自动交还。
            self._expire_manual_lock()

            # ★ 资源哨兵：每 ~2 秒看一眼可用内存，压力过大就警告、再大就主动
            #   把我们占的滚动缓冲放掉（见 resource_pressure 的说明）。
            self._watch_tick += 1
            if self._watch_tick >= 40:
                self._watch_tick = 0
                self._watch_resources()

    def _watch_resources(self):
        """资源哨兵：内存快没了就先自救（把回放缓冲还回去），别等整机换页卡死。

        为什么放在引擎里：用户现场是**正在直播时整机卡死、直播间也卡死**。
        真到换页风暴那一步，谁都救不了；能救的是"提前把自己占的几 GB 退出来"。
        """
        cfg = self.cfg
        if not cfg.get("resource_watch", True):
            return
        total, avail = system_memory_gb()
        state = resource_pressure(avail, total,
                                  cfg.get("low_mem_warn_pct", 12.0),
                                  cfg.get("low_mem_release_pct", 6.0))
        now = time.time()
        if state == "ok":
            if self._mem_alert and now - self._mem_alert_at > 60:
                log(f"   🧠 内存压力已恢复：可用 {avail:.1f}/{total:.1f} GB")
                self._mem_alert = ""
            return
        if now - self._mem_alert_at < 20:      # 20 秒内不重复刷
            return
        self._mem_alert_at = now
        pct = avail / total * 100 if total else 0
        if state == "warn":
            if self._mem_alert != "warn":
                self._mem_alert = "warn"
                log(f"   ⚠️ 内存吃紧：可用 {avail:.1f}/{total:.1f} GB（{pct:.0f}%）——"
                    f"继续下去可能整机换页卡死（直播也会跟着卡）。")
                log("      建议：把「单段素材上限」调小、关掉回放功能，或看看谁在吃内存。")
            return
        # state == "release"：动手自救
        self._mem_alert = "release"
        log(f"   🚨 内存快没了：可用 {avail:.1f}/{total:.1f} GB（{pct:.0f}%）——"
            f"**主动释放回放缓冲**，避免整机换页卡死（直播要紧）。")
        try:
            if self._capture_disabled is False:
                self.release_capture("内存哨兵：可用内存过低")
                log("      ✔ 已把插件的滚动缓冲放掉（那几 GB 还回去了）；"
                    "硬要再用回放，请先降时长/清内存。")
            else:
                log("      （滚动缓冲本来就没在占；如果你开着常驻缓冲模式，"
                    "建议改成「省内存」或关掉回放功能）")
        except Exception as e:
            log(f"      ⚠️ 释放失败：{e}")

    def _end_when_media_ends(self):
        """obs 后端专用：媒体源一播完就**立刻**切回直播。

        为什么不能只靠 `replay_until` 兜底：那个时间是"素材时长 ÷ 速度 + 5 秒安全垫"，
        真机实测（2026-10-08，5.93 秒素材 @70%）媒体第 8.5 秒就播完了，兜底要到第 14 秒
        才切回 —— 中间 5 秒观众盯着的是**卡住的最后一帧**（媒体源
        `clear_on_media_end=false` 会一直显示最后一帧）。插件后端是插件自己按帧切回，
        没有这个问题；obs 后端得由我们来盯。
        """
        now = time.time()
        if now - self._media_poll_at < 0.5:      # 最多每 0.5 秒问一次 OBS，别刷请求
            return
        self._media_poll_at = now
        if now - self._started_at < 1.0:         # 刚开播，状态可能还是上一次的
            return
        st = self._media_state()
        if st and str(st).endswith("ENDED"):
            log("   ⏹ 素材已经播完 → 立刻切回直播（不等兜底计时）")
            self._end_replay("素材播完")

    def _resume_after_wrap(self):
        """包装转场放完了 → 让回放真正开始播。

        和 `_start_replay` 里的"定格第一帧"配对：那边显示回放源后立刻 Pause，
        这里等 stinger 放完再续播（用媒体接口 PLAY，**绝不能**再按
        `ReplaySource.Replay` —— 那是 Load replay，会重新取一段素材，
        见 `_resume_playback` 的说明）。单独拆成方法是为了能脱离 ticker 死循环做单测。
        """
        if not (self.replay_active and self._wrap_resume_at
                and time.time() >= self._wrap_resume_at):
            return False
        self._wrap_resume_at = 0.0
        if self._resume_playback():
            log("   ▶️ 包装转场放完，回放从定格那一帧继续播（没有重新取素材）")
        return True

    def _expire_manual_lock(self):
        """人工接管的锁到点自动交还（manual_lock_seconds 秒；0 = 永不过期）。"""
        if self.replay_active or not self.locked:
            return False
        ttl = float(self.cfg.get("manual_lock_seconds", 45.0) or 0.0)
        if ttl > 0 and self._locked_at and time.time() - self._locked_at >= ttl:
            self.unlock(f"人工接管已超过 {ttl:.0f} 秒，自动交还自动导播")
            return True
        return False

    def on_gsi_tick(self, phase):
        """
        每个 GSI 包都调用：回放期间一旦新回合开打，立刻让位。

        ⚠️ 这里必须区分两种 "live"：
          * 自动触发时我们在 `over`/`freezetime` 死时间里放回放 → 变成 live 就说明
            新回合开了，必须马上让位，绝不能盖住比赛。
          * 手动在比赛进行中触发回放 → 一开始就是 live，属于导播的有意行为，
            不能一秒钟就被打断，交给计时器收尾。
        """
        if not self.replay_active:
            return
        # 手动模式下默认**不**抢控制权：导播按 Enter 播多久由他自己决定。
        # 自动模式下才需要"新回合开打就立刻让位"。
        interrupt = self.cfg.get("interrupt_on_live")
        if interrupt is None:
            interrupt = bool(self.cfg.get("auto_replay", False))
        if not interrupt:
            return
        if phase == "live" and self._phase_at_start != "live":
            if time.time() - self._started_at >= 0.8:   # 0.8s 宽限，忽略切换瞬间的残包
                self.counters["interrupted"] += 1
                self._end_replay("回合已开始，强制让位")

    def stop(self):
        self._stop = True
        if self.replay_active:
            self._end_replay("程序退出")

    def status(self):
        return {
            "locked": self.locked, "lockedReason": self.locked_reason,
            "replayActive": self.replay_active,
            "replayEnabled": bool(self.replay_enabled),
            "replayMode": self.replay_mode,
            "captureArmed": bool(self._capture_disabled is False),
            "memory": (self._memory_info or {}).get("hint", ""),
            "expectedHold": round(self.expected_hold, 2),
            "currentScene": self._current_scene,
            "clipReady": self._snapshot_ok,
            "clipAgoSec": (round(time.time() - self._last_snapshot_at, 1)
                           if self._last_snapshot_at else None),
            # ★ 按 ← 标了入点之后，这里能看到"入点已经过去多久"（0 秒内按 → 效果最好）
            "markInAgoSec": (round(time.time() - self.mark_in_at, 1)
                             if self.mark_in_at else None),
            "counters": dict(self.counters),
        }


# ============================================================================
# 4.5 手动键盘控制（全局热键，只读不拦截）
# ============================================================================

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_EXTENDED = 0x01
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_RETURN = 0x0D
VK_NUMPAD6 = 0x66          # 小键盘 6 / 小键盘右方向键（NumLock 关时变成 (VK_RIGHT, ext=True)）


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               ctypes.c_size_t, ctypes.c_ssize_t)


_PROTOTYPES_READY = False


def _setup_win_prototypes():
    """一次性设置 user32/kernel32 的函数签名。

    必须设 restype/argtypes，否则 64 位下句柄和指针会被截成 32 位
    （SetWindowsHookExW 会报 GetLastError=126 MOD_NOT_FOUND）。
    放在模块级、幂等调用，这样即使不启动消息循环也能直接测 _dispatch。
    """
    global _PROTOTYPES_READY
    if _PROTOTYPES_READY:
        return
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    u32.SetWindowsHookExW.restype = ctypes.c_void_p
    u32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC,
                                      ctypes.c_void_p, wintypes.DWORD]
    u32.UnhookWindowsHookEx.restype = wintypes.BOOL
    u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u32.CallNextHookEx.restype = ctypes.c_ssize_t
    u32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
    _PROTOTYPES_READY = True


# ---------------------------------------------------------------------------
# 4.5.1 可配置按键：键名 ←→ (vkCode, 扩展位)
# ---------------------------------------------------------------------------
# 键位不再写死在 make_manual_hook 里（用户 2026-10-07 要求"适应不同键盘"）。
# config.json 的 "keys" 里存的是**字符串键名**，例如：
#     "keys": {"mark_in": ["left"], "mark_out": ["right"],
#              "save": ["numpad6"], "play": ["numpad_enter"]}
# 换成没有小键盘的 60% 键盘：把 play 改成 ["f8"] 就行；每个动作也可以绑多个键。
KEY_ACTIONS = {
    "mark_in": "标记入点",
    "mark_out": "标记出点 / 裁片段",
    "save": "保留片段",
    "play": "播放 / 收起回放",
}

KEY_TABLE = {
    # --- 方向 / 编辑键 ---
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E, "backspace": 0x08, "tab": 0x09,
    "space": 0x20, "esc": 0x1B, "escape": 0x1B, "capslock": 0x14,
    "numlock": 0x90, "scrolllock": 0x91, "pause": 0x13, "printscreen": 0x2C,
    # --- 符号键（主键盘）---
    "backtick": 0xC0, "grave": 0xC0, "minus": 0xBD, "equal": 0xBB,
    "lbracket": 0xDB, "rbracket": 0xDD, "backslash": 0xDC,
    "semicolon": 0xBA, "quote": 0xDE, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF,
    # --- 小键盘（NumLock 灯亮着时才是这些码）---
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69,
    "numpad_multiply": 0x6A, "numpad_add": 0x6B, "numpad_subtract": 0x6D,
    "numpad_decimal": 0x6E, "numpad_divide": 0x6F,
    # --- 多媒体键（不同键盘差异大，但这三个最常见）---
    "media_next": 0xB0, "media_prev": 0xB1, "media_play_pause": 0xB3,
}
for _i in range(10):
    KEY_TABLE.setdefault(str(_i), 0x30 + _i)
for _i in range(26):
    KEY_TABLE.setdefault(chr(97 + _i), 0x41 + _i)
for _i in range(1, 25):
    KEY_TABLE.setdefault(f"f{_i}", 0x6F + _i)
del _i

# 主键盘 Enter 和小键盘 Enter 的 vkCode 都是 0x0D，只有这一族必须靠扩展位区分。
_EXT_SENSITIVE = {0x0D: {"enter": False, "numpad_enter": True}}

# 反向表：vkCode → 规范键名（显示 / 回写配置用；同码取最短名字）
_KEY_BY_VK = {}
for _name, _vk in KEY_TABLE.items():
    if _vk not in _KEY_BY_VK or len(_name) < len(_KEY_BY_VK[_vk]):
        _KEY_BY_VK[_vk] = _name
del _name, _vk


def key_token_name(vk, ext=None):
    """(vkCode, 扩展位) → 配置里用的键名；认不出来就回 'vk:0x41' 这种写法。"""
    vk = int(vk)
    if vk == 0x0D and ext is not None:
        return "numpad_enter" if ext else "enter"
    name = _KEY_BY_VK.get(vk)
    return name if name else f"vk:0x{vk:02X}"


def parse_key_token(tok):
    """键名 → (vkCode, 扩展位)。

    扩展位为 **None** 表示"不区分扩展位"（方向键、字母数字等绝大多数键）；
    0x0D 这一族返回 True/False（小键盘 Enter / 主键盘 Enter）。
    也接受 "vk:0x41"、"0x41"、"65"（十进制）这类写法，方便手工写配置。
    认不出来返回 None。
    """
    if tok is None:
        return None
    if isinstance(tok, (list, tuple)) and len(tok) == 2 and isinstance(tok[1], bool):
        return (int(tok[0]), bool(tok[1]))          # 兼容旧的 (vk, ext) 写法
    s = str(tok).strip().lower().replace(" ", "").replace("-", "_")
    if not s:
        return None
    if s in KEY_TABLE:
        return (KEY_TABLE[s], None)
    # 容忍 "numpad_6" / "page_up" 这种带下划线的写法（图形界面/手写配置都可能出现）
    if "_" in s and s.replace("_", "") in KEY_TABLE:
        return (KEY_TABLE[s.replace("_", "")], None)
    for vk, table in _EXT_SENSITIVE.items():
        if s in table:
            return (vk, table[s])
    if s.startswith("vk:"):
        s = s[3:]
    try:
        return (int(s, 16) if s.startswith("0x") else int(s, 10), None)
    except ValueError:
        return None


def normalize_keys(cfg):
    """把配置里的 "keys" 规范化。

    返回 `(keys, problems)`：
      * keys     —— `{动作: [规范键名, ...]}`，可直接显示 / 回写
      * problems —— 中文问题列表，直接给用户看（认不出的键、两个动作抢同一个键…）

    任何情况下都返回一份**能用的**键位表（认不出的退回默认值），
    绝不因为配置写错就让四个键一起失灵。
    """
    raw = cfg.get("keys")
    if isinstance(raw, dict) and "keys" in raw:      # 兼容误写成 {"keys": {...}} 两层
        raw = raw.get("keys")
    raw = raw if isinstance(raw, dict) else {}
    defaults = DEFAULTS["keys"]
    keys, problems, seen = {}, [], {}
    for action in KEY_ACTIONS:
        val = raw.get(action, defaults.get(action))
        if val in (None, "", [], ()):
            val = defaults.get(action)
        toks = list(val) if isinstance(val, (list, tuple)) else [val]
        if len(toks) == 2 and isinstance(toks[1], bool):     # 旧写法 (vk, ext)
            toks = [toks]
        out = []
        for tok in toks:
            parsed = parse_key_token(tok)
            if parsed is None:
                problems.append(f"按键「{tok}」不认识 → 已忽略")
                continue
            vk, ext = parsed
            name = key_token_name(vk, ext)
            if name in out:
                continue                                     # 同一动作里重复，去重
            other = seen.get(name)
            if other:
                problems.append(f"「{KEY_ACTIONS[other]}」和「{KEY_ACTIONS[action]}」都绑了 "
                                f"{name} → 后者已忽略（一个键只能干一件事）")
                continue
            seen[name] = action
            out.append(name)
        if not out:
            out = list(defaults.get(action) or [])
            problems.append(f"「{KEY_ACTIONS[action]}」没有可用按键 → 已恢复默认 "
                            f"{' / '.join(out)}")
        keys[action] = out
    return keys, problems


def format_keys(keys):
    """`{动作: [键名]}` → `{动作: 'left / f8'}`（给界面和日志显示）。"""
    return {a: " / ".join(keys.get(a) or []) for a in KEY_ACTIONS}


def binding_of_token(tok):
    """键名 → KeyHook 用的绑定键：`int`（不区分扩展位）或 `(vk, ext)` 元组。"""
    parsed = parse_key_token(tok)
    if parsed is None:
        return None
    vk, ext = parsed
    return vk if ext is None else (vk, bool(ext))


# ---------------------------------------------------------------------------
# 4.5.2 单次按键捕获（图形界面 / --set-keys 用）
# ---------------------------------------------------------------------------
# 临时装一个 WH_KEYBOARD_LL 钩子，抓到第一个"真正的键"就结束：
#   * 修饰键（Shift/Ctrl/Alt/Win）不算 —— 按住 Shift 再按 F8，记的是 F8；
#   * Esc = 取消；
#   * 钩子同样是**只读**的，不拦截、不吞键。
_CAPTURE_IGNORE_VK = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0x5D,
                      0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
VK_ESCAPE = 0x1B


class KeyCapture:
    """单次按键捕获（Tk 里用 `after()` 轮询 `poll()`）。"""

    def __init__(self, timeout=10.0):
        self.timeout = float(timeout)
        self._result = None
        self._cancelled = False
        self._done = threading.Event()
        self._ready = threading.Event()
        self._proc = None
        self._hook = None
        self._t0 = 0.0
        self._thread = None
        self.ok = False
        self.error = ""

    def _on_key(self, nCode, wParam, lParam):
        try:
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk, ext = int(kb.vkCode), bool(kb.flags & LLKHF_EXTENDED)
                if self._result is None:
                    if vk == VK_ESCAPE:
                        self._result = "cancel"
                    elif vk not in _CAPTURE_IGNORE_VK:
                        self._result = (vk, ext)
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        try:
            _setup_win_prototypes()
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            hmod = k32.GetModuleHandleW(None)
            self._hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, hmod, 0)
            if not self._hook:
                self._hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not self._hook:
                self.error = f"SetWindowsHookExW 失败 (GetLastError={k32.GetLastError()})"
                self._ready.set()
                self._done.set()
                return
            self.ok = True
            self._t0 = time.time()
            self._ready.set()
            # 低级钩子靠线程消息队列分发，必须抽消息；用 PeekMessage + sleep
            # 而不是阻塞的 GetMessage，好处是能顺便检查"超时/取消"。
            msg = wintypes.MSG()
            while not self._done.is_set():
                if u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.02)
                if self._result is not None or self._cancelled:
                    self._done.set()
                elif self.timeout > 0 and time.time() - self._t0 > self.timeout:
                    self._done.set()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.ok = False
            self._ready.set()
            self._done.set()
        finally:
            self._unhook()

    def _unhook(self):
        if self._hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
            except Exception:
                pass
            self._hook = None
            self.ok = False

    # ---------------- 对外接口 ----------------
    def start(self):
        _setup_win_prototypes()
        self._proc = _HOOKPROC(self._on_key)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(4.0)
        return self.ok

    def poll(self):
        """已捕获 → `(vkCode, 扩展位)`；还没按到 / 超时 / 取消 → None。"""
        if self._done.is_set():
            got = self._result if isinstance(self._result, tuple) else None
            self._cleanup()
            return got
        if self.timeout > 0 and self._t0 and time.time() - self._t0 > self.timeout:
            self.cancel()
        return None

    def cancel(self):
        self._cancelled = True
        self._done.set()
        self._cleanup()

    def _cleanup(self):
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.6)


def capture_next_key(timeout=10.0, prompt=None, echo=True):
    """命令行版：阻塞等一个键，返回 `(vk, ext)`；Esc / 超时返回 None。"""
    cap = KeyCapture(timeout=timeout)
    if prompt:
        print(prompt, flush=True)
    if not cap.start():
        if echo:
            print(f"❌ 装键盘钩子失败：{cap.error}", flush=True)
        return None
    try:
        while not cap._done.is_set():
            got = cap.poll()
            if got is not None:
                return got
            time.sleep(0.05)
        return cap.poll()
    finally:
        cap.cancel()


class KeyHook:
    """
    全局低级键盘钩子（WH_KEYBOARD_LL）。

    **只读取按键，不拦截、不改写、不注入、不模拟。** 按键会照常传给当前窗口，
    所以它完全不碰游戏进程，与 VAC 无关 —— 只是"监听到你按了哪个键"。

    绑定表支持两种键：
        VK_LEFT                → 匹配该键（不管扩展位）
        (VK_RETURN, True)      → 只匹配小键盘 Enter（扩展位=1）
      小键盘 Enter 和主键盘 Enter 的 vkCode 都是 0x0D，靠扩展位区分。

    ★★ 2026-10-08 重要改造：**回调里只入队，绝不做别的事**。
    低级键盘钩子有一个致命特性：**回调返回之前，全系统的键盘输入都排队等着**。
    只要钩子所在进程被卡住（内存换页、杀软扫描、磁盘满、线程创建要内存…），
    用户看到的就是"所有软件都没反应、像死机一样"。
    原来的写法在回调里 `threading.Thread(...).start()` —— 建线程要申请内存，
    在换页风暴里可能要几百毫秒甚至更久，正好会放大这种卡死。
    现在：回调只做「查表 + 去抖 + `queue.put_nowait`」（都是纯内存操作，微秒级），
    真正的动作由**一个常驻工作线程**从队列里取出来执行；队列满了就丢事件（绝不阻塞）。
    另外回调里会记耗时，一旦超过 `slow_ms` 就记一次日志并提示改用轮询模式。
    """

    def __init__(self, bindings, debounce=0.4, slow_ms=120.0):
        _setup_win_prototypes()
        self.bindings = bindings
        self.debounce = debounce
        self.slow_ms = float(slow_ms)
        self._last = {}
        self._proc = _HOOKPROC(self._dispatch)
        self._hook = None
        self.ok = False
        self.error = ""
        # 事件队列 + 单个消费者线程（容量给足；满了就丢，宁丢也不卡键盘）
        self._events = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._worker = None
        self._slow_logged = 0

    def _lookup(self, vk, ext):
        for k in ((vk, ext), vk):
            if k in self.bindings:
                return self.bindings[k]
        return None

    def _dispatch(self, nCode, wParam, lParam):
        # ⚠️ 这个函数里**禁止**任何可能阻塞的操作：不建线程、不开文件、不发网络、
        #    不拿锁、不打印（打印会写控制台/文件）。只做查表 + 入队。
        t0 = time.perf_counter()
        try:
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk, ext = int(kb.vkCode), bool(kb.flags & LLKHF_EXTENDED)
                cb = self._lookup(vk, ext)
                if cb:
                    now = time.time()
                    key = (vk, ext)
                    if now - self._last.get(key, 0.0) >= self.debounce:
                        self._last[key] = now
                        if len(self._last) > 64:      # 防止字典无限长
                            self._last.clear()
                            self._last[key] = now
                        try:
                            self._events.put_nowait(cb)
                        except queue.Full:
                            pass                      # 队列满 = 宁可丢这一次，也不卡键盘
        except Exception:
            pass
        finally:
            # 只在异常慢的时候进入（正常是微秒级），不影响性能
            try:
                cost = (time.perf_counter() - t0) * 1000.0
                if cost > self.slow_ms:
                    self._slow_logged += 1
                    if self._slow_logged <= 3:
                        log(f"   ⚠️ 键盘钩子回调耗时 {cost:.0f} ms（超过 {self.slow_ms:.0f} ms）——"
                            f"机器可能正被换页/杀软拖住；如果频繁出现，请把设置里的"
                            f"「键盘监听方式」改成**轮询**（不经过输入管线，物理上不会拖住键盘）")
            except Exception:
                pass
        return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _worker_loop(self):
        """唯一执行动作的地方（不在钩子回调里跑）。"""
        while not self._stop.is_set():
            try:
                cb = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                cb()
            except Exception:
                pass

    def start(self):
        """
        安装钩子 + 跑消息循环。

        ⚠️ 两个必须踩对的坑（都踩过）：
          1) GetModuleHandleW 必须设 restype=c_void_p，否则 64 位下句柄被截成
             32 位整数 → SetWindowsHookExW 报 GetLastError=126 (MOD_NOT_FOUND)。
          2) WH_KEYBOARD_LL 的回调是投递到**安装钩子的那个线程**的，
             所以安装和消息循环必须在**同一个线程**里做。
        """
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(4.0)
        return self.ok

    def _run(self):
        try:
            _setup_win_prototypes()
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            hmod = k32.GetModuleHandleW(None)
            self._hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, hmod, 0)
            if not self._hook:      # 退一步：LL 钩子允许 hMod 传 NULL
                self._hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not self._hook:
                self.error = f"SetWindowsHookExW 失败 (GetLastError={k32.GetLastError()})"
                self._ready.set()
                return
            self.ok = True
            self._ready.set()
            msg = wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.ok = False
            if hasattr(self, "_ready"):
                self._ready.set()

    def stop(self):
        self._stop.set()
        if self._hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
            except Exception:
                pass
            self._hook = None
            self.ok = False


class PollKeyWatcher:
    """**不用全局钩子**的备选按键监听：`GetAsyncKeyState` 轮询。

    为什么要有它（2026-10-08，用户报"使用时所有软件都不能正常运行"）：
    低级键盘钩子必须"秒回"，**回调没返回之前全系统的键盘都在排队**。
    如果引擎所在进程被卡住（内存换页、杀软扫描、磁盘满），用户就会觉得整机死了。
    轮询方案**根本不进入输入管线**，物理上不可能拖住键盘；代价是两条：

      1. 认不出**扩展位** —— 主键盘 Enter 和小键盘 Enter 在 vk 层面同码，
         所以轮询模式下按主键盘 Enter 也会触发「播放」（默认键是小键盘 Enter）。
         想把两者分开，就把「播放」改绑成 F8 之类；
      2. 极快的点按（短于轮询间隔）理论上可能漏掉 —— 默认 20ms（50Hz）足够，
         需要更灵敏可以调 `key_poll_ms`。

    另外：**它不占用任何钩子**，所以更适合"钩子被安全软件拦/被系统丢弃"的机器。
    """

    def __init__(self, bindings, debounce=0.4, interval=0.02):
        _setup_win_prototypes()
        # 轮询只能按 vk 认键：把 (vk, ext) 形式的绑定拍平成 vk
        self.bindings = {}
        self.conflicts = []
        for k, cb in bindings.items():
            vk = k[0] if isinstance(k, tuple) else k
            if vk in self.bindings:
                self.conflicts.append(vk)
                continue
            self.bindings[vk] = cb
        self.debounce = debounce
        self.interval = max(0.005, float(interval))
        self.ok = False
        self.error = ""
        self._stop = threading.Event()
        self._thread = None
        self._down = {}
        self._last = {}

    def _poll_loop(self):
        u32 = ctypes.windll.user32
        try:
            u32.GetAsyncKeyState.restype = ctypes.c_short
            u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                now = time.time()
                for vk, cb in self.bindings.items():
                    down = bool(u32.GetAsyncKeyState(int(vk)) & 0x8000)
                    if down and not self._down.get(vk):
                        if now - self._last.get(vk, 0.0) >= self.debounce:
                            self._last[vk] = now
                            try:
                                cb()
                            except Exception:
                                pass
                    self._down[vk] = down
            except Exception:
                pass
            time.sleep(self.interval)

    def start(self):
        self._stop.clear()
        self._down.clear()
        self._last.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.ok = True
        if self.conflicts:
            log(f"   ⚠️ 轮询模式：有 {len(self.conflicts)} 个键被多个动作共用，已只保留第一个")
        return True

    def stop(self):
        self._stop.set()
        self.ok = False


def build_key_bindings(controller, cfg):
    """按配置生成 `{绑定键: 回调}`（钩子模式和轮询模式共用）。"""
    keys, problems = normalize_keys(cfg if "keys" in cfg else {"keys": DEFAULTS["keys"]})
    handlers = {"mark_in": controller.on_mark_in,
                "mark_out": controller.on_mark_out,
                "save": controller.on_save,
                "play": controller.on_play}
    bindings = {}
    for action, toks in keys.items():
        for tok in toks:
            key = binding_of_token(tok)
            if key is not None:
                bindings[key] = handlers[action]
    return bindings, problems


def make_key_watcher(controller, cfg):
    """按 `key_mode` 造监听器：`hook`（默认，精确）/ `poll`（不碰输入管线，最稳）。

    2026-10-08：用户报"使用时所有软件都不能正常运行"，而低级键盘钩子一旦被卡住
    就会拖住全系统输入。所以除了修掉钩子回调里的阻塞操作，还给出这个**完全绕开
    输入管线**的轮询方案，让现场能一键排查到底是不是钩子的问题。
    """
    cfg = cfg or {}
    bindings, problems = build_key_bindings(controller, cfg)
    for p in problems:
        log(f"   ⚠️ 键位配置：{p}")
    debounce = float(cfg.get("key_debounce", 0.5) or 0.5)
    mode = str(cfg.get("key_mode") or "hook").lower()
    if mode == "poll":
        ms = cfg.get("key_poll_ms", 20)
        try:
            interval = max(0.005, float(ms) / 1000.0)
        except Exception:
            interval = 0.02
        log(f"⌨  按键监听方式：轮询（每 {interval * 1000:.0f} ms 查一次，"
            f"不安装全局钩子 —— 绝不会拖住系统输入）")
        log("      （轮询认不出主键盘/小键盘 Enter 的区别；要区分就把「播放」改成 F8 之类）")
        return PollKeyWatcher(bindings, debounce=debounce, interval=interval)
    log("⌨  按键监听方式：全局钩子（只读、回调里只入队，不阻塞输入）")
    return KeyHook(bindings, debounce=debounce)


class ManualController:
    """
    导播手动的即时回放 —— **手动框选片段**（2026-10-06 用户定的新流程）：

        ←  左方向键     标记入点：记住"这一刻"
        →  右方向键     标记出点：把"入点 → 现在"这一段裁成素材（结尾就停在出点）
        小键盘6         保留片段：把当前这段素材另存成文件（.flv，赛后能剪出去发）
        小键盘Enter     切到「即时回放」场景播放；再按一次立刻切回直播

    为什么出点一按就把片段裁出来（而不是等播放时才裁）：
        Replay Source 的滚动缓冲只保留最近 `record_max_seconds` 秒，而且是**边打边滚**的。
        等到按 Enter 才去取，入点早就滚出缓冲了。所以 → 必须**当场**
        把"入点→现在"从缓冲里取出来裁好（Load replay + StartDelay 负值），
        播放是之后按 Enter 的事。

    ★ 2026-10-06 用户决定：**去掉所有自动定格**（回合结束自动抓、击杀后延迟抓都删了）。
      引擎不会在你没按键的时候去碰回放缓冲 —— 攒素材只发生在按 → 的那一下。
    """

    def __init__(self, director):
        self.d = director
        self.hook = None
        # 键名可以改（2026-10-07），所以日志里一律用**当前绑定的键**，别写死"小键盘 Enter"。
        self.k = format_keys(normalize_keys(director.cfg)[0])

    def key(self, action):
        return self.k.get(action) or "?"

    # ---------------- 回放功能总开关 ----------------
    def _replay_off(self, what):
        """回放功能关着的时候，四个键只提示不动手（用户 2026-10-07 定的默认值）。"""
        if self.d.replay_enabled:
            return False
        log(f"   （回放功能当前是关的，{what} 不生效）")
        log("     想用就在设置窗口 / 手机页面上打开「回放功能」；")
        log("     提示器和自动切观察位不受这个开关影响，照常工作。")
        return True

    # ---------------- ← 标记入点 ----------------
    def on_mark_in(self):
        """记住"这一刻"当入点。只是记一个时间戳（+ 省内存模式下顺手开始攒帧）。"""
        d = self.d
        if self._replay_off(f"【{self.key('mark_in')} 标记入点】"):
            return
        if d.replay_active:
            log(f"   （正在回放中，先按【{self.key('play')}】收掉再标入点）")
            return
        d.mark_in_at = time.time()
        log(f"⏺  【{self.key('mark_in')} 标记入点】{time.strftime('%H:%M:%S')} —— "
            f"打完了按【{self.key('mark_out')}】把这一段裁出来")
        # ★ 省内存模式：平时插件是 Disable 的（不占内存），按 ← 才让它开始攒帧。
        #   所以在这个模式下，片段是从**按 ← 这一刻**开始的（不能往回标）。
        if d.replay_mode == "armed":
            d.arm_capture("按了入点键")

    # ---------------- → 标记出点（当场裁片段）----------------
    def on_mark_out(self):
        """把"入点 → 现在"裁成一段素材（结尾就停在出点）。"""
        d = self.d
        if self._replay_off(f"【{self.key('mark_out')} 标记出点】"):
            return
        if d.replay_active:
            log(f"   （正在回放中，先按【{self.key('play')}】收掉再标出点）")
            return
        mark = d.mark_in_at
        if not mark:
            # ★ 用户要求：没标入点就按 → 时**提示先按 ←，不要去抓一段没框过的画面**。
            log(f"⚠️  还没标入点：先按【{self.key('mark_in')}】标入点，"
                f"打完了再按【{self.key('mark_out')}】标出点")
            return
        min_len = float(d.cfg.get("min_clip_seconds", 0.3))
        elapsed = time.time() - mark
        if elapsed < min_len:
            log(f"⚠️  入点到出点只有 {elapsed:.2f} 秒，太短了（至少 {min_len:.1f} 秒）"
                f"—— 入点还留着，再按一次【{self.key('mark_out')}】就把这一段裁出来")
            return
        # 守卫：没在拍游戏 → 缓冲里没有帧，裁出来会是空的或极短
        if d._current_scene and d._current_scene != d.cfg["live_scene"]:
            log(f"⚠️  现在 OBS 在「{d._current_scene}」而不是「{d.cfg['live_scene']}」——")
            log("    游戏采集没在渲染，缓冲里可能没有画面，裁出来会很短甚至是空的。")
        skip = d.clip_skip_from_mark(mark)
        try:
            d._load_replay(None, skip=skip)
        except Exception as e:
            log(f"❌ 【{self.key('mark_out')} 标记出点】裁片段失败: {e}")
            return
        d.mark_in_at = 0.0                      # 这一段用完了，下次重新标
        got = getattr(d, "last_clip_seconds", None)
        log(f"⏹  【{self.key('mark_out')} 标记出点】片段已裁好：入点到现在 {elapsed:.1f} 秒"
            + (f"，可播 {got:.1f} 秒" if got else ""))
        log(f"    → 按【{self.key('play')}】切画面播放；"
            f"想留档再按【{self.key('save')}】存成文件")
        # ★ 省内存模式：帧已经搬到"已取出的那一段"里了，这时候把滚动缓冲摘掉，
        #   内存里就只剩最近这一段（用户 2026-10-07 要求："就留最近截取的片段"）。
        if d.replay_mode == "armed" and d.cfg.get("free_buffer_after_clip", True):
            d.release_capture("片段已裁好，收起滚动缓冲")

    def on_play(self):
        d = self.d
        if self._replay_off(f"【{self.key('play')} 播放】"):
            return
        if d.replay_active:
            log(f"⏏  【{self.key('play')}】立刻切回直播")
            d._end_replay("手动切回")
            return
        if not d._snapshot_ok:
            log(f"⚠️  还没有裁好的片段 —— 先按【{self.key('mark_in')}】标入点，"
                f"打完了按【{self.key('mark_out')}】标出点，再按播放")
            return
        # 不传长度：让引擎在锁内读**最新**的素材长度，避免用到旧的
        log(f"▶  【{self.key('play')}】切画面播放")
        d._start_replay("手动播放", None)

    # ---------------- 小键盘 6：保留片段 ----------------
    def on_save(self):
        """把当前那段素材另存成文件（赛后能剪出去发）。"""
        if self._replay_off(f"【{self.key('save')} 保留片段】"):
            return
        self.d.save_replay()


def numlock_on():
    """NumLock 灯亮着吗？

    ★ 为什么要在意：小键盘 6 在 **NumLock 关**的时候，Windows 发出的就是
      "右方向键 + 扩展位"，和键盘右边那个 → 完全一样（钩子层面区分不开，
      scancode 都是 0x4D）—— 于是"小键盘 6 = 保留片段"会被当成"→ 标记出点"。
      NumLock 亮着时它是 VK_NUMPAD6(0x66)，两者就能各干各的。
    """
    try:
        _setup_win_prototypes()
        return bool(ctypes.windll.user32.GetKeyState(0x90) & 1)   # 0x90 = VK_NUMLOCK
    except Exception:
        return None


def make_manual_hook(controller, cfg=None, debounce=None):
    """
    按配置里的键位表装钩子（2026-10-07 起键位可改，见 KEY_TABLE）。

      标记入点      默认 ←  左方向键
      标记出点      默认 →  右方向键（当场把"入点 → 现在"裁成一段素材）
      保留片段      默认 小键盘 6（把这段素材另存成文件，赛后能剪出去发）
      播放/收起     默认 小键盘 Enter

    ★ 为什么"小键盘 6"默认没有连 `(VK_RIGHT, True)` 一起绑：
      方向键在 Windows 里**本身就带扩展位**（E0 前缀），NumLock 关掉时小键盘 6
      发出来的也是 `(VK_RIGHT, ext=True)`、scancode 同样是 0x4D —— 两者在底层
      完全同码，钩子上区分不开。所以默认只认 NumLock 亮着时的 `VK_NUMPAD6`，
      免得把真正的 → 键劫持成"保留片段"（→ 是标出点键）。NumLock 没开时启动会提醒。

    ★ 键位来自 `cfg["keys"]`（字符串键名），在图形界面或 `--set-keys` 里可以改；
      没有小键盘的键盘可以把 play 改成 f8 之类。认不出的键会打日志并退回默认值。

    ★ 监听方式由 `cfg["key_mode"]` 决定（`hook` 默认 / `poll` 不用钩子）——
      见 `make_key_watcher`；这个函数保留下来只是为了不破坏老调用。
    """
    cfg = cfg or {}
    if debounce is not None:
        cfg = dict(cfg, key_debounce=debounce)
    return make_key_watcher(controller, cfg)


# ============================================================================
# 4.7 副驾提示器：实时算"现在该看谁"，手机上看
# ============================================================================

# 这些权重就是"注意力排序"的全部逻辑，故意做成一眼能看懂、能随手调的。
AW = {
    "taking_damage": 70,    # 这一包血量掉了 —— 最强的信号，他正在挨打
    "mutual_aim": 60,       # 他和他面前的敌人互相在瞄 —— 对枪一触即发
    "aiming_enemy": 25,     # 他在瞄一个敌人（单向，例如架枪）
    "enemy_very_close": 25, # 最近敌人 < 500 单位
    "enemy_close": 12,      # 最近敌人 < 1000 单位
    # ↓ 下面这些是**静态叙事**信号：跟"现在有没有打起来"无关。
    #   它们必须明显低于"交火主导权"(DUEL_DOMINANCE)，否则会出现
    #   「一个人架着枪，镜头却切到后面拿包的残局哥」这种莫名其妙的切换。
    "last_alive": 25,       # 自己队只剩他一个（残局）
    "has_bomb": 14,         # 拿着 C4
    "near_bomb_ct": 12,     # 已下包，他离包很近（拆包位）
    "just_killed": 30,      # 3 秒内刚拿到击杀（可能还在连杀）
    "flashed": 15,          # 被闪了（要出事）
    "nade_incoming": 18,    # 附近有飞行中的道具（要打起来了）
    "sniper": 8,            # 拿狙击（对枪观赏性高）
    "low_hp": 10,           # 残血
    "observed": 6,          # 当前正在看的这个（轻微粘性，别乱跳）
}
# ★★ 交火主导权：只要他**正在参与一场即将发生的交火**，就加这么多分。
#
# 为什么需要这个：原来的权重是把"动态"和"静态"信号直接相加，于是
#     架枪的人       = aiming_enemy 25 + enemy_very_close 25        = 50
#     包匪+残局+包点 = has_bomb 22 + last_alive 40 + near_bomb_ct 18 = 80
#     → 镜头切到后面拿包的人。连真正的对枪(55)都打不过这个组合。
#
# 加了这个之后，交火参与者 = 60 + 130 = 190，任何静态组合都追不上。
# 这也是导播的基本直觉：**正在打的人，镜头不该离开他。**
#
# ⚠️ 但"在瞄人"只有角度判定，**没有视线遮挡检查**（GSI 不给遮挡信息）。
#    隔着墙把准星对着附近敌人也会触发。所以分两级：
#      强交火（互相瞄 / 正在掉血）→ 满分主导权，**并且锁住镜头不许切走**
#      弱交火（单向瞄且敌人很近）  → 只有 70 分，压得过静态叙事，但不锁镜头
#    这样"架枪对峙"能压过包匪，而"隔墙瞄人"不会把镜头焊死。
DUEL_DOMINANCE = 130        # 强交火：真正在打
DUEL_DOMINANCE_WEAK = 70    # 弱交火：单向架枪/隔墙对位
# 弱交火（第 1 层）的距离上限 —— 放宽一点：AWP 在 1600 单位架长枪线也是真实对峙。
# 但第 0 层（真交火）仍要求 1200 以内，保证"近处真打起来"一定压过"远处架枪"。
ENGAGE_DIST = 1600.0
# ★ 互相瞄也分远近：1800 单位外互相看着只是"各守一条线"，
#   不是交火。不区分的话，两个隔着墙对望的人会拿满分主导权，
#   把真正的近距离对枪挤掉（实测到过：架枪的 173 分 vs 对枪的 172 分）。
DUEL_CLOSE_DIST = 1200.0    # 互相瞄且距离在这个以内 = 真交火（第 0 层）
ENGAGE_NEAR_BONUS = 45      # 交火越近越紧急：0 距离 +45，到 DUEL_CLOSE_DIST 递减到 0
STICKY_BONUS = 14           # 上一秒的第一名给一点加成，防止列表闪烁
NEAR_VERY_CLOSE = 500.0
NEAR_CLOSE = 1000.0
AIM_ANGLE_DEG = 22.0        # 朝向和"指向敌人"方向夹角小于这个值算"在瞄他"
AIM_MAX_DIST = 2600.0       # 超过这个距离不算"在瞄人"（看不见/不构成威胁）
DUEL_MAX_DIST = 1800.0      # "对枪中"要求更近一点；太松会开局就全触发，太紧就几乎不触发

# --- 自动切换的灵敏度档位：手机上可以实时切 ---
# (最小停留秒数, 领先多少分才切, 分数门槛)
#   收紧"对枪中"的判定之后分数会普遍变低，所以门槛也要跟着降，
#   否则会出现"谁也不够分 → 干脆不切"的迟钝感。
AUTO_PRESETS = {
    "fast":   (2.0, 6, 30),     # 灵敏：切得勤，适合动作多的比赛
    "normal": (3.0, 10, 38),    # 标准（默认）
    "calm":   (4.5, 18, 50),    # 保守：切得少，适合节奏慢的比赛
}

# ---------------------------------------------------------------------------
# 对枪胜率：一个"看得懂"的启发式，用到的全是 GSI 现成的字段
# ---------------------------------------------------------------------------
# 武器威力按距离分两档 —— (远距离, 近距离)。
# AWP 远距离统治、近距离吃亏；霰弹枪反过来；冲锋枪偏近；步枪中庸。
WEAPON_POWER = {
    "weapon_awp": (96, 46), "weapon_ssg08": (78, 40),
    "weapon_scar20": (70, 46), "weapon_g3sg1": (70, 46),
    "weapon_ak47": (74, 70), "weapon_m4a1": (69, 68), "weapon_m4a1_silencer": (69, 68),
    "weapon_aug": (70, 62), "weapon_sg556": (72, 64),
    "weapon_galilar": (56, 56), "weapon_famas": (53, 53),
    "weapon_mp9": (35, 58), "weapon_mac10": (32, 58), "weapon_mp7": (36, 56),
    "weapon_mp5sd": (36, 56), "weapon_ump45": (40, 55), "weapon_p90": (34, 58),
    "weapon_bizon": (30, 52),
    "weapon_nova": (10, 72), "weapon_xm1014": (10, 70),
    "weapon_mag7": (8, 68), "weapon_sawedoff": (8, 66),
    "weapon_m249": (45, 55), "weapon_negev": (45, 55),
    "weapon_deagle": (50, 62), "weapon_revolver": (50, 60),
    "weapon_glock": (22, 34), "weapon_hkp2000": (24, 36),
    "weapon_usp_silencer": (26, 38), "weapon_p250": (25, 38),
    "weapon_fiveseven": (26, 36), "weapon_cz75a": (30, 50),
    "weapon_tec9": (24, 44), "weapon_elite": (22, 36),
    "weapon_knife": (3, 8),
}
DUEL_WIN_BONUS = 42          # 胜率 100% 时的最大加成（50% 时为 0，输家拿负分）
DUEL_TEMP = 22.0             # 逻辑斯蒂温度：越大越平缓，越小越敢下判断


def weapon_power(weapons, dist, active=None):
    """按距离给出手里**当前拿着**那把枪的威力（0~100）。

    ⚠️ 必须只认 state=="active" 的那把，不能用整个武器栏。
    GSI 的 weapons 字典里会残留上一回合/之前捡过的枪（实测：
    一个人手里是 glock，栏位里却还躺着 weapon_awp），
    用整个栏位判定会把他算成 AWP 选手，对枪胜率直接算错。
    """
    if active:
        pw = WEAPON_POWER.get(active)
        if pw:
            if dist is None or dist < 500:
                return float(pw[1])
            if dist >= 1400:
                return float(pw[0])
            return (pw[0] + pw[1]) / 2.0
    if not weapons:
        return 20.0
    if dist is None or dist < 500:
        idx = 1
    elif dist >= 1400:
        idx = 0
    else:
        idx = None
    best = 0.0
    for w in weapons:
        pw = WEAPON_POWER.get(w)
        if not pw:
            continue
        v = pw[idx] if idx is not None else (pw[0] + pw[1]) / 2.0
        best = max(best, v)
    return best or 20.0


def duel_win_prob(pa, pb, dist):
    """p 对上 e 的胜率（0~1）。纯启发式，但每一项都能解释、能调。"""
    def power(x):
        p = (x.get("hp") or 0) * 0.78
        if x.get("armor"):
            p += 6
        if x.get("helmet"):
            p += 4
        p += weapon_power(x.get("weapons"), dist, x.get("active")) * 0.52
        if (x.get("flashed") or 0) > 0.5:
            p -= 22
        if x.get("burning"):
            p -= 8
        # 本场状态：K/D 偏离 1 越多越有手感
        k, d = x.get("m_kills"), x.get("m_deaths")
        if k is not None and d is not None:
            kd = (k + 1.0) / (d + 1.0)
            p += max(-10.0, min(12.0, (kd - 1.0) * 6.0))
        # 本轮手感：已经打出的伤害
        p += min(x.get("r_dmg") or 0, 150) * 0.06
        return p
    diff = power(pa) - power(pb)
    try:
        return 1.0 / (1.0 + math.exp(-diff / DUEL_TEMP))
    except OverflowError:
        return 0.0 if diff < 0 else 1.0


def load_or_make_token(base=None):
    """手机端开关自动切换需要一个口令，防止同网段的别人乱点。

    存在 viewer_token.txt 里，**重启后不变**，这样手机上的书签一直有效。
    `base` 可以指定目录（图形界面和引擎必须指向同一个文件，否则手机上
    预览到的地址和真正生效的口令会不一样）。
    """
    import secrets
    p = os.path.join(base or base_dir(), "viewer_token.txt")
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) >= 6:
                return t
        t = secrets.token_urlsafe(6)
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)
        return t
    except Exception as e:
        log(f"（口令文件写入失败，本次使用临时口令: {e}）")
        return secrets.token_urlsafe(6)


def obs_log_mark():
    """记下 OBS 日志当前的字节位置，用于只读"新写入的部分"。"""
    p = obs_log_newest()
    try:
        return (p, os.path.getsize(p)) if p else (None, 0)
    except Exception:
        return (p, 0)


def obs_log_since(mark, limit=50):
    """读 mark 之后**新写入**的 replay_source 行。

    ⚠️ 之前是"读最后 N 行、和之前的列表比对文本差异"来判断新行 —— 那个做法是错的：
    `replay added of 12.00 seconds` 每次文本完全一样，会被误判成"没有新行"，
    于是引擎拿到的是**上一次的旧长度**，播放时长就算短了，片子被腰斩。
    改成按字节偏移读，就不会认错。
    """
    p, off = mark
    if not p:
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size < off:      # 日志被轮转/截断了
                off = 0
            f.seek(off)
            data = f.read().decode("utf-8", "replace")
        return [l for l in data.splitlines() if "replay_source" in l][:limit]
    except Exception:
        return []


def mov_duration(path, probe_bytes=2 * 1024 * 1024):
    """只读文件头，解析 QuickTime/MP4 里 `mvhd` 的时长，返回秒（失败返回 None）。

    为什么需要：OBS 的 stinger 转场用 `transitionFixed=true` + `transitionDuration=null`
    上报 —— obs-websocket **问不出**这种固定转场的时长（实测 GetSceneTransitionOverride
    这个请求类型根本不存在）。但"包装转场有多长"直接决定"回放片头会被盖住多久"，
    所以只能自己量。纯标准库：读前 2MB（必要再读尾部 4MB）找 `mvhd` 原子。

    mvhd 布局（解析出来的偏移都是相对 'mvhd' 那 4 个字节的）：
        version(1) flags(3) creation(4) modification(4) timescale(4) duration(4)   ← version 0
        version(1) flags(3) creation(8) modification(8) timescale(4) duration(8)  ← version 1
    本机实测 J:/比赛包装/01_Logo_transition_long.mov（1,008,947,306 字节）→ 3.35 秒。
    """
    def _parse(buf, base):
        i = buf.find(b"mvhd")
        if i < 0:
            return None
        p = i + 4
        if p + 20 > len(buf):
            return None
        ver = buf[p]
        try:
            if ver == 1:
                if p + 32 > len(buf):
                    return None
                timescale = int.from_bytes(buf[p + 20:p + 24], "big")
                duration = int.from_bytes(buf[p + 24:p + 32], "big")
            else:
                timescale = int.from_bytes(buf[p + 12:p + 16], "big")
                duration = int.from_bytes(buf[p + 16:p + 20], "big")
        except Exception:
            return None
        if timescale > 0 and duration > 0:
            return duration / float(timescale)
        return None

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(probe_bytes)
        d = _parse(head, 0)
        if d:
            return d
        # moov 可能压在文件尾（某些导出工具会这样）
        if size > probe_bytes:
            with open(path, "rb") as f:
                f.seek(max(0, size - 4 * 1024 * 1024))
                d = _parse(f.read(), size)
        return d
    except Exception:
        return None


def stinger_visible_seconds(path, duration, transition_point_ms=None,
                            ffmpeg_exe=None, transparent_mean=8.0, timeout=30.0):
    """量出包装素材"动画真正放完"的时刻（秒）—— 回放等到这时候开播，一帧片头都不丢。

    为什么不直接用文件时长（2026-10-06 用户反馈："播放前会卡零点几秒不动"）：
    实测 `J:/比赛包装/01_Logo_transition_long.mov` 时长 3.35 秒，但**最后 0.40 秒
    是全透明的黑帧**（8x8 采样下 alpha=6/255、luma=0）。OBS 的 stinger 转场在
    `transition_point`(1000ms) 那一刻就已经把节目切到新场景，之后观众看到的是
    "盖在包装动画下面的回放画面" —— 动画 2.95 秒就放完了，却要等到 3.35 秒才开播，
    中间那 0.4 秒画面一动不动，看起来就是"卡了一下"。

    做法：用 ffmpeg 把 alpha 通道抽成 8x8 灰度序列（201 帧才 12 KB），从尾巴往前
    跳过"整帧透明"的帧，最后一张"还看得见"的帧就是动画结束点。
      * 量不出来（没装 ffmpeg / 解码失败 / 帧数不对）→ 返回 `duration`（和以前一样等满）。
      * **只会提前，不会推后**；下限是 `transition_point`（那之前节目还在旧场景上，
        播了观众也看不见）。
      * 整段都是透明帧 → 返回 0.0（等于没有包装，不用等）。
    """
    try:
        if not path or not duration or float(duration) <= 0:
            return duration
        dur = float(duration)
        exe = ffmpeg_exe if ffmpeg_exe is not None else shutil.which("ffmpeg")
        if not exe or not os.path.isfile(exe) or not os.path.isfile(path):
            return dur
        fd, tmp = tempfile.mkstemp(suffix=".raw", prefix="sting_alpha_")
        os.close(fd)
        try:
            cmd = [exe, "-v", "error", "-y", "-i", path,
                   "-vf", "alphaextract,scale=8:8:flags=area",
                   "-pix_fmt", "gray", "-f", "rawvideo", tmp]
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=timeout)
            if p.returncode != 0:
                return dur
            with open(tmp, "rb") as f:
                raw = f.read()
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
        per = 64                       # 8 x 8
        n = len(raw) // per
        if n < 4:
            return dur
        fps = n / dur                  # 帧数 / 文件时长 = 素材帧率，不用再问 ffprobe
        i = n - 1
        while i >= 0 and sum(raw[i * per:(i + 1) * per]) / float(per) <= transparent_mean:
            i -= 1
        if i < 0:
            return 0.0
        visible = (i + 1) / fps        # 这一帧的结束时刻
        floor = 0.0
        try:
            if transition_point_ms:
                floor = max(0.0, float(transition_point_ms) / 1000.0)
        except Exception:
            floor = 0.0
        return max(floor, min(dur, visible))
    except Exception:
        return duration


def lan_ips():
    """尽量找出本机的局域网 IP，给手机连。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))      # 不发包，只是让系统选出出口网卡
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def parse_vec(v):
    if isinstance(v, (list, tuple)):
        try:
            return [float(x) for x in v[:3]]
        except Exception:
            return None
    if isinstance(v, str):
        try:
            return [float(x) for x in v.split(",")[:3]]
        except Exception:
            return None
    return None


def _dist3(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def slot_key(slot):
    """observer_slot（0 起）→ 该按的数字键。

    CS2 观战界面里数字键 1~9、0 对应第 1~10 个观察位，所以 slot+1，
    slot==9 时是第 10 位 = 按 0。
    """
    try:
        s = int(slot)
    except Exception:
        return None
    if 0 <= s <= 8:
        return str(s + 1)
    if s == 9:
        return "0"
    return None


# ---------------------------------------------------------------------------
# 自动切换：把数字键"发"给游戏
# ---------------------------------------------------------------------------
# 用 SendInput + 扫描码（不用虚拟键码）—— 游戏普遍只认扫描码。
#
# ⚠️ 这是**合成按键**，会落到"当前有焦点的窗口"上。所以自动切换要求
#    **CS2 窗口在前台**（你手动按数字键切视角时本来也是这个前提）。
#    这和可编程键盘 / Stream Deck 发按键是同一类做法。
#    引擎会在切换后用 GSI 的 observer_slot 校验"到底切过去没有"，
#    连续失败会自动关掉自动切换，不会让你在直播中一头雾水。
SCANCODE = {"1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
            "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B}
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def foreground_exe():
    """当前前台窗口属于哪个进程（返回小写 exe 文件名，取不到返回 ""）。

    自动切换前必须确认前台是 CS2 —— 否则合成的数字键会打进别的程序
    （比如 OBS 里如果绑了数字键热键，就会乱切场景）。
    """
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                       ctypes.c_wchar_p,
                                                       ctypes.POINTER(wintypes.DWORD)]
            k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def send_digit(ch, tap_ms=28):
    """把一个数字键发进当前焦点窗口。返回 True/False。"""
    sc = SCANCODE.get(str(ch))
    if sc is None:
        return False
    n = ctypes.sizeof(_INPUT)
    down = _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTunion(ki=_KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE, 0, None)))
    up = _INPUT(type=INPUT_KEYBOARD,
                u=_INPUTunion(ki=_KEYBDINPUT(0, sc, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, None)))
    try:
        u32 = ctypes.windll.user32
        u32.SendInput.restype = wintypes.UINT
        u32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        sent = u32.SendInput(1, ctypes.byref(down), n)
        time.sleep(tap_ms / 1000.0)
        sent += u32.SendInput(1, ctypes.byref(up), n)
        return sent > 0
    except Exception:
        return False


# ===========================================================================
# 4.6 CS2 控制台"打字"（2026-10-07 加：每次开播前自动输入预设指令）
# ===========================================================================
# 用户要求："允许用户填写每次开播前在 cs 客户端控制台默认要输入的指令（支持多条）"。
#
# 做法：和自动切观察位**同一套** SendInput + 扫描码通路，只不过这次是逐字符打：
#   1. 先按一下"控制台键"（默认 ` ，可以在设置里改成 F10 之类）把控制台打开；
#   2. 等一小会儿（控制台是异步渲染的），然后每打一条指令就敲一次回车；
#   3. 默认再把控制台关掉（跟手动操作一模一样）。
#
# 安全阀（和自动切视角同源）：
#   * 默认**只有 CS2 在前台**才发键（`cs_console_require_focus`），
#     否则这些字符会打进 OBS / 浏览器 / 聊天窗口；
#   * 指令列表为空 = 这个功能完全不启动；
#   * 只在引擎启动后自动发**一次**（或"每场第 1 回合"一次），不会反复刷。
#
# 键位表按**美式布局**（Set 1 扫描码）。CS2 控制台吃的是"按键产生的字符"，
# 和系统输入法无关，但和**键盘布局**有关：非美式布局下个别符号可能对不上，
# 所以日志里会把实际发出去的每一条原文回显出来，一眼能看出哪里不对。
VK_SHIFT = 0x10
SC_LSHIFT = 0x2A
KEYEVENTF_EXTENDEDKEY = 0x0001


def _build_char_scancode():
    m = {}
    # 主键盘 1..9,0
    for i, ch in enumerate("123456789"):
        m[ch] = (0x02 + i, False)
    m["0"] = (0x0B, False)
    # 字母三排
    for row, start in (("qwertyuiop", 0x10), ("asdfghjkl", 0x1E), ("zxcvbnm", 0x2C)):
        for i, ch in enumerate(row):
            m[ch] = (start + i, False)
            m[ch.upper()] = (start + i, True)
    # 不按 Shift 的符号
    for ch, sc in {"`": 0x29, "-": 0x0C, "=": 0x0D, "[": 0x1A, "]": 0x1B,
                   "\\": 0x2B, ";": 0x27, "'": 0x28, ",": 0x33, ".": 0x34,
                   "/": 0x35, " ": 0x39}.items():
        m[ch] = (sc, False)
    # 要按 Shift 的符号
    for ch, sc in {"~": 0x29, "_": 0x0C, "+": 0x0D, "{": 0x1A, "}": 0x1B,
                   "|": 0x2B, ":": 0x27, '"': 0x28, "<": 0x33, ">": 0x34,
                   "?": 0x35, "!": 0x02, "@": 0x03, "#": 0x04, "$": 0x05,
                   "%": 0x06, "^": 0x07, "&": 0x08, "*": 0x09, "(": 0x0A,
                   ")": 0x0B}.items():
        m[ch] = (sc, True)
    return m


CHAR_SCANCODE = _build_char_scancode()

# 非字符键（给"控制台键"用；方向键/编辑键是扩展键，要带 E0 前缀）
NAMED_SCANCODE = {
    "esc": (0x01, False), "tab": (0x0F, False), "enter": (0x1C, False),
    "numpad_enter": (0x1C, True), "backspace": (0x0E, False),
    "space": (0x39, False), "backslash": (0x2B, False), "backtick": (0x29, False),
    "f1": (0x3B, False), "f2": (0x3C, False), "f3": (0x3D, False), "f4": (0x3E, False),
    "f5": (0x3F, False), "f6": (0x40, False), "f7": (0x41, False), "f8": (0x42, False),
    "f9": (0x43, False), "f10": (0x44, False), "f11": (0x57, False), "f12": (0x58, False),
    "insert": (0x52, True), "delete": (0x53, True), "home": (0x47, True),
    "end": (0x4F, True), "pageup": (0x49, True), "pagedown": (0x51, True),
    "up": (0x48, True), "down": (0x50, True), "left": (0x4B, True), "right": (0x4D, True),
    "numpad_divide": (0x35, True), "numpad_add": (0x4E, False),
    "numpad_subtract": (0x4A, False), "numpad_decimal": (0x53, False),
}
for _i in range(10):
    NAMED_SCANCODE.setdefault(f"numpad{_i}", (0x52 - _i if _i == 0 else 0x4F + _i, False))
NAMED_SCANCODE["numpad0"] = (0x52, False)


def show_char(ch):
    """把一个字符变成"看得见"的写法（不可见/非 ASCII 的用 U+XXXX）。

    为什么需要：用户从网页/Word 里复制控制台指令时，很容易带上**不换行空格 U+00A0**、
    零宽空格、全角引号这类"看得见才怪"的字符。旧日志里它们被原样打印，
    于是出现 `⚠️ 有字符没打出来：` 后面**什么都看不见**的诡异提示（2026-10-09 用户反馈）。
    """
    o = ord(ch)
    if 32 <= o < 127:
        return ch
    if ch == "\t":
        return "\\t"
    return "U+%04X" % o


# 常见"看着像 ASCII、其实不是"的字符 → 该换成什么（控制台只认 ASCII）
_CONSOLE_CHAR_FIX = {
    "\u00a0": " ",   # 不换行空格（网页复制最常见）
    "\u2007": " ",   # figure space
    "\u2009": " ",   # thin space
    "\u202f": " ",   # narrow no-break space
    "\u3000": " ",   # 全角空格
    "\u200b": "",    # 零宽空格
    "\ufeff": "",    # BOM / 零宽不换行空格
    "\u201c": '"', "\u201d": '"',   # 中文引号 “ ”
    "\u2018": "'", "\u2019": "'",   # 中文单引号 ‘ ’
    "\uff02": '"', "\uff07": "'",   # 全角引号
    "\uff0d": "-", "\u2013": "-", "\u2014": "-",   # 各种破折号
    "\uff1b": ";", "\uff1d": "=", "\uff0e": ".", "\uff0c": ",", "\uff1a": ":",
}


def sanitize_console_text(s):
    """把控制台指令里的"全角/不可见"字符换成 ASCII 等价物。

    返回 `(干净文本, [改了什么…])`。`改了什么` 是给人看的说明（例如 `U+00A0→空格`）。
    这条是为用户省事的：从网页复制来的指令经常带这些字符，旧版只会报一句
    "有字符没打出来"却看不见是哪个，现在**自动修好并说明**。
    """
    out, fixed = [], []
    for ch in s:
        o = ord(ch)
        if ch in _CONSOLE_CHAR_FIX:
            new = _CONSOLE_CHAR_FIX[ch]
            out.append(new)
            fixed.append("%s→%s" % (show_char(ch),
                                    "空格" if new == " " else ("删掉" if not new else new)))
            continue
        if 0xFF01 <= o <= 0xFF5E:          # 全角 ！-～ → 对应 ASCII
            new = chr(o - 0xFEE0)
            out.append(new)
            fixed.append("%s→%s" % (show_char(ch), new))
            continue
        out.append(ch)
    return "".join(out), fixed


def send_key_scancode(sc, shift=False, tap_ms=14, extended=False):
    """按一次扫描码（可选按住 Shift / 扩展位）。返回 True/False。"""
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    n = ctypes.sizeof(_INPUT)

    def mk(up=False):
        f = flags | (KEYEVENTF_KEYUP if up else 0)
        return _INPUT(type=INPUT_KEYBOARD,
                      u=_INPUTunion(ki=_KEYBDINPUT(0, sc, f, 0, None)))

    shift_dn = _INPUT(type=INPUT_KEYBOARD,
                      u=_INPUTunion(ki=_KEYBDINPUT(0, SC_LSHIFT, KEYEVENTF_SCANCODE, 0, None)))
    shift_up = _INPUT(type=INPUT_KEYBOARD,
                      u=_INPUTunion(ki=_KEYBDINPUT(0, SC_LSHIFT,
                                                   KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, None)))
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True) if _HAS_WIN_ERR else ctypes.windll.user32
        u32.SendInput.restype = wintypes.UINT
        u32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        sent = 0
        if shift:
            sent += u32.SendInput(1, ctypes.byref(shift_dn), n)
        down, up = mk(False), mk(True)
        sent += u32.SendInput(1, ctypes.byref(down), n)
        time.sleep(max(0.004, tap_ms / 1000.0))
        sent += u32.SendInput(1, ctypes.byref(up), n)
        if shift:
            sent += u32.SendInput(1, ctypes.byref(shift_up), n)
        if sent <= 0 and _HAS_WIN_ERR:
            # 记下 Windows 的错误码：5=拒绝访问（UIPI/安全软件）87=参数错误（结构体问题）
            global LAST_SENDINPUT_ERR
            LAST_SENDINPUT_ERR = ctypes.get_last_error()
        return sent > 0
    except Exception:
        return False


LAST_SENDINPUT_ERR = 0        # 最近一次 SendInput 失败时的 GetLastError
try:
    ctypes.WinDLL("user32", use_last_error=True)
    _HAS_WIN_ERR = True
except Exception:
    _HAS_WIN_ERR = False


def type_text(text, per_char_ms=14):
    """把一段文本"打"进当前焦点窗口。

    返回 `(ok, bad_chars, blocked)`：
      * `bad_chars` —— 字符表里没有的字符（原样跳过、不中断）；
      * `blocked`   —— **按键根本没送出去**（`SendInput` 返回 0）。

    ★ 2026-10-09 用户反馈："发送控制台指令时每条都显示 `⚠️ 有字符没打出来：`，但后面什么也没有"。
      原因就是这两种失败混成了一个 `ok=False`：字符都在表里（`bad` 是空的），
      真正失败的是 `SendInput` 被系统/安全软件挡了（UIPI：目标窗口权限比我们高）。
      现在分开报，并且给出最可能的原因 —— 不然用户只会看到一句空警告。
    """
    bad = []
    blocked = False
    for ch in text:
        ent = CHAR_SCANCODE.get(ch)
        if ent is None:
            bad.append(ch)
            continue
        sc, shift = ent
        if not send_key_scancode(sc, shift, tap_ms=per_char_ms):
            blocked = True
    return (not bad and not blocked), bad, blocked


def resolve_console_key(tok):
    """把配置里的"控制台键"解析成 (scancode, shift, extended)；认不出返回 None。

    既接受单个字符（`` ` `` / `'` / `\\` ），也接受键名（`f10` / `numpad_enter` / `backtick`）。
    """
    if tok is None:
        return None
    s = str(tok).strip()
    if len(s) == 1:
        ent = CHAR_SCANCODE.get(s)
        if ent:
            return (ent[0], ent[1], False)
    key = s.lower().replace(" ", "").replace("-", "_")
    if key in NAMED_SCANCODE:
        sc, ext = NAMED_SCANCODE[key]
        return (sc, False, ext)
    if "_" in key and key.replace("_", "") in NAMED_SCANCODE:
        sc, ext = NAMED_SCANCODE[key.replace("_", "")]
        return (sc, False, ext)
    ent = CHAR_SCANCODE.get(s)
    if ent:
        return (ent[0], ent[1], False)
    return None


class ConsoleTyper:
    """每次开播前自动往 CS2 控制台输入预设指令。

    配置：
      * `cs_console_commands`      —— 指令列表（一行一条，空行和 `//`、`;` 开头的注释行忽略）
      * `cs_console_key`           —— 控制台键，默认 `` ` ``（可改 f10 之类）
      * `cs_console_trigger`       —— `start`（引擎起来后等到 CS2 在前台就发一次，默认）
                                      / `round1`（每场第 1 个冻结时间发一次）/ `manual`（只手动）
      * `cs_console_delay_ms`      —— 打开控制台后等多久再开始打（默认 400）
      * `cs_console_gap_ms`        —— 两条指令之间（默认 120）
      * `cs_console_close`         —— 发完是否自动关掉控制台（默认 True）
      * `cs_console_require_focus` —— 是否要求 CS2 在前台（默认 True，强烈建议保持）
      * `cs_console_timeout_s`     —— `start` 模式下最多等多久（默认 900 秒）
    """

    def __init__(self, cfg, focus_exe="cs2.exe", dry_run=False):
        self.cfg = cfg
        self.focus_exe = str(cfg.get("auto_focus_exe") or "cs2.exe").lower()
        self._lock = threading.Lock()
        self._busy = False
        self._last_sent = 0.0
        self._stop = False
        self.dry_run = bool(dry_run or cfg.get("dry_run"))
        # 便于单测替换
        self._fg = foreground_exe
        self._type_text = type_text
        self._tap_key = send_key_scancode
        if self.dry_run:
            # ★ 空跑模式（--dry-run）绝不能真往你桌面打字 —— 那会打进当前焦点窗口。
            #   这里换成"只打日志"的桩，流程照样走完，方便离线验证。
            self._tap_key = self._dry_tap
            self._type_text = self._dry_type
            self._fg = lambda: self.focus_exe      # 假装 CS2 在前台，好把流程走完

    def _dry_tap(self, sc, shift=False, tap_ms=14, extended=False):
        log(f"      [空跑] 按键 0x{sc:02X}{'（+Shift）' if shift else ''}")
        return True

    def _warn_inject_blocked(self):
        """按键注入被挡时，把"怎么修"一次说清（否则用户只看到一句空警告）。

        2026-10-09 用户反馈"控制台指令每条都有个空警告 + 自动切换也不切" —— 同一个根因：
        `SendInput` 返回 0，一个键都没送出去。最常见的是**权限不一致**：
        CS2 以管理员身份运行、而本程序不是（Windows 的 UIPI 不允许低权限进程往高权限窗口注入），
        其次是安全软件拦了输入注入。
        """
        log("      ⚠️ 按键被系统挡住了（SendInput 返回 0）—— 控制台指令和「自动切换」都会失效。")
        if LAST_SENDINPUT_ERR:
            log(f"         Windows 错误码：{LAST_SENDINPUT_ERR}"
                f"（5 = 拒绝访问 → 权限/安全软件；87 = 参数错误）")
        log("         最常见原因：**CS2 用管理员身份运行，本程序不是**（两边权限不一致）。")
        log("         修法：把本程序也「以管理员身份运行」一次，或者别用管理员启动 CS2。")
        log("         如果权限一致还是不灵，多半是安全软件拦了按键注入（加白名单试试）。")

    def _dry_type(self, text, per_char_ms=14):
        log(f"      [空跑] 输入：{text}")
        return True, [], False

    # ---------------- 配置解析 ----------------
    def commands(self):
        """把配置里的指令整理成"要发出去的字符串列表"。

        ★ 2026-10-09：顺便把"全角/不可见字符"洗干净（`sanitize_console_text`）——
          用户从网页复制来的指令经常带不换行空格、全角引号，它们**看着和 ASCII 一样**，
          旧版只会报一句"有字符没打出来"，用户完全不知道是哪个字符、怎么改。
          现在自动换成 ASCII 等价物，并在日志里说明改了什么。
        """
        raw = self.cfg.get("cs_console_commands") or []
        if isinstance(raw, str):
            raw = raw.replace("\r\n", "\n").split("\n")
        out = []
        for line in raw:
            s = str(line).strip()
            if not s or s.startswith("//") or s.startswith(";"):
                continue                                  # 空行 / 注释
            clean, fixed = sanitize_console_text(s)
            if fixed and len(out) + 1 <= 99:
                # 只报前几条，避免刷屏；同一字符只提一次
                seen = getattr(self, "_fixed_seen", None)
                if seen is None:
                    seen = self._fixed_seen = set()
                new_bits = [f for f in fixed if f not in seen]
                if new_bits:
                    seen.update(new_bits)
                    log(f"   ✏️ 第 {len(out) + 1} 条指令里发现打不出来的字符，已自动替换："
                        f"{'，'.join(new_bits[:6])}"
                        + ("…" if len(new_bits) > 6 else ""))
                    log("      （多半是从网页/Word 复制来的：全角引号、不换行空格之类；"
                        "以后填指令建议手打或从纯文本复制）")
            out.append(clean)
        return out

    def enabled(self):
        return bool(self.commands()) and \
            str(self.cfg.get("cs_console_trigger") or "start").lower() != "off"

    def trigger(self):
        t = str(self.cfg.get("cs_console_trigger") or "start").lower()
        return t if t in ("start", "round1", "manual") else "start"

    # ---------------- 真正发键 ----------------
    def send_now(self, reason="手动", require_focus=None, wait_s=0.0):
        """打开控制台 → 逐条输入 → （可选）关掉控制台。

        `wait_s` > 0 时会先等 CS2 到前台（最多等这么久），`start` 模式用的就是它。
        """
        cmds = self.commands()
        if not cmds:
            log("   ℹ️ 没有配置 CS 控制台指令（cs_console_commands 是空的），跳过")
            return False
        if not self._lock.acquire(blocking=False):
            log("   （上一条控制台指令还在输入中，这次跳过）")
            return False
        try:
            self._busy = True
            want_focus = (self.cfg.get("cs_console_require_focus", True)
                          if require_focus is None else bool(require_focus))
            ck = resolve_console_key(self.cfg.get("cs_console_key") or "`")
            if ck is None:
                log(f"   ❌ 控制台键「{self.cfg.get('cs_console_key')}」认不出来 —— "
                    f"指令没发（可用：` 或 f10 / numpad_enter / backtick 这类键名）")
                return False

            if want_focus:
                cur = self._fg()
                if cur != self.focus_exe:
                    if wait_s <= 0:
                        log(f"   ⏸ 控制台指令没发：当前前台是「{cur or '未知'}」，"
                            f"不是 {self.focus_exe}（等 CS2 到前台再发）")
                        return False
                    t0 = time.time()
                    log(f"   ⏳ 等 CS2 到前台…（最多 {wait_s:.0f} 秒）")
                    while time.time() - t0 < wait_s and not self._stop:
                        if self._fg() == self.focus_exe:
                            break
                        time.sleep(0.5)
                    else:
                        log(f"   ⏸ 等了 {wait_s:.0f} 秒 CS2 也没到前台 —— 控制台指令没发")
                        return False
                    time.sleep(0.8)          # 刚切回游戏，给它一点时间

            log(f"⌨  【CS 控制台指令】{reason}：开始输入 {len(cmds)} 条"
                f"（控制台键 {self.cfg.get('cs_console_key') or '`'}）")
            sc, shift, ext = ck
            self._tap_key(sc, shift, tap_ms=40, extended=ext)
            time.sleep(max(0.0, float(self.cfg.get("cs_console_delay_ms", 400)) / 1000.0))

            gap = max(0.0, float(self.cfg.get("cs_console_gap_ms", 120)) / 1000.0)
            blocked_once = False
            for i, cmd in enumerate(cmds, 1):
                res = self._type_text(cmd)
                ok, bad = res[0], res[1]
                blocked = bool(res[2]) if len(res) > 2 else False
                # 每条指令都敲一次回车，控制台才会执行
                self._tap_key(0x1C, False, tap_ms=30)
                if bad:
                    shown = " ".join(show_char(c) for c in bad[:8])
                    flag = (f"  ⚠️ 打不出来的字符（已跳过）：{shown}"
                            + ("…" if len(bad) > 8 else ""))
                elif blocked:
                    err = LAST_SENDINPUT_ERR
                    why = {5: "拒绝访问（权限不够 / 被安全软件挡）",
                           87: "参数错误"}.get(err, "")
                    flag = (f"  ⚠️ 按键没送出去（SendInput 返回 0"
                            + (f"，错误码 {err}{'：' + why if why else ''}" if err else "")
                            + "）")
                    if not blocked_once:
                        blocked_once = True
                        self._warn_inject_blocked()
                else:
                    flag = ""
                log(f"      [{i}/{len(cmds)}] {cmd}{flag}")
                time.sleep(gap)
            if self.cfg.get("cs_console_close", True):
                self._tap_key(sc, shift, tap_ms=40, extended=ext)
                log("      已关掉控制台")
            self._last_sent = time.time()
            log("   ✔ 控制台指令发送完毕")
            return True
        finally:
            self._busy = False
            self._lock.release()

    # ---------------- 自动触发 ----------------
    def start_auto(self):
        """按 `cs_console_trigger` 起后台线程（`manual` 不动）。"""
        cmds = self.commands()
        if not cmds:
            return
        t = self.trigger()
        if t == "manual":
            log("⌨  CS 控制台指令：已配置 "
                f"{len(cmds)} 条（触发时机=只手动 → 用设置窗口的「现在发送一次」或 "
                f"http://127.0.0.1:{self.cfg.get('gsi_port')}/control/console）")
            return
        if t == "start":
            wait_s = float(self.cfg.get("cs_console_timeout_s", 900) or 900)
            threading.Thread(target=self._auto_start, args=(wait_s,), daemon=True).start()
        else:      # round1：由 App.on_gsi 触发
            log(f"⌨  CS 控制台指令：已配置 {len(cmds)} 条（触发时机=每场第 1 回合）")

    def _auto_start(self, wait_s):
        log(f"⌨  CS 控制台指令：已配置 {len(self.commands())} 条，"
            f"等 CS2 到前台就自动输入（最多等 {wait_s:.0f} 秒）")
        self.send_now("引擎启动后首次", wait_s=wait_s)

    def on_gsi(self, phase, round_no):
        """每场第 1 个冻结时间发一次（`round1` 模式）。"""
        if self.trigger() != "round1" or not self.commands():
            return
        try:
            rn = int(round_no or 0)
        except Exception:
            return
        if rn != 1 or str(phase) not in ("freezetime", "live"):
            return
        # ⚠️ 冻结时间的 GSI 是 10Hz，而"发完一遍"要好几秒 —— 必须**先占位**再起线程，
        #    否则第一条还没打完就会被下一个包再触发一次（自测抓到的 bug）。
        #    30 分钟内只发一次，同一场比赛里 round 回到 1 也不会重复刷。
        if self._busy or time.time() - self._last_sent < 1800:
            return
        self._last_sent = time.time()
        threading.Thread(target=self.send_now, args=("第 1 回合开始",),
                         daemon=True).start()

    def stop(self):
        self._stop = True


class AutoSwitcher:
    """
    自动切视角：直接把提示器排第一的那个人"按出来"。

    设计取舍（都是为了让它在直播里不会帮倒忙）：
      * **分数不够就不动**：没人交火时保持当前视角，绝不乱切（`auto_min_score`）。
      * **最小停留时间**：切过去至少待 `auto_min_dwell` 秒，避免抽搐。
      * **切了要校验**：切完 1.2 秒内看 GSI 的 observer_slot 有没有真的变过去；
        连续失败就自动关掉并说明原因（游戏没焦点 / 键位不对）。
      * **回放中不切**、**被 /control/lock 锁住时不切**。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = False
        self.preset = cfg.get("auto_preset", "normal")
        self.win_bonus = bool(cfg.get("auto_win_bonus", True))
        self.apply_preset(self.preset, quiet=True)
        self.last_switch = 0.0
        self.last_key = None
        self.switches = 0
        self.fails = 0
        self.status = "未启用"
        self.pending = None
        self.disabled_reason = ""

    def apply_preset(self, name, quiet=False):
        """套用灵敏度档位。手机上可以实时切，不用重启。"""
        name = (name or "normal").lower()
        dwell, margin, minscore = AUTO_PRESETS.get(name, AUTO_PRESETS["normal"])
        self.cfg["auto_min_dwell"] = dwell
        self.cfg["auto_switch_margin"] = margin
        self.cfg["auto_min_score"] = minscore
        self.preset = name
        self.status = f"档位 {name}（停留 {dwell}s / 领先 {margin} 分 / 门槛 {minscore}）"
        if not quiet:
            log(f"🎚  自动切换灵敏度 → {name}"
                f"（停留 {dwell}s，领先 {margin} 分才切，分数门槛 {minscore}）")

    def set_win_bonus(self, on):
        self.win_bonus = bool(on)
        self.cfg["auto_win_bonus"] = self.win_bonus
        log(f"🎚  对枪胜率加成 → {'开' if self.win_bonus else '关'}"
            + ("（镜头偏向预测活下来的一方）" if self.win_bonus else "（只看谁在交火，不猜输赢）"))

    def set_enabled(self, on):
        self.enabled = bool(on)
        self.pending = None
        if self.enabled:
            self.fails = 0
            self.disabled_reason = ""
            self.status = "已开启，等待第一个目标"
            log("🎯 自动切换：已开启（来自手机上的开关）")
        else:
            self.status = "已关闭"
            log("🎯 自动切换：已关闭")

    def on_packet(self, snap, director):
        now = time.time()

        # --- 1. 先校验上一次切换到底成没成 ---
        if self.pending:
            p = self.pending
            obs = next((r for r in (snap.get("players") or []) if r.get("observed")), None)
            if obs and obs.get("sid") == p["sid"]:
                self.pending = None
                self.fails = 0
                self.status = "已切到 按[%s]" % p["key"]
            elif now - p["at"] > 1.2:
                self.pending = None
                self.fails += 1
                self.status = "切换没生效（连续 %d 次）" % self.fails
                log("⚠️  自动切换：按了 [%s] 但画面没跟过去（连续第 %d 次）"
                    % (p["key"], self.fails))
                if self.fails >= 3:
                    self.enabled = False
                    self.disabled_reason = "切换连续 3 次没生效"
                    self.status = "已自动关闭：切换没生效"
                    log("")
                    log("=" * 64)
                    log("❌ 自动切换已自动关闭：连按了 3 次数字键，游戏画面都没有跟过去。")
                    log("   最可能的原因（按可能性排序）：")
                    log("     1) **CS2 窗口不在前台** —— 合成按键只落到有焦点的窗口。")
                    log("        切视角时游戏得是前台窗口（你手动按数字键也一样）。")
                    log("     2) 你的数字键没绑到观察位 —— 先把键位校准一次。")
                    log("     3) 安全软件拦了合成按键。")
                    log("   修好之后在手机上重新打开开关即可。")
                    log("=" * 64)
                    return
            else:
                return      # 还在等校验，这帧先不切别的

        if not self.enabled:
            return

        # 记录前台进程，手机上直接显示，方便判断"能不能自动切"
        try:
            self.focus = foreground_exe()
        except Exception:
            self.focus = ""

        # --- 2. 现在不该切的情况 ---
        if director.locked or director.replay_active:
            return
        if (snap.get("phase") or "") not in ("live", "bomb", "defuse"):
            self.status = "暂停（非交战阶段）"
            return
        # ★ 前台必须是 CS2，否则数字键会打进别的程序（OBS 里绑了数字键就会乱切场景）
        if self.cfg.get("auto_require_focus", True):
            exe = foreground_exe()
            if exe != (self.cfg.get("auto_focus_exe", "cs2.exe") or "").lower():
                self.status = f"暂停（前台是 {exe or '未知程序'}，不是 CS2）"
                return
        if now - self.last_switch < float(self.cfg.get("auto_min_dwell", 2.5)):
            return

        pl = snap.get("players") or []
        if not pl:
            return
        top = pl[0]
        if top.get("observed"):
            self.status = "已在最优视角"
            return
        # ★ 当前正在看的人**真的在对枪**（互相瞄）→ 镜头不许离开他。
        #   这是"架着枪却被切到后面拿包的人"那个问题的正面解法。
        #   注意只对"互相瞄"生效：单纯挨打 / 单向架枪 / 隔墙对位都不锁，
        #   否则镜头会被一个被远处蹭到的人焊死，错过真正的对枪。
        cur = next((r for r in pl if r.get("observed")), None)
        if cur is not None and cur.get("locked"):
            self.status = "锁定当前视角（他正在对枪，不切走）"
            return

        # ★★ 层级跃升：镜头现在看的人层级不如新目标 → **必须切**，不管分数差多少。
        #    "架枪的夺取正在对枪的视角"就是这么发生的：架枪那位叠了一堆静态分（可达 243），
        #    对枪那位因为"预测要输"被倒扣（低到 148）。分层之后，层级高的一定赢。
        top_tier = top.get("tier", 2)
        cur_tier = cur.get("tier", 2) if cur is not None else 2
        tier_up = (cur is None) or (top_tier < cur_tier)

        need = float(self.cfg.get("auto_min_score", 45))
        if not tier_up and top.get("score", 0) < need:
            self.status = "保持当前（最高分 %s < %d）" % (top.get("score"), int(need))
            return
        # 当前正在看的人如果分数差不多，就别切 —— 这是"切太快、看着乱"的主因。
        # 但"层级跃升"（真打起来了）不受这个限制。
        margin = float(self.cfg.get("auto_switch_margin", 18))
        if (not tier_up and cur is not None
                and (top.get("score", 0) - cur.get("score", 0)) < margin):
            self.status = "保持当前（领先不到 %d 分）" % int(margin)
            return
        if top.get("key") is None:
            return

        # --- 3. 切 ---
        key = str(top["key"])
        if send_digit(key):
            self.last_switch = now
            self.last_key = key
            self.switches += 1
            self.pending = {"sid": top["sid"], "key": key, "at": now}
            self.status = "切到 按[%s]（%s 分）" % (key, top.get("score"))
            log("🎯 自动切换 → 按 [%s]  %s  分数 %s"
                % (key, " · ".join(top.get("reasons") or []) or "—", top.get("score")))
        else:
            self.enabled = False
            self.status = "已关闭：发不出按键"
            self.disabled_reason = "SendInput 发送失败（按键被系统挡住了）"
            log("❌ 自动切换：SendInput 发送失败，已关闭。")
            log("   最常见原因：**CS2 以管理员身份运行、本程序不是** —— 两边权限要一致")
            log("   （把本程序也「以管理员身份运行」一次）；也可能是安全软件拦了按键注入。")

    def state(self):
        return {"enabled": self.enabled, "status": self.status,
                "switches": self.switches, "lastKey": self.last_key,
                "focus": getattr(self, "focus", ""),
                "wantExe": self.cfg.get("auto_focus_exe", "cs2.exe"),
                "preset": self.preset, "winBonus": self.win_bonus,
                "dwell": self.cfg.get("auto_min_dwell"),
                "margin": self.cfg.get("auto_switch_margin"),
                "minScore": self.cfg.get("auto_min_score"),
                "reason": self.disabled_reason}


class AttentionModel:
    """
    把每个 GSI 包变成"该看谁"的排序 + 一条击杀播报。

    关键点：GSI 的 allplayers[*] 里同时有 `position` 和 `forward`（视角朝向向量），
    所以能算出"谁在瞄谁"。两个玩家互相瞄住 = 对枪即将发生，这是最强的提示信号。
    （这一条之前被我漏掉了 —— 我一度以为 GSI 不提供视角朝向。）
    """

    def __init__(self):
        self.prev = {}          # sid -> {"hp":..., "kills":...}
        self.kills = []         # 最近的击杀，用于手机上的播报
        self.last_top = None
        self.last_top_at = 0.0
        self.snap = {"ready": False}

    def update(self, g: dict) -> dict:
        now = time.time()
        allp = g.get("allplayers") or {}
        rnd = g.get("round") or {}
        pc = g.get("phase_countdowns") or {}
        mp = g.get("map") or {}
        bomb = g.get("bomb") or {}

        players = {}
        observed_sid = ((g.get("player") or {}).get("steamid") or "").strip() or None
        for sid, p in allp.items():
            if not isinstance(p, dict):
                continue
            st = p.get("state") or {}
            hp = int(st.get("health") or 0) if "health" in st else None
            pos = parse_vec(p.get("position"))
            fwd = parse_vec(p.get("forward"))
            raw_ws = [(w.get("name"), (w.get("state") or ""))
                      for w in (p.get("weapons") or {}).values()
                      if isinstance(w, dict) and w.get("name")]
            ws = [n for n, _ in raw_ws]
            # ★ 只认真正拿在手里的那把（state == "active"）。
            #   武器栏里会残留之前捡过的枪，用整栏判定会算出错误的武器威力。
            active = next((n for n, s in raw_ws if s == "active"), None)
            if active is None and ws:
                # 没有 active 标记时（热身/观战某些时刻），退化成"排除刀和 C4 的第一把"
                active = next((n for n in ws
                               if not n.startswith("weapon_knife") and n != "weapon_c4"),
                              ws[0])
            slot = p.get("observer_slot")
            players[sid] = {
                "sid": sid, "name": (p.get("name") or sid[:8]).strip(), "team": p.get("team") or "",
                "hp": hp, "pos": pos, "fwd": fwd, "weapons": ws, "active": active,
                "slot": slot, "key": slot_key(slot),
                "observed": (sid == observed_sid),
                "kills": int(st.get("round_kills") or 0) if "round_kills" in st else None,
                "r_dmg": int(st.get("round_totaldmg") or 0) if "round_totaldmg" in st else 0,
                "armor": bool(st.get("armor")), "helmet": bool(st.get("helmet")),
                "m_kills": (p.get("match_stats") or {}).get("kills"),
                "m_deaths": (p.get("match_stats") or {}).get("deaths"),
                "flashed": float(st.get("flashed") or 0) if "flashed" in st else 0.0,
                "burning": bool(st.get("burning")), "money": st.get("money"),
                "has_c4": any(w == "weapon_c4" for w in ws),
                # ★ 只按"手里拿的"判狙击，不按武器栏（栏里有残枪会误判）
                "sniper": (active in ("weapon_awp", "weapon_ssg08")),
            }

        # ---- 击杀配对：kills 增加的是凶手，血量归零的是受害者 ----
        new_kills, new_deaths = [], []
        for sid, pl in players.items():
            old = self.prev.get(sid)
            if old is None:
                continue
            if pl["kills"] is not None and old["kills"] is not None and pl["kills"] > old["kills"]:
                new_kills.append(sid)
            if old["hp"] is not None and old["hp"] > 0 and pl["hp"] == 0:
                new_deaths.append(sid)
        for i, ksid in enumerate(new_kills):
            vsid = new_deaths[i] if i < len(new_deaths) else None
            kp = players.get(ksid, {})
            vp = players.get(vsid, {}) if vsid else {}
            self.kills.append({
                "t": now,
                "killer": kp.get("name", "?"), "killerTeam": kp.get("team", ""),
                "killerKey": kp.get("key"), "killerKills": kp.get("kills") or 1,
                "victim": vp.get("name", "?"), "victimTeam": vp.get("team", ""),
                "victimKey": vp.get("key"),
                "weapon": next((w for w in (kp.get("weapons") or [])
                                if w and not w.startswith("weapon_knife")
                                and w not in ("weapon_c4",)), ""),
            })
        self.kills = [k for k in self.kills if now - k["t"] < 30][-8:]

        # ---- 存活/队伍统计 ----
        alive = {"CT": 0, "T": 0}
        for pl in players.values():
            if pl["hp"] is not None and pl["hp"] > 0:
                alive[pl["team"]] = alive.get(pl["team"], 0) + 1

        # ---- 已下包/包的位置 ----
        planted = (bomb.get("state") == "planted") or (rnd.get("bomb") is True)
        bomb_pos = parse_vec(bomb.get("position"))
        bomb_site = bomb.get("countdown")  # 只是个占位，下面用 state 描述

        # ---- 逐人打分 ----
        scored = []
        for sid, pl in players.items():
            if pl["hp"] is None or pl["hp"] <= 0:
                continue
            s, why = 0, []
            old = self.prev.get(sid) or {}

            took_damage = False
            if old.get("hp") is not None and pl["hp"] < old["hp"]:
                s += AW["taking_damage"]
                took_damage = True
                why.append(f"正在挨打 -{old['hp'] - pl['hp']}")

            # 找最近的敌人 + 朝向判定
            best_d = None
            aim_any = mutual = False
            duel_dist = None
            duel_opp = None
            if pl["pos"]:
                for osid, op in players.items():
                    if osid == sid or op["team"] == pl["team"] or not op["pos"]:
                        continue
                    if op["hp"] is None or op["hp"] <= 0:
                        continue
                    d = _dist3(pl["pos"], op["pos"])
                    if best_d is None or d < best_d:
                        best_d = d
                    if pl["fwd"] and d < AIM_MAX_DIST:
                        import math
                        vx, vy = op["pos"][0] - pl["pos"][0], op["pos"][1] - pl["pos"][1]
                        n = (vx * vx + vy * vy) ** 0.5
                        if n > 1:
                            cosang = (pl["fwd"][0] * vx + pl["fwd"][1] * vy) / n
                            ang = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
                            if ang <= AIM_ANGLE_DEG:
                                aim_any = True
                                # 对方也在瞄我，而且近到真的能打起来 → 对枪
                                if op["fwd"] and d < DUEL_MAX_DIST:
                                    vx2, vy2 = pl["pos"][0] - op["pos"][0], pl["pos"][1] - op["pos"][1]
                                    n2 = (vx2 * vx2 + vy2 * vy2) ** 0.5
                                    if n2 > 1:
                                        c2 = (op["fwd"][0] * vx2 + op["fwd"][1] * vy2) / n2
                                        if math.degrees(math.acos(max(-1.0, min(1.0, c2)))) <= AIM_ANGLE_DEG:
                                            mutual = True
                                            duel_dist = d
                                            duel_opp = op
                            if not mutual and aim_any and duel_opp is None:
                                # 单向瞄：也算一个"先手"对枪，权重折半
                                duel_dist, duel_opp = d, op
            if mutual:
                s += AW["mutual_aim"]; why.append(f"对枪中 {int(duel_dist)}")
            elif aim_any:
                s += AW["aiming_enemy"]; why.append(f"在瞄人 {int(best_d or 0)}")

            # ★★ 交火主导权 + 层级（排序时**层级优先于分数**）
            #   0 = 真在打：正在挨打 / 互相瞄且距离很近   ← 任何静态信号都压不过
            #   1 = 对峙中：单向架枪，或远距离互相对望
            #   2 = 其他：按"叙事价值"排（持包/残局/包点…）
            #   为什么要分距离：DUEL_MAX_DIST=1800 太宽，两个人隔着墙在 1800 单位外
            #   对望也会拿满主导权，把真正的近距离对枪挤掉（实测：173 vs 172）。
            engage_d = duel_dist if mutual else best_d
            strong_fight = bool(took_damage) or (
                mutual and engage_d is not None and engage_d < DUEL_CLOSE_DIST)
            weak_fight = (not strong_fight) and aim_any and best_d is not None \
                and best_d < ENGAGE_DIST
            engaging = bool(strong_fight or weak_fight)
            # 锁镜头只对"真对枪"（互相瞄且很近）生效。
            # 单纯"挨打"也算第 0 层（要排前面），但不锁 ——
            # 否则被一个远处燃烧瓶蹭到就会把镜头焊死，错过真正的对枪。
            locked_in = bool(mutual and engage_d is not None
                             and engage_d < DUEL_CLOSE_DIST)
            tier = 0 if strong_fight else (1 if weak_fight else 2)
            if strong_fight:
                s += DUEL_DOMINANCE
                # 越近越紧急：同时有两场交火时，先看近的那场
                if engage_d is not None:
                    s += int(ENGAGE_NEAR_BONUS
                             * max(0.0, 1.0 - engage_d / DUEL_CLOSE_DIST))
            elif weak_fight:
                s += DUEL_DOMINANCE_WEAK
                why.append("交战中")

            # ★ 对枪胜率：把镜头往"能活下来的一方"上偏（手机上可关）
            if (duel_opp is not None and duel_dist is not None
                    and getattr(self, "win_bonus", True)):
                prob = duel_win_prob(pl, duel_opp, duel_dist)
                mul = 1.0 if mutual else 0.5     # 互相瞄 = 完整权重；单向瞄 = 折半
                s += int(round(DUEL_WIN_BONUS * (prob - 0.5) * 2 * mul))
                if mutual:
                    why.append(f"胜率 {int(round(prob * 100))}%")
                else:
                    why.append(f"先手 {int(round(prob * 100))}%")
            if best_d is not None:
                if best_d < NEAR_VERY_CLOSE:
                    s += AW["enemy_very_close"]; why.append(f"敌人很近 {int(best_d)}")
                elif best_d < NEAR_CLOSE:
                    s += AW["enemy_close"]

            if alive.get(pl["team"], 0) == 1 and (alive.get("T" if pl["team"] == "CT" else "CT", 0) > 0):
                s += AW["last_alive"]; why.append("残局")

            if pl["has_c4"]:
                s += AW["has_bomb"]; why.append("持包")
            if planted and pl["team"] == "CT" and bomb_pos and pl["pos"]:
                if _dist3(pl["pos"], bomb_pos) < 1200:
                    s += AW["near_bomb_ct"]; why.append("在包点")

            for k in self.kills:
                if now - k["t"] < 3.0 and k["killer"] == pl["name"]:
                    s += AW["just_killed"]; why.append("刚击杀")
                    break

            if pl["flashed"] > 0.5:
                s += AW["flashed"]; why.append("被闪")
            if pl["sniper"]:
                s += AW["sniper"]; why.append("狙击")
            if pl["hp"] < 40:
                s += AW["low_hp"]; why.append(f"残血 {pl['hp']}")
            # 附近有飞行中的道具
            gns = g.get("grenades") or g.get("allgrenades") or {}
            if pl["pos"] and gns:
                for gv in gns.values():
                    gp = parse_vec((gv or {}).get("position")) if isinstance(gv, dict) else None
                    if gp and _dist3(pl["pos"], gp) < 600:
                        s += AW["nade_incoming"]; why.append("道具在飞")
                        break

            scored.append({"sid": sid, "name": pl["name"], "team": pl["team"],
                           "hp": pl["hp"], "score": s, "reasons": why[:3],
                           "kills": pl["kills"] or 0,
                           "key": pl["key"], "slot": pl["slot"], "observed": pl["observed"],
                           "engaging": engaging, "locked": locked_in, "tier": tier,
                           "weapon": (pl.get("active") or "").replace("weapon_", ""),
                           "hasBomb": pl["has_c4"], "sniper": pl["sniper"]})

        # 粘性：上一秒的第一名加点分，避免列表每 100ms 乱跳
        if self.last_top and now - self.last_top_at < 2.5:
            for r in scored:
                if r["sid"] == self.last_top:
                    r["score"] += STICKY_BONUS
                    break
        # ★★ 先按"层级"排，再按分数排。
        #    这是**结构性优先级**，不是"加一个很大的数"。
        #    为什么必须这样：加法是可以被淹没的 ——
        #      架枪的人最多能叠到 243（在瞄人25+弱主导70+敌人很近25+残局25+持包14+刚击杀30…）
        #      对枪的人最低只有 148（对枪中60+强主导130-胜率42）
        #    于是架枪的会"夺取"正在对枪的视角。分层之后这种事不可能发生。
        scored.sort(key=lambda r: (r["tier"], -r["score"]))
        if scored:
            self.last_top, self.last_top_at = scored[0]["sid"], now

        self.prev = {sid: {"hp": pl["hp"], "kills": pl["kills"]} for sid, pl in players.items()}

        ct_score = ((mp.get("team_ct") or {}).get("score"))
        t_score = ((mp.get("team_t") or {}).get("score"))
        last_kill = self.kills[-1] if self.kills else None

        self.snap = {
            "ready": True,
            "ts": now,
            "map": (mp.get("name") or "").replace("de_", ""),
            "round": mp.get("round"),
            "phase": pc.get("phase") or rnd.get("phase") or "",
            "phaseIn": pc.get("phase_ends_in"),
            "scoreCT": ct_score, "scoreT": t_score,
            "aliveCT": alive.get("CT", 0), "aliveT": alive.get("T", 0),
            "planted": bool(planted),
            "players": scored,
            "hasKeys": any(r.get("key") for r in scored),
            "showNames": bool(getattr(self, "show_names", False)),
            "winBonus": bool(getattr(self, "win_bonus", True)),
            "killAgo": (now - last_kill["t"]) if last_kill else None,
            "lastKill": last_kill,
            "recentKills": [k for k in self.kills if now - k["t"] < 6],
        }
        return self.snap


VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0b0f14">
<title>导播副驾</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0;background:#0b0f14;color:#e8eef6;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  overscroll-behavior:none;user-select:none}
#wrap{padding:10px 12px 96px}
#hdr{display:flex;justify-content:space-between;align-items:center;
  font-size:15px;color:#8fa3b8;letter-spacing:.5px;margin-bottom:6px}
#hdr b{color:#e8eef6;font-size:19px}
#dot{width:9px;height:9px;border-radius:50%;background:#ff5252;
  display:inline-block;margin-right:6px}
#dot.on{background:#57d38c}
#state{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:9px}
.pill{background:#141b24;border:1px solid #1f2a36;border-radius:999px;
  padding:4px 11px;font-size:14px;color:#9fb3c8}
.pill.hot{background:#3a1414;border-color:#7a2020;color:#ff9a9a}
.pill.bomb{background:#3a2a10;border-color:#7a5a20;color:#ffc46b}

/* ===== 自动切换开关 ===== */
#autobox{background:#121922;border:1px solid #22303f;border-radius:14px;
  padding:10px 12px;margin-bottom:10px}
#autobtn{width:100%;border:none;border-radius:11px;padding:15px 10px;
  font-size:22px;font-weight:800;letter-spacing:.5px;color:#0b0f14;
  background:#2e3a48;color:#c8d8e8;transition:background .15s}
#autobtn.on{background:#39d07e;color:#04220f}
#autobtn:active{transform:scale(.985)}
#autostat{font-size:13px;color:#7d93a8;margin-top:7px;line-height:1.5}
#autostat b{color:#9fb3c8;font-weight:600}
#autowarn{display:none;font-size:13px;color:#ffc46b;margin-top:6px;line-height:1.5}
#autowarn.on{display:block}
#tuner{margin-top:9px;padding-top:9px;border-top:1px solid #1f2a36}
#tuner .lbl{font-size:12px;color:#5d7a99;margin-bottom:5px}
#tuner .row2{display:flex;gap:6px;margin-bottom:8px}
.pbtn{flex:1;background:#1a2530;border:1px solid #2b3d52;color:#9fb3c8;
  border-radius:9px;padding:9px 4px;font-size:14px;font-weight:600}
.pbtn.on{background:#1f4a6e;border-color:#4aa3ff;color:#fff}
.pbtn.wb{font-size:13px}
.pbtn.wb.on{background:#1e3a2a;border-color:#2f6f4f;color:#8ee6b0}

/* ===== 回放功能开关（默认关；2026-10-07 加）===== */
#featbox{background:#121922;border:1px solid #22303f;border-radius:14px;
  padding:10px 12px;margin-bottom:10px}
#featbtn{width:100%;border:none;border-radius:11px;padding:15px 10px;
  font-size:22px;font-weight:800;letter-spacing:.5px;background:#2e3a48;color:#c8d8e8;
  transition:background .15s}
#featbtn.on{background:#4aa3ff;color:#04203a}
#featbtn:active{transform:scale(.985)}
#featstat{font-size:13px;color:#7d93a8;margin-top:7px;line-height:1.5}
#featstat b{color:#9fb3c8;font-weight:600}
#featstat .mem{color:#8ee6b0}
#featwarn{display:none;font-size:13px;color:#ffc46b;margin-top:6px;line-height:1.5}
#featwarn.on{display:block}
#feattuner{margin-top:9px;padding-top:9px;border-top:1px solid #1f2a36}
#feattuner .lbl{font-size:12px;color:#5d7a99;margin-bottom:5px}
#feattuner .row2{display:flex;gap:6px}

#warn{display:none;background:#3a2a10;border:1px solid #7a5a20;color:#ffc46b;
  border-radius:10px;padding:9px 11px;font-size:14px;margin-bottom:9px;line-height:1.5}
#warn.on{display:block}
#cue{display:none;background:#16321f;border:1px solid #2f6f4f;border-radius:12px;
  padding:12px;margin-bottom:9px;font-size:20px;color:#8ee6b0;text-align:center;
  font-weight:700;animation:cue 1.1s infinite alternate}
#cue.on{display:block}
@keyframes cue{from{background:#16321f}to{background:#204d2e}}
#clipbox{display:none;border-radius:12px;padding:10px 12px;margin-bottom:9px;
  font-size:17px;line-height:1.45;text-align:center;font-weight:600}
#clipbox b{font-size:19px}
#clipbox.arm{display:block;background:#0f2a3a;border:1px solid #2f6f8f;color:#9fd8ff}
#clipbox.soon{display:block;background:#3a2f10;border:1px solid #a07a20;color:#ffd166;
  animation:clipblink .7s infinite alternate}
#clipbox.dead{display:block;background:#3a1414;border:1px solid #a03030;color:#ff9a9a;
  animation:clipblink .5s infinite alternate}
#clipbox.ok{display:block;background:#16321f;border:1px solid #2f6f4f;color:#8ee6b0}
#clipbox.play{display:block;background:#1d2b3a;border:1px solid #3d5a7a;color:#c8d8e8}
#clipbox.idle{display:block;background:#141c25;border:1px solid #24313f;color:#6d8399;
  font-size:15px;font-weight:500}
@keyframes clipblink{from{filter:brightness(1)}to{filter:brightness(1.4)}}
#killbar{margin-bottom:8px}
.kb{background:#2a1010;border:1px solid #ff5252;border-radius:11px;
  padding:9px 11px;font-size:17px;margin-bottom:6px;color:#ffd9d9}
.kb .k{font-weight:800;color:#fff;font-size:20px;margin-right:4px}
.kb .w{color:#ffd166}
.row{border-radius:14px;margin-bottom:8px;background:#121922;
  border-left:7px solid #2a3644;padding:11px 13px}
.row.ct{border-left-color:#4aa3ff}
.row.t{border-left-color:#ffb03a}
.row.top{background:#16202c;box-shadow:0 0 0 1px #2b3d52 inset;padding:14px 14px 12px}
.row.dim{opacity:.45;padding:8px 13px}
.kbig{text-align:center;margin:2px 0 8px;line-height:1}
.kbig small{font-size:28px;font-weight:600;color:#9fb3c8;margin-right:8px;
  vertical-align:middle}
.kbig u{text-decoration:none;font-size:78px;font-weight:800;letter-spacing:-2px;
  color:#fff;background:#134a75;border-radius:16px;padding:0 22px 8px;
  display:inline-block;min-width:130px}
.row.top.t .kbig u{background:#7a5210}
.kdone{text-align:center;font-size:26px;font-weight:700;color:#8ee6b0;
  margin:12px 0;line-height:1.4}
.rz{font-size:18px;color:#ffd166;text-align:center;margin-top:4px}
.bar{height:5px;border-radius:3px;background:#1d2836;margin-top:9px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#2f6f4f,#57d38c)}
.row.mid .bar i{background:linear-gradient(90deg,#6b5a1f,#e0b93c)}
.row.low .bar i{background:linear-gradient(90deg,#4a3a3a,#8a6a6a)}
.r2{display:flex;align-items:center;flex-wrap:wrap}
.kmid{font-size:36px;font-weight:800;color:#fff;background:#1c2836;border-radius:11px;
  padding:1px 14px 5px;min-width:56px;text-align:center;margin-right:11px}
.row.dim .kmid{font-size:27px;padding:0 11px 3px;min-width:46px}
.knone{font-size:22px;color:#5d7a99;margin-right:11px}
.txt{font-size:17px;color:#c8d8e8}
.row.dim .txt{font-size:15px}
.obs{font-size:15px;color:#8ee6b0;margin-left:9px}
#tgs{position:fixed;left:12px;right:12px;bottom:calc(10px + env(safe-area-inset-bottom));
  display:flex;gap:8px;justify-content:flex-end}
.tg{background:#1a2530;border:1px solid #2b3d52;color:#9fb3c8;border-radius:999px;
  padding:9px 15px;font-size:14px}
.tg.on{background:#1e3a2a;border-color:#2f6f4f;color:#8ee6b0}
</style></head><body><div id="wrap">
<div id="hdr"><span><span id="dot"></span><b id="map">&mdash;</b> <span id="rnd"></span></span>
  <span id="score"></span></div>
<div id="state"></div>
<div id="clipbox"></div>

<div id="featbox">
  <button id="featbtn">回放功能：读取中…</button>
  <div id="featstat">&nbsp;</div>
  <div id="feattuner">
    <div class="lbl">内存模式（省内存 = 按 ← 才开始攒帧，裁完就释放）</div>
    <div class="row2">
      <button class="pbtn" data-m="armed">省内存</button>
      <button class="pbtn" data-m="buffer">常驻缓冲</button>
    </div>
  </div>
  <div id="featwarn"></div>
</div>

<div id="autobox">
  <button id="autobtn">自动切换：读取中…</button>
  <div id="autostat">&nbsp;</div>
  <div id="tuner">
    <div class="lbl">切换灵敏度（实时生效，不用重启）</div>
    <div class="row2">
      <button class="pbtn" data-p="fast">灵敏</button>
      <button class="pbtn" data-p="normal">标准</button>
      <button class="pbtn" data-p="calm">保守</button>
    </div>
    <div class="row2" style="margin-bottom:0">
      <button class="pbtn wb" id="wbbtn">对枪胜率加成</button>
    </div>
  </div>
  <div id="autowarn"></div>
</div>

<div id="warn"></div>
<div id="cue">&#9654; 打完了：先按 [ &larr; ] 标入点、再按 [ &rarr; ] 裁出这一段</div>
<div id="killbar"></div>
<div id="list"></div>
</div>
<div id="tgs"><button class="tg" id="tgb">震动提醒</button><button class="tg" id="tgn">显示名字</button></div>
<script>
var vib=false, showNames=false, lastSnapAt=Date.now(), lastKillCount=0;
var lastSnap=null, clipStage=0;   // 入点提醒阶段：0 没提醒 / 1 快过期 / 2 已过期
var TOKEN=(new URLSearchParams(location.search)).get('k')||'';
var autoWanted=null;      // 用户点开关后的期望状态，等服务器确认
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

var abtn=document.getElementById('autobtn');
function tune(qs,btn){
  if(!TOKEN){ needToken(); return; }
  fetch('/auto?k='+encodeURIComponent(TOKEN)+'&'+qs)
    .then(function(r){return r.json();})
    .then(function(j){ if(j&&j.ok) paintAuto(j.auto); })
    .catch(function(){});
}
function needToken(){
  var w=document.getElementById('autowarn');
  w.className='on';
  w.textContent='这个页面没带口令，不能调自动切换。请用引擎窗口里打印的完整地址（带 ?k=...）。';
}
// ---------- 回放功能开关（默认关；开了才会响应 ← / → / 小键盘）----------
var fbtn=document.getElementById('featbtn');
function needTokenF(){
  var w=document.getElementById('featwarn');
  w.className='on';
  w.textContent='这个页面没带口令，不能改回放功能。请用引擎窗口里打印的完整地址（带 ?k=...）。';
}
function tuneFeature(qs){
  if(!TOKEN){ needTokenF(); return; }
  fetch('/feature?k='+encodeURIComponent(TOKEN)+'&'+qs)
    .then(function(r){return r.json();})
    .then(function(j){ if(j&&j.ok){ paintFeature(j.feature); if(lastSnap){lastSnap.feature=j.feature; paintClip();} } })
    .catch(function(){});
}
fbtn.onclick=function(){
  if(!TOKEN){ needTokenF(); return; }
  var want=fbtn.classList.contains('on')?0:1;
  fbtn.textContent='回放功能：'+(want?'开':'关')+'（切换中…）';
  tuneFeature('replay='+want);
};
Array.prototype.forEach.call(document.querySelectorAll('.pbtn[data-m]'),function(b){
  b.onclick=function(){
    if(!TOKEN){ needTokenF(); return; }
    document.querySelectorAll('.pbtn[data-m]').forEach(function(x){x.className='pbtn';});
    b.className='pbtn on';
    tuneFeature('mode='+b.getAttribute('data-m'));
  };
});
function paintFeature(f){
  if(!f) return;
  fbtn.className=f.enabled?'on':'';
  fbtn.textContent='回放功能：'+(f.enabled?'开':'关');
  document.querySelectorAll('.pbtn[data-m]').forEach(function(x){
    x.className='pbtn'+((x.getAttribute('data-m')===f.mode)?' on':'');
  });
  var k=f.keys||{};
  var s='';
  if(f.enabled){
    s+='← <b>'+esc(k.mark_in||'?')+'</b> 标入点　→ <b>'+esc(k.mark_out||'?')+
       '</b> 裁片段　<b>'+esc(k.save||'?')+'</b> 留档　<b>'+esc(k.play||'?')+'</b> 播放<br>';
    if(f.mode==='armed'){
      s+='省内存模式：按 ← 才开始攒帧；裁完立即释放，内存里只留最近这一段。<br>';
    } else {
      s+='常驻缓冲：一直攒着最近 <b>'+f.seconds+'s</b>，可以往回标 ←。<br>';
    }
    s+='<span class="mem">内存：'+(f.armed?('正在攒帧 ≈ '+(f.oneCopyGB*2).toFixed(2)+' GB')
                                        :('空闲 ≈ 0（上限 '+(f.oneCopyGB*2).toFixed(2)+' GB）'))+
       '</span>';
  } else {
    s='关着的时候 ← / → / 保留片段 / 播放 <b>都不动手</b>，插件内存也还回去了。<br>'+
      '提示器和自动切观察位<b>不受影响</b>，照常工作。<br>'+
      '<span class="mem">开启后最多约 '+(f.steadyGB).toFixed(2)+' GB（'+
      (f.mode==='armed'?'省内存模式，空闲时 ≈0':'常驻缓冲模式，一直占着')+'）</span>';
  }
  document.getElementById('featstat').innerHTML=s;
}
abtn.onclick=function(){
  if(!TOKEN){ needToken(); return; }
  var want=abtn.classList.contains('on')?0:1;
  abtn.textContent='自动切换：'+(want?'开':'关')+'（切换中…）';
  tune('on='+want);
};
Array.prototype.forEach.call(document.querySelectorAll('.pbtn[data-p]'),function(b){
  b.onclick=function(){
    if(!TOKEN){ needToken(); return; }
    document.querySelectorAll('.pbtn[data-p]').forEach(function(x){x.className='pbtn';});
    b.className='pbtn on';
    tune('preset='+b.getAttribute('data-p'));
  };
});
document.getElementById('wbbtn').onclick=function(){
  if(!TOKEN){ needToken(); return; }
  var want=this.classList.contains('on')?0:1;
  tune('winbonus='+want);
};
function paintAuto(a){
  if(!a) return;
  abtn.className=a.enabled?'on':'';
  abtn.textContent='自动切换：'+(a.enabled?'开':'关');
  document.querySelectorAll('.pbtn[data-p]').forEach(function(x){
    x.className='pbtn'+((x.getAttribute('data-p')===a.preset)?' on':'');
  });
  var wb=document.getElementById('wbbtn');
  wb.className='pbtn wb'+(a.winBonus?' on':'');
  wb.textContent='对枪胜率加成：'+(a.winBonus?'开':'关');
  var s='';
  if(a.enabled){
    s='状态：<b>'+esc(a.status||'')+'</b>　已切 '+(a.switches||0)+' 次';
    if(a.lastKey) s+='　最近：按['+esc(a.lastKey)+']';
    s+='<br>当前档位：停留 <b>'+(a.dwell||'?')+'s</b>　领先 <b>'+(a.margin||'?')+
       ' 分</b>才切　门槛 <b>'+(a.minScore||'?')+' 分</b>';
    var fg=esc(a.focus||'未知');
    var ok=(a.focus||'').toLowerCase()===(a.wantExe||'cs2.exe').toLowerCase();
    s+='<br>当前前台：'+(ok?('<b style="color:#8ee6b0">'+fg+' ✓</b>')
                          :('<b style="color:#ff9a9a">'+fg+' ✗ 不是 CS2，不会切</b>'));
  } else {
    s=esc(a.status||'关闭')+(a.reason?('　原因：'+esc(a.reason)):'');
    s+='<br>开之前先确认：<b>CS2 窗口要在前台</b>，数字键确实是切观察位的。';
  }
  document.getElementById('autostat').innerHTML=s;
}
document.getElementById('tgb').onclick=function(){
  vib=!vib; this.className='tg'+(vib?' on':'');
  if(vib&&navigator.vibrate) navigator.vibrate(30);
};
document.getElementById('tgn').onclick=function(){
  showNames=!showNames; this.className='tg'+(showNames?' on':'');
};
// ---- 顶部横幅：← 入点还剩几秒可以按 →（缓冲只有 bufMax 秒，超了就框不住）----
function paintClip(){
  var box=document.getElementById('clipbox'), d=lastSnap;
  if(!box) return;
  if(!d||!d.clip){ box.className=''; box.innerHTML=''; return; }
  var c=d.clip, cls='', h='';
  var drift=(Date.now()-lastSnapAt)/1000;
  if(c.markAgo==null) clipStage=0;
  var buf=Number(c.bufMax||10).toFixed(0);
  // 回放功能关着的时候，先说清楚"按了也不会动"，别让人以为按键坏了
  if(c.enabled===false){
    box.className='idle';
    box.innerHTML='回放功能已关闭 —— 按 &larr; / &rarr; / 小键盘都不会动手（上面的开关可以打开）';
    return;
  }
  if(c.markAgo!=null){
    var ago=Number(c.markAgo)+drift;
    var left=Number(c.markLeft!=null?c.markLeft:0)-drift;
    if(c.mode==='armed'){
      // 省内存模式：缓冲从按 ← 那一刻开始攒，超上限才会覆盖开头
      if(left<=0.2){
        cls='dead';
        h='<b>&#9888;&#65039; 攒帧超过 '+buf+' 秒了</b><br>开头会被覆盖，现在按 &rarr; '+
          '会从 '+Number(c.avail||0).toFixed(1)+' 秒处开始';
        if(clipStage<2){ clipStage=2;
          if(vib&&navigator.vibrate) navigator.vibrate([120,90,120,90,120]); }
      } else {
        cls=(left<=2.5)?'soon':'arm';
        h='<b>&#9193; 正在攒帧 '+(ago).toFixed(1)+' 秒</b><br>还能攒 '+left.toFixed(1)+
          ' 秒（到 '+buf+' 秒就不再变长，现在按 &rarr; 就裁到这里）';
        if(left<=2.5&&clipStage<1){ clipStage=1;
          if(vib&&navigator.vibrate) navigator.vibrate([70,80,70]); }
      }
    } else if(left<=0.2){
      cls='dead';
      h='<b>&#9888;&#65039; 入点已经滚出缓冲</b><br>现在按 &rarr; 只能给你最近 '+
        Number(c.avail||0).toFixed(1)+' 秒（起点不是入点）';
      if(clipStage<2){ clipStage=2;
        if(vib&&navigator.vibrate) navigator.vibrate([120,90,120,90,120]); }
    } else if(left<=2.5){
      cls='soon';
      h='<b>&#9193;&#65039; 快按 &rarr; ！入点已 '+(ago).toFixed(1)+' 秒</b><br>还剩 '+
        left.toFixed(1)+' 秒（缓冲 '+buf+' 秒）';
      if(clipStage<1){ clipStage=1;
        if(vib&&navigator.vibrate) navigator.vibrate([70,80,70]); }
    } else {
      cls='arm';
      h='<b>&#9193; 入点 '+(ago).toFixed(1)+' 秒</b><br>还剩 '+left.toFixed(1)+
        ' 秒内按 &rarr;（缓冲 '+buf+' 秒）';
    }
  } else if(c.replayActive){
    cls='play'; h='&#9654; 正在回放…（按播放键收掉再标下一段）';
  } else if(c.clipReady){
    var kk2=(d.feature&&d.feature.keys)||{};
    cls='ok';
    h='&#9989; 片段已裁好'+(c.clipAgo!=null?('（'+Math.round(Number(c.clipAgo)+drift)+' 秒前）'):'')+
      (c.lastClipSec?(' 长 '+Number(c.lastClipSec).toFixed(1)+' 秒'):'')+
      '<br>按 <b>'+esc(kk2.play||'播放键')+'</b> 播　·　按 <b>'+esc(kk2.save||'留档键')+'</b> 存文件';
  } else {
    var kk3=(d.feature&&d.feature.keys)||{};
    cls='idle';
    h=(c.mode==='armed')
      ? ('按 <b>'+esc(kk3.mark_in||'←')+'</b> 开始攒帧（省内存模式：不按就不占内存）')
      : ('先按 <b>'+esc(kk3.mark_in||'←')+'</b> 标入点，打完在 '+buf+' 秒内按 <b>'+
         esc(kk3.mark_out||'→')+'</b>');
  }
  box.className=cls; box.innerHTML=h;
}
function render(d){
  if(!d||!d.ready) return;
  document.getElementById('map').textContent=(d.map||'').toUpperCase();
  document.getElementById('rnd').textContent=d.round!=null?('R'+d.round):'';
  document.getElementById('score').textContent=
    (d.scoreCT!=null?('CT '+d.scoreCT+' : '+d.scoreT+' T'):'');
  var pills=[];
  var ph={live:'进行中',freezetime:'冻结',over:'回合结束',bomb:'已下包',
          defuse:'拆包中',warmup:'热身',paused:'暂停'}[d.phase]||d.phase||'';
  var t=d.phaseIn!=null?Math.max(0,Math.round(parseFloat(d.phaseIn))):null;
  pills.push('<span class="pill'+(d.phase==='freezetime'?' hot':'')+'">'+
             esc(ph)+(t!=null?(' '+t+'s'):'')+'</span>');
  pills.push('<span class="pill">CT '+d.aliveCT+'</span>'+
             '<span class="pill">T '+d.aliveT+'</span>');
  if(d.planted&&d.phase!=='bomb'&&d.phase!=='defuse')
    pills.push('<span class="pill bomb">已下包</span>');
  if(d.killAgo!=null){
    var kago=d.killAgo+(Date.now()-lastSnapAt)/1000;
    if(kago<30) pills.push('<span class="pill hot">击杀 '+kago.toFixed(0)+'s 前</span>');
  }
  document.getElementById('state').innerHTML=pills.join('');

  paintAuto(d.auto);
  paintFeature(d.feature);

  var w=document.getElementById('warn');
  if(d.hasKeys===false){
    w.className='on';
    w.textContent='读不到观察位（observer_slot），没法显示该按哪个键。'+
                  '请确认 GSI 配置里有 allplayers_id 和 allplayers_state。';
  } else { w.className=''; }
  var decided=(d.aliveCT===0||d.aliveT===0);
  document.getElementById('cue').className=(d.phase==='over'||decided)?'on':'';
  var ks=d.recentKills||[];
  if(ks.length>lastKillCount&&vib&&navigator.vibrate) navigator.vibrate([40,60,40]);
  lastKillCount=ks.length;
  var kb='';
  ks.forEach(function(k){
    var who=k.killerKey!=null?('按 [ '+esc(k.killerKey)+' ]'):'有人';
    var n=(k.killerKills>1)?(' 拿了第 '+k.killerKills+' 杀'):' 刚击杀';
    kb+='<div class="kb"><span class="k">'+who+'</span>'+n+
        (k.weapon?(' <span class="w">'+esc(String(k.weapon).replace('weapon_',''))+'</span>'):'')+
        (showNames?(' <span style="color:#7d93a8;font-size:14px">('+
          esc(k.killer)+' &rarr; '+esc(k.victim)+')</span>'):'')+'</div>';
  });
  document.getElementById('killbar').innerHTML=kb;
  var ps=d.players||[], h='';
  ps.forEach(function(p,i){
    var cls='row '+(p.team==='CT'?'ct':'t');
    if(i===0) cls+=' top'; else if(i>4) cls+=' dim';
    var lvl=p.score>=45?'':(p.score>=20?' mid':' low');
    var reason=esc((p.reasons||[]).join(' · '));
    var extra=[];
    if(p.hasBomb) extra.push('&#128163;');
    if(p.sniper) extra.push('&#127919;');
    if(showNames) extra.push(esc(p.name));
    var ex=extra.length?(' <span class="obs">'+extra.join(' ')+'</span>'):'';
    if(i===0){
      var head;
      if(p.observed) head='<div class="kdone">&#10003; 已经在他身上<br>不用按</div>';
      else if(p.key!=null) head='<div class="kbig"><small>按</small><u>'+
                                esc(p.key)+'</u></div>';
      else head='<div class="kdone">读不到键位</div>';
      h+='<div class="'+cls+'">'+head+
         (reason?('<div class="rz">'+reason+'</div>'):'')+
         '<div class="bar'+lvl+'"><i style="width:78%"></i></div></div>';
    } else {
      var kk=(p.key!=null)?('<span class="kmid">按 '+esc(p.key)+'</span>')
                          :'<span class="knone">按 &mdash;</span>';
      h+='<div class="'+cls+'"><div class="r2">'+kk+
         '<span class="txt">'+(reason||'&nbsp;')+'</span>'+
         (p.observed?'<span class="obs">&#10003; 在他身上</span>':'')+ex+'</div>'+
         '<div class="bar'+lvl+'"><i style="width:'+
         Math.max(6,Math.min(70,p.score))+'%"></i></div></div>';
    }
  });
  document.getElementById('list').innerHTML=h;
}
var es=new EventSource('/events');
es.onmessage=function(e){
  lastSnapAt=Date.now();
  document.getElementById('dot').className='on';
  try{ lastSnap=JSON.parse(e.data); render(lastSnap); paintClip(); }catch(x){}
};
es.onerror=function(){ document.getElementById('dot').className=''; };
setInterval(paintClip,250);   // 没有新包时也让倒计时继续走
</script></body></html>
"""


class WebHandler(http.server.BaseHTTPRequestHandler):
    """只读的手机端页面。绑定在 0.0.0.0 的这个端口上**不含任何控制端点**，
    避免同网段的人能触发切场景。"""
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        if path == "/auto":
            tok = (q.get("k") or [""])[0]
            if not self.app.viewer_token or tok != self.app.viewer_token:
                self._send(403, "application/json; charset=utf-8",
                           b'{"ok":false,"error":"bad token"}')
                return
            on = None
            if q.get("on"):
                on = (q.get("on") or ["0"])[0] in ("1", "true", "on", "yes")
            preset = (q.get("preset") or [None])[0]
            wb = None
            if q.get("winbonus"):
                wb = (q.get("winbonus") or ["1"])[0] in ("1", "true", "on", "yes")
            nm = None
            if q.get("names"):
                nm = (q.get("names") or ["1"])[0] in ("1", "true", "on", "yes")
            st = self.app.set_auto(on=on, preset=preset, win_bonus=wb, names=nm)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "auto": st}, ensure_ascii=False).encode("utf-8"))
            return
        if path == "/feature":
            # ★ 回放功能总开关 / 内存模式（默认关、默认省内存）。
            #   和 /auto 一样要口令：这是"会动 OBS"的开关。
            tok = (q.get("k") or [""])[0]
            if not self.app.viewer_token or tok != self.app.viewer_token:
                self._send(403, "application/json; charset=utf-8",
                           b'{"ok":false,"error":"bad token"}')
                return
            replay = None
            if q.get("replay"):
                replay = (q.get("replay") or ["0"])[0] in ("1", "true", "on", "yes")
            mode = (q.get("mode") or [None])[0]
            st = self.app.set_feature(replay=replay, mode=mode)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "feature": st}, ensure_ascii=False).encode("utf-8"))
            return
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", VIEWER_HTML.encode("utf-8"))
            return
        if path == "/state":
            body = (self.app.viewer_json() or "{}").encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last = None
            last_beat = time.time()
            try:
                while True:
                    s = self.app.viewer_json()
                    if s and s != last:
                        self.wfile.write(("data: " + s + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                        last = s
                        last_beat = time.time()
                    elif time.time() - last_beat > 10:
                        # 没有新数据时也发个注释，防止手机端 / 中间设备掐掉空闲连接
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
                    time.sleep(0.2)
            except Exception:
                self.close_connection = True
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")


class WebServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if sys.exc_info()[0] is ConnectionResetError:
            return
        super().handle_error(request, client_address)


def firewall_rule_hint(port):
    return (f'netsh advfirewall firewall add rule name="DSH CS2 Viewer" '
            f'dir=in protocol=TCP localport={port} action=allow')


# ============================================================================
# 5. 主程序
# ============================================================================

class App:
    def __init__(self, cfg, obs, simulate=False):
        self.cfg = cfg
        self.obs = obs
        self.director = ReplayDirector(cfg, obs, simulate=simulate)
        self.state = MatchState(self._on_round_end, self.director.on_kill)
        self.state.cfg_ref = cfg
        self.director.state_ref = self.state
        self.controller = None           # 手动键盘控制器（run_live 里注入）
        self.attention = AttentionModel()   # 副驾提示器
        self.attention.show_names = bool(cfg.get("show_names", False))
        self.auto = AutoSwitcher(cfg)       # 自动切换视角
        self.attention.win_bonus = self.auto.win_bonus
        self.console = ConsoleTyper(cfg, dry_run=simulate or bool(cfg.get("dry_run")))
        self.viewer_token = ""
        self._snap_json = None
        self.web = None
        self.server = None

    def viewer_json(self):
        return self._snap_json

    def on_gsi(self, payload):
        self.state.on_gsi(payload)
        self.director.on_gsi_tick(self.state.phase)
        # CS 控制台指令：round1 模式下，每场第 1 个冻结时间发一次
        try:
            self.console.on_gsi(self.state.phase, self.state.live_round)
        except Exception:
            pass
        # 副驾提示器 + 自动切换：每个包都重算一次（10 人 × 10Hz，开销可忽略）
        try:
            snap = self.attention.update(payload)
            self.auto.on_packet(snap, self.director)
            snap["auto"] = self.auto.state()
            # 片段框选状态（← 入点还剩几秒、片段裁好没有）—— 手机页面顶部那条横幅
            snap["clip"] = self.director.mark_window()
            snap["feature"] = self.feature_state()
            self._snap_json = json.dumps(snap, ensure_ascii=False)
        except Exception:
            log("!! 提示器/自动切换计算出错:\n" + traceback.format_exc())

    def feature_state(self):
        """回放功能开关 / 内存模式的状态（手机页面的大开关用）。"""
        d = self.director
        info = d.memory_info()
        return {
            "enabled": bool(d.replay_enabled),
            "mode": d.replay_mode,
            "armed": bool(d._capture_disabled is False),
            "seconds": float(d.cfg.get("record_max_seconds", 10.0) or 10.0),
            "oneCopyGB": info["one_copy_gb"],
            "steadyGB": info["steady_gb"],
            "memory": info["hint"],
            "keys": format_keys(normalize_keys(d.cfg)[0]),
        }

    def set_feature(self, replay=None, mode=None):
        """手机页面 / GUI 改「回放功能」开关与内存模式（立即生效 + 写回配置）。"""
        d = self.director
        if mode is not None:
            d.set_replay_mode(mode, persist=False)
        if replay is not None:
            d.set_replay_enabled(replay, persist=False)
        if replay is not None or mode is not None:
            d.persist_config()
        st = self.feature_state()
        if self._snap_json:
            try:
                s = json.loads(self._snap_json)
                s["feature"] = st
                s["clip"] = d.mark_window()
                self._snap_json = json.dumps(s, ensure_ascii=False)
            except Exception:
                pass
        return st

    def set_auto(self, on=None, preset=None, win_bonus=None, names=None):
        if preset is not None:
            self.auto.apply_preset(preset)
        if win_bonus is not None:
            self.auto.set_win_bonus(win_bonus)
        if on is not None:
            self.auto.set_enabled(on)
        if names is not None:
            # ★ 2026-10-07：手机页面上那个「显示名字」原来是**每台手机自己的本地开关**，
            #   现在服务端也能设（图形界面 / 命令行），并且会写回 config.json。
            #   手机上的本地开关仍然可以覆盖它（现场想看名字就点一下）。
            self.attention.show_names = bool(names)
            self.cfg["show_names"] = bool(names)
            log(f"👁 提示器显示名字 → {'开' if names else '关'}"
                f"（手机页面上的同名开关是每台设备自己的，会覆盖这里）")
            self.director.persist_config()
        # 胜率加成是作用在**排序**上的，所以要让提示器模型也同步
        self.attention.win_bonus = self.auto.win_bonus
        if self._snap_json:
            try:
                s = json.loads(self._snap_json)
                s["auto"] = self.auto.state()
                self._snap_json = json.dumps(s, ensure_ascii=False)
            except Exception:
                pass
        return self.auto.state()

    def _on_round_end(self, round_no, stats, score, reasons):
        self.director.on_round_end(round_no, stats, score, reasons)

    def status(self):
        s = self.director.status()
        s.update({"phase": self.state.phase, "map": self.state.map_name,
                  "round": self.state.live_round})
        return s

    def lock(self, r): self.director.lock(r)
    def unlock(self, r): self.director.unlock(r)
    def manual_replay(self): return self.director.manual_replay()

    def mark_in(self):
        if not self.controller:
            log("（没有键盘控制器，忽略 mark_in）")
            return False
        self.controller.on_mark_in()
        return True

    def mark_out(self):
        if not self.controller:
            log("（没有键盘控制器，忽略 mark_out）")
            return False
        self.controller.on_mark_out()
        return True

    def save_clip(self):
        """留档当前这段素材（等于按快捷键「保留片段」/ 小键盘 6）——`/control/save`。"""
        if not self.controller:
            log("（没有键盘控制器，忽略 save）")
            return False
        self.controller.on_save()
        return True

    def send_console(self, reason="手动"):
        """手动发一次 CS 控制台指令（设置窗口按钮 / /control/console）。"""
        return self.console.send_now(reason)

    def start_web_server(self):
        cfg = self.cfg
        if not cfg.get("web_enabled", True):
            return
        port, host = int(cfg["web_port"]), cfg["web_host"]
        if port_in_use(host, port):
            log(f"⚠️  提示器端口 {port} 被占用，手机页面没起来（不影响回放功能）")
            return
        WebHandler.app = self
        try:
            self.web = WebServer((host, port), WebHandler)
        except Exception as e:
            log(f"⚠️  提示器启动失败: {e}")
            return
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.viewer_token = load_or_make_token()
        log("")
        log("📱 副驾提示器已启动 —— 手机浏览器打开下面任意一个地址：")
        for ip in lan_ips():
            log(f"        http://{ip}:{port}/?k={self.viewer_token}")
        log(f"        （本机自测: http://127.0.0.1:{port}/?k={self.viewer_token} ）")
        log("     地址里的 ?k= 是口令，用来保护「自动切换」这个开关不被同网段的人乱点。")
        log("     地址**每次启动都一样**，手机上加到主屏幕书签，以后一点就开。")
        log("")
        log("     ⚠️ 如果手机连不上，多半是 Windows 防火墙拦了。用管理员权限执行一次：")
        log(f"        {firewall_rule_hint(port)}")

    def start_gsi_server(self):
        host, port = self.cfg["gsi_host"], self.cfg["gsi_port"]
        if port_in_use(host, port):
            log("")
            log("=" * 62)
            log(f"❌ 端口 {port} 已经被占用 —— 很可能你已经开了一个引擎窗口！")
            log(f"   地址 http://{host}:{port}/control/status 现在能打开吗？")
            log("   • 能打开 → 那个就是正在跑的引擎，这个窗口请直接关掉，不要开两个。")
            log("   • 打不开 → 是别的程序占了这个端口，改 config.json 里的 gsi_port，")
            log(f"     同时把 gamestate_integration_director.cfg 里的 {port} 也改掉，")
            log("     然后重启 CS2 让它重新加载 GSI 配置。")
            log("=" * 62)
            raise SystemExit(2)
        GsiHandler.app = self
        self.server = GsiServer((host, port), GsiHandler)
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        log(f"GSI 监听: http://{self.cfg['gsi_host']}:{self.cfg['gsi_port']}{self.cfg['gsi_path']}")
        log(f"控制端点: /control/status | /lock | /unlock | /replay"
            f" | /mark_in | /mark_out | /console | /save")


# ---------------------------------------------------------------------------
# 5.1 探测模式
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 5.1b 一键写入 Replay Source 的推荐设置
# ---------------------------------------------------------------------------

# 枚举值直接取自插件源码 replay-source.c 的 #define
VISIBILITY_ACTION = {"restart": 0, "pause": 1, "continue": 2, "none": 3}
END_ACTION = {"hide_single": 0, "pause_single": 1, "loop_single": 2, "reverse_single": 3,
              "hide_all": 4, "pause_all": 5, "loop_all": 6, "reverse_all": 7}

# 插件里所有设置项（key -> 说明），key 取自插件源码 replay.h 的 SETTING_* 宏
REPLAY_SETTINGS_DOC = [
    ("source", "Video Source｜要缓存画面的源", "必须设成你的游戏采集源"),
    ("source_audio", "Audio Source｜要缓存声音的源", "通常设成和视频源一样"),
    ("internal_frames", "Capture Internal Frames｜捕获内部帧", "源本身是滤镜/异步源时才需要，一般关闭"),
    ("duration", "Duration｜缓存最近多少毫秒", "和引擎的 capture_seconds 对应，默认 5000"),
    ("retrieve_delay", "Load Delay｜加载回放的延迟(ms)", "一般 0"),
    ("replays", "Maximum Replays｜内存里保留几个回放", "默认 1，够用"),
    ("visibility_action", "Visibility Action｜★源变可见时干什么", "必须是 Restart，默认 Continue 会导致回放不播"),
    ("start_delay", "Start Delay｜开始播放前的延迟(ms)", "一般 0"),
    ("frame_step_count", "Step 'N' Frames｜逐帧步进的帧数", "手动逐帧复盘才用，默认 5"),
    ("end_action", "End Action｜★播完之后干什么", "建议 Pause after single，默认 Loop single 会重复播放"),
    ("next_scene", "Next Scene｜播完后自动切到哪个场景", "留空！由导播引擎决定何时切回"),
    ("speed_percent", "Speed Percentage｜播放速度", "70 = 0.7 倍速慢放"),
    ("backward", "Backwards｜倒放", "关闭"),
    ("directory", "Directory｜手动存盘的目录", "只在手动 Save Replay 时用到"),
    ("file_format", "Filename Formatting｜存盘文件名格式", "同上"),
    ("lossless", "Lossless｜无损存盘", "同上，很占空间"),
    ("progress_source", "Progress Crop Source｜进度条源", "可选：按播放进度裁剪某张图片的宽度做进度条"),
    ("text_source", "Text Source｜文字输出源", "可选：把播放信息写进一个文本源"),
    ("text", "Text Format｜文字格式", "支持 %SPEED% %PROGRESS% %COUNT% %INDEX% %DURATION% %TIME% %FPS%"),
    ("sound_trigger", "Sound Trigger Load Replay｜声音自动触发", "音量超阈值自动生成回放，导播场景一般关闭"),
    ("threshold", "Threshold db｜声音触发阈值", "-60 ~ 0"),
    ("load_switch_scene", "Load Replay Switch Scene｜加载回放时切场景", "关闭！由导播引擎控制"),
]


def run_configure(cfg, duration_ms=5000, speed_percent=70.0, apply=False):
    log("=== Replay Source 设置检查 / 写入 ===")
    obs = ObsClient(cfg["obs_url"], cfg["obs_password"])
    try:
        obs.connect()
    except Exception as e:
        log(f"❌ 连不上 obs-websocket: {e}")
        return 1

    item = cfg["replay_item"]
    kind, cur = obs.input_settings(item)
    log(f"源「{item}」 kind={kind}")
    log("当前设置（只列出被显式保存过的项，没列出的就是插件默认值）:")
    for k, v in sorted(cur.items()):
        log(f"    {k} = {v!r}")

    def show(key, label):
        v = cur.get(key)
        if v is None:
            d = {"visibility_action": "continue(2)", "end_action": "loop_single(2)",
                 "duration": 5000, "replays": 1, "speed_percent": 100}.get(key, "未设置")
            log(f"    {label:28s} = 未设置 → 插件默认 {d}")
        else:
            log(f"    {label:28s} = {v}")

    log("关键项体检:")
    show("duration", "Duration(ms)")
    show("speed_percent", "Speed Percentage")
    show("visibility_action", "Visibility Action")
    show("end_action", "End Action")
    show("replays", "Maximum Replays")
    show("next_scene", "Next Scene")

    patch = {
        "source": cur.get("source") or "",
        "duration": int(duration_ms),
        "replays": 1,
        "speed_percent": float(speed_percent),
        "visibility_action": VISIBILITY_ACTION["restart"],
        "end_action": END_ACTION["pause_single"],
        "start_delay": 0,
        "next_scene": "",
    }
    if not patch["source"]:
        log("❌ Video Source 是空的，先手动绑定游戏采集源，否则不写入。")
        obs.close()
        return 1

    log("")
    log("将要写入的推荐值:")
    for k, v in patch.items():
        log(f"    {k} = {v!r}")

    if not apply:
        log("")
        log("（这是预演，没有真的写入。加 --apply 才会真正写入 OBS）")
        obs.close()
        return 0

    obs.request("SetInputSettings",
                {"inputName": item, "inputSettings": patch, "overwrite": False})
    log("✅ 已写入。重新读取确认:")
    _, after = obs.input_settings(item)
    for k in patch:
        log(f"    {k} = {after.get(k)!r}")
    log("")
    log("注意：SetInputSettings 不会重载插件内部状态，建议在 OBS 里把该源右键 → 属性 → 确定，")
    log("      或者重启 OBS，确保插件按新设置重建内部状态。")
    obs.close()
    return 0


def obs_log_newest():
    d = os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "logs")
    try:
        fs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".txt")]
        return max(fs, key=os.path.getmtime) if fs else None
    except Exception:
        return None


def obs_log_replay_lines(path, limit=60):
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [l.rstrip() for l in f if "replay_source" in l][-limit:]
    except Exception:
        return []


def run_test_load_replay(cfg):
    log("=== 单独测试 Load replay 热键 ===")
    obs = ObsClient(cfg["obs_url"], cfg["obs_password"])
    try:
        obs.connect()
    except Exception as e:
        log(f"❌ 连不上 obs-websocket: {e}")
        return 1

    try:
        kind, s = obs.input_settings(cfg["replay_item"])
    except Exception as e:
        log(f"❌ 读回放源失败: {e}")
        return 1
    vsrc = s.get("source") or ""
    log(f"回放源「{cfg['replay_item']}」 kind={kind}  Video Source =「{vsrc}」")
    if not vsrc:
        log("❌ Video Source 是空的，先在 OBS 里绑定。")
        return 1

    try:
        a = obs.request("GetSourceActive", {"sourceName": vsrc})
        log(f"「{vsrc}」当前状态: videoShowing={a.get('videoShowing')} "
            f"videoActive={a.get('videoActive')}")
        if not a.get("videoShowing"):
            log("⚠️  它现在不在渲染，回放滤镜里没有帧。")
            log("   请先把 OBS 切到含这个采集源的场景（比如 HUD）、保证游戏在跑，等 6 秒以上再测。")
    except Exception as e:
        log(f"   （查不到源状态: {e}）")

    lf = obs_log_newest()
    before = obs_log_replay_lines(lf)
    log(f"OBS 日志: {lf}")

    name = cfg.get("load_replay_hotkey") or ""
    log(f"触发: TriggerHotkeyByName(hotkeyName={name!r}, contextName={cfg['replay_item']!r})")
    try:
        obs.request("TriggerHotkeyByName",
                    {"hotkeyName": name, "contextName": cfg["replay_item"]})
        log("✔ API 调用成功")
    except Exception as e:
        log(f"❌ API 失败: {e}")
        log("   热键名可能不对。可以在 OBS 的 设置 → 热键 里搜索 'Load replay' 确认。")
        obs.close()
        return 1

    time.sleep(1.5)
    new = [l for l in obs_log_replay_lines(lf) if l not in before]
    if new:
        log("OBS 日志新增:")
        for l in new[-6:]:
            log("   " + l)
    if any("replay added" in l for l in new):
        log("")
        log("✅ 成功：缓冲区已快照成可播放的回放。")
        log("   现在切到「%s」场景应该就能看到慢动作回放了。" % cfg["replay_scene"])
    else:
        log("")
        log("⚠️  没在日志里看到 'replay added'。可能是：")
        log("   1) 采样源不在渲染 → 滤镜里没帧")
        log("   2) 自上次 Load replay 到现在不足 Duration 秒 → 只能拿到很短一段")
        log("   3) 请直接打开 OBS 日志确认")
    obs.close()
    return 0


def run_test_keys(cfg):
    log("=== 热键测试 ===")
    log("请依次按这四个键，窗口里会实时打印。按 Ctrl+C 结束。")
    keys, problems = normalize_keys(cfg)
    shown = format_keys(keys)
    for action, label in KEY_ACTIONS.items():
        log(f"   {shown[action]:<20}（{label}）")
    for p in problems:
        log(f"   ⚠️ 键位配置：{p}")
    if not cfg.get("replay_enabled", False):
        log("   ⛔ 注意：回放功能当前是关闭的，引擎不会动手；这里只测键盘钩子本身。")
    nl = numlock_on()
    if nl is None:
        log("   （读不到 NumLock 状态）")
    elif nl:
        log("   NumLock：✅ 亮着 —— 小键盘 6 和 → 是两个不同的键，互不干扰")
    else:
        log("   NumLock：❌ 关着 —— 现在小键盘 6 和 → 发的是同一个按键，"
            "两个都会走「标记出点」。请按一下 NumLock 让灯亮起来再测，"
            "或者把这两个键改到主键盘上。")

    def mk(label, is_target):
        def f():
            tag = "✅ 已绑定" if is_target else "（未绑定）"
            log(f"   收到: {label}  {tag}")
        return f

    bindings = {}
    for action, toks in keys.items():
        for tok in toks:
            key = binding_of_token(tok)
            if key is not None:
                bindings[key] = mk(f"{tok}（{KEY_ACTIONS[action]}）", True)
    # 顺便认一下常见的"没绑上的键"，省得用户以为绑了却没反应
    for extra in (VK_RETURN, VK_LEFT, VK_RIGHT, VK_NUMPAD6, (VK_RETURN, True)):
        if isinstance(extra, tuple):
            nm = key_token_name(extra[0], extra[1])
        else:
            nm = key_token_name(extra, None)
        bindings.setdefault(extra, mk(f"{nm}（未绑定的键）", False))
    hook = KeyHook(bindings, debounce=0.25)
    if not hook.start():
        log(f"❌ 钩子安装失败: {hook.error}")
        return 1
    log("（钩子已装好，只读监听，不会拦截你的按键）")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        hook.stop()
        log("已卸载钩子。")
    return 0


def run_probe(cfg):
    log("=== 探测模式：检查 OBS 连接与配置 ===")
    obs = ObsClient(cfg["obs_url"], cfg["obs_password"])
    try:
        obs.connect()
    except Exception as e:
        log(f"❌ 连不上 obs-websocket: {e}")
        log("   请先在 OBS 里：工具 → WebSocket 服务器设置 → 勾选「启用 WebSocket 服务器」")
        return 1
    log("✅ obs-websocket 连接成功")

    ver = obs.request("GetVersion")
    log(f"OBS 版本: {ver.get('obsVersion')}  obs-websocket: {ver.get('obsWebSocketVersion')} "
        f"rpcVersion: {ver.get('rpcVersion')}")

    avail = set(ver.get("availableRequests") or [])
    need = ["SetCurrentProgramScene", "GetSceneList", "GetSceneItemList",
            "SetSceneItemEnabled", "GetInputSettings", "TriggerHotkeyByName",
            "TriggerMediaInputAction", "GetMediaInputStatus",
            "SaveReplayBuffer", "GetReplayBufferStatus", "GetRecordDirectory"]
    log("本机支持的请求名核对：")
    for n in need:
        log(f"    {'✅' if n in avail else '❌'} {n}")

    scenes, current = obs.scene_list()
    log(f"场景（{len(scenes)}）: {', '.join(scenes)}")
    log(f"当前直播场景: {current}")

    items = obs.scene_items(cfg["replay_scene"])
    log(f"回放场景「{cfg['replay_scene']}」的源:")
    for it in items:
        log(f"    - {it.get('sourceName')} (id={it.get('sceneItemId')}, "
            f"{'可见' if it.get('sceneItemEnabled') else '隐藏'})")
    try:
        k, s = obs.input_settings(cfg["replay_item"])
        log(f"回放源「{cfg['replay_item']}」kind={k}")
        log("    关键设置: " + json.dumps(
            {kk: vv for kk, vv in s.items()
             if kk in ("source", "duration", "speed", "visibility_action", "end_action",
                       "next_scene", "load_delay", "max_replays", "show", "hide")},
            ensure_ascii=False))
        log("    （完整设置）: " + json.dumps(s, ensure_ascii=False)[:600])
    except Exception as e:
        log(f"⚠️  读回放源失败: {e}")

    try:
        log(f"Replay Buffer 状态: {obs.request('GetReplayBufferStatus')}")
    except Exception as e:
        log(f"    Replay Buffer: {e}（不用它也行，Replay Source 插件是独立的内存回放）")

    obs.close()
    log("=== 探测完成 ===")
    return 0


def run_selftest(cfg=None):
    """自检：这份程序（尤其是打包好的 exe）在**这台电脑**上到底能不能跑。

    为什么要有：2026-10-07 实测踩到 —— 打包机上没装 `websocket-client` 时，
    PyInstaller 的 `--hidden-import websocket` 只是**静默跳过**，打出来的 exe
    双击能开、但一连 OBS 就 `RuntimeError: 缺少 websocket-client`。
    别人拿到就是"用不了"，而且完全看不出原因。所以：
      * `build_exe.py` 打包前后各查一次（见那边）；
      * 这里也给用户一个能自己跑的 `--selftest`。
    """
    log("=== 自检（这份程序能不能跑）===")
    ok = True

    # 1) websocket-client：连 OBS 的硬依赖
    try:
        import websocket
        log(f"✅ websocket-client {getattr(websocket, '__version__', '?')}"
            f"（连 obs-websocket 必需）")
    except Exception as e:
        ok = False
        log(f"❌ 缺 websocket-client：{e}")
        log("   · 源码运行：  pip install websocket-client")
        log("   · 成品 exe：  这是**打包那台机器**漏了它（build_exe.py 现在会提前拦下）")

    # 2) tkinter：图形设置窗口（不是硬需求，没有也能 --no-gui 跑）
    try:
        import tkinter
        log(f"✅ tkinter {tkinter.TkVersion}（图形设置窗口可用）")
    except Exception as e:
        log(f"⚠️  没有 tkinter（{e}）：打不开设置窗口，用 --no-gui / 6-director.cmd 也行。")

    # 3) 纯逻辑自检：键位表 + 内存公式（这两个错了会静默出洋相）
    try:
        keys, problems = normalize_keys({"keys": DEFAULTS["keys"]})
        shown = format_keys(keys)
        log(f"✅ 键位表可用：{shown}"
            + (f"（{len(problems)} 条提示）" if problems else ""))
        m = estimate_replay_memory(1920, 1080, 60, 10, 2)
        log(f"✅ 内存公式：1080p60 10 秒两份 = {m['steady_gb']:.2f} GB"
            f"（应该 ≈9.95）")
        if abs(m["steady_gb"] - 9.95) > 0.1:
            ok = False
            log("❌ 内存公式和文档对不上，别用！")
    except Exception as e:
        ok = False
        log(f"❌ 基本逻辑自检失败：{type(e).__name__}: {e}")

    # 4) 平台 / 路径
    log(f"   平台：{sys.platform} / Python {sys.version.split()[0]}"
        f"{'（打包版）' if getattr(sys, 'frozen', False) else '（源码）'}")
    log(f"   程序目录：{base_dir()}")
    log(f"   配置文件：{CONFIG_PATH or os.path.join(base_dir(), 'config.json')}"
        f"{'' if CONFIG_PATH and os.path.exists(CONFIG_PATH) else '（还不存在 → 先跑首次设置向导）'}")
    try:
        log(f"   手机口令文件：{os.path.join(base_dir(), 'viewer_token.txt')}")
    except Exception:
        pass

    log("")
    if ok:
        # ★ 2026-10-08：回放有**两个后端**，这里要按当前用的是哪个来说"还差什么"。
        #   以前无条件写"要装 replay-source 插件" —— 用 obs 后端的人（不需要插件）
        #   看了会以为少装东西，反过来插件后端的人又不知道 ffmpeg 是可选的。
        backend = ""
        try:
            backend = str((cfg or {}).get("replay_backend") or "").lower()
        except Exception:
            backend = ""
        log("✅ 自检通过。还差的外部条件（这些不在这份程序里）：")
        log("   · OBS Studio 30+ 且开着 obs-websocket；")
        log("   · CS2 装在**同一台**电脑上（GSI 只推到 127.0.0.1）；")
        log("   · 首次设置向导跑过一次（会写 GSI 配置 + 放行防火墙）；")
        log("   · 回放功能要从下面两条里选一条（**自动切镜头和手机提示器都不需要**）：")
        log("       ① 插件后端：装 exeldro/obs-replay-source 插件（内存 GB 级，但能任意入点定格/倒放）；")
        log("       ② OBS 后端：本机有 ffmpeg + OBS 里启用「回放缓冲」（内存 MB 级，真机已验证）。")
        if backend == "obs":
            log("     当前配置用的是 ② OBS 后端 —— 所以**不需要**那个插件。")
            ff = find_ffmpeg(cfg)
            if ff:
                log(f"     ✅ 本机 ffmpeg：{ff}")
            else:
                log("     ⚠️ 本机没找到 ffmpeg —— 装一个：winget install Gyan.FFmpeg"
                    "（或者把 ffmpeg.exe 放到本程序目录旁边）")
            info = obs_replay_buffer_info()
            if info.get("enabled") is True and info.get("seconds"):
                log(f"     ✅ OBS 回放缓冲已启用：{info['seconds']:.0f} 秒")
            elif info.get("enabled") is False:
                log("     ⚠️ OBS 里「回放缓冲」还没勾上（设置 → 输出 → 回放缓冲），勾完重启一次 OBS")
        elif backend == "plugin":
            log("     当前配置用的是 ① 插件后端 —— 记得装 exeldro/obs-replay-source。")
            log("     （想不用插件：设置窗口 ① 页把「回放后端」换成 OBS 自带 Replay Buffer）")
    else:
        log("❌ 自检没通过 —— 先解决上面标 ❌ 的条目，否则连不上 OBS。")
    return 0 if ok else 3


# ---------------------------------------------------------------------------
# 5.2 仿真模式（不需要 CS2 / 不需要 OBS）
# ---------------------------------------------------------------------------

def build_sim_round(round_no, kills_by_sid, plant=False, defuse=False, clutch=False, phase_seq=None):
    """
    构造一串 GSI 数据包，模拟一个完整回合。

    故意加入两个"陷阱"来验证引擎的健壮性：
      1) `round.phase` 恒为 "live"，真实相位只放在 `phase_countdowns.phase` 里
         —— 引擎必须以 phase_countdowns 为准（Astra 的生产做法）。
      2) 每隔一个包就发"部分包"：一半选手的 state/weapons 被省略
         —— 引擎必须合并上一包，否则会把活人误判成死亡（假残局）。
    """
    names = {"1": "PromiSe", "2": "TangEr", "3": "Wu", "4": "Kyo", "5": "Ling",
             "6": "Ace", "7": "Nova", "8": "Zed", "9": "Rex", "10": "Vic"}
    sids = list(names.keys())

    def make(phase, kill_counts, alive_ct, alive_t, win_team=None, bomb=False,
             partial=False, dmg_zero=False):
        allp = {}
        for i, sid in enumerate(sids):
            team = "CT" if i < 5 else "T"
            entry = {"name": names[sid], "team": team, "clan": "",
                     "observer_slot": i}
            # partial=True 时，只给前 2 个选手完整信息，其余只发 id/team
            if not partial or sid in ("1", "2"):
                hp = (100 if i < alive_ct else 0) if team == "CT" else (100 if (i - 5) < alive_t else 0)
                entry["state"] = {"health": hp, "armor": 100, "helmet": True,
                                  "round_kills": kill_counts.get(sid, 0),
                                  "round_totaldmg": 0 if dmg_zero else 100,
                                  "money": 4000, "flashed": 0}
                entry["match_stats"] = {"kills": 10, "deaths": 5, "assists": 2, "score": 20}
                entry["weapons"] = ({"weapon_0": {"name": "weapon_awp", "type": "Sniper",
                                                  "ammo_clip": 5}}
                                    if sid == "1" else
                                    {"weapon_0": {"name": "weapon_ak47", "type": "Rifle",
                                                  "ammo_clip": 30}})
            allp[sid] = entry
        return {
            "provider": {"name": "Counter-Strike: Global Offensive", "appid": 730},
            "map": {"name": "de_dust2", "round": round_no, "phase": "live",
                    "mode": "competitive"},
            # ⚠️ 陷阱 1：真实相位只在这里。round.phase 故意恒为 live。
            "phase_countdowns": {"phase": phase, "phase_ends_in": "8.0"},
            "round": {"phase": "live", "bomb": bomb, "win_team": win_team or ""},
            "allplayers": allp,
            # ★ 顶层 `player` = 观察位（镜头）正拍着谁。GSI 里它跟 per-player 的
            #   `observer_slot` 是两套东西：这个给手机页面标"现在镜头在谁身上"。
            "player": {"steamid": "1", "name": names["1"], "observer_slot": 0},
            "bomb": {"state": "planted" if bomb else "carried"},
        }

    seq = phase_seq or [("live", 3.0), ("live", 2.0), ("freezetime", 4.0)]
    n = 0

    for phase, dur in seq:
        if phase == "over":
            n += 1
            yield make("over", kills_by_sid, 0 if not clutch else 1, 2, "CT",
                       bomb=plant, partial=(n % 2 == 0)), 1.0
        elif phase == "freezetime":
            n += 1
            yield make("freezetime", {}, 5, 5, "", partial=(n % 2 == 0)), 1.0
        else:
            # live / bomb / defuse：逐步推进击杀数，且保留真实相位名
            steps = max(1, int(dur / 1.5))
            for s in range(steps):
                n += 1
                frac = (s + 1) / steps
                kc = {sid: int(v * frac) for sid, v in kills_by_sid.items()}
                yield make(phase, kc, max(1, 5 - int(3 * frac)), 5 - int(3 * frac),
                           bomb=plant, partial=(n % 2 == 0)), 0.35


def run_simulate(cfg):
    log("=== 仿真模式：用伪造的 GSI 数据验证决策逻辑（不连 OBS、不连 CS2）===")
    cfg = dict(cfg)
    cfg["min_score"] = 25
    cfg["verbose"] = True
    # ★ 仿真要跑"回放能不能用"的全套回归，所以这里显式打开回放功能
    #   （真实默认值是关的，2026-10-07 起；关掉的行为另有专门的回归段落）。
    cfg["replay_enabled"] = True
    # 仿真跑在 armed（省内存）模式下：按入点键才 Enable，裁完 Disable ——
    # 这条通路在真题里没测过，用仿真先把调用顺序验一遍。
    cfg.setdefault("replay_mode", "armed")
    log(f"（仿真设置：replay_enabled=True，replay_mode={cfg['replay_mode']}）")

    obs = NullObsClient(scenes=[cfg["live_scene"], cfg["replay_scene"], "bp"],
                        items={cfg["replay_scene"]: [
                            {"sourceName": "游戏采集", "sceneItemId": 1, "sceneItemEnabled": True},
                            {"sourceName": cfg["replay_item"], "sceneItemId": 2, "sceneItemEnabled": False},
                        ]})
    app = App(cfg, obs, simulate=True)
    app.state.cfg_ref = cfg

    if not app.director.preflight():
        log("❌ preflight 失败")
        return 1

    # 仿真里的时间缩放：让回放时长短到能在几秒内跑完
    app.director.expected_hold = 1.2

    t = threading.Thread(target=app.director.ticker, daemon=True)
    t.start()

    rounds = [
        # (回合, 击杀分布, 下包, 拆包, 残局)
        (1, {"2": 1}, False, False, False),                    # 平淡 → 应跳过
        (2, {"1": 3}, False, False, False),                    # 3杀+AWP → 应回放
        (3, {"2": 1}, True, False, False),                     # 下包1杀 → 12+8=20 应跳过
        (4, {"7": 2}, False, True, True),                      # 2杀+拆包+残局 → 应回放
        (5, {"9": 1}, False, False, True),                     # 1杀+残局 → 8+25=33 应回放
    ]

    for rno, kills, plant, defuse, clutch in rounds:
        log("")
        log(f"──────── 仿真回合 {rno} ────────")
        seq = [("live", 3.0), ("live", 2.0)]
        if plant:
            seq.append(("bomb", 1.5))
        if defuse:
            seq.append(("defuse", 1.5))
        seq.append(("over", 1.0))
        seq.append(("freezetime", 3.0))
        for payload, delay in build_sim_round(rno, kills, plant, defuse, clutch, seq):
            app.on_gsi(payload)
            time.sleep(delay)

    # --- 手动框选流程：← 标入点 / → 标出点 / 小键盘 Enter 播放 ---
    #   （2026-10-06 起引擎不再自动定格，素材只能这样框出来）
    log("")
    log("──────── 手动框选：← 入点 → 出点 → 小键盘 Enter 播放 ────────")
    ctl = ManualController(app.director)
    log("  （没标入点就按 → ：应被拒绝，不会裁片）")
    ctl.on_mark_out()
    log(f"  snapshots = {app.director.counters.get('snapshots', 0)}  (期望 0)")
    log("  （← 标入点，过 0.6 秒再按 → ：应裁出一段）")
    ctl.on_mark_in()
    time.sleep(0.6)
    ctl.on_mark_out()
    log(f"  snapshots = {app.director.counters.get('snapshots', 0)}  (期望 1)")
    log("  （手抖：刚标完入点就按 → ：应被拒绝，入点还留着）")
    ctl.on_mark_in()
    ctl.on_mark_out()
    log(f"  snapshots = {app.director.counters.get('snapshots', 0)}  (期望仍是 1)")
    app.director.mark_in_at = 0.0
    log("  （播放刚裁好的那一段）")
    ctl.on_play()
    log(f"  回放中? {app.director.replay_active}  (期望 True)")
    app.director._end_replay("仿真收尾")
    log(f"  回放中? {app.director.replay_active}  (期望 False)")

    # --- 安全性测试：回放期间回合开打，必须强制让位 ---
    log("")
    log("──────── 安全性测试：自动模式下回放被回合开始打断 ────────")
    log("  （手动模式默认**不**打断，让导播自己按 Enter 收；这里显式打开来测）")
    app.director.cfg["interrupt_on_live"] = True
    app.director.expected_hold = 30.0          # 故意设很长，逼出打断逻辑
    app.director.manual_replay()
    log(f"  回放中? {app.director.replay_active}")
    time.sleep(1.0)                             # 越过 0.8s 宽限期
    live_pkt, _ = next(build_sim_round(9, {}, phase_seq=[("live", 1.0)]))
    app.on_gsi(live_pkt)
    log(f"  收到 live 包后，回放中? {app.director.replay_active}  (期望 False)")
    app.director.cfg["interrupt_on_live"] = None   # 恢复手动模式默认

    # --- 安全性测试：回放中被人工切走 → 必须结束回放并锁定 ---
    log("")
    log("──────── 安全性测试：回放中被人工切走 ────────")
    app.director.expected_hold = 30.0
    app.director.manual_replay()
    log(f"  回放中? {app.director.replay_active}")
    app.director.on_obs_event("CurrentProgramSceneChanged", {"sceneName": "bp"})
    log(f"  回放中? {app.director.replay_active}  (期望 False)")
    log(f"  locked = {app.director.locked}  (期望 True)")
    app.unlock("测试")

    # --- 安全性测试：非回放期间切场景不应锁定（否则 Astra 的阶段切换会把引擎永久锁死）---
    log("")
    log("──────── 安全性测试：非回放期间切场景不锁定（Astra 协作）────────")
    app.director.on_obs_event("CurrentProgramSceneChanged", {"sceneName": "中场休息"})
    log(f"  locked = {app.director.locked}  (期望 False —— 这是 Astra 的阶段切换)")

    # --- 安全性测试：不在直播场景时不插回放（不和 Astra 抢场景）---
    log("")
    log("──────── 安全性测试：不在直播场景时跳过回放 ────────")
    app.obs.current = "中场休息"
    st = RoundStats(6)
    st.final_kills = {"1": 3}
    st.names = {"1": "Test"}
    st.win_team = "CT"
    app.director.on_round_end(6, st, 60, ["3杀"])
    log(f"  replays = {app.director.counters['replays']}  (期望不变，因为不在 HUD 场景)")
    app.obs.current = cfg["live_scene"]

    # --- 安全性测试：freezetime 兜底路径不能重复触发（这是实测抓到的严重 bug）---
    log("")
    log("──────── 安全性测试：freezetime 兜底只能触发一次 ────────")
    log("  （历史 bug：兜底检查的键和写入的键不是同一个回合号，")
    log("    导致冻结时间里每个 GSI 包都重复触发一次，实测 379 条只对应 6 个回合）")
    # 先跑一个"没有 over 包"的回合 7
    for p, d in build_sim_round(7, {"1": 2}, phase_seq=[("live", 2.0)]):
        app.on_gsi(p)
        time.sleep(0.02)
    time.sleep(2.1)                       # 越过防重复时间窗
    base = app.director.counters["rounds"]
    # 再灌 25 个回合 8 的 freezetime 包（模拟冻结时间 10Hz × 2.5 秒）
    pkt, _ = next(build_sim_round(8, {}, phase_seq=[("freezetime", 1.0)]))
    for _ in range(25):
        app.on_gsi(pkt)
        time.sleep(0.01)
    got = app.director.counters["rounds"] - base
    log(f"  25 个 freezetime 包 → 触发 {got} 次回合结束  (期望 1)")

    # --- 2026-10-07 新增：可改按键 / 回放开关 / 省内存模式 ---
    log("")
    log("──────── 可改按键：解析、冲突、兜底 ────────")
    k1, p1 = normalize_keys({"keys": {"mark_in": ["left"], "mark_out": ["right"],
                                      "save": ["numpad6"], "play": ["numpad_enter"]}})
    log(f"  默认键位 → {format_keys(k1)}  (问题 {len(p1)} 个，期望 0)")
    k2, p2 = normalize_keys({"keys": {"mark_in": ["f8", "9"], "mark_out": ["f8"],
                                      "save": ["不存在的键"], "play": ["numpad_enter"]}})
    log(f"  自定义+冲突+错键 → {format_keys(k2)}")
    log(f"  问题 {len(p2)} 个（期望 4：f8 冲突、mark_out 无键退回默认、错键、save 退回默认）：")
    for _p in p2:
        log(f"      · {_p}")
    log(f"  parse_key_token('numpad_enter') = {parse_key_token('numpad_enter')}"
        f"  (期望 (13, True))")
    log(f"  parse_key_token('enter')        = {parse_key_token('enter')}"
        f"  (期望 (13, False))")
    log(f"  binding_of_token('left')        = {binding_of_token('left')}  (期望 37，不区分扩展位)")

    log("")
    log("──────── 内存估算（纯计算）────────")
    _m = estimate_replay_memory(1920, 1080, 60, 10, 2)
    log(f"  1080p60 10 秒 × 2 份 = {_m['steady_gb']:.2f} GB  (期望 ≈9.95)")
    _m2 = estimate_replay_memory(3840, 2160, 60, 10, 2)
    log(f"  4K60    10 秒 × 2 份 = {_m2['steady_gb']:.2f} GB  (期望 ≈39.8)")
    _m3 = memory_estimate_from_cfg({"replay_mode": "armed", "record_max_seconds": 10})
    log(f"  省内存模式提示：{_m3['hint']}")

    log("")
    log("──────── 回放功能默认关闭：四个键不动手 + 插件被 Disable ────────")
    obs.calls.clear()                       # 只数这一段之后的调用
    app.director.set_replay_enabled(False, persist=False)
    for fn, label in ((ctl.on_mark_in, "mark_in"), (ctl.on_mark_out, "mark_out"),
                      (ctl.on_play, "play"), (ctl.on_save, "save")):
        fn()
    n_load = sum(1 for c in obs.calls if c[0] == "TriggerHotkeyByName" and c[1]
                 and c[1].get("hotkeyName") == "ReplaySource.Replay")
    log(f"  关着的时候按四个键 → 触发 Load replay 次数 = {n_load}  (期望 0)")
    hk = [c[1].get("hotkeyName") for c in obs.calls
          if c[0] == "TriggerHotkeyByName" and c[1]]
    log(f"  关掉时发出的插件热键：{hk}  (期望含 ReplaySource.Disable)")
    log(f"  replayEnabled = {app.director.replay_enabled}  (期望 False)")

    log("")
    log("──────── 省内存模式：按入点键才 Enable，裁完立刻 Disable ────────")
    app.director.set_replay_enabled(True, persist=False)
    app.director.set_replay_mode("armed", persist=False)
    obs.calls.clear()
    ctl.on_mark_in()
    hk1 = [c[1].get("hotkeyName") for c in obs.calls
           if c[0] == "TriggerHotkeyByName" and c[1]]
    log(f"  按入点键后插件热键：{hk1}  (期望含 ReplaySource.Enable)")
    time.sleep(0.6)
    obs.calls.clear()
    ctl.on_mark_out()
    hk2 = [c[1].get("hotkeyName") for c in obs.calls
           if c[0] == "TriggerHotkeyByName" and c[1]]
    log(f"  按出点键后插件热键：{hk2}  (期望含 ReplaySource.Replay，且之后是 Disable)")
    log(f"  captureArmed = {app.director._capture_disabled is False}  (期望 False = 已收起缓冲)")
    log(f"  skip = {app.director.clip_skip_from_mark(time.time() - 2.0)}"
        f"  (省内存模式下趁早按 → 期望 0.0)")
    _w = app.director.mark_window()
    log(f"  mark_window: mode={_w.get('mode')} enabled={_w.get('enabled')} "
        f"expired={_w.get('expired')}")
    app.director.set_replay_enabled(False, persist=False)

    log("")
    log("  ℹ️ 基线说明：核心流程计数器（rounds/replays/skipped/interrupted/hold_warnings）")
    log("     应与历史基线一致 —— rounds 7, replays 3, skipped 7, interrupted 1, hold_warnings 2；")
    log("     而 snapshots 从 1 变成 2：多的那 1 次是本段「省内存模式」回归自己裁的片段。")
    log("")
    log("──────── CS 控制台指令：解析 + 安全阀 + 时序（用桩，不真的发按键）────────")
    _c = ConsoleTyper({"cs_console_commands": ["sv_cheats 1", "", "// 注释",
                                               "mp_freezetime 5"],
                       "cs_console_key": "`", "cs_console_trigger": "start",
                       "cs_console_delay_ms": 1, "cs_console_gap_ms": 1}, )
    log(f"  解析出 {len(_c.commands())} 条指令：{_c.commands()}"
        f"  (期望 2：空行和 // 注释被忽略)")
    _c._fg = lambda: "obs64.exe"
    _sent = []
    _c._tap_key = lambda *a, **k: _sent.append(a) or True
    _c._type_text = lambda *a, **k: (True, [])
    log(f"  CS2 不在前台时 send_now = {_c.send_now('仿真')}  (期望 False)")
    log(f"  实际发出的按键数 = {len(_sent)}  (期望 0 —— 安全阀生效)")
    _c._fg = lambda: "cs2.exe"
    _seq = []
    _c._tap_key = lambda sc, shift=False, tap_ms=0, extended=False: _seq.append(
        f"0x{sc:02X}") or True
    _c._type_text = lambda text, per_char_ms=14: _seq.append(text) or (True, [])
    _c.send_now("仿真")
    log(f"  前台是 CS2 时的顺序：{_seq}")
    log(f"    (期望：0x29 开控制台 → 指令 → 0x1C 回车 → 指令 → 0x1C → 0x29 关控制台)")
    log(f"  parse_key/控制台键：resolve_console_key('`') = {resolve_console_key('`')}"
        f"  resolve_console_key('f10') = {resolve_console_key('f10')}")
    log(f"  字符表：A → {CHAR_SCANCODE['A']}（期望 shift=True）"
        f"  _ → {CHAR_SCANCODE['_']}（期望 shift=True）"
        f"  a → {CHAR_SCANCODE['a']}（期望 shift=False）")

    log("")
    log("──────── 内存护栏（纯函数：按内存决定能攒几秒）────────")
    _mc = canvas_from_obs_config()
    log(f"  从 OBS 配置读到的画布：{_mc if _mc else '（没读到，退回 1080p60）'}")
    for _name, _w, _h, _fps, _want, _tot, _av in (
            ("4K60 10s / 32GB 机器（就是死机那台）", 3840, 2160, 60, 10, 32, 20),
            ("4K60 10s / 64GB 机器", 3840, 2160, 60, 10, 64, 40),
            ("1080p60 10s / 32GB 机器", 1920, 1080, 60, 10, 32, 20),
            ("1080p60 10s / 16GB 机器", 1920, 1080, 60, 10, 16, 8),
            ("1080p60 10s / 8GB 机器", 1920, 1080, 60, 10, 8, 4),
            ("4K60 2s / 8GB 机器", 3840, 2160, 60, 2, 8, 4)):
        _safe, _note = plan_replay_seconds(_w, _h, _fps, _want, _tot, _av)
        _budget = memory_budget_gb(_tot, _av)
        _verdict = ("❌ 装不下 → 拒绝开回放" if _safe is None else
                    (f"✅ 维持 {_safe:.1f}s" if _safe == _want else f"⬇ 自动降到 {_safe:.1f}s"))
        log(f"  {_name:34s} 预算 {_budget:5.2f} GB  {_verdict}   {_note}")
    log(f"  本机实际内存：{['%.1f' % x if x else '?' for x in system_memory_gb()]} GB（总/可用）")

    log("")
    log("──────── obs 后端（OBS 自带 Replay Buffer）的纯函数：裁切时间轴 ────────")
    for _name, _fsec, _el in (("缓冲 20s，入点到现在 6s", 20, 6),
                              ("缓冲 20s，入点到现在 20s", 20, 20),
                              ("缓冲 20s，入点已滚出（25s）", 20, 25),
                              ("缓冲 6s（刚启动），入点到现在 2s", 6, 2)):
        _st, _ln, _rolled = trim_plan(_fsec, _el)
        log(f"  {_name:28s} → 从 {_st:.1f}s 处裁 {_ln:.1f}s"
            f"{'（入点滚出缓冲→给整段）' if _rolled else ''}")
    log(f"  OBS 文件名格式翻译：{translate_obs_time_format('回放_%CCYY-%MM-%DD_%hh.%mm.%ss')}"
        f"  (期望 回放_%Y-%m-%d_%H.%M.%S)")

    log("")
    log("=== 仿真结果 ===")
    log(json.dumps(app.status(), ensure_ascii=False, indent=2))
    app.director.stop()
    return 0


# ---------------------------------------------------------------------------
# 5.3 正式运行
# ---------------------------------------------------------------------------

def run_live(cfg, manual_only=False):
    log("=== CS2 自动即时重放导播 启动 ===")
    obs = NullObsClient() if cfg["dry_run"] else ObsClient(cfg["obs_url"], cfg["obs_password"])

    app = App(cfg, obs)
    app.director.auto_enabled = not manual_only

    if cfg["dry_run"]:
        log("⚠️  空跑模式：用假 OBS，只打日志，不会真的切换场景")
    else:
        try:
            obs.connect()
        except Exception as e:
            log(f"❌ 连不上 obs-websocket: {e}")
            log("   请先在 OBS 里：工具 → WebSocket 服务器设置 → 勾选「启用 WebSocket 服务器」")
            return 1
        obs.on_event = app.director.on_obs_event
        log("✅ obs-websocket 已连接")

    if not app.director.preflight():
        return 1

    if manual_only:
        log("")
        log("★ 手动模式（--manual-only）：不会自动触发回放。")
        log("  用来安全地验证 OBS 侧通路 —— 在浏览器打开下面这个地址就会放一次回放：")
        log(f"      http://{cfg['gsi_host']}:{cfg['gsi_port']}/control/replay")
        log("  同时它会把每个回合的判定结果打进日志，方便你对比")
        log("  「引擎觉得该放的」和「你自己觉得该放的」是否一致。")

    # ★ 手动键盘控制（唯一的工作方式：手动框选片段）
    ctl = None
    if cfg.get("manual_keys", True):
        ctl = ManualController(app.director)
        app.controller = ctl          # 让 HTTP 端点 /control/mark_in|mark_out 也走同一套逻辑
        if cfg["dry_run"]:
            # 空跑模式不装钩子（不该真的监听你的键盘），但控制器**要建**：
            # 这样 /control/mark_in|mark_out|replay 能驱动完整流程，方便离线验证。
            log("")
            log("⌨  空跑模式：不装键盘钩子（不会监听你的按键），但可以用 HTTP 端点驱动：")
            log("      /control/mark_in | /control/mark_out | /control/replay | /control/status")
        else:
            hook = make_manual_hook(ctl, cfg)
            if hook.start():
                ctl.hook = hook
                kk = ctl.k
                log("")
                log("⌨  全局热键已启用（只读监听，不拦截按键）：")
                log(f"      {kk['mark_in']:<16}标记入点（记住这一刻）")
                if app.director.replay_mode == "armed":
                    log(f"      {kk['mark_out']:<16}标记出点：把「入点 → 现在」裁成一段素材"
                        f"（省内存模式：攒帧从按入点键开始，最长 {cfg.get('record_max_seconds')} 秒）")
                else:
                    log(f"      {kk['mark_out']:<16}标记出点：把「入点 → 现在」裁成一段素材"
                        f"（缓冲最长 {cfg.get('record_max_seconds')} 秒，所以要在 "
                        f"{cfg.get('record_max_seconds')} 秒内按）")
                log(f"      {kk['play']:<16}切到「即时回放」播放；再按一次立刻切回")
                if cfg.get("save_replay_key", True):
                    log(f"      {kk['save']:<16}保留片段（把当前这段素材另存成文件）"
                        f" → {cfg.get('save_dir') or 'OBS 录制目录'}")
                log("      ★ 键位可以改（图形界面 / --set-keys），配错也能在 config.json 的 keys 里手动写。")
                log("      ★ 引擎不做任何自动定格：不按出点键就没有新素材。")
                if not app.director.replay_enabled:
                    log("      ⛔ 回放功能当前是**关闭**的：上面四个键只提示不动手。")
                    log("         想用就在设置窗口 / 手机页面上打开「回放功能」。")
                nl = numlock_on()
                if nl is False and ("numpad6" in kk.values() or "numpad_enter" in kk.values()):
                    log("      ⚠️ NumLock 灯是灭的：小键盘 6 和 → 发的是同一个按键，"
                        "现在按小键盘 6 只会当「标记出点」。")
                    log("         想让「保留片段」生效，请按一下 NumLock 让灯亮起来，"
                        "或者把这两个键改到主键盘上（设置窗口里点「修改」按一下就行）。")
            else:
                log(f"⚠️  全局热键安装失败：{hook.error}")
                log("    可以用浏览器控制端点代替：/control/mark_in /mark_out /replay")

    app.start_gsi_server()
    app.start_web_server()
    tick = threading.Thread(target=app.director.ticker, daemon=True)
    tick.start()
    # ★ CS 控制台指令：按 cs_console_trigger 自动发（start=等 CS2 到前台发一次）
    try:
        app.console.start_auto()
    except Exception:
        log("!! 启动 CS 控制台指令出错:\n" + traceback.format_exc())

    log("")
    log("就绪。把 gamestate_integration_director.cfg 放进 CS2 的 game/csgo/cfg/ 目录，")
    log("然后启动 CS2 观战即可。控制端点: /control/status | /lock | /unlock | /replay"
        " | /mark_in | /mark_out | /console | /save")
    log("按 Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1)
            if not cfg["dry_run"] and not obs._running:
                log("!! obs-websocket 已断开，引擎停止工作（保持当前画面）")
                break
    except KeyboardInterrupt:
        log("收到中断，退出中…")
    finally:
        app.director.stop()
        if app.server:
            app.server.shutdown()
        obs.close()
    return 0


# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="CS2 自动即时重放导播 (Tier 0)")
    p.add_argument("--config", help="JSON 配置文件路径（覆盖默认值）")
    p.add_argument("--probe", action="store_true", help="探测 OBS 连接与配置后退出")
    p.add_argument("--check-replay-source", action="store_true",
                   help="检查 Replay Source 的设置，并预演将要写入的推荐值")
    p.add_argument("--configure-replay-source", action="store_true",
                   help="把 Replay Source 的关键设置写成推荐值（需与 --apply 一起用才真正写入）")
    p.add_argument("--apply", action="store_true", help="配合 --configure-replay-source，真正写入 OBS")
    p.add_argument("--duration-ms", type=int, default=5000, help="写入的 Duration（毫秒）")
    p.add_argument("--speed-percent", type=float, default=70.0, help="写入的 Speed Percentage")
    p.add_argument("--test-keys", action="store_true",
                   help="只测试全局热键（← / → / 小键盘Enter），不动 OBS")
    p.add_argument("--test-load-replay", action="store_true",
                   help="单独测试 Load replay 热键能否被触发（会真的生成一次回放）")
    p.add_argument("--simulate", action="store_true", help="仿真模式（不需要 CS2/OBS）")
    p.add_argument("--dry-run", action="store_true",
                   help="用假 OBS 跑，只打日志不切场景（纯逻辑验证）")
    p.add_argument("--manual-only", action="store_true",
                   help="连真 OBS，但只响应手动 /control/replay，不自动触发（安全验证 OBS 通路）")
    # ★ 2026-10-07：图形界面 / 回放开关 / 按键
    p.add_argument("--gui", action="store_true", help="打开图形设置窗口（双击 exe 的默认行为）")
    p.add_argument("--engine", "--run", dest="engine", action="store_true",
                   help="正常跑引擎（图形界面用它拉起子进程；不带 GUI）")
    p.add_argument("--no-gui", action="store_true",
                   help="即使没带参数也走命令行引擎，不开图形界面（脚本里用）")
    p.add_argument("--replay", choices=["on", "off"], help="回放功能总开关（默认关）")
    p.add_argument("--mode", choices=["armed", "buffer"],
                   help="内存模式：armed=省内存（按入点键才攒帧，默认）；buffer=常驻滚动缓冲")
    p.add_argument("--backend", choices=["plugin", "obs"],
                   help="回放后端：plugin=插件内存缓冲（默认，未压缩帧、GB 级）；"
                        "obs=OBS 自带 Replay Buffer（编码后、几十~几百 MB，需 ffmpeg）")
    p.add_argument("--media-item", help="obs 后端用：回放场景里的媒体源名字（默认「回放媒体源」）")
    p.add_argument("--memory-report", action="store_true",
                   help="只做内存体检：按画布/时长算出回放插件占多少内存，然后退出")
    p.add_argument("--show-keys", action="store_true", help="打印当前键位后退出")
    p.add_argument("--selftest", action="store_true",
                   help="自检这份程序能不能跑（缺 websocket-client / tkinter / 键位表 / 内存公式）")
    p.add_argument("--set-keys", action="store_true",
                   help="交互式改键：按一下你要用的键就绑上（默认会写回 config.json）")
    p.add_argument("--bind", action="append", metavar="动作=键名[,键名]",
                   help="直接改键，可重复：--bind play=f8 --bind save=f9,numpad6")
    p.add_argument("--save-bind", action="store_true",
                   help="把 --bind / --replay / --mode 的改动写回 config.json")
    # 常用覆盖
    p.add_argument("--gsi-port", type=int)
    p.add_argument("--obs-url")
    p.add_argument("--obs-password")
    p.add_argument("--live-scene")
    p.add_argument("--replay-scene")
    p.add_argument("--replay-item")
    p.add_argument("--min-score", type=int)
    p.add_argument("--capture-seconds", type=float)
    p.add_argument("--speed", type=float)
    p.add_argument("--log-file", help="同时把日志以 UTF-8 写入该文件（建议开启，方便复盘）")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# 5.x 按键设置 / 内存体检 / 图形界面（2026-10-07 加）
# ---------------------------------------------------------------------------

def apply_bind_args(cfg, binds):
    """把 `--bind play=f8` 这类参数应用到 `cfg["keys"]` 上。

    返回 `(keys, problems)`；键名认不出来只报告不动手（绝不悄悄绑错键）。
    """
    keys, problems = normalize_keys(cfg)
    problems = list(problems)
    for item in binds or []:
        if "=" not in str(item):
            problems.append(f"--bind 参数「{item}」格式不对，应该写成 动作=键名")
            continue
        action, val = str(item).split("=", 1)
        action = action.strip().lower()
        if action not in KEY_ACTIONS:
            problems.append(f"--bind 里的动作「{action}」不认识；"
                            f"可用：{'/'.join(KEY_ACTIONS)}")
            continue
        toks = [t.strip() for t in val.replace(" ", ",").split(",") if t.strip()]
        parsed = [(t, parse_key_token(t)) for t in toks]
        bad = [t for t, p in parsed if p is None]
        if bad or not toks:
            problems.append(f"--bind {action}= 里的键名不认识：{'/'.join(bad) or '(空)'}")
            continue
        keys[action] = [key_token_name(*p) for _, p in parsed]
    cfg["keys"] = keys
    return keys, problems


def print_keys(cfg):
    keys, problems = normalize_keys(cfg)
    shown = format_keys(keys)
    log("当前键位（图形界面里点「修改」按一下就能改，或 --bind 动作=键名）：")
    for action, label in KEY_ACTIONS.items():
        log(f"    {label:<18} {shown[action]}")
    for p in problems:
        log(f"    ⚠️ {p}")
    log("小提示：没有小键盘的键盘（60%）可以把「播放 / 收起回放」改成 f8、"
        "「保留片段」改成 f9 —— 在图形界面里按一下就绑上。")


def run_set_keys(cfg, cfg_path, save=True):
    """交互式改键：四个动作各按一下你要用的键（Esc / 超时 = 跳过）。"""
    keys, _ = normalize_keys(cfg)
    shown = format_keys(keys)
    log("")
    log("=== 改键：每个动作按一下你要用的键（Esc 跳过这个动作）===")
    log(f"当前：{shown}")
    for action, label in KEY_ACTIONS.items():
        print(f"\n【{label}】当前是 {shown[action]} —— 请按你要用的键"
              f"（Esc 跳过 / 10 秒不按也跳过）：", flush=True)
        got = capture_next_key(timeout=10.0, echo=False)
        if got is None:
            print("    跳过（保持原样）", flush=True)
            continue
        vk, ext = got
        name = key_token_name(vk, ext)
        print(f"    捕获到：{name}（vkCode=0x{vk:02X}，扩展位={int(bool(ext))}）", flush=True)
        keys[action] = [name]
    cfg["keys"] = keys
    keys, problems = normalize_keys(cfg)      # 再规范化一次：查冲突 / 兜底
    cfg["keys"] = keys
    for p in problems:
        log(f"    ⚠️ {p}")
    print("\n新的键位：" + str(format_keys(keys)), flush=True)
    if save and cfg_path:
        save_config(cfg, cfg_path)
        print(f"已写回 {cfg_path}（引擎下次启动生效；图形界面里改是立即生效）", flush=True)
    return 0


def run_memory_report(cfg, dry_run=False):
    """内存体检：把"回放插件到底占多少内存"算清楚（能连 OBS 就用真画布）。"""
    log("=== 回放插件内存体检 ===")
    obs = NullObsClient() if dry_run else ObsClient(cfg["obs_url"], cfg["obs_password"])
    connected = False
    if not dry_run:
        try:
            obs.connect()
            connected = True
            log("✅ 已连上 obs-websocket，用真实画布参数算")
        except Exception as e:
            log(f"⚠️  连不上 obs-websocket（{e}）—— 用 1920x1080@60 估算")
    d = ReplayDirector(cfg, obs)
    try:
        if connected:
            d._memory_audit()
        else:
            info = d.memory_info()
            log(f"    画布/帧率：{info['width']}x{info['height']} @ {info['fps']:g}fps"
                f"（每帧 {info['frame_mb']:.1f} MB，每秒 {info['mb_per_sec']:.0f} MB）")
            log(f"    单段 {info['seconds']:.1f} 秒 → 一份 ≈ {info['one_copy_gb']:.2f} GB，"
                f"稳态 {info['copies']:g} 份 ≈ {info['steady_gb']:.2f} GB")
        info = d.memory_info()
        log("")
        log("结论：**不是内存泄漏** —— obs-replay-source 把最近 N 秒的未压缩帧放在内存里")
        log("      （画布宽 × 高 × 4 字节 × 帧率 × 秒数）。省内存模式（armed）下只有按了")
        log("      入点键才攒帧、裁完立刻释放；回放功能关闭时插件被 Disable，")
        log("      滚动缓冲那份内存直接还回去。")
        log(f"      想更省：record_max_seconds 从 {info['seconds']:.0f} 秒调小（减半省一半），"
            "或把画布/帧率降一档。")
        return 0
    finally:
        try:
            obs.close()
        except Exception:
            pass



def main():
    global LOG_FILE, CONFIG_PATH
    # 打包后只有这一个 exe：用 --setup 触发首次设置向导。
    # 必须放在 parse_args() 之前 —— 向导自己还有 --dry-run / --check 要接。
    if "--setup" in sys.argv[1:]:
        rest = [a for a in sys.argv[1:] if a != "--setup"]
        try:
            import setup_wizard
        except Exception as e:
            print(f"❌ 找不到 setup_wizard.py（应该和本程序在同一目录）：{e}")
            return 2
        return setup_wizard.main(rest)

    args = parse_args()
    if args.log_file:
        LOG_FILE = args.log_file
    cfg, cfg_path = load_config(args.config)
    CONFIG_PATH = cfg_path
    for a in ("gsi_port", "obs_url", "obs_password", "live_scene", "replay_scene",
              "replay_item", "min_score", "capture_seconds", "speed"):
        v = getattr(args, a, None)
        if v is not None:
            cfg[a] = v
    if args.quiet:
        cfg["verbose"] = False
    cfg["dry_run"] = bool(args.dry_run)

    # ★ 2026-10-07：命令行改键 / 回放开关 / 内存模式（GUI 与手机页面也走同一套字段）
    did_setup = False
    if args.bind:
        _, problems = apply_bind_args(cfg, args.bind)
        for p in problems:
            log(f"⚠️ {p}")
    if args.replay is not None:
        cfg["replay_enabled"] = (args.replay == "on")
    if args.mode is not None:
        cfg["replay_mode"] = args.mode
    if args.backend is not None:
        cfg["replay_backend"] = args.backend
    if args.media_item:
        cfg["replay_media_item"] = args.media_item
    if (args.bind or args.replay is not None or args.mode is not None
            or args.backend is not None or args.media_item) and args.save_bind:
        cfg_path = save_config(cfg, cfg_path) or cfg_path
        CONFIG_PATH = cfg_path
        log(f"已把设置写回 {cfg_path}")

    if args.show_keys:
        print_keys(cfg)
        return 0
    if args.selftest:
        return run_selftest(cfg)
    if args.set_keys:
        return run_set_keys(cfg, cfg_path, save=True)
    if args.memory_report:
        return run_memory_report(cfg, dry_run=args.dry_run)

    # ★ 双击 exe / 不带任何参数 = 打开图形设置窗口（用户 2026-10-07 要求"为整个程序增加 GUI"）。
    #   带明确动作参数（--simulate / --probe / --engine / ...）时仍然走命令行，
    #   这样 6-director.cmd 之类的脚本行为**完全不变**。
    wants_gui = args.gui or (not args.no_gui and not args.engine
                             and len(sys.argv) <= 1)
    if wants_gui:
        try:
            import director_gui
        except Exception as e:
            print(f"⚠️  打不开图形界面（{e}）—— 改用命令行模式。")
            print("    想强制命令行：  开始导播.exe --no-gui")
            wants_gui = False
        else:
            return director_gui.main([])

    # 全新电脑上双击 exe：旁边没有 config.json，也没让它干具体活
    # → 直接把「首次设置向导」拉起来，真正做到开箱即用。
    no_action = not any([args.test_keys, args.probe, args.test_load_replay,
                         args.check_replay_source, args.configure_replay_source,
                         args.simulate, args.manual_only, args.dry_run, args.engine])
    if no_action and not cfg_path:
        print("=" * 66)
        print("  这台电脑还没做过首次设置（旁边没有 config.json）。")
        print("  现在自动帮你配置一次 —— 每一步都会先说明再动手。")
        print("=" * 66)
        print()
        try:
            import setup_wizard
            rc = setup_wizard.main([])
            did_setup = True
        except Exception:
            print("自动配置没能跑起来。手动运行：  开始导播.exe --setup")
            traceback.print_exc()
            rc = 1
        if rc != 0:
            print()
            print("配置没成功 —— 先按上面的提示处理，再双击一次就行。")
            return rc
        cand = os.path.join(base_dir(), "config.json")
        if os.path.exists(cand):          # 配好了，读回来继续启动
            cfg, _ = load_config(cand)
            CONFIG_PATH = cand

    if args.test_keys:
        return run_test_keys(cfg)
    if args.probe:
        return run_probe(cfg)
    if args.test_load_replay:
        return run_test_load_replay(cfg)
    if args.check_replay_source or args.configure_replay_source:
        return run_configure(cfg, args.duration_ms, args.speed_percent,
                             apply=args.configure_replay_source and args.apply)
    if args.simulate:
        return run_simulate(cfg)
    if did_setup:
        log("首次设置完成 —— 接着启动引擎。想改按键/回放开关，双击 exe 打开设置窗口。")
    return run_live(cfg, manual_only=args.manual_only)


if __name__ == "__main__":
    sys.exit(main())

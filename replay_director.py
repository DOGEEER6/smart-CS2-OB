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
import socket
import socketserver
import sys
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

    # --- 手动键盘控制（默认工作方式）---
    #   ←  开始录制：清空缓冲，从此刻开始累积素材
    #   →  结束录制：把这一段定稿成一段可播放的回放
    #   小键盘Enter：切到回放场景播放；再按一次立刻切回直播
    # --- 手动键盘控制 ---
    # record_mode:
    #   "retrospective"（默认，推荐单人导播）只用 → ：
    #        回合结束按一次 → 就抓"最近 N 秒"（击杀通常就在里面），
    #        不用在交火那一瞬间腾出手按键。切视角和回放彻底在时间上分开。
    #   "bracket" 精确框选：← 开始 / → 结束，正好框住那一段，
    #        但必须在交火瞬间按 ←，单人操作容易漏。
    "record_mode": "retrospective",
    "manual_keys": True,
    "record_max_seconds": 10.0,       # 滚动缓冲/单段素材最长多少秒（会写进插件的 Duration）
    "key_debounce": 0.5,              # 同一按键的重复触发间隔（秒）
    # ★ 回合结束后自动"定格"素材：GSI 一看到 phase 变成 over，就等这么久做快照。
    #   这样素材的**结尾正好是回合结束后 1 秒**，后面那些冻结时间/跑位全是废的，
    #   不用你再掐着表按 →。
    "auto_capture_on_round_end": True,
    "capture_after_round_end": 1.0,   # 回合结束后多久定格（秒）
    # 抓到之后要不要自动播。既然现在每回合都会自动定格，默认改成"手动按 Enter 播"，
    # 不然就变成每回合都自动放一遍回放了。
    "auto_play_after_capture": False,
    "auto_play_delay": 0.5,
    # ★ 提前多久切回直播场景。现在默认 **0**：改由插件在片子结束那一帧触发切回
    #   （见 preflight 里写的 next_scene）。只有你把 next_scene 清空、
    #   想让引擎自己掐时间时才需要设成 0.9 之类。
    "return_before_end": 0.0,
    # 手动模式默认不抢控制权：回放期间回合开打也不打断，导播自己按 Enter 收。
    # 自动模式下会自动设为 True。想强制让位就写 true。
    "interrupt_on_live": None,

    # --- 回放素材的取样时机（关键）---
    # 回放缓存的是"触发那一刻往前 N 秒"。如果在回合结束时才快照，拿到的
    # 是回合最后几秒的垃圾时间（跑位/拆枪），击杀早就过去了。
    # 所以要在**击杀发生的那一刻**快照存下来，回合结束时才播。
    "snapshot_on_kill": True,         # 击杀时快照（关掉就退回旧的"回合末快照"行为）
    "snapshot_delay": 1.2,            # 击杀后等几秒再快照，让击杀落在片子中间而不是末尾
    "retrieve_delay_ms": 0,           # 同时写入插件的 "Load Delay"（一般保持 0）

    # --- 和 Astra 的协作（重要）---
    # Astra 自己会按比赛阶段自动切场景（BP/直播/中场/图结束…）。为了不互相打架：
    #   require_live_scene : 只有当直播场景正好是 live_scene 时才插回放。
    #                        Astra 把画面切到"中场休息/数据看板"等场景时，引擎自动让位。
    #   lock_on_manual_scene : 是否把"任何非引擎发起的场景切换"都当成人工接管并锁定。
    #                        ⚠️ 用 Astra 的话必须保持 false，否则 Astra 每次阶段切换
    #                        都会把引擎永久锁死。人工接管请用 /control/lock。
    "require_live_scene": True,
    "lock_on_manual_scene": False,

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
        elif path == "/control/record_start":
            body = json.dumps({"ok": app.record_start()}).encode("utf-8")
            code = 200
        elif path == "/control/record_stop":
            body = json.dumps({"ok": app.record_stop()}).encode("utf-8")
            code = 200
        else:
            body = (b'{"error":"use /control/status | /lock | /unlock | /replay'
                    b' | /record_start | /record_stop"}')
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

    def input_settings(self, name):
        return "replay_source", {"source": "游戏采集", "duration": 5.0, "speed": 0.7}

    def request(self, req_type, data=None, timeout=5.0):
        self.calls.append((req_type, data))
        if req_type == "GetSourceActive":
            return {"videoShowing": True, "videoActive": True}
        if req_type == "TriggerHotkeyByName":
            log(f"    [OBS] 触发热键 {data.get('hotkeyName')} @ {data.get('contextName')}")
        return {}


# ============================================================================
# 4. 重放编排器
# ============================================================================

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
        self._snapshot_round = None      # 快照属于哪个回合
        self._snapshot_ok = False        # 本回合是否已经拿到了"击杀瞬间"的快照
        self._last_snapshot_at = 0.0     # 上次快照的时刻（手动回放会复用 20 秒内的）
        self._pending_snapshot_at = 0.0  # 计划在什么时刻做快照（击杀后延迟 / 回合结束后）
        self._pending_reason = ""        # 这次定时定格是为了什么（写日志用）
        self._snapshot_kills = 0         # 本回合已快照的击杀数（用于日志）
        self.last_clip_seconds = None    # 最近一次快照的**真实**长度（从 OBS 日志读）
        self._current_scene = None       # 本地镜像的当前节目场景，避免频繁查询 OBS
        self.last_replay_round = None
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
                mark = "   <== 这就是回放源"
                self.replay_item_id = it.get("sceneItemId")
            log(f"    - {it.get('sourceName')}  (id={it.get('sceneItemId')}, "
                f"{'可见' if it.get('sceneItemEnabled') else '隐藏'}){mark}")
        if self.replay_item_id is None:
            log(f"❌ 回放场景里找不到名为「{cfg['replay_item']}」的源，请核对名字。")
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
        except Exception as e:
            log(f"⚠️  读回放源设置失败（不影响运行）: {e}")

        self.expected_hold = min(
            float(cfg.get("record_max_seconds", 10.0)) / max(cfg["speed"], 0.05)
            - float(cfg.get("return_before_end", 0.0)) + 1.0,
            cfg["max_hold"])
        log(f"预计单次回放最长占用 {self.expected_hold:.1f}s"
            f"（= 素材上限 {cfg.get('record_max_seconds')} 秒 / {cfg['speed']*100:.0f}% 速度）")

        # 把"单段素材上限"和速度写进插件（config 变成唯一事实来源，不用手动同步两处）
        want_ms = int(float(cfg.get("record_max_seconds", 10.0)) * 1000)
        want_spd = round(float(cfg.get("speed", 0.7)) * 100.0, 2)
        try:
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

            # ★ 冲一次旧缓冲：改了 Duration 之后，滤镜里可能还留着"按旧上限攒的帧"
            #   （实测：配置 10 秒但第一次定格拿到 12 秒）。启动时先定格一次把它倒掉，
            #   之后每次定格的长度就都是按新配置算的了。
            try:
                self._hk("ReplaySource.Replay")
                log("   已冲掉启动前的旧缓冲（第一次定格会略长，之后就是新上限了）")
            except Exception:
                pass
            log(f"   单段素材上限 {want_ms/1000:.0f} 秒，回放速度 {want_spd:.0f}%"
                f" → 播放约 {want_ms/1000/max(want_spd/100,0.05):.1f} 秒")
            log(f"   ⚠️ 内存提示：这段缓冲要一直占着内存。4K 采集下 10 秒约 7.5 GB，"
                f"1080p 约 1.9 GB。不够用就把 record_max_seconds 调小。")
        except Exception as e:
            log(f"   （同步 Duration/Speed 失败，不影响使用: {e}）")
        return True if self.replay_item_id is not None else False

    # ---------------- 击杀事件：计划快照 ----------------
    def on_kill(self, sid, kill_count, round_no, name):
        """（只在自动模式下用）检测到击杀，排一个延迟快照。"""
        cfg = self.cfg
        if not cfg.get("auto_replay", False):
            return
        if not cfg.get("snapshot_on_kill", True):
            return
        if self.locked or self.replay_active:
            return
        if self.state_ref is not None and round_no != self.state_ref.live_round:
            return
        # 没在拍游戏（比如导播切到了数据看板）→ 滤镜里没有帧，快照也是空的
        if self._current_scene and self._current_scene != cfg["live_scene"]:
            logv(cfg, f"   [击杀] {name} 第 {kill_count} 杀，但当前在「{self._current_scene}」"
                      f"不是直播场景，不快照")
            return

        first = (self._snapshot_round != round_no)
        if first:
            self._snapshot_round = round_no
            self._snapshot_ok = False
            self._snapshot_kills = 0
        self._snapshot_kills += 1
        delay = float(cfg.get("snapshot_delay", 1.2))
        self._pending_snapshot_at = time.time() + delay
        tag = "⏱ 重排" if not first else "⏱"
        log(f"   {tag} [击杀] 回合 {round_no}  {name} 拿到第 {kill_count} 杀"
            f" → {delay:.1f}s 后快照回放素材")

    def _fire_pending_snapshot(self):
        self._pending_snapshot_at = 0.0
        if self.replay_active or self.locked:
            return
        why = getattr(self, "_pending_reason", "") or "定时定格"
        log(f"   📸 定格素材（{why}）")
        self._pending_reason = ""
        self._load_replay(self._snapshot_round)

    # ---------------- 人工接管 / 锁 ----------------
    def lock(self, reason):
        with self.lock_state:
            if not self.locked:
                log(f"🔒 已锁定自动导播（原因：{reason}）—— 手动接管优先")
            self.locked = True
            self.locked_reason = reason

    def unlock(self, reason):
        with self.lock_state:
            if self.locked:
                log(f"🔓 已解锁自动导播（原因：{reason}）")
            self.locked = False
            self.locked_reason = ""

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
        #   识别方式：从「即时回放」回到「直播场景」的转变，而且不是我们发起的
        #   （我们自己切的时候 replay_active 已经先置 False 了，所以不会误判）。
        if (self.replay_active and scene == self.cfg["live_scene"]
                and prev == self.cfg["replay_scene"]):
            self._end_replay("素材播完（插件按 next_scene 自动切回）")
            return
        if scene in self._self_scenes and now <= self._self_scene_until:
            logv(self.cfg, f"   （场景事件 {scene} 由引擎自己触发，忽略）")
            return
        # 回放中被人切走 —— 无疑义的人工接管
        if self.replay_active and scene != self.cfg["replay_scene"]:
            self.lock(f"回放中被切到「{scene}」")
            self._end_replay("人工接管")
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
        self._current_scene = scene
        log(f"   [OBS] 切到场景「{scene}」")
        self.obs.set_scene(scene)

    # ---------------- 回合结束回调 ----------------
    def on_round_end(self, round_no, stats, score, reasons):
        cfg = self.cfg
        self.counters["rounds"] += 1

        # ★ 不管自动回放开没开，都在"回合结束 +1 秒"处把素材定格。
        #   这样素材结尾正好是回合结束后 1 秒，后面那些冻结时间/跑位全是废的，
        #   不用你掐着表按 →。你只需要在想放的时候按一下【小键盘 Enter】。
        if cfg.get("auto_capture_on_round_end", True):
            delay = float(cfg.get("capture_after_round_end", 1.0))
            if self.replay_active or self.locked:
                self.counters["skipped"] += 1
                log(f"   （回合 {round_no} 结束，但正在回放/已锁定，本回合不定格素材）")
            elif time.time() - self._last_snapshot_at < float(cfg["record_max_seconds"]):
                # 刚定格过（比如你手动按过 →），缓冲还没攒够，再定格只会拿到很短一段
                self.counters["skipped"] += 1
                log(f"   （回合 {round_no} 结束，但 {cfg['record_max_seconds']:.0f} 秒内刚定格过，跳过）")
            else:
                self._pending_snapshot_at = time.time() + delay
                self._pending_reason = f"回合 {round_no} 结束 +{delay:.1f}s 自动定格"
                log(f"⏱  回合 {round_no} 结束 → {delay:.1f} 秒后自动定格素材"
                    f"（结尾就停在回合结束后 {delay:.1f} 秒，不留无用尾巴）")
                log(f"   本回合评分 {score}（{', '.join(reasons) or '平淡'}）——仅供参考")

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
        self._start_replay("手动触发", None, reuse_snapshot=True)
        return True

    # ---------------- 回放生命周期 ----------------
    def _start_replay(self, why, round_no, reuse_snapshot=False, hold_seconds=None):
        """
        把"决定用哪段素材 + 算播放时长 + 真正开播"整段放进 _capture_lock。

        ⚠️ 为什么必须这样：
          定格（_load_replay）要读 OBS 日志才能知道素材多长，要花 0.5~1 秒。
          如果这期间另一条线程（你按 Enter）开始播放，它会读到**上一次的旧长度**，
          于是按旧长度算播放时长 → 素材 12 秒却只播 7 秒，精彩的部分被砍掉。
          （这是实测到的真实故障，不是你操作问题。）
        """
        with self._capture_lock:
            # --- 1. 决定用哪段素材；没有就现在定格（RLock，可重入）---
            have = self._snapshot_ok and (
                (round_no is not None and self._snapshot_round == round_no)
                or (reuse_snapshot and self._last_snapshot_at > 0))
            if have:
                age = time.time() - self._last_snapshot_at
                log(f"   ✔ 播放已定稿的素材（{age:.0f} 秒前存的，不再重新定格）")
                self._pending_snapshot_at = 0.0
                try:
                    self._hk("ReplaySource.Last")
                    log("   ✔ 已选中最新一段素材")
                except Exception as e:
                    logv(self.cfg, f"   （ReplaySource.Last 失败，忽略: {e}）")
            else:
                log("   ⚠️ 还没有定稿的素材 → 现在定格一次（取最近 N 秒）")
                self._load_replay(round_no)

            # --- 2. 用**锁内读到的最新长度**算播放时长 ---
            clip = hold_seconds if hold_seconds is not None else self.last_clip_seconds
            hold = self.expected_hold
            if clip:
                play = clip / max(self.cfg["speed"], 0.05)
                back = float(self.cfg.get("return_before_end", 0.0))
                back = min(back, max(0.0, play * 0.35))
                hold = max(0.6, play - back)
                log(f"   本次素材 {clip:.2f} 秒 @ {self.cfg['speed']*100:.0f}% 速度"
                    f" → 播 {play:.1f} 秒"
                    + (f"，在剩 {back:.1f} 秒时切回" if back > 0.05 else "，播完由插件切回"))
            else:
                # ★ 长度未知时**不要往短了猜** —— 猜短了片子会被腰斩（实测过）。
                #   按素材上限给足时间，精确收尾交给插件的 next_scene。
                log(f"   素材长度未知 → 按上限给足 {hold:.1f} 秒，由插件精确收尾")

            self.replay_active = True
            self._started_at = time.time()
            self._phase_at_start = self.state_ref.phase if self.state_ref else None
            # 播放窗口 += 5 秒安全垫：正常情况下插件会在片子结束那一帧就切回去，
            # 这个是"插件没生效"时的兜底。
            self.replay_until = time.time() + hold + 5.0
            self.replay_hard_deadline = self.replay_until + 10.0
            self.last_replay_round = round_no if round_no is not None else self.last_replay_round
            self.counters["replays"] += 1
            self._pending_snapshot_at = 0.0   # 已经决定播了，取消还没到点的定格
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
            # 切场景 + 显示回放源。Visibility Action=Restart 会让它从头开始播。
            if self.obs.scene_list()[1] != self.cfg["replay_scene"]:
                self._switch_program(self.cfg["replay_scene"])
            else:
                self._current_scene = self.cfg["replay_scene"]
            self.obs.set_item_enabled(self.cfg["replay_scene"], self.replay_item_id, True)
            for name, iid in self.overlay_item_ids:
                self.obs.set_item_enabled(self.cfg["replay_scene"], iid, True)
        except Exception:
            log("!! 启动回放失败:\n" + traceback.format_exc())
            self._end_replay("启动失败")

    # ------------------------------------------------------------------
    def _hk(self, name):
        """触发 Replay Source 的源级热键（必须带 contextName）。"""
        self.obs.request("TriggerHotkeyByName",
                         {"hotkeyName": name, "contextName": self.cfg["replay_item"]})

    def _load_replay(self, round_no=None):
        """
        触发 Replay Source 的 "Load replay" 热键，把回放滤镜缓存的最近 N 秒
        快照成一个可播放的回放。

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
            return self._load_replay_locked(round_no)

    def _load_replay_locked(self, round_no=None):
        """
        实测结论（2026-10-05 在本机 OBS 32.2.2 + replay-source 上验证）：
          * obs-websocket 的 TriggerHotkeyByName **可以**触发源级热键，
            但必须带 contextName = 回放源的名字。
          * 触发后 OBS 日志会出现 `replay added of X seconds`，用**字节偏移**读新行，
            不能用"文本差异"判断（同样的长度文本完全一样，会认错）。
          * ⚠️ 每次快照会"吃掉"滤镜缓冲：紧接着再按一次，只会有很短的一小段。
        """
        cfg = self.cfg
        name = cfg.get("load_replay_hotkey") or ""
        if not name:
            return
        # 限流：两次 Load replay 之间至少要隔着 record_max_seconds，否则第二次是残缺的
        gap = time.time() - self._last_load_replay
        need = max(1.0, float(cfg.get("record_max_seconds", 10.0)))
        if self._last_load_replay and gap < need:
            log(f"   ⚠️  距上次定格只有 {gap:.1f}s（需要 {need:.1f}s），"
                f"这次素材会比上限短（{gap:.1f} 秒左右）。")
        try:
            mark = obs_log_mark()
            self.obs.request("TriggerHotkeyByName",
                             {"hotkeyName": name, "contextName": cfg["replay_item"]})
            self._last_load_replay = time.time()
            self._last_snapshot_at = time.time()
            self._snapshot_ok = True
            self._snapshot_round = round_no if round_no is not None else self._snapshot_round
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
                self.last_clip_seconds = true_len
                want = float(cfg.get("record_max_seconds", 10.0))
                log(f"   ✔ 素材已定稿（OBS 实测 {true_len:.2f} 秒）")
                if true_len < 1.0:
                    log(f"   ❌ 太短了！只有 {true_len:.2f} 秒，几乎看不到东西。")
                    log("      常见原因：")
                    log("        1) 抓取前 OBS 不在 HUD 场景 → 游戏采集没在渲染，缓冲是空的")
                    log("        2) 游戏窗口被最小化 / 游戏没在出画面")
                    log("        3) 刚刚抓过一次，缓冲才刚开始重新累积")
                elif true_len < want * 0.6:
                    log(f"   ⚠️ 比上限 {want:.0f} 秒短不少。如果这不是你刚抓过一次，")
                    log("      检查一下游戏采集是否一直在出画面（别切走 HUD 场景）。")
            else:
                self.last_clip_seconds = None
                log(f"   ✔ 已触发 Load replay（{name} @ {cfg['replay_item']}）")
        except Exception as e:
            log(f"   ⚠️  触发 Load replay 失败: {e}")
            log("      回放里会是空的。可用 --test-load-replay 单独排查。")

    def _end_replay(self, why):
        if not self.replay_active:
            return
        self.replay_active = False
        log(f"⏹  结束回放（{why}）")
        # ★ 顺序很重要：**先切场景，再隐藏源**。
        #   反过来的话，源先消失、画面会闪一下空帧，看起来就是"卡顿"。
        #   先切场景时，转场会把慢放画面盖住，回放源在被隐藏时已经不在节目输出里了。
        try:
            if self.obs.scene_list()[1] != self.cfg["live_scene"]:
                self._switch_program(self.cfg["live_scene"])
            else:
                log(f"   [OBS] 已经在直播场景「{self.cfg['live_scene']}」")
        except Exception:
            log("!! 切回直播场景失败:\n" + traceback.format_exc())
        try:
            self.obs.set_item_enabled(self.cfg["replay_scene"], self.replay_item_id, False)
            for name, iid in self.overlay_item_ids:
                self.obs.set_item_enabled(self.cfg["replay_scene"], iid, False)
            log("   [OBS] 已隐藏回放源")
        except Exception:
            log("!! 隐藏回放源失败:\n" + traceback.format_exc())

    # ---------------- 时钟线程 ----------------
    def ticker(self):
        while not self._stop:
            time.sleep(0.05)
            # 到点了就做"击杀瞬间"快照（回合还在打，画面继续直播，只是把素材存下来）
            if self._pending_snapshot_at and time.time() >= self._pending_snapshot_at:
                try:
                    self._fire_pending_snapshot()
                except Exception:
                    log("!! 快照出错:\n" + traceback.format_exc())
            if self.replay_active and time.time() >= self.replay_until:
                self._end_replay("播放完成")
            elif self.replay_active and time.time() >= self.replay_hard_deadline:
                self.counters["interrupted"] += 1
                self._end_replay("超过硬上限")

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
            "expectedHold": round(self.expected_hold, 2),
            "currentScene": self._current_scene,
            "snapshotRound": self._snapshot_round,
            "snapshotReady": self._snapshot_ok,
            "snapshotAgoSec": (round(time.time() - self._last_snapshot_at, 1)
                               if self._last_snapshot_at else None),
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


class KeyHook:
    """
    全局低级键盘钩子（WH_KEYBOARD_LL）。

    **只读取按键，不拦截、不改写、不注入、不模拟。** 按键会照常传给当前窗口，
    所以它完全不碰游戏进程，与 VAC 无关 —— 只是"监听到你按了哪个键"。

    绑定表支持两种键：
        VK_LEFT                → 匹配该键（不管扩展位）
        (VK_RETURN, True)      → 只匹配小键盘 Enter（扩展位=1）
      小键盘 Enter 和主键盘 Enter 的 vkCode 都是 0x0D，靠扩展位区分。
    """

    def __init__(self, bindings, debounce=0.4):
        _setup_win_prototypes()
        self.bindings = bindings
        self.debounce = debounce
        self._last = {}
        self._proc = _HOOKPROC(self._dispatch)
        self._hook = None
        self.ok = False
        self.error = ""

    def _lookup(self, vk, ext):
        for k in ((vk, ext), vk):
            if k in self.bindings:
                return self.bindings[k]
        return None

    def _dispatch(self, nCode, wParam, lParam):
        try:
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk, ext = int(kb.vkCode), bool(kb.flags & LLKHF_EXTENDED)
                cb = self._lookup(vk, ext)
                if cb:
                    now = time.time()
                    if now - self._last.get((vk, ext), 0.0) >= self.debounce:
                        self._last[(vk, ext)] = now
                        # 回调放独立线程跑，绝不阻塞钩子（会拖慢全系统输入）
                        threading.Thread(target=cb, daemon=True).start()
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

    def start(self):
        """
        安装钩子 + 跑消息循环。

        ⚠️ 两个必须踩对的坑（都踩过）：
          1) GetModuleHandleW 必须设 restype=c_void_p，否则 64 位下句柄被截成
             32 位整数 → SetWindowsHookExW 报 GetLastError=126 (MOD_NOT_FOUND)。
          2) WH_KEYBOARD_LL 的回调是投递到**安装钩子的那个线程**的，
             所以安装和消息循环必须在**同一个线程**里做。
        """
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
        if self._hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
            except Exception:
                pass
            self._hook = None
            self.ok = False


class ManualController:
    """
    导播手动的三段式即时回放：

        ←  开始录制   清空缓冲，从此刻开始累积素材（Disable → Enable）
        →  结束录制   把这一段定稿成一段可播放的回放（Load replay）
        小键盘Enter   切到「即时回放」场景播放；再按一次立刻切回直播

    为什么 ← 要先 Disable 再 Enable：
        实测（2026-10-05）`ReplaySource.Enable` 会重建滤镜、**清空缓冲重新累积**。
        所以"Disable + Enable"= 从此刻开始全新录一段，
        按 → 时拿到的素材长度 ≈ 你按住的那段时间，而不是模糊的"倒回 N 秒"。
    """

    def __init__(self, director):
        self.d = director
        self.recording = False
        self.rec_started = 0.0
        self.clip_seconds = None
        self.hook = None
        self.mode = (director.cfg.get("record_mode") or "retrospective").lower()

    # ---------------- 事后抓取模式（默认，单人导播用这个）----------------
    def on_capture(self):
        """
        只用这一个键：抓"最近 N 秒"。

        为什么单人导播应该用这个：
          它把"切视角"和"回放"在时间上彻底分开 ——
          回合进行中你只管切视角，回合结束才按这一个键，
          不用在交火那一瞬间腾出手。
        """
        d = self.d
        if d.replay_active:
            log("   （正在回放中，先按【小键盘 Enter】收掉再抓）")
            return
        if self.recording:
            log("   （上一段还在录，先按 → 结束它）")
            return
        # 守卫：没在拍游戏 → 缓冲里没有帧，抓出来会是空的或极短
        if d._current_scene and d._current_scene != d.cfg["live_scene"]:
            log(f"⚠️  现在 OBS 在「{d._current_scene}」而不是「{d.cfg['live_scene']}」——")
            log("    游戏采集没在渲染，缓冲里可能没有画面。抓出来会很短甚至空的。")
            log("    事后抓取要求你**一直在 HUD 场景**（那是唯一含游戏采集的场景）。")
        log(f"⏺  【→ 抓取】把最近 {d.cfg.get('record_max_seconds')} 秒定稿为一段素材")
        try:
            d._load_replay(None)
        except Exception as e:
            log(f"❌ 抓取失败: {e}")
            return
        self.clip_seconds = getattr(d, "last_clip_seconds", None)
        if self.clip_seconds:
            log(f"⏹  素材就绪（OBS 实测 {self.clip_seconds:.2f} 秒）")
        if d.cfg.get("auto_play_after_capture", True):
            delay = float(d.cfg.get("auto_play_delay", 0.5))
            log(f"    {delay:.1f} 秒后自动切画面播放（想手动播就把 "
                f"auto_play_after_capture 改成 false）")
            time.sleep(delay)
            self.on_play()
        else:
            log("    → 按【小键盘 Enter】切画面播放")

    # ---------------- 精确框选模式（← 开始 / → 结束）----------------
    def on_record_start(self):
        d = self.d
        if self.mode != "bracket":
            log("ℹ️  当前是【事后抓取】模式，← 键没启用。")
            log("    回合结束按一次 → 就抓最近 N 秒；想用精确框选，"
                "把 config.json 的 record_mode 改成 \"bracket\"。")
            return
        try:
            d._hk("ReplaySource.Disable")     # 清空
            time.sleep(0.05)
            d._hk("ReplaySource.Enable")      # 从此刻重新累积
        except Exception as e:
            log(f"❌ 【← 开始录制】失败: {e}")
            return
        self.recording = True
        self.rec_started = time.time()
        self.clip_seconds = None
        d.last_clip_seconds = None            # 别把上一段的长度带过来
        log("⏺  【← 开始录制】已清空缓冲，从这一刻开始录")

    def on_record_stop(self):
        # 事后抓取模式下，→ 就是"抓取"这一个动作
        if self.mode != "bracket":
            return self.on_capture()
        d = self.d
        dur = (time.time() - self.rec_started) if self.recording else None
        if dur is None:
            log("⏹  【→ 结束录制】⚠️ 你还没按过 ←！")
            log("    现在取的是插件的滚动缓冲（最近 N 秒），很可能不是你要的那一段。")
            log("    正确用法：打起来时先按 ←，打完了再按 →。")
        # ⚠️ 必须走 _load_replay()，不能直接调热键：
        #    否则引擎不知道自己已经有素材了，你在按 Enter 播放时它会**再快照一次**，
        #    而插件只留 1 个回放 → 把刚录好的那一段覆盖成"从 → 到 Enter"的垃圾画面。
        try:
            d._load_replay(None)
        except Exception as e:
            log(f"❌ 【→ 结束录制】失败: {e}")
            return
        self.recording = False
        # 用 OBS 日志里读到的**真实**素材长度来算播放时长
        self.clip_seconds = getattr(d, "last_clip_seconds", None)
        log("⏹  【→ 结束录制】素材已定稿")

    def on_play(self):
        d = self.d
        if d.replay_active:
            log("⏏  【小键盘 Enter】立刻切回直播")
            d._end_replay("手动切回")
            return
        # 不传长度：让引擎在锁内读**最新**的素材长度，避免用到旧的
        log("▶  【小键盘 Enter】切画面播放")
        d._start_replay("手动播放", None, reuse_snapshot=True)


def make_manual_hook(controller, debounce=0.5):
    """
    键位是分开绑定的，按模式走：

      事后抓取(rétrospective，默认)  →  只用一个键：
          →  抓取最近 N 秒（抓到后可选自动播）
          小键盘 Enter  切画面播放 / 再按一次切回

      精确框选(bracket)  →  三个键：
          ←  开始录制    →  结束录制    小键盘 Enter  播放

    两种模式下 ← 都绑上，但事后抓取模式里按它只会打印一句提示，不做任何事。
    """
    return KeyHook({VK_LEFT: controller.on_record_start,
                    VK_RIGHT: controller.on_record_stop,
                    (VK_RETURN, True): controller.on_play},
                   debounce=debounce)


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


def load_or_make_token():
    """手机端开关自动切换需要一个口令，防止同网段的别人乱点。

    存在 viewer_token.txt 里，**重启后不变**，这样手机上的书签一直有效。
    """
    import secrets
    p = os.path.join(base_dir(), "viewer_token.txt")
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
            log("❌ 自动切换：SendInput 发送失败，已关闭")

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

#warn{display:none;background:#3a2a10;border:1px solid #7a5a20;color:#ffc46b;
  border-radius:10px;padding:9px 11px;font-size:14px;margin-bottom:9px;line-height:1.5}
#warn.on{display:block}
#cue{display:none;background:#16321f;border:1px solid #2f6f4f;border-radius:12px;
  padding:12px;margin-bottom:9px;font-size:20px;color:#8ee6b0;text-align:center;
  font-weight:700;animation:cue 1.1s infinite alternate}
#cue.on{display:block}
@keyframes cue{from{background:#16321f}to{background:#204d2e}}
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
<div id="cue">&#9654; 回合结束，按 [ &rarr; ] 抓回放</div>
<div id="killbar"></div>
<div id="list"></div>
</div>
<div id="tgs"><button class="tg" id="tgb">震动提醒</button><button class="tg" id="tgn">显示名字</button></div>
<script>
var vib=false, showNames=false, lastSnapAt=Date.now(), lastKillCount=0;
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
  try{ render(JSON.parse(e.data)); }catch(x){}
};
es.onerror=function(){ document.getElementById('dot').className=''; };
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
            st = self.app.set_auto(on=on, preset=preset, win_bonus=wb)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "auto": st}, ensure_ascii=False).encode("utf-8"))
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
        self.viewer_token = ""
        self._snap_json = None
        self.web = None
        self.server = None

    def viewer_json(self):
        return self._snap_json

    def on_gsi(self, payload):
        self.state.on_gsi(payload)
        self.director.on_gsi_tick(self.state.phase)
        # 副驾提示器 + 自动切换：每个包都重算一次（10 人 × 10Hz，开销可忽略）
        try:
            snap = self.attention.update(payload)
            self.auto.on_packet(snap, self.director)
            snap["auto"] = self.auto.state()
            self._snap_json = json.dumps(snap, ensure_ascii=False)
        except Exception:
            log("!! 提示器/自动切换计算出错:\n" + traceback.format_exc())

    def set_auto(self, on=None, preset=None, win_bonus=None):
        if preset is not None:
            self.auto.apply_preset(preset)
        if win_bonus is not None:
            self.auto.set_win_bonus(win_bonus)
        if on is not None:
            self.auto.set_enabled(on)
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

    def record_start(self):
        if not self.controller:
            log("（没有键盘控制器，忽略 record_start）")
            return False
        self.controller.on_record_start()
        return True

    def record_stop(self):
        if not self.controller:
            log("（没有键盘控制器，忽略 record_stop）")
            return False
        self.controller.on_record_stop()
        return True

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
            f" | /record_start | /record_stop")


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
    log("请依次按这三个键，窗口里会实时打印。按 Ctrl+C 结束。")
    log("   ←  左方向键")
    log("   →  右方向键")
    log("   小键盘 Enter（不是主键盘那个大的 Enter）")

    def mk(label, is_target):
        def f():
            tag = "✅ 已绑定" if is_target else "（未绑定）"
            log(f"   收到: {label}  {tag}")
        return f

    hook = KeyHook({
        VK_LEFT: mk("← 左方向键", True),
        VK_RIGHT: mk("→ 右方向键", True),
        (VK_RETURN, True): mk("小键盘 Enter", True),
        VK_RETURN: mk("主键盘 Enter", False),
    }, debounce=0.25)
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
            entry = {"name": names[sid], "team": team, "clan": ""}
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
            "allplayers": allp, "player": {},
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
    cfg["snapshot_delay"] = 0.4     # 仿真里压缩时间，好在回合结束前把快照做出来
    cfg["verbose"] = True

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

    # ★ 手动键盘控制（默认工作方式）
    ctl = None
    if cfg.get("manual_keys", True) and not cfg["dry_run"]:
        ctl = ManualController(app.director)
        app.controller = ctl          # ← 让 HTTP 端点 /control/record_start|stop 也能走同一套逻辑
        hook = make_manual_hook(ctl, debounce=float(cfg.get("key_debounce", 0.4)))
        if hook.start():
            ctl.hook = hook
            log("")
            log("⌨  全局热键已启用（只读监听，不拦截按键）：")
            if (cfg.get("record_mode") or "").lower() == "bracket":
                log("      模式：精确框选（bracket）")
                log("      ←  左方向键      开始录制（清空缓冲，从此刻起录）")
                log("      →  右方向键      结束录制（把这一段定稿）")
            else:
                log("      模式：事后抓取（retrospective）—— 单人导播推荐")
                log(f"      →  右方向键      抓取最近 {cfg.get('record_max_seconds')} 秒"
                    f"为一段素材   ★ 你平时只需要按这一个")
                if cfg.get("auto_play_after_capture", True):
                    log("                      （抓到后会自动切画面播放）")
                log("      ←  左方向键      此模式下未启用（想精确框选请把 config.json 的")
                log("                       record_mode 改成 \"bracket\"）")
            log("      小键盘 Enter     切到「即时回放」播放；再按一次立刻切回")
        else:
            log(f"⚠️  全局热键安装失败：{hook.error}")
            log("    可以用浏览器控制端点代替：/control/record_start /record_stop /replay")

    app.start_gsi_server()
    app.start_web_server()
    tick = threading.Thread(target=app.director.ticker, daemon=True)
    tick.start()

    log("")
    log("就绪。把 gamestate_integration_director.cfg 放进 CS2 的 game/csgo/cfg/ 目录，")
    log("然后启动 CS2 观战即可。控制端点: /control/status | /lock | /unlock | /replay")
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


def main():
    global LOG_FILE
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
    cfg = dict(DEFAULTS)
    cfg_path = args.config
    if not cfg_path:
        # 打包成 exe 双击运行时没人给它 --config，自动认旁边的 config.json
        cand = os.path.join(base_dir(), "config.json")
        if os.path.exists(cand):
            cfg_path = cand
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    for a in ("gsi_port", "obs_url", "obs_password", "live_scene", "replay_scene",
              "replay_item", "min_score", "capture_seconds", "speed"):
        v = getattr(args, a, None)
        if v is not None:
            cfg[a] = v
    if args.quiet:
        cfg["verbose"] = False
    cfg["dry_run"] = bool(args.dry_run)

    # 全新电脑上双击 exe：旁边没有 config.json，也没让它干具体活
    # → 直接把「首次设置向导」拉起来，真正做到开箱即用。
    no_action = not any([args.test_keys, args.probe, args.test_load_replay,
                         args.check_replay_source, args.configure_replay_source,
                         args.simulate, args.manual_only, args.dry_run])
    if no_action and not cfg_path:
        print("=" * 66)
        print("  这台电脑还没做过首次设置（旁边没有 config.json）。")
        print("  现在自动帮你配置一次 —— 每一步都会先说明再动手。")
        print("=" * 66)
        print()
        try:
            import setup_wizard
            rc = setup_wizard.main([])
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
            with open(cand, encoding="utf-8") as f:
                cfg.update(json.load(f))

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
    return run_live(cfg, manual_only=args.manual_only)


if __name__ == "__main__":
    sys.exit(main())

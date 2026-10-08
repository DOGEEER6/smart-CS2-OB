#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CS2 导播副驾 —— 桌面设置窗口 (tkinter)

给不习惯开记事本改 config.json 的人一个点点点的界面：
  · 上面填 OBS 地址/密码，下面一眼看到手机该扫的地址
  · 中间是「回放功能」总开关、内存模式、单段秒数，改一下就实时算内存
  · 四个热键（标记入点/标记出点/保留片段/播放）可以现场按键改，不用查键码
  · 底部能直接启动/停止引擎、跑首次设置向导、看引擎日志

用法：
    python director_gui.py                 # 直接开窗口
    python director_gui.py --config x.json # 用指定配置文件

接口契约见 frozen spec（本文件只使用其中的引擎 API，不复制引擎逻辑）：
    rd.DEFAULTS / base_dir / lan_ips / load_or_make_token / load_config / save_config
    rd.KEY_ACTIONS / key_token_name / normalize_keys / format_keys / KeyCapture
    rd.estimate_replay_memory / DEFAULT_CANVAS / memory_estimate_from_cfg

只用标准库；中文界面，UTF-8。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser

# ★ 必须是静态 import：PyInstaller 只认静态导入才会把它打进包里。
import replay_director as rd

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    _TK_IMPORT_ERROR = ""
except Exception as _e:            # 精简版 Python 没带 tkinter
    tk = ttk = messagebox = None
    _TK_IMPORT_ERROR = str(_e)

WINDOW_TITLE = "CS2 导播副驾 · 设置"
NO_TK_MESSAGE = "这台电脑没有 tkinter，请用 6-director.cmd 命令行模式"

UI_FONT = ("Microsoft YaHei UI", 9)
SMALL_FONT = ("Microsoft YaHei UI", 8)
LOG_FONT = ("Consolas", 9)

ESC_VK = 0x1B                      # Esc：取消这次按键修改
CAPTURE_TIMEOUT = 10.0             # 与 rd.KeyCapture(timeout=...) 对齐
POLL_MS = 80                       # 规格要求：after(80, poll)

MODE_ARMED = "armed"               # 省内存：按 ← 才开始攒帧
MODE_BUFFER = "buffer"             # 常驻缓冲：一直保留最近 N 秒

MODE_LABELS = (
    (MODE_ARMED,
     "省内存（推荐）：按 ← 才开始攒帧，裁完立即释放滚动缓冲，内存里只留最近这一段"),
    (MODE_BUFFER,
     "常驻缓冲：一直保留最近 N 秒，可以回溯按 ← 之前的画面（更吃内存）"),
)

# 每个动作「恢复默认」时回落到哪 —— 取自引擎的 DEFAULTS["keys"]（这里的值必须和它一致）
KEY_FALLBACK = {
    "mark_in": "left",
    "mark_out": "right",
    "save": "numpad6",
    "play": "numpad_enter",
}

KEY_HINT = "小键盘按键需要 NumLock 灯亮着；没有小键盘的键盘可以把播放改成 F8 之类。"

# 手机页面上有、GUI 也要有的那几项（2026-10-07 补齐）
PRESET_LABELS = (
    ("fast", "灵敏（2.0s / 6 分 / 30 分）"),
    ("normal", "标准（3.0s / 10 分 / 38 分）"),
    ("calm", "保守（4.5s / 18 分 / 50 分）"),
)
# (配置里的值, 下拉框里显示的文字)
POSTPROCESS_LABELS = (
    ("off", "不处理（默认）"),
    ("remux", "转 mp4（-c copy，零损失、秒级）"),
    ("reencode", "重编码压小（libx264 CRF26，需要 ffmpeg）"),
)
# CS 控制台指令的触发时机
CONSOLE_TRIGGER_LABELS = (
    ("start", "引擎启动后，等 CS2 到前台就发一次（默认）"),
    ("round1", "每场第 1 个冻结时间发一次"),
    ("manual", "只手动（点下面的按钮 / HTTP）"),
)
# 键盘监听方式（排查"一开这软件整机就卡"时切到轮询做对照）
KEY_MODE_LABELS = (
    ("hook", "全局钩子（默认，精确：能分主/小键盘 Enter）"),
    ("poll", "轮询（不经过输入管线，物理上不会拖住键盘）"),
)
# 回放后端（内存 vs 兼容性）
BACKEND_LABELS = (
    ("plugin", "插件 obs-replay-source（默认，GB 级内存）"),
    ("obs", "OBS 自带 Replay Buffer（几十~几百 MB，需 ffmpeg）"),
)

# 第一次用（旁边还没有 config.json）时顶在按键区上方的那句提醒
FIRST_RUN_HINT = (
    "👋 第一次用：先点右边的「修改」，然后按一下你想用的那个键。"
    "笔记本 / 60% 键盘没有小键盘的话，建议把「播放 / 收起回放」改成 F8、"
    "「保留片段」改成 F9 —— 这样不受 NumLock 影响。"
)


# ===========================================================================
#  子进程命令行（规格 §4）
# ===========================================================================
def engine_cmd(cfg_path, extra):
    """引擎子进程命令行。打包成 exe 后只有 exe 自己，靠 --engine 回到引擎。"""
    extra = list(extra or [])
    if getattr(sys, "frozen", False):
        return [sys.executable, "--engine", "--config", cfg_path] + extra
    return [sys.executable, os.path.join(rd.base_dir(), "replay_director.py"),
            "--engine", "--config", cfg_path] + extra


def wizard_cmd(extra=None):
    """首次设置向导的命令行。

    向导自己会去找旁边的 config.json，**不认 --config**，所以不能直接套
    engine_cmd（那条命令里带了 --config，argparse 会报错）。
    """
    extra = list(extra or [])
    if getattr(sys, "frozen", False):
        return [sys.executable, "--setup"] + extra
    return [sys.executable, os.path.join(rd.base_dir(), "replay_director.py"),
            "--setup"] + extra


# ===========================================================================
#  主窗口
# ===========================================================================
class _TabStripAdapter:
    """让"一排大按钮"用起来像 ttk.Notebook（老代码里 nb.tab/select/index 照旧能用）。"""

    def __init__(self, gui):
        self.gui = gui

    def index(self, what):
        if str(what) == "end":
            return len(self.gui._tab_keys)
        return int(what)

    def tab(self, i, opt=None):
        key = self.gui._tab_keys[int(i)]
        return self.gui._tab_titles[key] if opt in (None, "text") else ""

    def select(self, i):
        self.gui.show_tab(self.gui._tab_keys[int(i)])


class DirectorGui:
    """整个设置窗口。构造期间不碰子进程、不装钩子、不弹对话框。"""

    def __init__(self, master, cfg_path=None):
        self.master = master
        self.proc = None            # 引擎子进程
        self.report_proc = None     # 「内存体检」子进程
        self.q = queue.Queue()      # 子进程输出 → 界面
        self._capture_win = None    # 正在等待按键的小窗
        self._closing = False

        # ---- 读配置（引擎负责合并默认值，并告诉实际用的是哪个文件）----
        self.cfg, self.cfg_path = rd.load_config(cfg_path)
        if not self.cfg_path:
            self.cfg_path = os.path.join(rd.base_dir(), "config.json")
        # 第一次用（旁边还没有 config.json）→ 界面上给一条"先去改按键"的提示
        self.first_run = not os.path.exists(self.cfg_path)
        self.cfg = dict(self.cfg)

        # ---- 手机口令 + 本机局域网地址 ----
        self.token = rd.load_or_make_token(base=rd.base_dir())
        ips = rd.lan_ips()
        self.lan_ip = ips[0] if ips else "127.0.0.1"

        # ---- 按键：先取默认，再让配置覆盖，最后交给引擎规范化 ----
        self.keys = dict(rd.DEFAULTS.get("keys") or {})
        self.keys.update(dict(self.cfg.get("keys") or {}))
        probe = dict(self.cfg)
        probe["keys"] = self.keys
        self.keys, key_problems = rd.normalize_keys(probe)
        self.keys = dict(self.keys)

        master.title(WINDOW_TITLE)
        master.minsize(760, 560)

        self._build_vars()
        self._build_widgets()
        self._refresh_key_labels()
        self._refresh_phone_url()
        self._update_estimate()
        self._tick_status()
        self._refresh_console_hint()
        self._fit_window()

        for p in key_problems:
            self.append_log("⚠ 按键设置：" + p)

        self.append_log("配置文件：" + self.cfg_path)
        self.append_log("手机地址：" + self.phone_url())
        # 选项分散在标签页里，第一次用的人容易"找不到" —— 这里直接指个路
        self.append_log("提示：选项分在 4 个标签页 —— 「回放与按键」/「副驾 / 存盘」/"
                        "「CS 控制台指令」/「场景」")
        if not (self.cfg.get("cs_console_commands") or []):
            self.append_log("      · 想让它开播前自动敲控制台指令 → 「CS 控制台指令」标签页，"
                            "一行一条填进去")

        master.protocol("WM_DELETE_WINDOW", self.on_close)
        self.master.after(200, self._drain_queue)

    def _fit_window(self):
        """按内容 + 屏幕大小定窗口尺寸。

        2026-10-07 踩到的坑：选项越加越多，写死 880x780 时最后一块
        （CS 控制台指令）直接被排到可视区外面，用户"找不到"。
        现在：分页 + 按内容/屏幕自适应，标签栏和日志区都能看到。
        """
        try:
            self.master.update_idletasks()
            need_w = max(900, self.master.winfo_reqwidth())
            need_h = max(660, self.master.winfo_reqheight())
            sw = self.master.winfo_screenwidth()
            sh = self.master.winfo_screenheight()
            w = min(need_w + 20, max(720, sw - 80))
            h = min(need_h + 10, max(560, sh - 100))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.master.geometry(f"{int(w)}x{int(h)}+{x}+{y}")
        except Exception:
            self.master.geometry("900x860")

    # ------------------------------------------------------------------
    #  变量
    # ------------------------------------------------------------------
    def _build_vars(self):
        cfg = self.cfg
        d = rd.DEFAULTS
        self.var_obs_url = tk.StringVar(value=str(cfg.get("obs_url", d.get("obs_url", ""))))
        self.var_obs_password = tk.StringVar(value=str(cfg.get("obs_password", "")))
        self.var_gsi_port = tk.StringVar(value=str(cfg.get("gsi_port", d.get("gsi_port", 23416))))
        self.var_web_port = tk.StringVar(value=str(cfg.get("web_port", d.get("web_port", 23417))))

        self.var_replay_enabled = tk.BooleanVar(value=bool(cfg.get("replay_enabled", False)))
        mode = str(cfg.get("replay_mode", MODE_ARMED))
        self.var_mode = tk.StringVar(value=mode if mode in (MODE_ARMED, MODE_BUFFER) else MODE_ARMED)
        self.var_seconds = tk.StringVar(value=self._fmt_num(cfg.get("record_max_seconds", 10.0)))
        self.var_speed = tk.StringVar(value=self._fmt_num(cfg.get("speed", 0.7)))
        self.var_estimate = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="未启动")
        self.var_phone = tk.StringVar(value="")   # 内容由 _refresh_phone_url() 填

        self.var_live_scene = tk.StringVar(value=str(cfg.get("live_scene", d.get("live_scene", ""))))
        self.var_replay_scene = tk.StringVar(value=str(cfg.get("replay_scene", d.get("replay_scene", ""))))
        self.var_replay_item = tk.StringVar(value=str(cfg.get("replay_item", d.get("replay_item", ""))))

        # 手机页面上那些开关（自动切换 / 灵敏度 / 胜率加成 / 显示名字）+ 存盘后处理
        self.var_auto_switch = tk.BooleanVar(value=bool(cfg.get("auto_switch", False)))
        preset = str(cfg.get("auto_preset", "normal"))
        self.var_auto_preset = tk.StringVar(
            value=preset if preset in [p[0] for p in PRESET_LABELS] else "normal")
        self.var_win_bonus = tk.BooleanVar(value=bool(cfg.get("auto_win_bonus", True)))
        self.var_show_names = tk.BooleanVar(value=bool(cfg.get("show_names", False)))
        pp = str(cfg.get("save_postprocess", "off")).lower()
        self.var_postprocess = tk.StringVar(
            value=dict(POSTPROCESS_LABELS).get(pp, POSTPROCESS_LABELS[0][1]))

        # CS 控制台指令（每次开播前自动输入）
        trig = str(cfg.get("cs_console_trigger", "start")).lower()
        self.var_console_trigger = tk.StringVar(
            value=dict(CONSOLE_TRIGGER_LABELS).get(trig, CONSOLE_TRIGGER_LABELS[0][1]))
        self.var_console_key = tk.StringVar(value=str(cfg.get("cs_console_key", "`")))
        self.var_console_delay = tk.StringVar(
            value=str(int(cfg.get("cs_console_delay_ms", 400) or 0)))
        self.var_console_gap = tk.StringVar(
            value=str(int(cfg.get("cs_console_gap_ms", 120) or 0)))
        self.var_console_close = tk.BooleanVar(value=bool(cfg.get("cs_console_close", True)))
        self.var_console_focus = tk.BooleanVar(
            value=bool(cfg.get("cs_console_require_focus", True)))
        # 键盘监听方式（hook / poll）
        km = str(cfg.get("key_mode", "hook")).lower()
        self.var_key_mode = tk.StringVar(
            value=dict(KEY_MODE_LABELS).get(km, KEY_MODE_LABELS[0][1]))
        # 回放后端（plugin / obs）+ obs 后端的媒体源名
        bk = str(cfg.get("replay_backend", "plugin")).lower()
        self.var_backend = tk.StringVar(
            value=dict(BACKEND_LABELS).get(bk, BACKEND_LABELS[0][1]))
        self.var_media_item = tk.StringVar(
            value=str(cfg.get("replay_media_item", "回放媒体源")))
        self.var_backend.trace_add("write", lambda *_: self._update_estimate())

        # 秒数/模式一变就重算内存；端口一变就重算手机地址
        self.var_seconds.trace_add("write", lambda *_: self._update_estimate())
        self.var_mode.trace_add("write", lambda *_: self._update_estimate())
        self.var_web_port.trace_add("write", lambda *_: self._refresh_phone_url())

    # ------------------------------------------------------------------
    #  控件
    # ------------------------------------------------------------------
    def _build_widgets(self):
        root = self.master
        root.columnconfigure(0, weight=1)
        # ★ 2026-10-07（用户第二次反馈）：默认 ttk 标签页又小又灰，"不仔细看根本看不见"。
        #   所以不用 Notebook 了 —— 换成**一排大按钮**当标签页：选中蓝底白字、
        #   没填内容的「CS 控制台指令」还会变成橙色提醒。
        root.rowconfigure(2, weight=1)      # 内容区吸收多余高度

        self._build_top(root)               # row=0：OBS 与手机（常驻）

        strip = tk.Frame(root, background="#eef3f8", highlightthickness=1,
                         highlightbackground="#c8d6e5")
        strip.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 0))
        self._tab_keys = ["replay", "companion", "console", "scenes"]
        self._tab_titles = {
            "replay": "① 回放与按键",
            "companion": "② 副驾 / 存盘",
            "console": "③ CS 控制台指令",
            "scenes": "④ 场景",
        }
        self._tab_buttons = {}
        for k in self._tab_keys:
            b = tk.Button(strip, text=self._tab_titles[k],
                          command=lambda kk=k: self.show_tab(kk),
                          font=("Microsoft YaHei UI", 11, "bold"),
                          padx=16, pady=8, relief="raised", bd=2, cursor="hand2")
            b.pack(side="left", padx=(8, 0), pady=6)
            self._tab_buttons[k] = b

        self._content = tk.Frame(root)
        self._content.grid(row=2, column=0, sticky="nsew", padx=10, pady=(6, 4))
        self._content.columnconfigure(0, weight=1)
        self._content.rowconfigure(0, weight=1)
        self._tab_frames = {}
        for k in self._tab_keys:
            fr = ttk.Frame(self._content)
            fr.grid(row=0, column=0, sticky="nsew")
            fr.columnconfigure(0, weight=1)
            self._tab_frames[k] = fr

        self._build_replay(self._tab_frames["replay"])
        self._build_keys(self._tab_frames["replay"])
        self._build_companion(self._tab_frames["companion"])
        self._build_console(self._tab_frames["console"])
        self._build_scenes(self._tab_frames["scenes"])
        self._build_bottom(root)            # row=3：引擎按钮 + 日志（常驻）

        # 兼容按"标签页"写的老调用（自测脚本 / goto_console_tab 用 nb.tab/select/index）
        self.nb = _TabStripAdapter(self)
        self.show_tab("replay")

    def show_tab(self, key):
        """切到某个标签页（key 见 self._tab_keys）。"""
        if key not in getattr(self, "_tab_frames", {}):
            return
        self._tab_frames[key].tkraise()
        self._active_tab = key
        self._paint_tab_buttons()

    def _paint_tab_buttons(self):
        """按钮配色：选中=蓝底白字；「CS 控制台指令」没填内容时=橙色提醒。"""
        if not getattr(self, "_tab_buttons", None):
            return
        n_cmds = 0
        if hasattr(self, "txt_cmds"):
            try:
                n_cmds = len(self.console_commands())
            except Exception:
                n_cmds = 0
        for k, b in self._tab_buttons.items():
            if k == getattr(self, "_active_tab", None):
                b.configure(bg="#1f6feb", fg="#ffffff", relief="sunken", activebackground="#1f6feb",
                            activeforeground="#ffffff")
            elif k == "console" and not n_cmds:
                # 还没填 → 醒目的橙色，直到你填了为止
                b.configure(bg="#ffd9a0", fg="#7a3d00", relief="raised",
                            activebackground="#ffc46b", activeforeground="#5c2d00")
            else:
                b.configure(bg="#dde7f0", fg="#20303f", relief="raised",
                            activebackground="#c9dcee", activeforeground="#20303f")

    def current_tab(self):
        return getattr(self, "_active_tab", "replay")

    # ---- 3.1 顶部状态区 ----
    def _build_top(self, root):
        f = ttk.LabelFrame(root, text="OBS 与手机")
        f.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        f.columnconfigure(1, weight=1)
        f.columnconfigure(4, weight=1)

        ttk.Label(f, text="OBS 地址（obs-websocket）").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(f, textvariable=self.var_obs_url).grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Label(f, text="密码").grid(row=0, column=4, sticky="e", padx=(12, 4), pady=4)
        ttk.Entry(f, textvariable=self.var_obs_password, show="*", width=18).grid(
            row=0, column=5, sticky="ew", padx=(0, 8), pady=4)

        ttk.Label(f, text="游戏数据端口（GSI）").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(f, from_=1, to=65535, textvariable=self.var_gsi_port, width=8).grid(
            row=1, column=1, sticky="w", pady=4)
        ttk.Label(f, text="手机页面端口").grid(row=1, column=2, sticky="e", padx=(12, 4), pady=4)
        ttk.Spinbox(f, from_=1, to=65535, textvariable=self.var_web_port, width=8).grid(
            row=1, column=3, sticky="w", pady=4)

        ttk.Label(f, text="手机地址").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        self.ent_phone = ttk.Entry(f, textvariable=self.var_phone, state="readonly")
        self.ent_phone.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Button(f, text="复制", width=6, command=self.copy_phone_url).grid(
            row=2, column=4, sticky="w", padx=(12, 4), pady=4)
        ttk.Button(f, text="打开", width=6, command=self.open_phone_url).grid(
            row=2, column=5, sticky="w", padx=(0, 8), pady=4)

        ttk.Label(f, text="引擎状态：").grid(row=3, column=0, sticky="w", padx=8, pady=(2, 8))
        ttk.Label(f, textvariable=self.var_status, font=("Microsoft YaHei UI", 9, "bold")).grid(
            row=3, column=1, columnspan=5, sticky="w", pady=(2, 8))

    # ---- 3.2 回放功能 ----
    def _build_replay(self, root):
        f = ttk.LabelFrame(root, text="回放功能（核心）")
        f.grid(row=1, column=0, sticky="ew", padx=10, pady=6)
        f.columnconfigure(1, weight=1)

        ttk.Checkbutton(f, text="启用回放功能（← / → / 小键盘播放）",
                        variable=self.var_replay_enabled).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 4))

        ttk.Label(f, text="内存模式").grid(row=1, column=0, sticky="nw", padx=8, pady=(2, 0))
        for i, (val, text) in enumerate(MODE_LABELS):
            # 说明文字很长，用经典 tk.Radiobutton（ttk 的那个不支持 -wraplength）
            tk.Radiobutton(f, text=text, value=val, variable=self.var_mode,
                           wraplength=660, justify="left", anchor="w",
                           highlightthickness=0).grid(
                row=1 + i, column=1, columnspan=3, sticky="w", padx=4, pady=(2, 0))

        row = 1 + len(MODE_LABELS)
        ttk.Label(f, text="单段素材上限（秒）").grid(row=row, column=0, sticky="w", padx=8, pady=(8, 4))
        ttk.Spinbox(f, from_=1, to=20, increment=0.5, textvariable=self.var_seconds,
                    width=8).grid(row=row, column=1, sticky="w", pady=(8, 4))
        ttk.Label(f, text="回放速度（倍）").grid(row=row, column=2, sticky="e", padx=(16, 4), pady=(8, 4))
        ttk.Spinbox(f, from_=0.2, to=2.0, increment=0.1, textvariable=self.var_speed,
                    width=8).grid(row=row, column=3, sticky="w", padx=(0, 8), pady=(8, 4))

        row += 1
        ttk.Label(f, text="回放后端").grid(row=row, column=0, sticky="w", padx=8, pady=(8, 2))
        ttk.Combobox(f, textvariable=self.var_backend, state="readonly", width=32,
                     values=[v for _, v in BACKEND_LABELS]).grid(
            row=row, column=1, columnspan=3, sticky="w", pady=(8, 2))
        row += 1
        ttk.Label(f, text="obs 后端：媒体源名").grid(row=row, column=0, sticky="w",
                                             padx=8, pady=2)
        ttk.Entry(f, textvariable=self.var_media_item, width=22).grid(
            row=row, column=1, sticky="w", pady=2)
        ttk.Label(f, text="（回放场景里那个媒体源；缺了引擎会自动建）",
                  font=SMALL_FONT, foreground="#666666").grid(
            row=row, column=2, columnspan=2, sticky="w", padx=(4, 8), pady=2)
        row += 1
        ttk.Label(f, text="插件后端＝内存放未压缩帧（1080p60 十秒两份 ≈ 9.95 GB，能瞬间定格/任意入点）；"
                          "obs 后端＝OBS 自带 Replay Buffer（编码后 ≈ 几十~几百 MB，"
                          "需 ffmpeg 且 OBS 里已启用回放缓冲）",
                  font=SMALL_FONT, foreground="#666666", wraplength=820,
                  justify="left").grid(row=row, column=0, columnspan=4, sticky="w",
                                       padx=8, pady=(0, 6))

        self.lbl_estimate = ttk.Label(f, textvariable=self.var_estimate,
                                      wraplength=800, justify="left")
        self.lbl_estimate.grid(
            row=row + 1, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 0))
        self._estimate_labels = [self.lbl_estimate]
        ttk.Label(f, text="省内存模式：空闲时不占内存；常驻缓冲：上面这个数字会一直在",
                  font=SMALL_FONT, foreground="#666666").grid(
            row=row + 2, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 8))

        # ★ 2026-10-07：控制台指令藏在标签页里，用户反馈"找不到" —— 这里放一个
        #   一眼能看到的入口（状态 + 跳转按钮），点了直接切到那一页并把光标放进输入框。
        row += 3
        box = ttk.Frame(f)
        box.grid(row=row, column=0, columnspan=4, sticky="ew", padx=8, pady=(0, 10))
        self.lbl_console_hint = ttk.Label(box, text="", font=("Microsoft YaHei UI", 9, "bold"),
                                          foreground="#0b5fa5", wraplength=560, justify="left")
        self.lbl_console_hint.pack(side="left")
        ttk.Button(box, text="去填控制台指令 →",
                   command=self.goto_console_tab).pack(side="right")

    # ---- 3.3 按键设置 ----
    def _build_keys(self, root):
        f = ttk.LabelFrame(root, text="按键设置")
        f.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        f.columnconfigure(1, weight=1)

        self.key_labels = {}
        for i, (action, label) in enumerate(rd.KEY_ACTIONS.items()):
            ttk.Label(f, text=label, width=14).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            lb = ttk.Label(f, text="", font=("Microsoft YaHei UI", 9, "bold"))
            lb.grid(row=i, column=1, sticky="w", padx=(4, 8), pady=3)
            self.key_labels[action] = lb
            ttk.Button(f, text="修改", width=6,
                       command=lambda a=action: self.begin_capture(a)).grid(
                row=i, column=2, sticky="e", padx=(8, 4), pady=3)
            ttk.Button(f, text="恢复默认", width=9,
                       command=lambda a=action: self.reset_key(a)).grid(
                row=i, column=3, sticky="e", padx=(0, 8), pady=3)

        row0 = len(rd.KEY_ACTIONS)
        if getattr(self, "first_run", False):
            ttk.Label(f, text=FIRST_RUN_HINT, font=("Microsoft YaHei UI", 9, "bold"),
                      foreground="#b35900", wraplength=820, justify="left").grid(
                row=row0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 0))
            row0 += 1

        # ★ 2026-10-08：按键监听方式。用户报"开这软件后所有程序都像死机"，
        #   而低级键盘钩子一旦被卡住就会拖住全系统输入 —— 这里给一个一键切换的
        #   "不碰输入管线"方案，方便现场做对照实验。
        ttk.Label(f, text="键盘监听方式", width=14).grid(
            row=row0, column=0, sticky="w", padx=8, pady=(6, 2))
        ttk.Combobox(f, textvariable=self.var_key_mode, state="readonly", width=30,
                     values=[v for _, v in KEY_MODE_LABELS]).grid(
            row=row0, column=1, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Label(f, text="钩子＝精确（能分主/小键盘 Enter）；轮询＝不经过输入管线，"
                          "物理上不会拖住键盘（排查死机时先切这个做对照）",
                  font=SMALL_FONT, foreground="#666666", wraplength=820,
                  justify="left").grid(row=row0 + 1, column=0, columnspan=4, sticky="w",
                                       padx=8, pady=(0, 4))
        row0 += 2

        ttk.Label(f, text=KEY_HINT, font=SMALL_FONT, foreground="#666666",
                  wraplength=800, justify="left").grid(
            row=row0, column=0, columnspan=4, sticky="w",
            padx=8, pady=(6, 8))

    # ---- 3.4 场景 ----
    def _build_scenes(self, root):
        f = ttk.LabelFrame(root, text="场景")
        f.grid(row=5, column=0, sticky="new", padx=10, pady=6)
        f.columnconfigure(1, weight=1)

        pairs = (("直播场景", self.var_live_scene),
                 ("回放场景", self.var_replay_scene),
                 ("回放源名称", self.var_replay_item))
        for i, (text, var) in enumerate(pairs):
            ttk.Label(f, text=text, width=14).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            ttk.Entry(f, textvariable=var).grid(row=i, column=1, sticky="ew", padx=(4, 8), pady=3)

    # ---- 3.4b 副驾提示器 / 自动切换 / 存盘（= 手机页面上那些开关，2026-10-07 补齐）----
    def _build_companion(self, root):
        f = ttk.LabelFrame(root, text="副驾提示器 / 自动切换（手机页面上的开关，这里也能设）")
        f.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        f.columnconfigure(1, weight=1)

        ttk.Checkbutton(f, text="自动切换观察位（开＝引擎替你按数字键 1~0）",
                        variable=self.var_auto_switch).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))
        ttk.Label(f, text="  安全阀：只有 CS2 在前台、且不在回放中才发键；连续 3 次没切动会自己关掉",
                  font=SMALL_FONT, foreground="#666666").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 4))

        ttk.Label(f, text="灵敏度").grid(row=2, column=0, sticky="w", padx=8, pady=(4, 2))
        row2 = ttk.Frame(f)
        row2.grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 2))
        for val, text in PRESET_LABELS:
            ttk.Radiobutton(row2, text=text, value=val,
                            variable=self.var_auto_preset).pack(side="left", padx=(0, 10))

        ttk.Checkbutton(f, text="对枪胜率加成（镜头偏向「预测能活下来的一方」）",
                        variable=self.var_win_bonus).grid(
            row=3, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f, text="提示器显示玩家名字（关＝只看按键，比赛时更快）",
                        variable=self.var_show_names).grid(
            row=4, column=0, columnspan=4, sticky="w", padx=8, pady=2)

        ttk.Label(f, text="存盘后处理").grid(row=5, column=0, sticky="w", padx=8, pady=(8, 2))
        cb = ttk.Combobox(f, textvariable=self.var_postprocess, state="readonly",
                          width=34, values=[p[1] for p in POSTPROCESS_LABELS])
        cb.grid(row=5, column=1, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Label(f, text="只影响小键盘 6 存出来的文件：remux＝换 mp4（零损失、秒级）；"
                          "reencode＝重编码压小（需要 ffmpeg）",
                  font=SMALL_FONT, foreground="#666666", wraplength=800,
                  justify="left").grid(row=6, column=0, columnspan=4, sticky="w",
                                       padx=8, pady=(0, 6))
        ttk.Label(f, text="说明：这些值随「保存配置 / 启动引擎」写进 config.json；"
                          "比赛现场想立刻改，用手机页面上的开关（实时生效）。\n"
                          "手机页面右下角还有个「震动提醒」—— 那是每台手机自己的本地开关"
                          "（不经过引擎），所以这里没有。",
                  font=SMALL_FONT, foreground="#666666", wraplength=800,
                  justify="left").grid(row=7, column=0, columnspan=4, sticky="w",
                                       padx=8, pady=(0, 8))

    # ---- 3.4c CS 控制台指令（每次开播前自动输入，2026-10-07 加）----
    def _build_console(self, root):
        f = ttk.LabelFrame(root, text="CS 控制台指令（每次开播前自动输入，一行一条，可多条）")
        f.grid(row=4, column=0, sticky="ew", padx=10, pady=6)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        ttk.Label(f, text="要输入的指令（一行一条）",
                  font=("Microsoft YaHei UI", 9, "bold")).grid(
            row=0, column=0, sticky="nw", padx=8, pady=(8, 2))
        self.txt_cmds = tk.Text(f, height=6, wrap="none", font=("Consolas", 10),
                                background="#FFFFFF", relief="sunken", borderwidth=2,
                                insertbackground="#000000")
        self.txt_cmds.grid(row=0, column=1, columnspan=3, sticky="nsew", padx=(4, 8), pady=(8, 2))
        sb = ttk.Scrollbar(f, orient="vertical", command=self.txt_cmds.yview)
        sb.grid(row=0, column=4, sticky="ns", pady=(6, 2))
        self.txt_cmds.configure(yscrollcommand=sb.set)
        # 预填当前配置
        cmds = self.cfg.get("cs_console_commands") or []
        if isinstance(cmds, str):
            cmds = cmds.splitlines()
        self.txt_cmds.insert("1.0", "\n".join(str(x) for x in cmds))
        # 输入框里给个浅色示例，第一次打字就自动消失（不会存进配置）
        self._placeholder_on = not cmds
        if self._placeholder_on:
            self.txt_cmds.insert("1.0", "sv_cheats 1\nmp_freezetime 5\ncl_draw_only_deathnotices 1")
            self.txt_cmds.configure(foreground="#9aa7b4")
            self.txt_cmds.bind("<Key>", self._clear_placeholder, add="+")
            self.txt_cmds.bind("<Button-1>", self._clear_placeholder, add="+")

        ttk.Label(f, text="  一行一条，会依次敲进控制台并各自回车；空行和 // 开头的行忽略",
                  font=SMALL_FONT, foreground="#666666").grid(
            row=1, column=0, columnspan=5, sticky="w", padx=8)

        row = 2
        ttk.Label(f, text="触发时机").grid(row=row, column=0, sticky="w", padx=8, pady=(6, 2))
        ttk.Combobox(f, textvariable=self.var_console_trigger, state="readonly", width=30,
                     values=[v for _, v in CONSOLE_TRIGGER_LABELS]).grid(
            row=row, column=1, columnspan=3, sticky="w", pady=(6, 2))

        row += 1
        ttk.Label(f, text="控制台键").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        ttk.Entry(f, textvariable=self.var_console_key, width=8).grid(
            row=row, column=1, sticky="w", pady=2)
        ttk.Label(f, text="（默认 ` ；也能写 f10 / backtick / numpad_enter）",
                  font=SMALL_FONT, foreground="#666666").grid(
            row=row, column=2, columnspan=2, sticky="w", padx=(4, 8), pady=2)

        row += 1
        ttk.Label(f, text="打开后等待(ms)").grid(row=row, column=0, sticky="w", padx=8, pady=2)
        ttk.Spinbox(f, from_=0, to=5000, increment=50, textvariable=self.var_console_delay,
                    width=8).grid(row=row, column=1, sticky="w", pady=2)
        ttk.Label(f, text="两条之间(ms)").grid(row=row, column=2, sticky="e", padx=(16, 4), pady=2)
        ttk.Spinbox(f, from_=0, to=5000, increment=20, textvariable=self.var_console_gap,
                    width=8).grid(row=row, column=3, sticky="w", padx=(0, 8), pady=2)

        row += 1
        ttk.Checkbutton(f, text="发完自动关掉控制台", variable=self.var_console_close).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f, text="必须 CS2 在前台才发（安全阀，建议保持开）",
                        variable=self.var_console_focus).grid(
            row=row, column=2, columnspan=3, sticky="w", padx=8, pady=2)

        row += 1
        bar = ttk.Frame(f)
        bar.grid(row=row, column=0, columnspan=5, sticky="ew", padx=8, pady=(4, 8))
        ttk.Button(bar, text="现在发送一次", command=self.send_console_now).pack(side="left")
        ttk.Label(bar, text="（引擎在跑才会真的发出去；发送时会先按一下控制台键）",
                  font=SMALL_FONT, foreground="#666666").pack(side="left", padx=(8, 0))

    def _clear_placeholder(self, event=None):
        """第一次打字/点击时清掉示例文字（示例不会进配置）。"""
        if getattr(self, "_placeholder_on", False):
            self._placeholder_on = False
            self.txt_cmds.delete("1.0", "end")
            self.txt_cmds.configure(foreground="#000000")

    def console_commands(self):
        """输入框里真正要发的指令（空行、// 和 ; 开头的注释行忽略；示例文字不算）。"""
        if getattr(self, "_placeholder_on", False):
            return []
        raw = self.txt_cmds.get("1.0", "end").replace("\r\n", "\n").split("\n")
        return [l.strip() for l in raw
                if l.strip() and not l.strip().startswith(("//", ";"))]

    def goto_console_tab(self):
        """跳到「CS 控制台指令」页并把光标放进输入框。"""
        self.show_tab("console")
        self._clear_placeholder()
        self.txt_cmds.focus_set()

    def _refresh_console_hint(self):
        n = len(self.console_commands())
        if hasattr(self, "lbl_console_hint"):
            self.lbl_console_hint.configure(
                text=(f"开播前自动敲的控制台指令：已填 {n} 条"
                      f"（在第 ③ 页「CS 控制台指令」里）") if n else
                     "开播前自动敲的控制台指令：还没填 →")
        self._paint_tab_buttons()      # 没填时把第 ③ 页按钮刷成橙色

    def send_console_now(self):
        """手动发一次：引擎在跑就走它的 HTTP 端点，否则提示先启动引擎。"""
        cmds = self.console_commands()
        if not cmds:
            messagebox.showinfo("没有指令", "上面一条指令都没填。")
            return
        if self.proc is None or self.proc.poll() is not None:
            self.append_log("引擎没在跑 —— 控制台指令要先「启动引擎」（或点「保存配置」后用手机/HTTP）。")
            messagebox.showinfo("先启动引擎", "引擎没在运行，没人替你发按键。\n"
                                             "先点「保存配置」再「启动引擎」。")
            return
        url = "http://127.0.0.1:%s/control/console" % self._gsi_port()
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as r:
                body = r.read().decode("utf-8", "replace")
            self.append_log("手动发控制台指令：%s → %s" % (url, body))
        except Exception as e:
            self.append_log("⚠ 手动发控制台指令失败：%s（看引擎日志里的原因）" % e)

    def _gsi_port(self):
        try:
            return int(self.var_gsi_port.get())
        except Exception:
            return 23416

    # ---- 3.5 底部按钮 + 日志 ----
    def _build_bottom(self, root):
        f = ttk.LabelFrame(root, text="引擎与日志")
        f.grid(row=3, column=0, sticky="nsew", padx=10, pady=(6, 10))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))
        ttk.Button(bar, text="保存配置", command=self.save_config_clicked).pack(side="left")
        self.btn_start = ttk.Button(bar, text="启动引擎", command=self.start_engine)
        self.btn_start.pack(side="left", padx=(6, 0))
        self.btn_stop = ttk.Button(bar, text="停止引擎", command=self.stop_engine, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="首次设置向导", command=self.open_wizard).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="内存体检", command=self.memory_report).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="清空", command=self.clear_log).pack(side="right")

        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(wrap, height=7, wrap="word", font=LOG_FONT,
                               background="#111111", foreground="#DDDDDD",
                               insertbackground="#DDDDDD")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        self.txt_log.configure(state="disabled")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)

    # ------------------------------------------------------------------
    #  日志区
    # ------------------------------------------------------------------
    def append_log(self, msg, stamp=True):
        """往只读日志区加一行（引擎自己的输出已经带时间戳了，stamp=False）。"""
        line = ("[%s] %s" % (time.strftime("%H:%M:%S"), msg)) if stamp else str(msg)
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------------
    #  数值小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_num(v):
        """10.0 → "10"，10.5 → "10.5"（输入框里不要一串没意义的 0）。"""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return str(int(f)) if abs(f - round(f)) < 1e-9 else str(f)

    def _read_num(self, var, default, what, quiet=False):
        raw = str(var.get()).strip().replace("，", ".")
        try:
            return float(raw)
        except ValueError:
            if not quiet:      # 边打字边回调的路径不刷屏
                self.append_log("⚠「%s」填的不是数字（%r），这次按 %s 处理"
                                % (what, raw, default))
            return float(default)

    # ------------------------------------------------------------------
    #  手机地址
    # ------------------------------------------------------------------
    def phone_url(self, quiet=False):
        port = int(self._read_num(self.var_web_port, 23417, "手机页面端口", quiet=quiet))
        return "http://%s:%d/?k=%s" % (self.lan_ip, port, self.token)

    def _refresh_phone_url(self, *_):
        self.var_phone.set(self.phone_url(quiet=True))

    def copy_phone_url(self):
        url = self.phone_url()
        self.master.clipboard_clear()
        self.master.clipboard_append(url)
        self.append_log("已复制手机地址：" + url)

    def open_phone_url(self):
        url = self.phone_url()
        self.append_log("用浏览器打开：" + url)
        webbrowser.open(url)

    # ------------------------------------------------------------------
    #  内存估算（规格 §3.2）
    # ------------------------------------------------------------------
    def _update_estimate(self, *_):
        """实时内存估算 + 内存护栏判断。

        2026-10-08：用户报"电脑总是死机"，取证发现 32 GB 机器 + **4K60 画布** + 10 秒
        → 插件要两份 ≈39.8 GB → 换页把整机拖死（OBS 自己卡成"停止与 Windows 交互"）。
        所以这里必须用 **OBS 配置里的真实画布**（basic.ini 的 BaseCX/BaseCY）来估，
        并直接告诉用户"这台机器装不装得下"，而不是按 1080p 估一个偏小 4 倍的数字。
        """
        copies = 1.0 if self.var_mode.get() == MODE_ARMED else 2.0
        # obs 后端放的是**编码后**的数据（几十~几百 MB），和插件那套 GB 级算法无关
        try:
            bk2val = {text: val for val, text in BACKEND_LABELS}
            is_obs = bk2val.get(self.var_backend.get(), "plugin") == "obs"
        except Exception:
            is_obs = False
        raw = str(self.var_seconds.get()).strip().replace("，", ".")
        try:
            seconds = float(raw)
        except ValueError:
            seconds = None
        if seconds is None or seconds <= 0:
            seconds = float(self.cfg.get("record_max_seconds", 10.0) or 10.0)
        ms = ["", "（你在输入框里还没打完，先按 10 秒算）"][0]

        try:
            canvas = rd.canvas_from_obs_config()
        except Exception:
            canvas = None
        w, h, fps = canvas or rd.DEFAULT_CANVAS
        src = "OBS 画布" if canvas else "默认 1080p（没读到 OBS 配置）"
        if is_obs:
            mbps = float(self.cfg.get("obs_buffer_mbps", 40.0) or 40.0)
            mb = mbps * seconds / 8.0 * 2.0
            text = (f"obs 后端（OBS 自带 Replay Buffer）：按 {mbps:.0f} Mbps 估，"
                    f"{seconds:.1f} 秒 ≈ {mb:.0f} MB"
                    f"（插件后端同条件是 {w * h * 4 * fps * seconds / 1e9:.2f} GB/份）")
            text += ("\n需要：OBS 里已启用「回放缓冲」（时长 ≥ "
                     f"{seconds:.0f} 秒）+ 本机有 ffmpeg（用来按 ←/→ 裁片段）")
            self.var_estimate.set(text)
            for wdg in getattr(self, "_estimate_labels", []):
                try:
                    wdg.configure(foreground="#1a6b3c")
                except Exception:
                    pass
            return
        est = rd.estimate_replay_memory(w, h, fps, seconds, copies=copies)
        text = f"{est['hint']}　[{src}]"
        color = "#222222"
        try:
            total, avail = rd.system_memory_gb()
            if total:
                safe, note = rd.plan_replay_seconds(w, h, fps, seconds, total, avail,
                                                    copies=2.0)
                high_res = w >= 2560 or h >= 1440
                peak = w * h * 4 * fps * seconds / 1e9 * 2
                if safe is None or (high_res and safe < 4.0):
                    text += (f"\n⛔ 内存护栏：本机内存 {total:.1f} GB，这一套设置装不下"
                             f"（峰值要两份 ≈ {peak:.1f} GB）")
                    text += ("\n　　 把 OBS 的【基础(画布)分辨率】降到 1920x1080 再开回放"
                             "（一键工具：python tools\\rescale_canvas.py --apply）"
                             if high_res else
                             "\n　　 把「单段素材上限」调小，或先不开回放功能")
                    color = "#b00020"
                elif safe != seconds:
                    text += (f"\n⚠️ 内存护栏：本机内存 {total:.1f} GB，打开回放时会自动把"
                             f"单段上限降到 {safe:.1f} 秒（{note}）")
                    color = "#8a4b00"
                else:
                    text += f"\n✅ 内存护栏：本机内存 {total:.1f} GB，这套设置安全"
                    color = "#1a6b3c"
        except Exception:
            pass
        self.var_estimate.set(text + ms)
        for wdg in getattr(self, "_estimate_labels", []):
            try:
                wdg.configure(foreground=color)
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  按键
    # ------------------------------------------------------------------
    def _refresh_key_labels(self):
        shown = rd.format_keys(self.keys)
        for action, lb in self.key_labels.items():
            lb.configure(text=str(shown.get(action) or "—"))

    def reset_key(self, action):
        default = (rd.DEFAULTS.get("keys") or {}).get(action)
        if not default and KEY_FALLBACK.get(action):
            default = [KEY_FALLBACK[action]]
        if not default:
            self.append_log("⚠ 引擎的 DEFAULTS 里没有「%s」的默认按键，跳过"
                            % rd.KEY_ACTIONS.get(action, action))
            return
        old = " / ".join(self.keys.get(action) or []) or "—"
        self.keys[action] = list(default)
        self._refresh_key_labels()
        self.append_log("「%s」按键恢复默认：%s → %s"
                        % (rd.KEY_ACTIONS.get(action, action), old, " / ".join(default)))

    def begin_capture(self, action):
        """弹小窗等按键。用 rd.KeyCapture 装一次性钩子，after(80) 轮询。"""
        if self._capture_win is not None:
            return
        win = tk.Toplevel(self.master)
        self._capture_win = win
        win.title("修改按键")
        win.transient(self.master)
        win.resizable(False, False)
        tk.Label(win, text="请按下你要用的键…（Esc 取消）",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(padx=26, pady=(20, 6))
        tk.Label(win, text="正在改：「%s」" % rd.KEY_ACTIONS.get(action, action),
                 font=SMALL_FONT, foreground="#666666").pack(padx=26, pady=(0, 18))

        cap = rd.KeyCapture(timeout=CAPTURE_TIMEOUT)
        if not cap.start():
            self._close_capture_win()
            messagebox.showerror(
                "按键捕获失败",
                "%s\n\n可以先用「首次设置向导」检查环境，或直接改 config.json 里的 keys。"
                % (cap.error or "装不上键盘钩子"))
            return

        state = {"done": False}
        started = time.monotonic()

        def finish():
            state["done"] = True
            try:
                cap.cancel()
            finally:
                self._close_capture_win()

        def poll():
            if state["done"]:
                return
            got = cap.poll()
            if got is not None:
                vk, ext = got
                if vk == ESC_VK:
                    finish()
                    self.append_log("已取消按键修改")
                    return
                name = rd.key_token_name(vk, ext)
                finish()
                self.apply_captured_key(action, name)
                return
            if time.monotonic() - started >= CAPTURE_TIMEOUT:
                finish()
                messagebox.showinfo("没听到按键",
                                    "%d 秒内没有捕获到按键，这次修改取消。"
                                    % int(CAPTURE_TIMEOUT))
                return
            self.master.after(POLL_MS, poll)

        def on_esc(_event=None):
            # 引擎的 poll() 把 Esc 和超时都归成 None，所以 Esc 由窗口自己接住
            # （钩子是只读的，键照样能到窗口），这样按 Esc 立刻就能取消。
            if not state["done"]:
                finish()
                self.append_log("已取消按键修改")

        win.protocol("WM_DELETE_WINDOW", on_esc)
        win.bind("<Escape>", on_esc)
        win.focus_force()
        self.master.after(POLL_MS, poll)

    def _close_capture_win(self):
        win, self._capture_win = self._capture_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def apply_captured_key(self, action, token):
        """捕获到的键写进待保存配置；和别人撞了就把这次修改回滚。

        引擎里一个动作可以绑**多个**键（`keys[动作]` 是列表），界面上的「修改」
        是整个换掉这一个动作的绑定 —— 想给一个动作配多个键就直接改 config.json。
        """
        for other, toks in self.keys.items():
            if other != action and token in list(toks or []):
                messagebox.showwarning(
                    "按键冲突",
                    "「%s」已经绑了 %s。\n这次修改已回滚，换个键再试。"
                    % (rd.KEY_ACTIONS.get(other, other), token))
                self.append_log("⚠「%s」想绑 %s，但「%s」已经占了，已回滚"
                                % (rd.KEY_ACTIONS.get(action, action), token,
                                   rd.KEY_ACTIONS.get(other, other)))
                return
        old = " / ".join(self.keys.get(action) or []) or "—"
        self.keys[action] = [token]
        self._refresh_key_labels()
        self.append_log("「%s」按键：%s → %s"
                        % (rd.KEY_ACTIONS.get(action, action), old, token))

    # ------------------------------------------------------------------
    #  配置读写
    # ------------------------------------------------------------------
    def gather_cfg(self):
        """把界面上的值收成一个完整 cfg（保留我们不认识的键）。"""
        cfg = dict(self.cfg)
        d = rd.DEFAULTS
        cfg["obs_url"] = self.var_obs_url.get().strip()
        cfg["obs_password"] = self.var_obs_password.get()
        cfg["gsi_port"] = int(self._read_num(self.var_gsi_port, d.get("gsi_port", 23416),
                                             "游戏数据端口（GSI）"))
        cfg["web_port"] = int(self._read_num(self.var_web_port, d.get("web_port", 23417),
                                             "手机页面端口"))
        cfg["replay_enabled"] = bool(self.var_replay_enabled.get())
        cfg["replay_mode"] = self.var_mode.get()
        cfg["record_max_seconds"] = self._read_num(self.var_seconds, 10.0, "单段素材上限（秒）")
        cfg["speed"] = self._read_num(self.var_speed, d.get("speed", 0.7), "回放速度（倍）")
        cfg["keys"] = dict(self.keys)
        cfg["live_scene"] = self.var_live_scene.get().strip()
        cfg["replay_scene"] = self.var_replay_scene.get().strip()
        cfg["replay_item"] = self.var_replay_item.get().strip()
        # 手机页面上那些开关（这里做了同样的字段，现场改完重启引擎即生效）
        cfg["auto_switch"] = bool(self.var_auto_switch.get())
        cfg["auto_preset"] = self.var_auto_preset.get()
        cfg["auto_win_bonus"] = bool(self.var_win_bonus.get())
        cfg["show_names"] = bool(self.var_show_names.get())
        # 下拉框显示的是中文，这里映射回配置里的值
        label2val = {text: val for val, text in POSTPROCESS_LABELS}
        cfg["save_postprocess"] = label2val.get(self.var_postprocess.get(), "off")
        # CS 控制台指令
        cfg["cs_console_commands"] = self.console_commands()
        trig2val = {text: val for val, text in CONSOLE_TRIGGER_LABELS}
        cfg["cs_console_trigger"] = trig2val.get(self.var_console_trigger.get(), "start")
        cfg["cs_console_key"] = self.var_console_key.get().strip() or "`"
        cfg["cs_console_delay_ms"] = int(self._read_num(
            self.var_console_delay, 400, "控制台打开后等待（毫秒）"))
        cfg["cs_console_gap_ms"] = int(self._read_num(
            self.var_console_gap, 120, "控制台两条指令之间（毫秒）"))
        cfg["cs_console_close"] = bool(self.var_console_close.get())
        cfg["cs_console_require_focus"] = bool(self.var_console_focus.get())
        # 键盘监听方式
        km2val = {text: val for val, text in KEY_MODE_LABELS}
        cfg["key_mode"] = km2val.get(self.var_key_mode.get(), "hook")
        # 回放后端 + obs 后端的媒体源名
        bk2val = {text: val for val, text in BACKEND_LABELS}
        cfg["replay_backend"] = bk2val.get(self.var_backend.get(), "plugin")
        cfg["replay_media_item"] = self.var_media_item.get().strip() or "回放媒体源"
        return cfg

    def write_config(self, notify=True):
        """规范按键 → 落盘。返回 True 表示真的写成功了。"""
        cfg = self.gather_cfg()
        keys, problems = rd.normalize_keys(cfg)
        cfg["keys"] = dict(keys)
        self.keys = dict(keys)
        self._refresh_key_labels()
        for p in problems:
            self.append_log("⚠ 按键设置：" + p)
        try:
            rd.save_config(cfg, self.cfg_path)
        except OSError as e:
            messagebox.showerror("保存失败", "写不进 %s：\n%s" % (self.cfg_path, e))
            return False
        self.cfg = cfg
        self.append_log("配置已保存：" + self.cfg_path)
        if notify:
            messagebox.showinfo("已保存", "配置已写入：\n%s" % self.cfg_path)
        return True

    def save_config_clicked(self):
        self.write_config(notify=True)

    # ------------------------------------------------------------------
    #  引擎子进程
    # ------------------------------------------------------------------
    def _tick_status(self):
        proc = self.proc
        if proc is None:
            self.var_status.set("未启动")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        elif proc.poll() is None:
            self.var_status.set("运行中 (PID %d)" % proc.pid)
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        else:
            self.var_status.set("已停止")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        if not self._closing:
            self.master.after(1000, self._tick_status)

    def _popen_with_pipe(self, cmd):
        """引擎/体检这类要把输出打到日志区的进程。"""
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=rd.base_dir(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True, encoding="utf-8", errors="replace")

    def _pump(self, proc):
        """reader 线程：子进程 stdout 逐行丢进队列，由界面线程取。"""
        try:
            for line in proc.stdout:
                self.q.put(line.rstrip("\r\n"))
        except Exception as e:                       # 管道被关掉是正常退出路径
            self.q.put("（读取子进程输出失败：%s）" % e)
        finally:
            self.q.put(None)

    def _drain_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self.append_log("—— 子进程已结束 ——", stamp=False)
                else:
                    self.append_log(item, stamp=False)
        except queue.Empty:
            pass
        if not self._closing:
            self.master.after(200, self._drain_queue)

    def start_engine(self):
        if self.proc is not None and self.proc.poll() is None:
            self.append_log("引擎已经在跑了（PID %d），不用重复启动" % self.proc.pid)
            return
        # ★ 第一次用：旁边还没有 config.json 时直接启引擎没有意义
        #   （OBS 的密码/端口/场景名都还是默认值，连不上 obs-websocket）。
        if not os.path.exists(self.cfg_path):
            self.append_log("还没有 config.json —— 先做一次「首次设置向导」再启动引擎。")
            if messagebox.askyesno(
                    "先做首次设置",
                    "这台电脑还没做过首次设置（旁边没有 config.json）。\n\n"
                    "现在运行「首次设置向导」吗？\n"
                    "（向导要连 OBS，请先把 OBS 打开；中间会弹一次 UAC 用来放行手机访问）"):
                self.open_wizard()
            return
        # ★ 规格要求：启动前先落盘，保证引擎读到的就是界面上的值
        if not self.write_config(notify=False):
            return
        cmd = engine_cmd(self.cfg_path, [])
        self.append_log("启动引擎：" + " ".join(cmd))
        try:
            self.proc = self._popen_with_pipe(cmd)
        except OSError as e:
            self.proc = None
            self._tick_status()
            messagebox.showerror("启动失败", "拉不起引擎进程：\n%s" % e)
            return
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self._tick_status()

    def stop_engine(self):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self.append_log("引擎没在跑，不用停")
            self._tick_status()
            return
        self.append_log("正在停止引擎（PID %d）…" % proc.pid)
        try:
            proc.terminate()
        except OSError as e:
            self.append_log("terminate 失败：%s" % e)
        self.master.after(1000, lambda: self._force_kill(proc))
        self._tick_status()

    def _force_kill(self, proc):
        if proc.poll() is None:
            self.append_log("1 秒还没退出，强制结束（PID %d）" % proc.pid)
            try:
                proc.kill()
            except OSError as e:
                self.append_log("强杀失败：%s" % e)
        else:
            self.append_log("引擎已退出（退出码 %s）" % proc.returncode)
        self._tick_status()

    def memory_report(self):
        if self.report_proc is not None and self.report_proc.poll() is None:
            self.append_log("内存体检还在跑，等它出完结果")
            return
        if not self.write_config(notify=False):
            return
        cmd = engine_cmd(self.cfg_path, ["--memory-report"])
        self.append_log("内存体检：" + " ".join(cmd))
        try:
            self.report_proc = self._popen_with_pipe(cmd)
        except OSError as e:
            self.report_proc = None
            messagebox.showerror("内存体检失败", "拉不起子进程：\n%s" % e)
            return
        threading.Thread(target=self._pump, args=(self.report_proc,), daemon=True).start()

    def open_wizard(self):
        """首次设置向导：新开一个控制台窗口，不接管道（用户要能跟它交互）。"""
        cmd = wizard_cmd([])
        self.append_log("首次设置向导：" + " ".join(cmd))
        try:
            subprocess.Popen(cmd, cwd=rd.base_dir(),
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError as e:
            messagebox.showerror("打不开向导", "拉不起向导进程：\n%s" % e)

    def _first_run_prompt(self):
        """第一次用（旁边没有 config.json）：问一句要不要现在配置。

        保留老版本"双击就能配好"的体验 —— 只是这次是在窗口里问，
        而不是把向导直接糊到你脸上。
        """
        if not self.first_run or self._closing:
            return
        if messagebox.askyesno(
                "第一次使用",
                "这台电脑还没做过首次设置（旁边没有 config.json）。\n\n"
                "向导会自动：找 CS2、装 GSI 配置、读 OBS 密码、检查 Replay Source 插件、"
                "认场景、放行手机访问（会弹一次 UAC）。\n\n"
                "现在运行吗？\n"
                "（请先把 OBS 打开；装完向导再回到这个窗口点「启动引擎」）"):
            self.open_wizard()
        else:
            self.append_log("跳过首次设置 —— 想配的时候点下面的「首次设置向导」按钮。")
            self.append_log("注意：没配过之前，引擎连不上 OBS（密码/端口都还是默认值）。")

    # ------------------------------------------------------------------
    #  关窗
    # ------------------------------------------------------------------
    def on_close(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            if messagebox.askyesno("引擎还在跑", "引擎还在运行，要一起关掉吗？"):
                self._terminate_now(proc)
            else:
                self.append_log("保留引擎进程继续运行（PID %d）" % proc.pid)
        self._closing = True
        self.master.destroy()

    def _terminate_now(self, proc, wait=1.0):
        """关窗路径专用：terminate → 最多等 1 秒 → kill（after 回调活不下来了）。"""
        try:
            proc.terminate()
        except OSError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


# ===========================================================================
#  入口
# ===========================================================================
def main(argv=None) -> int:
    if tk is None:
        print(NO_TK_MESSAGE)
        if _TK_IMPORT_ERROR:
            print("（%s）" % _TK_IMPORT_ERROR)
        return 2

    args = list(sys.argv[1:] if argv is None else argv)
    cfg_path = None
    if "--config" in args:
        i = args.index("--config")
        if i + 1 < len(args):
            cfg_path = args[i + 1]

    root = tk.Tk()
    win = DirectorGui(root, cfg_path=cfg_path)
    if win.first_run:
        # 第一次用：窗口先亮出来，再问要不要跑首次设置向导（保持"双击就能用"的老体验）
        root.after(300, win._first_run_prompt)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

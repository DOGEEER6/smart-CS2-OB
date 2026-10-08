#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 导播副驾 —— 首次设置向导

把原来要手动做的 6 件事全部自动化：
  1. 找 CS2 安装目录
  2. 把 GSI 配置装进游戏
  3. 读 OBS 的 obs-websocket 密码（不用你手抄）
  4. 检查 Replay Source 插件装没装
  5. 自动识别直播场景 / 创建回放场景
  6. 写好 config.json 并做一次端到端自检

用法：
    python setup_wizard.py             # 自动配置（会写入文件）
    python setup_wizard.py --dry-run   # 只看会做什么，不写任何东西
    python setup_wizard.py --check     # 只自检，不修改
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                        else __file__))
sys.path.insert(0, HERE)

try:
    from replay_director import (ObsClient, log, DEFAULTS, lan_ips,
                                 load_or_make_token, obs_log_newest)
except Exception as e:
    print(f"❌ 找不到 replay_director.py（应该在同一个目录里）：{e}")
    sys.exit(2)

CONFIG_PATH = os.path.join(HERE, "config.json")
GSI_NAME = "gamestate_integration_director.cfg"

GSI_TEMPLATE = '''"CS2 Auto Replay Director"
{
    "uri"       "http://127.0.0.1:@PORT@/cs2/input"
    "timeout"   "5.0"
    "buffer"    "0"
    "throttle"  "0"
    "heartbeat" "10.0"
    "data"
    {
        "provider"                  "1"
        "map"                       "1"
        "round"                     "1"
        "phase_countdowns"          "1"
        "bomb"                      "1"
        "player_id"                 "1"
        "player_state"              "1"
        "player_weapons"            "1"
        "player_match_stats"        "1"
        "player_position"           "1"
        "allplayers_id"             "1"
        "allplayers_state"          "1"
        "allplayers_weapons"        "1"
        "allplayers_match_stats"    "1"
        "allplayers_position"       "1"
        "allgrenades"               "1"
        "map_round_wins"            "1"
    }
}
'''

# 游戏采集类源的 kind（不同 OBS 版本/语言下名字不一样，按 kind 判最可靠）
CAPTURE_KINDS = ("game_capture", "window_capture", "monitor_capture",
                 "screen_capture", "display_capture", "browser_source", "dshow_input")


# ===========================================================================
#  检测
# ===========================================================================
def steam_roots():
    """从注册表找 Steam 安装目录。"""
    out = []
    try:
        import winreg
    except Exception:
        return out
    keys = [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                for name in ("SteamPath", "InstallPath"):
                    try:
                        v, _ = winreg.QueryValueEx(k, name)
                        if v and os.path.isdir(v):
                            out.append(os.path.normpath(v))
                    except OSError:
                        pass
        except OSError:
            pass
    seen, res = set(), []
    for p in out:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


def steam_libraries(roots):
    """Steam 库目录 = 安装目录 + libraryfolders.vdf 里列出的所有盘。"""
    libs = list(roots)
    for r in roots:
        for rel in (r"steamapps\libraryfolders.vdf", r"config\libraryfolders.vdf"):
            vdf = os.path.join(r, rel)
            if not os.path.exists(vdf):
                continue
            try:
                txt = io.open(vdf, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                p = m.group(1).replace("\\\\", "\\")
                if os.path.isdir(p):
                    libs.append(os.path.normpath(p))
    seen, res = set(), []
    for p in libs:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


CS2_SUBPATHS = (
    r"steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg",
    r"steamapps\common\Counter-Strike Global Offensive\csgo\cfg",
    r"steamapps\common\Counter-Strike Global Offensive\game\csgo_addons\cfg",
)


def find_cs2_cfg_dirs(libs):
    """找出 CS2 的 GSI 配置目录。

    CS2 读的是 `game\\csgo\\cfg`；老版本 CS:GO 留下的 `csgo\\cfg` 也在硬盘上但**没用**。
    所以只要找到 CS2 的那个，就只用它 —— 免得往没用的目录里塞文件让人困惑。
    """
    primary, legacy = [], []
    for lib in libs:
        for sub in CS2_SUBPATHS:
            d = os.path.join(lib, sub)
            if not os.path.isdir(d):
                continue
            (primary if sub.endswith(r"game\csgo\cfg") else legacy).append(d)

    def dedup(xs):
        seen, res = set(), []
        for d in xs:
            k = os.path.normcase(d)
            if k not in seen:
                seen.add(k)
                res.append(d)
        return res

    return dedup(primary) or dedup(legacy), dedup(legacy)


def find_obs_exe():
    """尽量找到 obs64.exe。"""
    cands = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        b = os.environ.get(env)
        if b:
            cands.append(os.path.join(b, "obs-studio", "bin", "64bit", "obs64.exe"))
    # 从正在运行的 OBS 进程反推安装目录
    try:
        import ctypes
        from ctypes import wintypes
        k32, u32, psapi = (ctypes.windll.kernel32, ctypes.windll.user32,
                           ctypes.windll.psapi)
        arr = (wintypes.DWORD * 2048)()
        need = wintypes.DWORD()
        if psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr),
                               ctypes.byref(need)):
            n = need.value // ctypes.sizeof(wintypes.DWORD)
            for i in range(n):
                h = k32.OpenProcess(0x1000, False, arr[i])
                if not h:
                    continue
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    sz = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf,
                                                      ctypes.byref(sz)):
                        p = buf.value
                        if os.path.basename(p).lower() == "obs64.exe":
                            cands.insert(0, p)
                finally:
                    k32.CloseHandle(h)
    except Exception:
        pass
    # 常见非默认盘
    for drv in "CDEFGHIJ":
        cands.append(f"{drv}:\\obs-studio\\bin\\64bit\\obs64.exe")
        cands.append(f"{drv}:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe")
    for c in cands:
        if c and os.path.exists(c):
            return os.path.normpath(c)
    return None


FW_RULE = "CS2DirectorViewer"      # 故意不带空格 —— 免得 netsh / PowerShell 的引号打架


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def firewall_rule_exists():
    """规则在不在。netsh 找不到时返回码是 1 —— 比解析中/英文输出可靠。"""
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                            "name=" + FW_RULE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        return r.returncode == 0
    except Exception:
        return None            # 问不出来（netsh 不在？）


def add_firewall_rule(port):
    """加一条入站放行规则。没有管理员权限就弹 UAC，用户点「是」即可。"""
    parts = ["advfirewall", "firewall", "add", "rule", "name=" + FW_RULE,
             "dir=in", "protocol=TCP", "localport=%d" % int(port),
             "remoteip=LocalSubnet", "action=allow"]
    try:
        if is_admin():
            return subprocess.run(["netsh"] + parts, timeout=30).returncode == 0
        # 提权执行：ArgumentList 用数组，且参数里没有空格，不会被拆错
        arr = ",".join("'" + p + "'" for p in parts)
        ps = ("Start-Process -Verb RunAs -Wait -WindowStyle Hidden -FilePath netsh "
              "-ArgumentList @(" + arr + ")")
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              timeout=180).returncode == 0
    except Exception:
        return False


def obsws_config_path():
    a = os.environ.get("APPDATA")
    if not a:
        return None
    p = os.path.join(a, "obs-studio", "plugin_config", "obs-websocket", "config.json")
    return p if os.path.exists(p) else None


def read_obsws():
    """读 obs-websocket 的端口/密码/是否启用。"""
    p = obsws_config_path()
    if not p:
        return {"ok": False, "err": "找不到 obs-websocket 配置文件", "path": None}
    try:
        d = json.loads(io.open(p, encoding="utf-8").read())
        return {"ok": True, "path": p,
                "enabled": bool(d.get("server_enabled", False)),
                "port": int(d.get("server_port", 4455) or 4455),
                "password": d.get("server_password") or "",
                "auth": bool(d.get("auth_required", True))}
    except Exception as e:
        return {"ok": False, "err": f"读不了配置文件: {e}", "path": p}


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.loads(io.open(CONFIG_PATH, encoding="utf-8").read()))
        except Exception as e:
            log(f"⚠️  config.json 读失败（用默认值）: {e}")
    return cfg


def save_cfg(cfg):
    io.open(CONFIG_PATH, "w", encoding="utf-8").write(
        json.dumps(cfg, ensure_ascii=False, indent=2))


# ===========================================================================
#  OBS 自动配置
# ===========================================================================
class Wizard:
    def __init__(self, cfg, dry=False, check_only=False):
        self.cfg = cfg
        self.check_only = check_only
        self.dry = dry or check_only      # 只自检也绝不动任何东西
        self.obs = None
        self.changes = []
        self.todo = []
        self.problems = []
        self.ok = []

    # ---------------- 小工具 ----------------
    def say(self, s=""):
        log(s if s else "")

    def step(self, n, total, title):
        self.say()
        self.say("─" * 66)
        self.say(f"[{n}/{total}] {title}")
        self.say("─" * 66)

    def did(self, s):
        self.changes.append(s)
        self.say(f"   ✅ {s}")

    def need(self, s):
        self.todo.append(s)
        self.say(f"   ⚠️  {s}")

    def bad(self, s):
        self.problems.append(s)
        self.say(f"   ❌ {s}")

    def good(self, s):
        self.ok.append(s)
        self.say(f"   ✅ {s}")

    # ---------------- OBS ----------------
    def connect(self, password, obs_url):
        try:
            c = ObsClient(obs_url, password)
            if c.connect():
                self.obs = c
                return True
            self.bad("连上了但鉴权失败（密码不对？）")
            return False
        except Exception as e:
            self.bad(f"连不上 OBS（{e}）")
            return False

    def scene_items(self, scene):
        try:
            d = self.obs.request("GetSceneItemList", {"sceneName": scene})
            return d.get("sceneItems") or []
        except Exception:
            return []

    def input_kind(self, name):
        try:
            return (self.obs.request("GetInputKind", {"inputName": name})
                    or {}).get("inputKind") or ""
        except Exception:
            return ""

    def is_capture(self, name):
        k = self.input_kind(name)
        if k in CAPTURE_KINDS:
            return True
        # 插件类采集源（例如某些录制插件）按名字兜底
        return any(t in name for t in ("采集", "Capture", "游戏", "显示器", "窗口"))

    def all_source_names(self):
        """OBS 里所有可以当"视频来源"的东西：输入源（场景由调用方另外并入）。"""
        try:
            return [i["inputName"] for i in
                    (self.obs.request("GetInputList") or {}).get("inputs") or []]
        except Exception:
            return []

    def filter_host(self, kind):
        """谁身上挂了这类滤镜，就返回谁；没有返回 None。"""
        try:
            for n in self.all_source_names():
                fl = (self.obs.request("GetSourceFilterList", {"sourceName": n})
                      or {}).get("filters") or []
                if any(f.get("filterKind") == kind for f in fl):
                    return n
        except Exception:
            pass
        return None

    def audio_filter_host(self):
        """老式做法：谁身上挂了 replay_filter_audio 滤镜，谁就是音频缓冲的来源。"""
        return self.filter_host("replay_filter_audio")

    # ---------------- 主流程 ----------------
    def run(self):
        cfg = self.cfg
        TOTAL = 8

        # ---- 1. CS2 ----
        self.step(1, TOTAL, "找 CS2 安装目录")
        roots = steam_roots()
        if not roots:
            self.need("读不到 Steam 安装路径（注册表里没有）")
        else:
            self.good(f"Steam: {', '.join(roots)}")
        libs = steam_libraries(roots)
        if libs:
            self.good(f"游戏库: {', '.join(libs)}")
        cfg_dirs, legacy = find_cs2_cfg_dirs(libs)
        if cfg_dirs:
            for d in cfg_dirs:
                self.good(f"CS2 配置目录: {d}")
            for d in legacy:
                if d not in cfg_dirs:
                    self.say(f"   （老版 CS:GO 留下的 {d}，CS2 不用这个，跳过）")
        else:
            self.need("没找到 CS2 的 game\\csgo\\cfg 目录 —— "
                      "请确认 CS2 已安装，或稍后手动把 GSI 文件拷进去")

        # ---- 2. 装 GSI ----
        self.step(2, TOTAL, "把 GSI 配置装进游戏")
        # ⚠️ 不能用 str.format()：CS2 的 cfg 本身全是花括号，会被当成占位符
        gsi_body = GSI_TEMPLATE.replace("@PORT@", str(cfg.get("gsi_port", 23416)))
        if not cfg_dirs:
            self.need(f"跳过（没找到目录）。手动把 {GSI_NAME} 拷到 "
                      f"…\\game\\csgo\\cfg\\ 即可")
        else:
            for d in cfg_dirs:
                dst = os.path.join(d, GSI_NAME)
                old = ""
                if os.path.exists(dst):
                    try:
                        old = io.open(dst, encoding="utf-8").read()
                    except Exception:
                        pass
                if old.strip() == gsi_body.strip():
                    self.good(f"已是最新: {dst}")
                elif self.dry:
                    self.did(f"[演练] 会写入 {dst}")
                else:
                    try:
                        io.open(dst, "w", encoding="utf-8").write(gsi_body)
                        self.did(f"已安装: {dst}")
                    except Exception as e:
                        self.bad(f"写不了 {dst}: {e}")

        # ---- 3. obs-websocket ----
        self.step(3, TOTAL, "读 OBS 的 obs-websocket 配置")
        ws = read_obsws()
        # 配置里存的是完整 URL（obs_url），不是单独的端口
        old_url = cfg.get("obs_url") or "ws://127.0.0.1:4455"
        m = re.search(r":(\d+)\s*$", old_url)
        port = int(m.group(1)) if m else 4455
        password = cfg.get("obs_password", "")
        if not ws.get("ok"):
            self.need(f"{ws.get('err')} —— 请在 OBS 里打开 "
                      f"工具→WebSocket 服务器设置，勾选「启用」")
        else:
            self.good(f"配置文件: {ws['path']}")
            if not ws["enabled"]:
                self.need("obs-websocket 是**关闭**的 —— 请在 OBS 里勾选「启用 WebSocket 服务器」")
            else:
                self.good(f"WebSocket 已启用，端口 {ws['port']}")
            port = ws["port"]
            if ws["password"]:
                password = ws["password"]
                self.good(f"已自动读到密码（{len(password)} 位，不用你手抄）")
            else:
                self.need("服务器没设密码 —— 建议在 OBS 里设一个（本机用也可以不设）")
            if cfg.get("obs_url") != f"ws://127.0.0.1:{port}" or cfg.get("obs_password") != password:
                cfg["obs_url"] = f"ws://127.0.0.1:{port}"
                cfg["obs_password"] = password
                self.did(("[演练] 会把端口/密码写进配置" if self.dry
                          else "已把端口/密码写进配置"))

        # ---- 4. 连 OBS + 插件 ----
        self.step(4, TOTAL, "连 OBS 并检查 Replay Source 插件")
        obs_exe = find_obs_exe()
        if obs_exe:
            self.good(f"OBS: {obs_exe}")
        else:
            self.say("   （没找到 obs64.exe，不影响使用）")
        if not self.connect(password, cfg["obs_url"]):
            self.need("OBS 没连上 —— 请先**启动 OBS**，然后重跑本向导")
            self.summary()
            return 1
        self.good("已连上 OBS")
        kinds = []
        try:
            kinds = (self.obs.request("GetInputKindList",
                                      {"unversioned": False}) or {}).get("inputKinds") or []
        except Exception:
            pass
        if "replay_source" in kinds:
            self.good("Replay Source 插件已装 ✅")
        else:
            self.bad("**Replay Source 插件没装**（OBS 里没有 replay_source 这个源类型）")
            self.say("       下载: https://github.com/exeldro/obs-replay-source/releases")
            self.say("       解压后把 obs-plugins 和 data 两个文件夹覆盖到 OBS 安装目录，重启 OBS")
            self.todo.append("安装 Exeldro 的 obs-replay-source 插件")
        self.say("   检查回放源与滤镜 ...")
        try:
            inputs = (self.obs.request("GetInputList") or {}).get("inputs") or []
            rep = [i["inputName"] for i in inputs
                   if (i.get("inputKind") or "") == "replay_source"]
        except Exception:
            rep = []
        if rep:
            self.good(f"已有回放源: {', '.join(rep)}")
        else:
            self.need("还没有 Replay Source 源 —— 下一步会自动创建")

        # ---- 5. 场景 ----
        self.step(5, TOTAL, "识别直播场景 / 准备回放场景")
        try:
            sl = self.obs.request("GetSceneList") or {}
            scenes = [s["sceneName"] for s in (sl.get("scenes") or [])]
            program = sl.get("currentProgramSceneName")
        except Exception as e:
            self.bad(f"拿不到场景列表: {e}")
            self.summary()
            return 1
        self.good(f"现有场景: {', '.join(scenes) or '（无）'}")
        self.say(f"   当前节目场景: {program}")

        # 直播场景 = 含采集源的那个
        live_candidates = []
        for s in scenes:
            caps = [it["sourceName"] for it in self.scene_items(s)
                    if self.is_capture(it["sourceName"])]
            if caps:
                live_candidates.append((s, caps))
        if live_candidates:
            for s, caps in live_candidates:
                mark = "  ← 就是它" if s == program else ""
                self.good(f"「{s}」里有采集源: {', '.join(caps)}{mark}")
        else:
            self.bad("**没有任何场景里有采集源**（游戏采集/窗口采集/显示器采集）")
            self.say("       请先在 OBS 里给游戏加一个「游戏采集」，再重跑向导")
            self.todo.append("在 OBS 里添加游戏采集源")

        live_scene = None
        if live_candidates:
            for s, _ in live_candidates:
                if s == program:
                    live_scene = s
                    break
            live_scene = live_scene or live_candidates[0][0]
            self.good(f"选定的直播场景: 「{live_scene}」")
            if cfg.get("live_scene") != live_scene:
                cfg["live_scene"] = live_scene
                self.did(f"已写入 live_scene = {live_scene}")
        else:
            live_scene = cfg.get("live_scene")

        # 回放场景
        replay_scene = cfg.get("replay_scene") or "即时回放"
        has_replay_scene = replay_scene in scenes
        replay_caps = [it["sourceName"] for it in self.scene_items(replay_scene)] \
            if has_replay_scene else []
        if has_replay_scene:
            self.good(f"回放场景已存在: 「{replay_scene}」（内含: "
                      f"{', '.join(replay_caps) or '空'}）")
        else:
            if self.dry:
                self.did(f"[演练] 会创建场景「{replay_scene}」")
            else:
                try:
                    self.obs.request("CreateScene", {"sceneName": replay_scene})
                    self.did(f"已创建场景「{replay_scene}」")
                    has_replay_scene = True
                except Exception as e:
                    self.bad(f"创建场景失败: {e}")

        # 回放源
        replay_item = cfg.get("replay_item") or "Replay Source"
        if has_replay_scene and replay_item not in replay_caps:
            if "replay_source" not in kinds:
                self.need(f"跳过创建回放源（插件没装）")
            elif self.dry:
                self.did(f"[演练] 会在「{replay_scene}」里创建回放源「{replay_item}」")
            else:
                try:
                    self.obs.request("CreateInput", {
                        "sceneName": replay_scene, "inputName": replay_item,
                        "inputKind": "replay_source", "inputSettings": {},
                        "sceneItemEnabled": True})
                    self.did(f"已创建回放源「{replay_item}」")
                except Exception as e:
                    self.bad(f"创建回放源失败: {e}")
                    self.say("       手动做法：在「即时回放」场景里 加号→"
                             "选 Replay Source→命名 Replay Source")
        elif has_replay_scene:
            self.good(f"回放源已存在: 「{replay_item}」")

        # 录像缓冲：插件有两种做法，两种都合法 ——
        #   (a) 新版：直接在"回放源"上选「Video Source / Audio Source」（用户现在就是这种）
        #   (b) 老式：给采集源挂 replay_filter / replay_filter_audio 滤镜
        # 已经配好的**绝不动它**，只补缺失的。特别是：视频来源允许填场景名，
        # 回放就能带上 HUD 叠加层 —— 那是刻意的，不能被"修正"成裸采集源。
        if live_scene and live_candidates and "replay_source" in kinds:
            cap_name = next((c[0] for s2, c in live_candidates if s2 == live_scene),
                            live_candidates[0][1][0])
            valid = set(self.all_source_names()) | set(scenes)
            cur = None
            try:
                _, cur = self.obs.input_settings(replay_item)
                cur = cur or {}
            except Exception as e:
                self.say(f"   （读不到回放源设置: {e}）")

            if cur is not None:
                patch = {}
                v_src = (cur.get("source") or "").strip()
                v_aud = (cur.get("source_audio") or "").strip()
                if v_src and v_src in valid:
                    self.good(f"回放视频来源已设好: 「{v_src}」")
                else:
                    patch["source"] = cap_name
                    self.did(f"回放视频来源{'（原来是 ' + repr(v_src) + '，无效）' if v_src else '（空）'}"
                             f" → 设为「{cap_name}」")
                if v_aud and v_aud in valid:
                    self.good(f"回放音频来源已设好: 「{v_aud}」")
                else:
                    fb = self.audio_filter_host() or cap_name
                    patch["source_audio"] = fb
                    self.did(f"回放音频来源 → 设为「{fb}」")
                if patch:
                    if self.dry:
                        self.did(f"[演练] 会写入 {patch}")
                    else:
                        try:
                            self.obs.request("SetInputSettings", {
                                "inputName": replay_item, "inputSettings": patch,
                                "overwrite": False})
                        except Exception as e:
                            self.need(f"写回放源设置失败: {e}")
                else:
                    self.good("录像通路不需要改动")
        cfg["replay_scene"] = replay_scene
        cfg["replay_item"] = replay_item

        # ---- 6. 回放源参数 ----
        self.step(6, TOTAL, "写回放源的参数")
        want = {
            "duration": int(round(float(cfg.get("record_max_seconds", 10.0)) * 1000)),
            "speed_percent": round(float(cfg.get("speed", 0.7)) * 100 + 0.01, 2),
            "visibility_action": 0 if self.cfg.get("replay_visibility_restart", True) else 2,
            "end_action": 1,                 # Pause after single
            "next_scene": cfg.get("live_scene") or "",
        }
        try:
            _, cur = self.obs.input_settings(replay_item)
            cur = cur or {}
            patch = {k: v for k, v in want.items() if cur.get(k) != v}
            if not patch:
                self.good("参数已经是对的")
            elif self.dry:
                self.did(f"[演练] 会同步参数: {patch}")
            else:
                self.obs.request("SetInputSettings", {
                    "inputName": replay_item, "inputSettings": patch,
                    "overwrite": False})
                self.did(f"已同步参数: {patch}")
        except Exception as e:
                self.need(f"写参数失败: {e}（引擎启动时也会自己同步一次）")

        # ---- 7. 放行手机访问 ----
        self.step(7, TOTAL, "放行手机访问（Windows 防火墙）")
        web_port = int(cfg.get("web_port", 23417))
        fw = firewall_rule_exists()
        if fw is True:
            self.good(f"防火墙已放行 TCP {web_port}（规则名 {FW_RULE}）")
        elif self.dry:
            self.did(f"[演练] 会加一条防火墙规则，放行入站 TCP {web_port}")
        else:
            self.say(f"   手机上要能打开副驾提示器，得让 Windows 防火墙放行入站 TCP {web_port}。")
            self.say(f"   马上会弹一个 UAC 授权框 —— 点「是」就行，只加一条规则。")
            if add_firewall_rule(web_port):
                self.good(f"已放行入站 TCP {web_port}（限同一局域网，公网访问不到）")
            else:
                self.need(f"防火墙规则没加成（可能你点了「否」）—— "
                          f"手动用管理员权限跑一次 8-allow-phone.cmd")

        # ---- 8. 自检 ----
        self.step(8, TOTAL, "端到端自检")
        self.self_test(replay_item, replay_scene, live_scene)

        self.summary()
        return 0

    def self_test(self, replay_item, replay_scene, live_scene):
        obs = self.obs
        # a) 回放源能不能被认出来
        try:
            items = self.scene_items(replay_scene)
            names = [i["sourceName"] for i in items]
            if replay_item in names:
                self.good(f"「{replay_scene}」里能找到回放源「{replay_item}」")
            else:
                self.bad(f"「{replay_scene}」里找不到「{replay_item}」（现在有: {names}）")
        except Exception as e:
            self.bad(f"检查回放场景失败: {e}")
        # b) 录像通路：新版在回放源上选「Video Source」；老式给采集源挂 replay_filter 滤镜。
        #    两种都行 —— 只要有一条通，回放就有画面。
        try:
            _, st = obs.input_settings(replay_item)
            st = st or {}
            sl2 = obs.request("GetSceneList") or {}
            valid = set(self.all_source_names()) | \
                {s["sceneName"] for s in (sl2.get("scenes") or [])}
            v_src = (st.get("source") or "").strip()
            v_aud = (st.get("source_audio") or "").strip()
            if v_src and v_src in valid:
                self.good(f"录像通路 OK：视频来源 = 「{v_src}」"
                          + ("（场景，回放会带 HUD 叠加层）" if v_src in
                             {s["sceneName"] for s in (sl2.get("scenes") or [])}
                             and v_src not in self.all_source_names() else ""))
                self.cfg["replay_capture_source"] = v_src
                if not v_aud or v_aud not in valid:
                    self.need("回放源没选「音频来源」—— 回放会没声音"
                              "（OBS 里双击回放源选一下）")
                else:
                    self.good(f"回放音频来源 = 「{v_aud}」")
            else:
                host = self.filter_host("replay_filter")
                if host:
                    self.good(f"录像通路 OK：用录制滤镜，挂在「{host}」上")
                    self.cfg["replay_capture_source"] = host
                else:
                    self.bad("录像通路没配 —— 回放会拿不到画面")
                    self.say("       修法：OBS 里双击「Replay Source」→ 把「Video Source」"
                             "选成采集源（或选直播场景，这样回放会带 HUD）")
        except Exception as e:
            self.need(f"检查录像通路失败: {e}")
        # c) 场景切换
        try:
            sl = obs.request("GetSceneList") or {}
            names = [s["sceneName"] for s in (sl.get("scenes") or [])]
            if live_scene in names:
                self.good(f"直播场景「{live_scene}」存在")
            else:
                self.bad(f"直播场景「{live_scene}」不存在")
            if obs.scene_list()[1] != live_scene:
                self.say(f"   ℹ️  当前节目场景是「{obs.scene_list()[1]}」，不是「{live_scene}」"
                         f" —— 开播前切过去即可（比赛开始后按 → 也会自动回来）")
            else:
                self.good("当前就在直播场景上")
        except Exception as e:
            self.need(f"场景检查失败: {e}")
        # d) 热键能力
        try:
            hk = (obs.request("GetHotkeyList") or {}).get("hotkeys") or []
            if "ReplaySource.Replay" in hk:
                self.good("能触发 ReplaySource.Replay 热键")
            else:
                self.need("OBS 里没有 ReplaySource.Replay 热键（重启 OBS 后会注册）")
        except Exception as e:
            self.need(f"热键检查失败: {e}")

    def summary(self):
        self.say()
        self.say("=" * 66)
        self.say("  设置结果")
        self.say("=" * 66)
        if self.changes:
            self.say(f"  ✅ 这次做了 {len(self.changes)} 项修改：")
            for c in self.changes:
                self.say(f"      · {c}")
        if self.problems:
            self.say()
            self.say(f"  ❌ 有 {len(self.problems)} 个问题必须先解决：")
            for p in self.problems:
                self.say(f"      · {p}")
        if self.todo:
            self.say()
            self.say(f"  ⚠️  还有 {len(self.todo)} 件事要你手动处理：")
            for t in self.todo:
                self.say(f"      · {t}")
        if not self.problems and not self.todo:
            self.say()
            self.say("  🎉 全部就绪！")
        self.say("=" * 66)
        if not self.problems and not self.todo:
            self.say()
            if getattr(sys, "frozen", False):
                self.say("  下一步：双击同一个文件夹里的「开始导播.exe」")
            else:
                self.say("  下一步：双击 6-director.cmd 开始导播")
            self.say("         手机浏览器打开引擎窗口里打印的那个地址")
        else:
            self.say()
            self.say("  处理完上面的事情后，重新运行一次本向导即可。")
        # ★ 2026-10-07：第一次用一定要提醒两件事 —— 按键可以改、回放功能默认是关的。
        #   这两条以前没人告诉用户，结果"按了键没反应"和"没小键盘怎么用"都要靠猜。
        self.say()
        self.say("-" * 66)
        self.say("  ★ 还有两件事建议现在就做（双击「开始导播.exe」打开的设置窗口里）：")
        self.say("     1. 改按键：点「修改」然后按一下你要用的键。")
        self.say("        笔记本 / 60% 键盘没有小键盘的话，把「播放 / 收起回放」改成 F8、")
        self.say("        「保留片段」改成 F9 —— 这样不受 NumLock 影响。")
        self.say("     2. 打开「回放功能」：它出厂是**关着的**（关了不占内存）。")
        self.say("        不打开的话，← / → / 保留片段 / 播放 四个键按下去只会有提示。")
        self.say("-" * 66)


def pause(msg="\n按回车键关闭..."):
    """双击 .cmd 时停一下好让人看清；被脚本调用/管道时不要卡住。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
        input(msg)
    except (EOFError, OSError, ValueError):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="CS2 导播副驾 · 首次设置向导")
    ap.add_argument("--dry-run", action="store_true", help="只显示会做什么，不修改任何东西")
    ap.add_argument("--check", action="store_true", help="只自检，不修改")
    args = ap.parse_args(argv)

    log("=" * 66)
    log("  CS2 导播副驾 · 首次设置向导")
    if args.dry_run:
        log("  模式：演练（不会修改任何文件）")
    elif args.check:
        log("  模式：只自检")
    log("=" * 66)

    cfg = load_cfg()
    w = Wizard(cfg, dry=args.dry_run, check_only=args.check)
    rc = w.run()

    if not args.dry_run and not args.check:
        try:
            save_cfg(cfg)
            log("")
            log(f"✅ 配置已保存: {CONFIG_PATH}")
            load_or_make_token()
            log(f"✅ 手机口令已生成: viewer_token.txt")
            log("")
            log("📱 手机访问地址（引擎启动时也会打印）：")
            tok = load_or_make_token()
            for i, ip in enumerate(lan_ips()):
                tag = "   ← 一般是这个" if i == 0 else ""
                log(f"      http://{ip}:{cfg.get('web_port', 23417)}/?k={tok}{tag}")
            log("   地址里的 ?k= 是口令，手机加到主屏幕书签，以后一点就开。")
            log("   如果手机连不上，用管理员权限运行一次 8-allow-phone.cmd")
        except Exception:
            log("!! 保存配置失败:\n" + traceback.format_exc())
            rc = 1
    try:
        if w.obs:
            w.obs.close()
    except Exception:
        pass
    if not args.dry_run and not args.check:
        pause()
    return rc


if __name__ == "__main__":
    sys.exit(main())

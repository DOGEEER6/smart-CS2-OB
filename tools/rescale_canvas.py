# -*- coding: utf-8 -*-
"""OBS 场景整体缩放工具（4K 画布 → 1080p，或任意比例）

为什么需要它：把基础(画布)分辨率从 3840x2160 改成 1920x1080 后，OBS 不会自动搬运
每个源的坐标/缩放，画面会整体错位。这个脚本通过 obs-websocket **在线**把所有场景里
每个场景项（含分组内的子项）的 position / scale / bounds 按比例缩放，并同时改画布
分辨率 —— 不用关 OBS，不用手改场景文件，随时可一键还原。
文字源字号、滤镜像素值、场景项裁剪**故意不动**：它们与画布尺寸无关（字号随 item scale
等比变小，裁剪按源像素算）。

用法（在 G:\\dsh\\cs2-replay-director 下）：
    python tools\\rescale_canvas.py                      # 只看：列出将要改什么（默认 dry-run）
    python tools\\rescale_canvas.py --shots C:\\Temp\\before   # 先给每个场景拍 1080p 截图
    python tools\\rescale_canvas.py --apply              # 真正执行（默认目标 1920x1080）
    python tools\\rescale_canvas.py --restore C:\\Temp\\canvas_backup.json   # 一键还原

安全设计：
  * --apply 前会检查是否正在推流/录制，正在推流则拒绝执行（除非 --force）。
  * 每次 --apply 都会把所有旧值写进备份 JSON（--backup 指定，默认 C:\\Temp\\canvas_backup.json）。
  * 默认 dry-run；比例必须能整除画布高度，且目标必须小于当前画布（拒绝放大）。
"""
import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import replay_director as rd  # noqa: E402

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
DEFAULT_BACKUP = r"C:\Temp\canvas_backup.json"

# 画布像素 → 跟着比例缩放的键。注意：**裁剪(cropXxx)不在其中** —— OBS 的裁剪按"源像素"
# 算，与场景项缩放无关，减半会让画面被裁掉一块。文字源字号同理不动（字号在源纹理里渲染，
# 再由场景项 scale 缩放：item scale 减半后相对大小不变，F/2160 == 0.5F/1080）。
TRANSFORM_PX_KEYS = ("positionX", "positionY", "boundsWidth", "boundsHeight")
TRANSFORM_SCALE_KEYS = ("scaleX", "scaleY")


def connect():
    cfg = dict(rd.DEFAULTS)
    if os.path.exists(CONFIG):
        cfg.update(json.load(io.open(CONFIG, encoding="utf-8")))
    obs = rd.ObsClient(cfg["obs_url"], cfg["obs_password"])
    obs.connect()
    return obs, cfg


def collect(obs):
    """把所有场景（含分组）里的场景项、文字源、滤镜的当前值抓出来。"""
    scenes = obs.request("GetSceneList")["scenes"]
    items = []

    def walk(scene_name, group_path):
        try:
            lst = (obs.request("GetGroupSceneItemList", {"sceneName": scene_name})
                   if group_path else
                   obs.request("GetSceneItemList", {"sceneName": scene_name}))
        except Exception as e:
            print("  !! 读不到 %s 的图层：%s" % (scene_name, e))
            return
        for it in lst.get("sceneItems", []):
            items.append({
                "scene": scene_name,
                "groupPath": group_path,
                "sceneItemId": it["sceneItemId"],
                "sourceName": it.get("sourceName"),
                "isGroup": bool(it.get("isGroup")),
                "transform": it.get("sceneItemTransform") or {},
            })
            if it.get("isGroup"):
                walk(it["sourceName"], group_path + [scene_name] if group_path else [scene_name])
    for s in scenes:
        walk(s["sceneName"], [])
    return scenes, items


def scaled_transform(tr, ratio):
    new = {}
    for k in TRANSFORM_PX_KEYS:
        v = tr.get(k)
        if not isinstance(v, (int, float)):
            continue
        if v <= 0 and not k.startswith("position"):
            continue  # boundsWidth/Height = 0 表示"没用边界框"，OBS 不收 <1 的值
        new[k] = round(v * ratio, 3)
    for k in TRANSFORM_SCALE_KEYS:
        v = tr.get(k)
        if isinstance(v, (int, float)) and v != 0:
            new[k] = round(v * ratio, 6)
    return new


def apply_transform(obs, it, tr):
    """写回 transform。两个坑（2026-10-07 都踩过）：
    * `boundsWidth/boundsHeight` < 1 会被 OBS 拒绝（0 表示"没用边界框"）→ 整键去掉；
    * `width/height/sourceWidth/sourceHeight` 是 OBS 算出来的**只读结果**，
      原样回写会让画面再放大一倍（实测 scale 2.0 被写成 4.0）→ 必须去掉。
    """
    drop = ("boundsWidth", "boundsHeight", "width", "height",
            "sourceWidth", "sourceHeight")
    clean = {k: v for k, v in tr.items() if k not in drop}
    obs.request("SetSceneItemTransform", {
        "sceneName": it["scene"], "sceneItemId": it["sceneItemId"],
        "sceneItemTransform": clean})


def shoot(obs, dest):
    """给每个场景拍一张 1920x1080 截图 —— 改画布前后各拍一次，用来肉眼比对有没有错位。"""
    scenes = obs.request("GetSceneList")["scenes"]
    os.makedirs(dest, exist_ok=True)
    cur = obs.request("GetCurrentProgramScene")["sceneName"]
    for s in scenes:
        name = s["sceneName"]
        path = os.path.join(dest, "%s.png" % name.replace("/", "_"))
        try:
            obs.request("SetCurrentProgramScene", {"sceneName": name})
            time.sleep(0.45)
            obs.request("SaveSourceScreenshot", {
                "sourceName": name, "imageFormat": "png", "imageFilePath": path,
                "imageWidth": 1920, "imageHeight": 1080})
            print("   截图 %s" % path)
        except Exception as e:
            print("   截图失败 %s：%s" % (name, e))
    obs.request("SetCurrentProgramScene", {"sceneName": cur})
    print("截图完成，已切回场景「%s」" % cur)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    ap.add_argument("--to", default="1920x1080", help="目标画布，默认 1920x1080")
    ap.add_argument("--backup", default=DEFAULT_BACKUP)
    ap.add_argument("--restore", default=None, help="从备份 JSON 还原")
    ap.add_argument("--shots", default=None, help="给每个场景拍 1080p 截图到该目录")
    ap.add_argument("--force", action="store_true", help="正在推流/录制时也照改")
    args = ap.parse_args()

    to_w, to_h = [int(x) for x in args.to.lower().split("x")]
    obs, cfg = connect()
    vs = obs.request("GetVideoSettings")
    from_w, from_h = int(vs["baseWidth"]), int(vs["baseHeight"])

    # ---------- 还原 ----------
    if args.restore:
        bak = json.load(io.open(args.restore, encoding="utf-8"))
        print("还原画布 %sx%s → %sx%s" % (bak["baseWidth"], bak["baseHeight"],
                                          bak["fromWidth"], bak["fromHeight"]))
        # 同样先改画布（OBS 会自动缩放场景项），再按备份写一遍绝对值
        obs.request("SetVideoSettings", {"baseWidth": bak["fromWidth"],
                                         "baseHeight": bak["fromHeight"]})
        for it in bak["items"]:
            apply_transform(obs, it, it["transform"])
        print("✅ 已还原 %d 个场景项，画布回到 %dx%d" %
              (len(bak["items"]), bak["fromWidth"], bak["fromHeight"]))
        return

    # ---------- 缩放 ----------
    if to_h == from_h and to_w == from_w:
        print("画布已经是 %dx%d，不用改。" % (from_w, from_h))
        if args.shots:
            shoot(obs, args.shots)
        return
    if to_h > from_h:
        print("拒绝放大：当前 %dx%d → 目标 %dx%d" % (from_w, from_h, to_w, to_h))
        return
    ratio = float(to_h) / float(from_h)

    stream = obs.request("GetStreamStatus")
    rec = obs.request("GetRecordStatus")
    busy = bool(stream.get("outputActive")) or bool(rec.get("outputActive"))
    if busy and not args.force and args.apply:
        print("⛔ 正在推流/录制（stream=%s record=%s）—— 先停掉再加 --force" %
              (stream.get("outputActive"), rec.get("outputActive")))
        return
    if busy:
        print("⚠️  正在推流/录制：stream=%s record=%s" %
              (stream.get("outputActive"), rec.get("outputActive")))

    scenes, items = collect(obs)

    print("画布：%dx%d → %dx%d（比例 %.4f），输出 %sx%s @ %s fps" % (
        from_w, from_h, to_w, to_h, ratio, vs.get("outputWidth"), vs.get("outputHeight"),
        vs.get("fpsNumerator")))
    print("场景 %d 个，场景项 %d 个（含分组子项）" % (len(scenes), len(items)))

    changes = 0
    skipped = 0
    for it in items:
        tr = it["transform"] or {}
        if not tr:
            skipped += 1
            continue
        new = scaled_transform(tr, ratio)
        if not new:
            skipped += 1
            continue
        changes += 1
        if changes <= 6 or changes % 25 == 0:
            print("   %-28s %-24s pos(%.0f,%.0f)→(%.0f,%.0f) scale %.3f→%.3f" % (
                it["scene"], (it["sourceName"] or "")[:24],
                tr.get("positionX", 0), tr.get("positionY", 0),
                new.get("positionX", 0), new.get("positionY", 0),
                tr.get("scaleX", 1), new.get("scaleX", 1)))
        it["newTransform"] = new
    print("要改的场景项：%d（跳过 %d）" % (changes, skipped))
    print("（文字源字号、滤镜像素值、场景项裁剪都不动 —— 它们与画布尺寸无关）")

    if args.shots:
        shoot(obs, args.shots)

    if not args.apply:
        print("\n（这是预览。确认无误后加 --apply 执行）")
        return

    bak = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseWidth": from_w, "baseHeight": from_h,
        "fromWidth": from_w, "fromHeight": from_h,
        "toWidth": to_w, "toHeight": to_h,
        "items": [{"scene": i["scene"], "sceneItemId": i["sceneItemId"],
                   "sourceName": i["sourceName"], "isGroup": i["isGroup"],
                   "transform": i["transform"]} for i in items],
    }
    io.open(args.backup, "w", encoding="utf-8", newline="\n").write(
        json.dumps(bak, ensure_ascii=False, indent=1))
    print("\n旧值已备份到 %s" % args.backup)

    # ⚠️ 顺序很重要：**先改画布，再写场景项**。OBS 在基础分辨率变化时会自动把整个场景
    # 集合的 position/scale 按比例缩放（实测 1080p 下 scale=1.0 的项，画布改 4K 后自己变成
    # 2.0）。所以先写场景项再改画布 = 缩两次（2026-10-07 踩过：scale 被写成 1/4）。
    # 先改画布让 OBS 自己缩，再按"原值 × ratio"写一遍绝对值，结果就唯一确定。
    obs.request("SetVideoSettings", {"baseWidth": to_w, "baseHeight": to_h})

    done = 0
    applied = []
    try:
        for it in items:
            if it.get("newTransform"):
                apply_transform(obs, it, it["newTransform"])
                applied.append(it)
                done += 1
    except Exception as e:
        # 中途失败必须回滚：否则"改了一半"的状态再跑一次就会把 scale 缩两次
        print("\n!! 应用途中出错：%s" % e)
        print("   正在回滚已经改过的 %d 个场景项 …" % len(applied))
        bad = 0
        for it in applied:
            try:
                apply_transform(obs, it, it["transform"])
            except Exception:
                bad += 1
        obs.request("SetVideoSettings", {"baseWidth": from_w, "baseHeight": from_h})
        print("   回滚完成（失败 %d 个），画布已回到 %dx%d。" % (bad, from_w, from_h))
        return
    # 文字源字号、滤镜像素值**故意不动**：字号在源纹理里渲染，再由场景项 scale 缩放，
    # item scale 减半后相对大小不变（F/2160 == 0.5F/1080）；裁剪按源像素算，也与缩放无关。
    print("✅ 已改 %d 个场景项，画布 → %dx%d（字号/裁剪未动）" % (done, to_w, to_h))
    print("   想还原：python tools\\rescale_canvas.py --restore %s" % args.backup)


main()

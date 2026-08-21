"""版本检查与一键更新(插件自更新 + blendremote-server exe 更新)。

数据来源:GitHub Releases API(siciyuan404/BlendRemote)。
Release 资产命名(由 CI 构建生成):
- 插件 zip   : blendremote-addon-<ver>.zip
- 服务端 exe : blendremote-server-windows-x64.exe
- Android APK: blendremote-v<tag>.apk(手机端自更新用,本模块不处理)

线程约定:
- 网络(check/download)在后台线程执行,结果通过 bpy.app.timers 调度回主线程;
- 涉及 bpy 的操作(reload/register/启动服务)只在主线程调用,避免破坏 bpy 主线程约束。
- 对文件系统的替换(exe / addon 文件)不触碰 bpy,可在前台/后台线程安全调用。
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile


# ============================================================================
# 常量
# ============================================================================

OWNER = "siciyuan404"
REPO = "BlendRemote"
RELEASE_API = (
    "https://api.github.com/repos/{owner}/{repo}/releases/latest"
)
ADDON_ZIP_PREFIX = "blendremote-addon-"
SERVER_WIN_ASSET = "blendremote-server-windows-x64.exe"
_UA = "BlendRemote-Addon/1.0"


# ============================================================================
# 版本比较
# ============================================================================

def parse_version(text):
    """解析 "v0.1.3" / "0.1.3" → (0, 1, 3);无法解析返回空元组。"""
    raw = re.sub(r"^[vV]", "", str(text or "").strip())
    parts = []
    for seg in raw.split("."):
        m = re.match(r"\d+", seg)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def version_str(triple):
    return ".".join(str(int(x)) for x in triple)


def is_newer(latest, current):
    """latest 是否严格大于 current(均为版本串)。"""
    l = parse_version(latest)
    c = parse_version(current)
    if not l or not c:
        return str(latest) != str(current)
    return l > c


# ============================================================================
# 网络(后台线程调用)
# ============================================================================

def fetch_latest():
    """查询 GitHub 最新 Release,返回 {tag, version, notes, addon_zip, server_win}。

    网络异常时抛异常,由调用方捕获。
    """
    url = RELEASE_API.format(owner=OWNER, repo=REPO)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    tag = data.get("tag_name", "")
    assets = {
        a.get("name", ""): a.get("browser_download_url", "")
        for a in data.get("assets", [])
    }
    addon_zip = next(
        (u for n, u in assets.items()
         if n.startswith(ADDON_ZIP_PREFIX) and n.endswith(".zip")),
        None,
    )
    server_win = assets.get(SERVER_WIN_ASSET)
    return {
        "tag": tag,
        "version": tag.removeprefix("v"),
        "notes": (data.get("body") or "").strip(),
        "addon_zip": addon_zip,
        "server_win": server_win,
    }


def current_server_version(server_port):
    """从本地 /serverinfo 读取运行中服务端 app_version;失败返回 None。"""
    url = f"http://127.0.0.1:{server_port + 4}/serverinfo"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("app_version")
    except Exception:
        return None


def download(url, dest, progress_cb=None):
    """流式下载 url 到 dest,可选进度回调(0-100)。后台线程调用。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        last_emit = 0.0
        import time
        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if progress_cb is not None and total > 0:
                    pct = int(got * 100 / total)
                    now = time.monotonic()
                    if got >= total or now - last_emit >= 0.1:
                        progress_cb(pct)
                        last_emit = now
        if progress_cb is not None:
            progress_cb(100)
    return dest


# ============================================================================
# 文件操作(不触碰 bpy,可后台)
# ============================================================================

def extract_addon_zip(zip_path, addon_dir):
    """把插件 zip 解压到 addon_dir(替换旧文件)。

    zip 内含一个根目录(如 blendremote-addon/),解压时剥掉该根目录,
    将其下文件覆盖到 addon_dir。仅写普通文件,规避 zip-slip。
    """
    staging = tempfile.mkdtemp(prefix="blendremote-addon-extract-")
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            # 根目录 = 第一个普通文件路径的第一段(如 blendremote-addon)
            root = ""
            for n in names:
                parts = n.split("/")
                if len(parts) > 1 and parts[0]:
                    root = parts[0]
                    break
            for n in names:
                parts = n.split("/")
                if root and parts and parts[0] == root:
                    parts = parts[1:]
                rel = "/".join(parts)
                if not rel or rel.endswith("/"):
                    continue
                if ".." in parts or rel.startswith("/") or "\\" in rel:
                    continue
                target = os.path.join(staging, rel.replace("/", os.sep))
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with z.open(n) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

        updated = []
        for base, _dirs, files in os.walk(staging):
            for f in files:
                src = os.path.join(base, f)
                rel = os.path.relpath(src, staging)
                dst = os.path.join(addon_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                updated.append(rel)
        # 清理旧字节码缓存,避免重载命中过期 pyc
        pycache = os.path.join(addon_dir, "__pycache__")
        if os.path.isdir(pycache):
            shutil.rmtree(pycache, ignore_errors=True)
        return updated
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def replace_exe(new_path, target_exe):
    """原子替换 exe:目标 → .bak,新文件 → 目标。返回 (ok, err)。"""
    try:
        bak = target_exe + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
        if os.path.exists(target_exe):
            os.replace(target_exe, bak)
        os.replace(new_path, target_exe)
        return True, ""
    except OSError as e:
        return False, f"替换服务端程序失败: {e}"


def looks_like_pe(path):
    """Windows 下校验下载内容为 PE 可执行文件(MZ 头)。"""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except Exception:
        return False


# ============================================================================
# 主线程回调:插件热重载
# ============================================================================

def reload_addon():
    """卸载当前插件模块并按文件系统上的新版本重新 import + register。

    bpy 主线程调用。返回 (ok, err)。
    """
    pkg = __package__ or __name__.split(".")[0]
    mod = sys.modules.get(pkg)
    if mod is not None:
        try:
            mod.unregister()
        except Exception as e:
            return False, f"卸载旧插件失败: {e}"
    prefix = pkg + "."
    for name in [n for n in list(sys.modules) if n == pkg or n.startswith(prefix)]:
        sys.modules.pop(name, None)
    try:
        import importlib
        new = importlib.import_module(pkg)
        new.register()
        # 把成功状态写入重载后的新模块 UI(旧模块的 ui 已随模块被移除)
        try:
            new.updater.ui["last"] = "插件已更新并重新加载"
            new.updater.mark_dirty()
        except Exception:
            pass
        return True, ""
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, f"重新加载插件失败: {e}"


def schedule_once(fn):
    """下一帧在主线程执行 fn(单次 timer)。"""
    import bpy
    def cb():
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
        return None
    bpy.app.timers.register(cb, first_interval=0.05)


# ============================================================================
# 后台执行器(结果回主线程)
# ============================================================================

def run_background(fn, on_main):
    """后台线程执行 fn,完成/异常后把结果调度回主线程 on_main(result)。

    fn 返回 (kind, payload) 表示成功;抛异常时统一返回 ("error", str)。
    """
    def wrapper():
        try:
            result = fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            result = ("error", str(e))
        schedule_once(lambda: on_main(result))
    threading.Thread(target=wrapper, daemon=True, name="blendremote-updater").start()


# ============================================================================
# 更新状态(供 N 面板读取;UI 线程)
# ============================================================================

ui = {
    "busy": False,          # 有后台任务进行中
    "kind": "",             # "" | "check" | "addon" | "server"
    "progress": None,       # 下载进度(0-100)
    "current_addon": "",
    "current_server": "",
    "latest": None,         # fetch_latest 结果 dict
    "error": "",
    "last": "",
    "_dirty": False,
}


def mark_dirty():
    ui["_dirty"] = True


def consume_dirty():
    """取出并重置 UI 脏标记(供主线程 timer 触发面板重绘)。"""
    if ui["_dirty"]:
        ui["_dirty"] = False
        return True
    return False


def snapshot():
    """供面板 draw 的只读快照。"""
    return {
        "busy": ui["busy"],
        "kind": ui["kind"],
        "progress": ui["progress"],
        "current_addon": ui["current_addon"],
        "current_server": ui["current_server"],
        "latest": ui["latest"],
        "error": ui["error"],
        "last": ui["last"],
    }
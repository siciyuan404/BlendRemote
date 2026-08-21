# BlendRemote - 用手机远程控制 Blender(视图/对象/动画/渲染/自定义按钮)
#
# 架构:
# - 本插件(运行在 Blender 内):本地 HTTP 命令桥(127.0.0.1:29390) + N 面板 UI
# - blendremote-server(Rust 守护进程):局域网网关(mDNS 发现/配对/TCP 控制通道),
#   将手机命令转发到本插件的命令桥
# - Android App:手机端控制 UI
#
# 依赖:需要手动启动 blendremote-server(或在本面板点击"启动服务",
# 偏好设置里可指定 blendremote-server 可执行文件路径)。

bl_info = {
    "name": "BlendRemote - 手机远程控制",
    "author": "BlendRemote",
    "version": (0, 1, 3),
    "blender": (3, 6, 0),
    "location": "3D 视图 > 侧边栏 > BlendRemote",
    "description": "用手机远程控制 Blender:视图/对象/动画/渲染/自定义按钮",
    "category": "Interface",
}

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import bpy

from . import bridge
from . import custom_buttons as custom_buttons_mod
from . import layout
from . import updater

# ============================================================================
# 常量
# ============================================================================

DEFAULT_BRIDGE_PORT = 29390
DEFAULT_SERVER_PORT = 28900
PAIRING_HTTP_PORT = DEFAULT_SERVER_PORT + 5  # 127.0.0.1:28905/pairing

# ============================================================================
# 偏好设置
# ============================================================================


class BlendRemotePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    bridge_port: bpy.props.IntProperty(
        name="命令桥端口",
        description="本插件本地 HTTP 命令桥端口(Rust 服务转发命令用)",
        default=DEFAULT_BRIDGE_PORT,
        min=1024,
        max=65535,
    )
    server_port: bpy.props.IntProperty(
        name="服务端口",
        description="blendremote-server 基础端口(control=base)",
        default=DEFAULT_SERVER_PORT,
        min=1024,
        max=65535,
    )
    server_path: bpy.props.StringProperty(
        name="服务端程序",
        description="blendremote-server 可执行文件路径(留空则在 PATH 中查找)",
        subtype="FILE_PATH",
        default="",
    )
    auto_start_server: bpy.props.BoolProperty(
        name="启用插件时自动启动服务",
        description="启用插件时自动启动 blendremote-server 守护进程",
        default=True,
    )
    custom_buttons_json: bpy.props.StringProperty(
        name="自定义按钮",
        description="自定义按钮列表 JSON(手机端可增删)",
        default="[]",
    )
    control_layout_json: bpy.props.StringProperty(
        name="控制面板布局",
        description="手机控制面板按钮布局 JSON(触控板/视图/对象/动画/渲染/自定义页)",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "server_path")
        layout.prop(self, "server_port")
        layout.prop(self, "bridge_port")
        layout.prop(self, "auto_start_server")


# ============================================================================
# 服务端进程管理
# ============================================================================


class ServerManager:
    """管理 blendremote-server 子进程。"""

    _proc = None

    @classmethod
    def is_running(cls):
        return cls._proc is not None and cls._proc.poll() is None

    @classmethod
    def start(cls, server_path, server_port, bridge_port):
        if cls.is_running():
            return True, "服务已在运行"
        exe = server_path
        if not exe:
            found = shutil.which("blendremote-server")
            if found is None:
                return False, (
                    "未找到 blendremote-server,请在偏好设置指定可执行文件路径,"
                    "或从 GitHub Releases 下载"
                )
            exe = found
        elif not os.path.isfile(exe):
            return False, f"服务端程序不存在: {exe}"
        try:
            creationflags = 0
            if sys.platform == "win32":
                # 隐藏子进程控制台窗口
                creationflags = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                [
                    exe,
                    "--port",
                    str(server_port),
                    "--addon-port",
                    str(bridge_port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            cls._proc = proc
            return True, "服务已启动"
        except OSError as e:
            return False, f"启动服务端失败: {e}"

    @classmethod
    def stop(cls):
        if cls._proc is not None:
            try:
                cls._proc.terminate()
                cls._proc.wait(timeout=5)
            except Exception:
                try:
                    cls._proc.kill()
                except Exception:
                    pass
            cls._proc = None
            return True, "服务已停止"
        return False, "服务未运行"


def _resolve_server_exe(prefs):
    """解析服务端可执行文件绝对路径;找不到返回 None。"""
    exe = prefs.server_path if prefs is not None else ""
    if not exe:
        exe = shutil.which("blendremote-server")
    if exe and os.path.isfile(exe):
        return os.path.abspath(exe)
    return None


# ============================================================================
# 服务端配对信息缓存(通过 127.0.0.1:{server_port+5}/pairing 获取)
# ============================================================================

_pairing_cache = {"pin": "", "paired_clients": [], "error": ""}
_pairing_lock = threading.Lock()


def refresh_pairing_cache(server_port):
    """拉取服务端配对信息到缓存(由主线程 timer 周期调用)。"""
    global _pairing_cache
    url = f"http://127.0.0.1:{server_port + 5}/pairing"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        with _pairing_lock:
            _pairing_cache = {
                "pin": data.get("pin", ""),
                "paired_clients": data.get("paired_clients", []),
                "error": "",
            }
    except Exception as e:
        with _pairing_lock:
            _pairing_cache["error"] = str(e)


def pairing_cache():
    with _pairing_lock:
        return dict(_pairing_cache)


# ============================================================================
# 主线程 timer
# ============================================================================

_timer_handle = None
_last_uirefresh = 0.0


def _tag_redraw():
    """重绘 3D 视图区域的 N 面板(更新状态可见)。"""
    try:
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        pass


def _main_timer():
    """主线程周期回调:执行命令队列 + 刷新状态 + 刷新配对缓存。

    周期 30ms:命令从入队到 bpy 执行的平均延迟约为周期一半(~15ms),
    显著降低控制延迟(参考 Moonlight 对输入通道的低延迟要求)。
    """
    bridge.executor.process()
    prefs = _prefs()
    if prefs is not None and ServerManager.is_running():
        refresh_pairing_cache(prefs.server_port)

    # 周期刷新更新面板(节流 ~3s),脏标记触发即时重绘
    global _last_uirefresh
    _dirty = updater.consume_dirty()
    now = time.monotonic()
    if _dirty or now - _last_uirefresh >= 3.0:
        if _dirty:
            _last_uirefresh = 0.0
        prev = updater.ui.get("current_server")
        updater.ui["current_addon"] = updater.version_str(bl_info["version"])
        cur = updater.current_server_version(prefs.server_port) if prefs else None
        updater.ui["current_server"] = cur or "未运行"
        _last_uirefresh = now
        if _dirty or cur != prev:
            _tag_redraw()
    return 0.03


def _ensure_timer():
    global _timer_handle
    if _timer_handle is not None:
        return
    try:
        _timer_handle = bpy.app.timers.register(_main_timer)
    except Exception:
        _timer_handle = None


def _remove_timer():
    global _timer_handle
    if _timer_handle is not None:
        try:
            bpy.app.timers.unregister(_timer_handle)
        except Exception:
            pass
        _timer_handle = None


# ============================================================================
# Operators
# ============================================================================


class BLENDREMOTE_OT_start_server(bpy.types.Operator):
    bl_idname = "blendremote.start_server"
    bl_label = "启动服务"
    bl_description = "启动 blendremote-server 守护进程"

    def execute(self, context):
        prefs = _prefs(context)
        ok, msg = ServerManager.start(prefs.server_path, prefs.server_port, prefs.bridge_port)
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"}


class BLENDREMOTE_OT_stop_server(bpy.types.Operator):
    bl_idname = "blendremote.stop_server"
    bl_label = "停止服务"
    bl_description = "停止 blendremote-server 守护进程"

    def execute(self, context):
        ok, msg = ServerManager.stop()
        self.report({"INFO"} if ok else {"ERROR"}, msg)
        return {"FINISHED"}


class BLENDREMOTE_OT_refresh_pin(bpy.types.Operator):
    bl_idname = "blendremote.refresh_pin"
    bl_label = "刷新 PIN"
    bl_description = "生成新的 6 位配对 PIN(旧 PIN 立即失效)"

    def execute(self, context):
        prefs = _prefs(context)
        url = f"http://127.0.0.1:{prefs.server_port + 5}/pairing/refresh"
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.report({"INFO"}, f"新 PIN: {data.get('pin', '')}")
        except Exception as e:
            self.report({"ERROR"}, f"刷新 PIN 失败: {e}")
        return {"FINISHED"}


class BLENDREMOTE_OT_reset_pairing(bpy.types.Operator):
    bl_idname = "blendremote.reset_pairing"
    bl_label = "重置配对"
    bl_description = "清空所有已配对手机"

    def execute(self, context):
        prefs = _prefs(context)
        url = f"http://127.0.0.1:{prefs.server_port + 5}/pairing/reset"
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.report({"INFO"}, "配对已重置" if data.get("ok") else f"失败: {data}")
        except Exception as e:
            self.report({"ERROR"}, f"重置配对失败: {e}")
        return {"FINISHED"}


class BLENDREMOTE_OT_add_custom_button(bpy.types.Operator):
    bl_idname = "blendremote.add_custom_button"
    bl_label = "添加自定义按钮"
    bl_description = "添加自定义按钮(operator 如 object.delete 或 mesh.primitive_cube_add(size=2))"

    name: bpy.props.StringProperty(name="名称", default="")
    operator: bpy.props.StringProperty(name="Operator", default="")

    def execute(self, context):
        name = self.name.strip()
        operator = self.operator.strip()
        if not name or not operator:
            self.report({"ERROR"}, "名称和 operator 均不能为空")
            return {"CANCELLED"}
        custom_buttons_mod.save_button(name, operator)
        self.report({"INFO"}, f"已添加按钮: {name}")
        return {"FINISHED"}


class BLENDREMOTE_OT_delete_custom_button(bpy.types.Operator):
    bl_idname = "blendremote.delete_custom_button"
    bl_label = "删除自定义按钮"

    name: bpy.props.StringProperty(name="名称", default="")

    def execute(self, context):
        custom_buttons_mod.delete_button(self.name)
        self.report({"INFO"}, f"已删除按钮: {self.name}")
        return {"FINISHED"}


# ============================================================================
# 更新(检查/更新插件/更新服务端)
# ============================================================================


class BLENDREMOTE_OT_check_update(bpy.types.Operator):
    bl_idname = "blendremote.check_update"
    bl_label = "检查更新"
    bl_description = "检查插件与 blendremote-server 是否有新版本"

    def execute(self, context):
        if updater.ui["busy"]:
            self.report({"WARNING"}, "已有任务在进行中")
            return {"CANCELLED"}
        updater.ui["current_addon"] = updater.version_str(bl_info["version"])
        updater.ui["busy"] = True
        updater.ui["kind"] = "check"
        updater.ui["error"] = ""
        updater.ui["last"] = ""
        updater.mark_dirty()

        def work():
            return ("ok", updater.fetch_latest())

        def on_main(res):
            if res[0] == "error":
                updater.ui["error"] = res[1]
            else:
                updater.ui["latest"] = res[1]
                updater.ui["last"] = f"已检查:最新版本 v{res[1]['version']}"
            updater.ui["busy"] = False
            updater.ui["kind"] = ""
            updater.mark_dirty()

        updater.run_background(work, on_main)
        self.report({"INFO"}, "正在检查更新...")
        return {"FINISHED"}


class BLENDREMOTE_OT_update_addon(bpy.types.Operator):
    bl_idname = "blendremote.update_addon"
    bl_label = "更新插件"
    bl_description = "下载新版本插件并自动重新加载"

    def execute(self, context):
        if updater.ui["busy"]:
            self.report({"WARNING"}, "已有任务在进行中")
            return {"CANCELLED"}
        latest = updater.ui.get("latest")
        url = latest.get("addon_zip") if latest else None
        if not url:
            self.report({"ERROR"}, "上游未找到插件 zip,请先检查更新")
            return {"CANCELLED"}

        updater.ui["busy"] = True
        updater.ui["kind"] = "addon"
        updater.ui["progress"] = 0
        updater.ui["error"] = ""
        updater.ui["last"] = ""
        updater.mark_dirty()

        addon_dir = os.path.dirname(os.path.abspath(__file__))

        def work():
            dest = os.path.join(tempfile.mkdtemp(prefix="blendremote-upd-"), "addon.zip")
            updater.download(
                url, dest, progress_cb=lambda p: updater.ui.__setitem__("progress", p)
            )
            return ("ok", dest)

        def on_main(res):
            if res[0] == "error":
                updater.ui["busy"] = False
                updater.ui["kind"] = ""
                updater.ui["error"] = res[1]
                updater.mark_dirty()
                return
            try:
                updater.extract_addon_zip(res[1], addon_dir)
                ok, err = updater.reload_addon()
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            updater.ui["busy"] = False
            updater.ui["kind"] = ""
            updater.ui["progress"] = None
            if ok:
                updater.ui["latest"] = None
            else:
                updater.ui["error"] = err
            updater.mark_dirty()

        updater.run_background(work, on_main)
        self.report({"INFO"}, "正在下载并更新插件...")
        return {"FINISHED"}


class BLENDREMOTE_OT_update_server(bpy.types.Operator):
    bl_idname = "blendremote.update_server"
    bl_label = "更新服务端(exe)"
    bl_description = "下载新版本 blendremote-server 并替换,若更新前在运行则自动重启"

    def execute(self, context):
        if updater.ui["busy"]:
            self.report({"WARNING"}, "已有任务在进行中")
            return {"CANCELLED"}
        latest = updater.ui.get("latest")
        url = latest.get("server_win") if latest else None
        if not url:
            self.report({"ERROR"}, "上游未找到 Windows 服务端 exe,请先检查更新")
            return {"CANCELLED"}
        prefs = _prefs(context)
        target = _resolve_server_exe(prefs)
        if not target:
            self.report({"ERROR"}, "找不到 blendremote-server 可执行文件,请在偏好设置指定路径")
            return {"CANCELLED"}

        was_running = ServerManager.is_running()
        # Windows 下运行中的 exe 无法覆盖,先停止
        if was_running:
            ServerManager.stop()

        updater.ui["busy"] = True
        updater.ui["kind"] = "server"
        updater.ui["progress"] = 0
        updater.ui["error"] = ""
        updater.ui["last"] = ""
        updater.mark_dirty()

        def work():
            temp = os.path.join(os.path.dirname(target), ".blendremote-server.new.exe")
            updater.download(
                url, temp, progress_cb=lambda p: updater.ui.__setitem__("progress", p)
            )
            if sys.platform == "win32" and not updater.looks_like_pe(temp):
                raise RuntimeError("下载的不是有效的 Windows 可执行文件")
            return ("ok", temp)

        def on_main(res):
            if res[0] == "error":
                if was_running:
                    ServerManager.start(prefs.server_path, prefs.server_port, prefs.bridge_port)
                updater.ui["busy"] = False
                updater.ui["kind"] = ""
                updater.ui["error"] = res[1]
                updater.mark_dirty()
                return
            ok, err = updater.replace_exe(res[1], target)
            if was_running:
                ServerManager.start(prefs.server_path, prefs.server_port, prefs.bridge_port)
            updater.ui["busy"] = False
            updater.ui["kind"] = ""
            updater.ui["progress"] = None
            if ok:
                updater.ui["last"] = "服务端已更新" + ("并重启" if was_running else "")
                updater.ui["latest"] = None
            else:
                updater.ui["error"] = err
            updater.mark_dirty()

        updater.run_background(work, on_main)
        self.report({"INFO"}, "正在下载并更新服务端...")
        return {"FINISHED"}


# ============================================================================
# N-panel UI
# ============================================================================


class BLENDREMOTE_PT_panel(bpy.types.Panel):
    bl_label = "BlendRemote"
    bl_idname = "BLENDREMOTE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlendRemote"

    def draw(self, context):
        layout = self.layout
        prefs = _prefs(context)

        # --- 服务状态 ---
        box = layout.box()
        col = box.column(align=True)
        if ServerManager.is_running():
            col.label(text="服务状态: 运行中", icon="CHECKMARK")
        else:
            col.label(text="服务状态: 已停止", icon="X")
        row = col.row(align=True)
        row.operator("blendremote.start_server", text="启动服务")
        row.operator("blendremote.stop_server", text="停止服务")
        col.label(
            text=f"手机连接: blendremote://{_local_ip_hint()}:{prefs.server_port}",
            icon="WORLD",
        )

        # --- 配对 ---
        box = layout.box()
        col = box.column(align=True)
        col.label(text="配对", icon="KEYINGSET")
        info = pairing_cache()
        if ServerManager.is_running():
            if info.get("error"):
                col.label(text=f"无法获取配对信息: {info['error'][:40]}", icon="ERROR")
            else:
                pin = info.get("pin", "")
                col.label(text=f"当前 PIN: {pin or '(无)'}", icon="LOCKED")
                row = col.row(align=True)
                row.operator("blendremote.refresh_pin", text="刷新 PIN")
                row.operator("blendremote.reset_pairing", text="重置配对")
                paired = info.get("paired_clients", [])
                col.separator()
                if paired:
                    for c in paired:
                        col.label(text=f"• {c.get('client_name', '?')}", icon="PHONE")
                else:
                    col.label(text="暂无已配对手机", icon="INFO")
                col.separator()
                col.label(text="手机 App: 发现设备 → 输入上方 PIN", icon="QUESTION")
        else:
            col.label(text="启动服务后显示配对信息", icon="INFO")

        # --- 自定义按钮 ---
        box = layout.box()
        col = box.column(align=True)
        col.label(text="自定义按钮", icon="PREFERENCES")
        buttons = custom_buttons_mod.list_buttons()
        if buttons:
            for b in buttons:
                row = col.row(align=True)
                row.label(text=b["name"])
                op = row.operator("blendremote.delete_custom_button", text="", icon="X")
                op.name = b["name"]
        else:
            col.label(text="暂无自定义按钮(可在手机端添加)", icon="INFO")
        col.separator()
        add = col.operator("blendremote.add_custom_button", text="添加按钮", icon="ADD")
        # 展开式输入:点击后由 UI 属性填充
        props = context.window_manager.blendremote_new_button
        if props.show_form:
            col.prop(props, "name", text="名称")
            col.prop(props, "operator", text="Operator")
            row = col.row(align=True)
            confirm = row.operator("blendremote.add_custom_button", text="保存")
            confirm.name = props.name
            confirm.operator = props.operator
            row.operator("blendremote.toggle_button_form", text="取消")

        # --- 状态摘要(调试) ---
        status = bridge.executor.status()
        if status.get("ok"):
            box = layout.box()
            col = box.column(align=True)
            col.label(text="Blender 状态", icon="OUTLINER_OB_MESH")
            col.label(text=f"模式: {status.get('mode', '?')}")
            col.label(
                text=f"帧: {status.get('frame_current', 0)}/{status.get('frame_end', 0)}"
            )
            col.label(text=f"选中: {status.get('selected_count', 0)} 个对象")

        # --- 更新(插件 / 服务端) ---
        box = layout.box()
        col = box.column(align=True)
        s = updater.snapshot()
        col.label(text="更新", icon="FILE_REFRESH")
        col.label(text=f"插件版本: {s['current_addon']}")
        col.label(text=f"服务端版本: {s['current_server']}")
        row = col.row(align=True)
        row.operator("blendremote.check_update", text="检查更新")
        if s["busy"]:
            if s["kind"] == "check":
                col.label(text="正在检查更新...", icon="INFO")
            else:
                pct = f"{s['progress']}%" if s["progress"] is not None else "..."
                what = "插件" if s["kind"] == "addon" else "服务端"
                col.label(text=f"{what}正在更新 {pct}", icon="INFO")
        if s["error"]:
            col.label(text=s["error"][:70], icon="ERROR")
        if s["last"]:
            col.label(text=s["last"], icon="CHECKMARK")
        latest = s.get("latest")
        if latest:
            col.separator()
            col.label(text=f"发现新版本 v{latest['version']}", icon="NEW")
            row = col.row(align=True)
            if latest.get("addon_zip"):
                row.operator("blendremote.update_addon", text="更新插件")
            if latest.get("server_win"):
                row.operator("blendremote.update_server", text="更新服务端")
            notes = latest.get("notes") or ""
            if notes:
                for line in notes.splitlines()[:4]:
                    col.label(text=line[:80], icon="BLANK1")


class BLENDREMOTE_OT_toggle_button_form(bpy.types.Operator):
    bl_idname = "blendremote.toggle_button_form"
    bl_label = "切换输入框"

    def execute(self, context):
        wm = context.window_manager
        wm.blendremote_new_button.show_form = not wm.blendremote_new_button.show_form
        return {"FINISHED"}


class BlendRemoteNewButtonProps(bpy.types.PropertyGroup):
    show_form: bpy.props.BoolProperty(default=False)
    name: bpy.props.StringProperty(default="")
    operator: bpy.props.StringProperty(default="object.delete")


# ============================================================================
# 注册
# ============================================================================

_classes = (
    BlendRemotePreferences,
    BLENDREMOTE_OT_start_server,
    BLENDREMOTE_OT_stop_server,
    BLENDREMOTE_OT_refresh_pin,
    BLENDREMOTE_OT_reset_pairing,
    BLENDREMOTE_OT_add_custom_button,
    BLENDREMOTE_OT_delete_custom_button,
    BLENDREMOTE_OT_check_update,
    BLENDREMOTE_OT_update_addon,
    BLENDREMOTE_OT_update_server,
    BLENDREMOTE_OT_toggle_button_form,
    BLENDREMOTE_PT_panel,
    BlendRemoteNewButtonProps,
)


def _prefs(context=None):
    """返回 AddonPreferences 实例(Addon 对象在 .preferences 属性上)。"""
    prefs = (context or bpy.context).preferences
    addon = prefs.addons.get(__package__)
    if addon is None:
        return None
    return getattr(addon, "preferences", None)


def _local_ip_hint():
    """尽力返回一个本机局域网 IP(仅用于提示)。"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "局域网IP"


# ============================================================================
# 命令桥服务(按偏好设置端口启动)
# ============================================================================

_bridge_server = None


def _start_bridge(port):
    global _bridge_server
    if _bridge_server is not None:
        _bridge_server.stop()
        _bridge_server = None
    _bridge_server = bridge.BridgeServer(port)
    return _bridge_server.start()


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.blendremote_new_button = bpy.props.PointerProperty(
        type=BlendRemoteNewButtonProps
    )

    # 启动命令桥
    prefs = _prefs()
    bridge_port = prefs.bridge_port if prefs else DEFAULT_BRIDGE_PORT
    ok, msg = _start_bridge(bridge_port)
    if not ok:
        print(f"[BlendRemote] 命令桥启动失败: {msg}")

    # 主线程 timer(命令执行 + 状态/配对刷新)
    _ensure_timer()

    # 自动启动 Rust 服务
    if prefs is not None and prefs.auto_start_server:
        ok2, msg2 = ServerManager.start(prefs.server_path, prefs.server_port, prefs.bridge_port)
        print(f"[BlendRemote] 自动启动服务: {'成功' if ok2 else msg2}")


def unregister():
    _remove_timer()
    global _bridge_server
    if _bridge_server is not None:
        _bridge_server.stop()
        _bridge_server = None
    ServerManager.stop()
    bpy.types.WindowManager.blendremote_new_button = None
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
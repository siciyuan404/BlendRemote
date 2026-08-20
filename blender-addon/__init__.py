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
    "version": (0, 1, 0),
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
import threading
import urllib.request

import bpy

from . import bridge
from . import custom_buttons as custom_buttons_mod

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


def _main_timer():
    """主线程周期回调:执行命令队列 + 刷新状态 + 刷新配对缓存。"""
    bridge.executor.process()
    prefs = bpy.context.preferences.addons.get(__package__)
    if prefs is not None and ServerManager.is_running():
        refresh_pairing_cache(prefs.server_port)
    return 0.25


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
        prefs = context.preferences.addons.get(__package__)
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
        prefs = context.preferences.addons.get(__package__)
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
        prefs = context.preferences.addons.get(__package__)
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
        prefs = context.preferences.addons.get(__package__)

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
    BLENDREMOTE_OT_toggle_button_form,
    BLENDREMOTE_PT_panel,
    BlendRemoteNewButtonProps,
)


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
    prefs = bpy.context.preferences.addons.get(__package__)
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
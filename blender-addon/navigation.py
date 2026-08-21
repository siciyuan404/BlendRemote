"""视图导航:直接操作 3D 视图的 region_3d,实现平滑轨道旋转/平移/缩放。

参考 MeowMic 触摸板的操作手感,通过手机拖动映射为:
- 单指拖动 → 轨道旋转(orbit)
- 双指拖动 → 平移(pan)
- 双指捏合/滚轮 → 缩放(zoom)
"""

import math

import bpy
import mathutils

# 复位基准视图:首次拿到 region_3d 时缓存,作为"复位"回到的初始视图
# 存纯数据(旋转/位置/距离/透视),不持有 region 引用,避免跨会话失效
_home_view = None


def _capture_home(region):
    global _home_view
    if _home_view is None:
        _home_view = {
            "rotate": region.view_rotation.copy(),
            "location": region.view_location.copy(),
            "distance": region.view_distance,
            "perspective": bool(region.is_perspective),
        }


def get_region3d():
    """获取第一个 3D 视图的 region_3d(不依赖 Blender 焦点上下文)。

    bpy.context.region_3d 只在焦点是 3D 视图时可用,且受 modal 操作影响;
    这里遍历所有窗口/区域,取第一个 3D 视图,保证手机控制时不被鼠标操作抢占。
    首次找到视图时记录复位基准 _home_view。
    """
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces[0]
                if hasattr(space, "region_3d") and space.region_3d is not None:
                    _capture_home(space.region_3d)
                    return space.region_3d
    return None


def find_view3d():
    """返回 (window, area, region) 用于 bpy.context.temp_override。"""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return window, area, region
    return None, None, None


def _view_frame(region):
    """返回 (right, up) 视图局部轴(Blender 视图沿局部 -Z 看)。"""
    mat = region.view_rotation.to_matrix()
    return mat.col[0].copy(), mat.col[1].copy()


def orbit(region, dx, dy):
    """轨道旋转:围绕视图局部 up 轴水平转 dx,围绕局部 right 轴垂直转 dy。

    dx/dy 为归一化拖动量(手机触摸像素),正方向:右/上。
    """
    if region is None:
        return
    k = 0.008
    right, up = _view_frame(region)
    q_yaw = mathutils.Quaternion(up, -dx * k)
    q_pitch = mathutils.Quaternion(right, -dy * k)
    region.view_rotation = (q_yaw @ q_pitch) @ region.view_rotation


def pan(region, dx, dy):
    """平移视图:沿 right/up 轴移动 view_location,量级随视图距离缩放。"""
    if region is None:
        return
    right, up = _view_frame(region)
    k = max(region.view_distance * 0.0012, 0.001)
    region.view_location -= right * (dx * k)
    region.view_location += up * (dy * k)


def zoom(region, delta):
    """缩放:delta 为正放大,为负缩小(指数映射,避免反走)。"""
    if region is None:
        return
    factor = math.exp(-delta * 0.02)
    region.view_distance = max(region.view_distance * factor, 0.001)


# 预设视角 → view3d.view_axis type 映射(对应小键盘)
PRESET_AXIS = {
    "TOP": "TOP",
    "BOTTOM": "BOTTOM",
    "FRONT": "FRONT",
    "BACK": "BACK",
    "LEFT": "LEFT",
    "RIGHT": "RIGHT",
}


def view_preset(preset):
    """小键盘预设视角(TOP/BOTTOM/FRONT/BACK/LEFT/RIGHT/USER)。"""
    preset = (preset or "USER").upper()
    window, area, region = find_view3d()
    if window is None:
        return False, "没有找到 3D 视图"
    if preset == "USER":
        bpy.ops.view3d.view_axis(type="USER", align_active=False)
        return True, ""
    if preset == "CAMERA":
        bpy.ops.view3d.view_camera()
        return True, ""
    axis = PRESET_AXIS.get(preset)
    if axis is None:
        return False, f"未知预设视角: {preset}"
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.view3d.view_axis(type=axis, align_active=False)
    return True, ""


def toggle_perspective():
    """透视/正交切换。"""
    window, area, region = find_view3d()
    if window is None:
        return False, "没有找到 3D 视图"
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.view3d.view_persportho()
    return True, ""


def frame_selected():
    """视图聚焦选中物体:将视口对准当前选中的对象(无选中时等价于框选全部)。"""
    window, area, region = find_view3d()
    if window is None:
        return False, "没有找到 3D 视图"
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.view3d.view_selected(use_all_regions=False)
    return True, ""


def reset_view():
    """复位视图:回到首次连接时缓存的初始视图(旋转/位置/距离/透视)。"""
    global _home_view
    region = get_region3d()
    if region is None:
        return False, "没有找到 3D 视图"
    if _home_view is None:
        return False, "尚未记录复位基准视图"
    region.view_rotation = _home_view["rotate"].copy()
    region.view_location = _home_view["location"].copy()
    region.view_distance = _home_view["distance"]
    region.is_perspective = _home_view["perspective"]
    return True, ""


def set_shading(shading):
    """切换视口着色模式: SOLID / WIREFRAME / MATERIAL / RENDERED。"""
    shading = (shading or "SOLID").upper()
    valid = {"SOLID", "WIREFRAME", "MATERIAL", "RENDERED"}
    if shading not in valid:
        return False, f"未知着色模式: {shading}"
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces[0]
                if hasattr(space, "shading"):
                    space.shading.type = shading
    return True, ""


def view_snapshot(region):
    """当前视图状态(供状态快照)。"""
    if region is None:
        return None
    return {
        "perspective": bool(region.is_perspective),
        "distance": round(region.view_distance, 3),
        "rotation": [round(v, 4) for v in region.view_rotation],
        "location": [round(v, 3) for v in region.view_location],
    }
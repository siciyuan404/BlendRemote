"""控制面板按钮布局配置。

手机端控制面板的所有按钮(含触控板快捷栏)由本模块定义的布局驱动:
- 布局存插件偏好设置(跨会话持久),用户可在手机端编辑并回写。
- 每条按钮:{id, label, method, params, style}
  - method:命令名(如 "view3d.orbit"),对应 commands.REGISTRY
  - params:命令参数 dict
  - style:按钮样式(filled/outlined/text)
- 触控板页(touchpad)是特殊页:quick_buttons 为全屏手势区下方的快捷按钮栏。

数据流:
- 手机启动 → custom.layout_get → 本模块返回当前布局
- 手机编辑保存 → custom.layout_set(layout) → 本模块持久化
"""

import json

import bpy

DEFAULT_LAYOUT = {
    # 触控板页:按钮列表作为手势区下方的快捷按钮栏
    "touchpad": [
        {"id": "frame_all", "label": "框选全部", "method": "view3d.frame_all", "params": {}, "style": "filled"},
        {"id": "frame_sel", "label": "聚焦选中", "method": "view3d.frame_selected", "params": {}, "style": "filled"},
        {"id": "reset_view", "label": "复位", "method": "view3d.reset", "params": {}, "style": "filled"},
        {"id": "toggle_persp", "label": "透视", "method": "view3d.toggle_persp", "params": {}, "style": "outlined"},
        {"id": "shading_solid", "label": "着色", "method": "view3d.shading", "params": {"shading": "solid"}, "style": "outlined"},
        {"id": "shading_wire", "label": "线框", "method": "view3d.shading", "params": {"shading": "wireframe"}, "style": "outlined"},
        {"id": "preset_front", "label": "前", "method": "view3d.preset", "params": {"preset": "front"}, "style": "outlined"},
        {"id": "preset_top", "label": "顶", "method": "view3d.preset", "params": {"preset": "top"}, "style": "outlined"},
        {"id": "preset_camera", "label": "相机", "method": "view3d.preset", "params": {"preset": "camera"}, "style": "outlined"},
    ],
    # 视图页
    "view": [
        {"id": "orbit", "label": "旋转", "method": "view3d.orbit", "params": {}, "style": "filled"},
        {"id": "pan", "label": "平移", "method": "view3d.pan", "params": {}, "style": "outlined"},
        {"id": "zoom", "label": "缩放", "method": "view3d.zoom", "params": {}, "style": "outlined"},
        {"id": "frame_all", "label": "框选全部", "method": "view3d.frame_all", "params": {}, "style": "filled"},
        {"id": "frame_sel", "label": "聚焦选中", "method": "view3d.frame_selected", "params": {}, "style": "filled"},
        {"id": "reset_view", "label": "复位", "method": "view3d.reset", "params": {}, "style": "filled"},
        {"id": "toggle_persp", "label": "透视切换", "method": "view3d.toggle_persp", "params": {}, "style": "outlined"},
        {"id": "shading_solid", "label": "实体", "method": "view3d.shading", "params": {"shading": "solid"}, "style": "outlined"},
        {"id": "shading_wire", "label": "线框", "method": "view3d.shading", "params": {"shading": "wireframe"}, "style": "outlined"},
        {"id": "shading_mat", "label": "材质", "method": "view3d.shading", "params": {"shading": "material"}, "style": "outlined"},
        {"id": "shading_rendered", "label": "渲染", "method": "view3d.shading", "params": {"shading": "rendered"}, "style": "outlined"},
        {"id": "preset_front", "label": "前", "method": "view3d.preset", "params": {"preset": "front"}, "style": "outlined"},
        {"id": "preset_back", "label": "后", "method": "view3d.preset", "params": {"preset": "back"}, "style": "outlined"},
        {"id": "preset_left", "label": "左", "method": "view3d.preset", "params": {"preset": "left"}, "style": "outlined"},
        {"id": "preset_right", "label": "右", "method": "view3d.preset", "params": {"preset": "right"}, "style": "outlined"},
        {"id": "preset_top", "label": "顶", "method": "view3d.preset", "params": {"preset": "top"}, "style": "outlined"},
        {"id": "preset_bottom", "label": "底", "method": "view3d.preset", "params": {"preset": "bottom"}, "style": "outlined"},
        {"id": "preset_camera", "label": "相机", "method": "view3d.preset", "params": {"preset": "camera"}, "style": "outlined"},
    ],
    # 对象页
    "object": [
        {"id": "mode_object", "label": "对象模式", "method": "mode.set", "params": {"mode": "OBJECT"}, "style": "filled"},
        {"id": "mode_edit", "label": "编辑模式", "method": "mode.set", "params": {"mode": "EDIT"}, "style": "filled"},
        {"id": "mode_sculpt", "label": "雕刻", "method": "mode.set", "params": {"mode": "SCULPT"}, "style": "filled"},
        {"id": "mode_pose", "label": "姿态", "method": "mode.set", "params": {"mode": "POSE"}, "style": "filled"},
        {"id": "toggle_edit", "label": "切换编辑", "method": "mode.toggle_edit", "params": {}, "style": "outlined"},
        {"id": "add_cube", "label": "立方体", "method": "object.add", "params": {"type": "Cube"}, "style": "outlined"},
        {"id": "add_sphere", "label": "球体", "method": "object.add", "params": {"type": "Sphere"}, "style": "outlined"},
        {"id": "add_cylinder", "label": "圆柱", "method": "object.add", "params": {"type": "Cylinder"}, "style": "outlined"},
        {"id": "add_plane", "label": "平面", "method": "object.add", "params": {"type": "Plane"}, "style": "outlined"},
        {"id": "add_light", "label": "灯光", "method": "object.add", "params": {"type": "Light"}, "style": "outlined"},
        {"id": "add_empty", "label": "空物体", "method": "object.add", "params": {"type": "Empty"}, "style": "outlined"},
        {"id": "delete", "label": "删除", "method": "object.delete", "params": {}, "style": "filled"},
        {"id": "duplicate", "label": "复制", "method": "object.duplicate", "params": {}, "style": "outlined"},
        {"id": "select_all", "label": "全选", "method": "object.select_all", "params": {}, "style": "outlined"},
    ],
    # 动画页
    "anim": [
        {"id": "play", "label": "▶ 播放", "method": "anim.play", "params": {}, "style": "filled"},
        {"id": "pause", "label": "⏸ 暂停", "method": "anim.pause", "params": {}, "style": "filled"},
        {"id": "goto_start", "label": "⏮ 首帧", "method": "anim.goto_start", "params": {}, "style": "outlined"},
        {"id": "goto_end", "label": "末帧 ⏭", "method": "anim.goto_end", "params": {}, "style": "outlined"},
        {"id": "step_back", "label": "◀", "method": "anim.frame_step", "params": {"delta": -1}, "style": "outlined"},
        {"id": "step_fwd", "label": "▶", "method": "anim.frame_step", "params": {"delta": 1}, "style": "outlined"},
        {"id": "key_insert", "label": "关键帧", "method": "anim.keyframe_insert", "params": {}, "style": "filled"},
        {"id": "key_prev", "label": "◀ 上关键帧", "method": "anim.keyframe_prev", "params": {}, "style": "outlined"},
        {"id": "key_next", "label": "下关键帧 ▶", "method": "anim.keyframe_next", "params": {}, "style": "outlined"},
    ],
    # 渲染页
    "render": [
        {"id": "still", "label": "渲染当前帧", "method": "render.still", "params": {}, "style": "filled"},
        {"id": "animation", "label": "渲染动画", "method": "render.animation", "params": {}, "style": "filled"},
        {"id": "cancel", "label": "取消", "method": "render.cancel", "params": {}, "style": "filled"},
        {"id": "engine_eevee", "label": "EEVEE", "method": "render.engine", "params": {"engine": "EEVEE"}, "style": "outlined"},
        {"id": "engine_cycles", "label": "Cycles", "method": "render.engine", "params": {"engine": "CYCLES"}, "style": "outlined"},
        {"id": "engine_workbench", "label": "工作台", "method": "render.engine", "params": {"engine": "WORKBENCH"}, "style": "outlined"},
    ],
    # 自定义页(用户可增删)
    "custom": [],
}


def _prefs():
    addon = bpy.context.preferences.addons.get(__package__.split(".")[0])
    if addon is None:
        return None
    return getattr(addon, "preferences", None)


def _layout_json():
    prefs = _prefs()
    if prefs is None:
        return None
    return getattr(prefs, "control_layout_json", "")


def get_layout():
    """返回当前布局 dict;未配置时返回默认布局。"""
    raw = _layout_json()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    return json.loads(json.dumps(DEFAULT_LAYOUT))


def set_layout(layout):
    """保存布局 dict 到偏好设置。返回 (ok, err)。"""
    prefs = _prefs()
    if prefs is None:
        return False, "插件偏好设置不可用"
    try:
        prefs.control_layout_json = json.dumps(layout, ensure_ascii=False)
    except Exception as e:
        return False, f"保存布局失败: {e}"
    return True, ""


def reset_layout():
    """恢复默认布局。"""
    prefs = _prefs()
    if prefs is None:
        return False, "插件偏好设置不可用"
    prefs.control_layout_json = json.dumps(DEFAULT_LAYOUT, ensure_ascii=False)
    return True, ""


def list_tabs():
    """返回布局中的所有页名(顺序),触控板页固定在最前。"""
    layout = get_layout()
    tabs = list(layout.keys())
    if "touchpad" in tabs:
        tabs.remove("touchpad")
        tabs.insert(0, "touchpad")
    return tabs

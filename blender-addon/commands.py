"""命令注册与分发。

手机端通过 TCP → Rust 服务 → 插件本地桥 → dispatch() 执行 Blender 操作。
所有命令在 Blender 主线程执行(bpy.app.timers 驱动),本模块只定义纯函数。
"""

import math

import bpy
import mathutils

from . import navigation
from . import custom_buttons


def _ok(result=None):
    return {"ok": True, "result": result if result is not None else {}}


def _err(error):
    return {"ok": False, "error": str(error)}


# ============================================================================
# 视图控制
# ============================================================================

def cmd_view_orbit(params):
    region = navigation.get_region3d()
    if region is None:
        return _err("没有找到 3D 视图")
    navigation.orbit(region, float(params.get("dx", 0)), float(params.get("dy", 0)))
    return _ok()


def cmd_view_pan(params):
    region = navigation.get_region3d()
    if region is None:
        return _err("没有找到 3D 视图")
    navigation.pan(region, float(params.get("dx", 0)), float(params.get("dy", 0)))
    return _ok()


def cmd_view_zoom(params):
    region = navigation.get_region3d()
    if region is None:
        return _err("没有找到 3D 视图")
    navigation.zoom(region, float(params.get("delta", 0)))
    return _ok()


def cmd_view_preset(params):
    ok, err = navigation.view_preset(params.get("preset", "USER"))
    if ok:
        return _ok()
    return _err(err)


def cmd_view_toggle_persp(params):
    ok, err = navigation.toggle_perspective()
    if ok:
        return _ok()
    return _err(err)


def cmd_view_shading(params):
    ok, err = navigation.set_shading(params.get("mode", "SOLID"))
    if ok:
        return _ok()
    return _err(err)


def cmd_view_frame_all(params):
    window, area, region = navigation.find_view3d()
    if window is None:
        return _err("没有找到 3D 视图")
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.view3d.view_all(center=True)
    return _ok()


# ============================================================================
# 模式切换
# ============================================================================

def cmd_mode_set(params):
    mode = (params.get("mode") or "OBJECT").upper()
    valid = {
        "OBJECT", "EDIT", "SCULPT", "VERTEX_PAINT", "WEIGHT_PAINT",
        "TEXTURE_PAINT", "GPENCIL", "POSE",
    }
    if mode not in valid:
        return _err(f"未知模式: {mode}")
    if bpy.context.active_object is None:
        return _err("没有活动对象")
    if mode == "EDIT":
        # 编辑模式需要对象类型支持
        if bpy.context.active_object.type not in {"MESH", "CURVE", "SURFACE", "TEXT", "LATTICE", "META", "ARMATURE", "GPENCIL"}:
            return _err(f"{bpy.context.active_object.type} 类型不支持编辑模式")
    bpy.ops.object.mode_set(mode=mode)
    return _ok()


def cmd_mode_toggle_edit(params):
    if bpy.context.active_object is None:
        return _err("没有活动对象")
    if bpy.context.mode == "OBJECT":
        if bpy.context.active_object.type not in {"MESH", "CURVE", "SURFACE", "TEXT", "LATTICE", "META", "ARMATURE", "GPENCIL"}:
            return _err(f"{bpy.context.active_object.type} 类型不支持编辑模式")
        bpy.ops.object.mode_set(mode="EDIT")
    else:
        bpy.ops.object.mode_set(mode="OBJECT")
    return _ok()


# ============================================================================
# 对象操作
# ============================================================================

OBJECT_ADD_TYPES = {
    "CUBE": "cube",
    "SPHERE": "uvsphere",
    "PLANE": "plane",
    "CYLINDER": "cylinder",
    "CONE": "cone",
    "TORUS": "torus",
    "MONKEY": "monkey",
    "CIRCLE": "circle",
    "GRID": "grid",
    "ICOSPHERE": "ico_sphere",
    "EMPTY": None,
    "LIGHT": None,
    "CAMERA": None,
}


def cmd_object_add(params):
    obj_type = (params.get("type") or "CUBE").upper()
    if obj_type in {"LIGHT", "CAMERA"}:
        if obj_type == "LIGHT":
            bpy.ops.object.light_add(type="POINT")
        else:
            bpy.ops.object.camera_add()
        return _ok()
    if obj_type == "EMPTY":
        bpy.ops.object.empty_add(type="PLAIN_AXES")
        return _ok()
    mesh_type = OBJECT_ADD_TYPES.get(obj_type)
    if mesh_type is None:
        return _err(f"未知对象类型: {obj_type}")
    op = getattr(bpy.ops.mesh, f"primitive_{mesh_type}_add")
    op()
    return _ok()


def cmd_object_delete(params):
    if bpy.context.selected_objects:
        bpy.ops.object.delete(use_global=False)
        return _ok()
    return _err("没有选中的对象")


def cmd_object_duplicate(params):
    if bpy.context.selected_objects:
        bpy.ops.object.duplicate()
        return _ok()
    return _err("没有选中的对象")


def cmd_object_select_all(params):
    action = (params.get("action") or "SELECT").upper()
    valid = {"SELECT", "DESELECT", "INVERT"}
    if action not in valid:
        return _err(f"未知选择动作: {action}")
    if bpy.context.mode == "OBJECT":
        for obj in bpy.data.objects:
            if action == "SELECT":
                obj.select_set(True)
            elif action == "DESELECT":
                obj.select_set(False)
            else:
                obj.select_set(not obj.select_get())
    else:
        # 编辑模式:按元素选择
        bpy.ops.mesh.select_all(action=action)
    return _ok()


def cmd_object_select_by_name(params):
    name = params.get("name", "")
    if not name:
        return _err("缺少对象名")
    obj = bpy.data.objects.get(name)
    if obj is None:
        return _err(f"对象不存在: {name}")
    for other in bpy.data.objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return _ok()


def cmd_object_transform(params):
    """直接变换:tool = MOVE / ROTATE / SCALE,delta_x/y/z 为增量。

    对象级变换(编辑模式/姿态模式等复杂场景暂不处理)。
    """
    tool = (params.get("tool") or "MOVE").upper()
    if tool not in {"MOVE", "ROTATE", "SCALE"}:
        return _err(f"未知变换工具: {tool}")
    obj = bpy.context.active_object
    if obj is None:
        return _err("没有活动对象")
    dx = float(params.get("delta_x", 0))
    dy = float(params.get("delta_y", 0))
    dz = float(params.get("delta_z", 0))
    if tool == "MOVE":
        obj.location.x += dx
        obj.location.y += dy
        obj.location.z += dz
    elif tool == "ROTATE":
        obj.rotation_euler.x += math.radians(dx)
        obj.rotation_euler.y += math.radians(dy)
        obj.rotation_euler.z += math.radians(dz)
    else:
        scale = (1.0 + dx * 0.01, 1.0 + dy * 0.01, 1.0 + dz * 0.01)
        obj.scale[0] *= scale[0]
        obj.scale[1] *= scale[1]
        obj.scale[2] *= scale[2]
    return _ok()


def cmd_object_visibility(params):
    vis = (params.get("visibility") or "HIDE").upper()
    if vis not in {"HIDE", "SHOW", "ISOLATE"}:
        return _err(f"未知可见性动作: {vis}")
    if vis == "HIDE":
        for obj in bpy.context.selected_objects:
            obj.hide_set(True)
    elif vis == "SHOW":
        for obj in bpy.data.objects:
            obj.hide_set(False)
    else:
        # 隔离:隐藏所有未选中对象
        for obj in bpy.data.objects:
            obj.hide_set(not obj.select_get())
    return _ok()


def cmd_object_origin(params):
    if bpy.context.selected_objects:
        bpy.ops.object.origin_set(type="ORIGIN_CENTER_OF_MASS")
        return _ok()
    return _err("没有选中的对象")


# ============================================================================
# 动画控制
# ============================================================================

def cmd_anim_play(params):
    bpy.ops.screen.animation_play()
    return _ok()


def cmd_anim_pause(params):
    if bpy.context.screen.is_animation_playing:
        bpy.ops.screen.animation_play()
    return _ok()


def cmd_anim_frame_jump(params):
    frame = params.get("frame")
    if frame is None:
        return _err("缺少 frame")
    bpy.context.scene.frame_set(int(frame))
    return _ok()


def cmd_anim_frame_step(params):
    delta = int(params.get("delta", 1))
    bpy.context.scene.frame_current += delta
    return _ok({"frame": bpy.context.scene.frame_current})


def cmd_anim_goto_start(params):
    bpy.context.scene.frame_set(bpy.context.scene.frame_start)
    return _ok()


def cmd_anim_goto_end(params):
    bpy.context.scene.frame_set(bpy.context.scene.frame_end)
    return _ok()


def cmd_anim_keyframe_insert(params):
    obj = bpy.context.active_object
    if obj is None:
        return _err("没有活动对象")
    bpy.ops.anim.keyframe_insert_menu(type="LocRotScale")
    return _ok()


def cmd_anim_keyframe_prev(params):
    bpy.ops.screen.keyframe_jump(next=False)
    return _ok()


def cmd_anim_keyframe_next(params):
    bpy.ops.screen.keyframe_jump(next=True)
    return _ok()


# ============================================================================
# 渲染控制
# ============================================================================

def cmd_render_still(params):
    bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
    return _ok()


def cmd_render_animation(params):
    bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    return _ok()


def cmd_render_engine(params):
    engine = params.get("engine", "")
    valid = {
        "CYCLES": "CYCLES",
        "EEVEE": "BLENDER_EEVEE_NEXT",
        "EEVEE_LEGACY": "BLENDER_EEVEE",
        "WORKBENCH": "BLENDER_WORKBENCH",
    }
    mapped = valid.get(engine.upper())
    if mapped is None:
        return _err(f"未知渲染引擎: {engine}")
    bpy.context.scene.render.engine = mapped
    return _ok()


def cmd_render_resolution(params):
    pct = params.get("percentage")
    if pct is not None:
        bpy.context.scene.render.resolution_percentage = max(1, min(100, int(pct)))
    return _ok({"percentage": bpy.context.scene.render.resolution_percentage})


def cmd_render_samples(params):
    samples = params.get("samples")
    if samples is None:
        return _err("缺少 samples")
    scene = bpy.context.scene
    engine = scene.render.engine
    if engine == "CYCLES":
        scene.cycles.samples = max(1, int(samples))
    elif engine == "BLENDER_EEVEE_NEXT":
        scene.eevee.eevee_next.taa_render_samples = max(1, int(samples))
    elif engine == "BLENDER_EEVEE":
        scene.eevee.taa_render_samples = max(1, int(samples))
    else:
        return _err("当前引擎不支持采样数设置")
    return _ok()


def cmd_render_cancel(params):
    # 退出渲染模式(若有渲染进度)
    for area in bpy.context.screen.areas:
        if area.type == "IMAGE_EDITOR":
            pass
    bpy.ops.render.cancel()
    return _ok()


# ============================================================================
# 自定义按钮
# ============================================================================

def cmd_custom_list(params):
    return _ok({"buttons": custom_buttons.list_buttons()})


def cmd_custom_run(params):
    name = params.get("name") or params.get("index")
    ok, err = custom_buttons.run_button(name)
    if ok:
        return _ok()
    return _err(err)


def cmd_custom_save(params):
    name = (params.get("name") or "").strip()
    operator = (params.get("operator") or "").strip()
    if not name or not operator:
        return _err("name 和 operator 均不能为空")
    custom_buttons.save_button(name, operator)
    return _ok()


def cmd_custom_delete(params):
    name = params.get("name")
    if not name:
        return _err("缺少 name")
    custom_buttons.delete_button(name)
    return _ok()


# ============================================================================
# 通用 operator 执行(高级用户/测试用)
# ============================================================================

def cmd_operator(params):
    """执行任意 operator: {bl_idname: "object.delete", kwargs: {...}}"""
    bl_idname = params.get("bl_idname", "")
    if not bl_idname:
        return _err("缺少 bl_idname")
    kwargs = params.get("kwargs") or {}
    # 解析 "object.delete" → bpy.ops.object.delete
    parts = bl_idname.split(".")
    if len(parts) != 2:
        return _err(f"bl_idname 格式应为 module.opname: {bl_idname}")
    module = getattr(bpy.ops, parts[0], None)
    if module is None:
        return _err(f"未知 operator 模块: {parts[0]}")
    op = getattr(module, parts[1], None)
    if op is None:
        return _err(f"未知 operator: {bl_idname}")
    try:
        result = op(**kwargs)
        if _op_result_cancelled(result):
            return _err("操作被取消")
        return _ok()
    except TypeError as e:
        return _err(f"operator 参数错误: {e}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def _op_result_cancelled(result):
    try:
        if isinstance(result, set):
            return "CANCELLED" in result
        if hasattr(result, "CANCELLED"):
            return bool(result.CANCELLED) and not bool(result.FINISHED)
    except Exception:
        pass
    return False


# ============================================================================
# 分发表
# ============================================================================

REGISTRY = {
    "view3d.orbit": cmd_view_orbit,
    "view3d.pan": cmd_view_pan,
    "view3d.zoom": cmd_view_zoom,
    "view3d.preset": cmd_view_preset,
    "view3d.toggle_persp": cmd_view_toggle_persp,
    "view3d.shading": cmd_view_shading,
    "view3d.frame_all": cmd_view_frame_all,
    "mode.set": cmd_mode_set,
    "mode.toggle_edit": cmd_mode_toggle_edit,
    "object.add": cmd_object_add,
    "object.delete": cmd_object_delete,
    "object.duplicate": cmd_object_duplicate,
    "object.select_all": cmd_object_select_all,
    "object.select_by_name": cmd_object_select_by_name,
    "object.transform": cmd_object_transform,
    "object.visibility": cmd_object_visibility,
    "object.origin": cmd_object_origin,
    "anim.play": cmd_anim_play,
    "anim.pause": cmd_anim_pause,
    "anim.frame_jump": cmd_anim_frame_jump,
    "anim.frame_step": cmd_anim_frame_step,
    "anim.goto_start": cmd_anim_goto_start,
    "anim.goto_end": cmd_anim_goto_end,
    "anim.keyframe_insert": cmd_anim_keyframe_insert,
    "anim.keyframe_prev": cmd_anim_keyframe_prev,
    "anim.keyframe_next": cmd_anim_keyframe_next,
    "render.still": cmd_render_still,
    "render.animation": cmd_render_animation,
    "render.engine": cmd_render_engine,
    "render.resolution": cmd_render_resolution,
    "render.samples": cmd_render_samples,
    "render.cancel": cmd_render_cancel,
    "custom.list": cmd_custom_list,
    "custom.run": cmd_custom_run,
    "custom.save": cmd_custom_save,
    "custom.delete": cmd_custom_delete,
    "operator": cmd_operator,
}


def dispatch(method, params):
    """分发命令,返回 {"ok": bool, "result": ..., "error": str}。"""
    handler = REGISTRY.get(method)
    if handler is None:
        return _err(f"未知命令: {method}")
    try:
        return handler(params)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _err(f"{type(e).__name__}: {e}")
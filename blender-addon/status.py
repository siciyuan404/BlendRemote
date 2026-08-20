"""Blender 状态快照(供 Rust 服务周期轮询并广播给手机)。"""

import bpy

from . import navigation


def build_status():
    """构建状态快照 JSON 字典。所有字段尽力而为,失败返回 None。"""
    try:
        scene = bpy.context.scene
        region = navigation.get_region3d()

        mode = bpy.context.mode
        selected = [o for o in bpy.data.objects if o.select_get()]
        active = bpy.context.active_object

        status = {
            "blender_version": bpy.app.version_string,
            "scene": scene.name if scene else None,
            "mode": mode,
            "frame_current": scene.frame_current if scene else 0,
            "frame_start": scene.frame_start if scene else 0,
            "frame_end": scene.frame_end if scene else 0,
            "is_playing": bool(bpy.context.screen.is_animation_playing),
            "engine": scene.render.engine if scene else None,
            "selected_count": len(selected),
            "selected_names": [o.name for o in selected[:20]],
            "active_object": active.name if active else None,
            "view3d": navigation.view_snapshot(region),
            "shading": _shading_mode(),
            "resolution": {
                "x": scene.render.resolution_x if scene else 0,
                "y": scene.render.resolution_y if scene else 0,
                "percentage": scene.render.resolution_percentage if scene else 0,
            },
            "samples": _samples(scene),
            "custom_buttons": _custom_count(),
        }
        return status
    except Exception:
        import traceback
        traceback.print_exc()
        return None


def _shading_mode():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces[0]
                if hasattr(space, "shading"):
                    return space.shading.type
    return None


def _samples(scene):
    if scene is None:
        return 0
    engine = scene.render.engine
    try:
        if engine == "CYCLES":
            return scene.cycles.samples
        if engine == "BLENDER_EEVEE_NEXT":
            return scene.eevee.eevee_next.taa_render_samples
        if engine == "BLENDER_EEVEE":
            return scene.eevee.taa_render_samples
    except Exception:
        return 0
    return 0


def _custom_count():
    try:
        from . import custom_buttons
        return len(custom_buttons.list_buttons())
    except Exception:
        return 0
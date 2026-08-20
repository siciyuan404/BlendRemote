"""自定义按钮存储与执行。

按钮定义:{name, operator}(operator 为 bpy.ops 调用字符串,如
"object.delete" 或 "mesh.primitive_cube_add(size=2)")。
存储在插件偏好设置中,跨会话持久,手机端可增删。
"""

import bpy


def _prefs():
    return bpy.context.preferences.addons.get(__package__.split(".")[0])


def _buttons():
    prefs = _prefs()
    if prefs is None:
        return []
    import json
    try:
        return json.loads(prefs.custom_buttons_json or "[]")
    except (ValueError, TypeError):
        return []


def _save_buttons(buttons):
    import json
    prefs = _prefs()
    if prefs is None:
        return
    prefs.custom_buttons_json = json.dumps(buttons, ensure_ascii=False)


def list_buttons():
    """返回按钮列表 [{name, operator}]。"""
    return [{"name": b["name"], "operator": b["operator"]} for b in _buttons()]


def save_button(name, operator):
    buttons = _buttons()
    for b in buttons:
        if b["name"] == name:
            b["operator"] = operator
            _save_buttons(buttons)
            return
    buttons.append({"name": name, "operator": operator})
    _save_buttons(buttons)


def delete_button(name):
    buttons = [b for b in _buttons() if b["name"] != name]
    _save_buttons(buttons)


def run_button(name):
    """按名称执行自定义按钮。name 可以是按钮名或列表下标(数字字符串)。"""
    buttons = _buttons()
    button = None
    if isinstance(name, int) or (isinstance(name, str) and name.isdigit()):
        idx = int(name)
        if 0 <= idx < len(buttons):
            button = buttons[idx]
    else:
        for b in buttons:
            if b["name"] == name:
                button = b
                break
    if button is None:
        return False, f"找不到自定义按钮: {name}"
    return run_operator_str(button["operator"])


def run_operator_str(expr):
    """解析并执行 operator 调用字符串。

    支持格式:
    - "object.delete"
    - "mesh.primitive_cube_add(size=2, location=(1,0,0))"
    - "bpy.ops.object.duplicate()" / "object.duplicate()"
    """
    expr = expr.strip()
    if not expr:
        return False, "按钮 operator 为空"
    # 去掉 "bpy.ops." 前缀(容错)
    if expr.startswith("bpy.ops."):
        expr = expr[len("bpy.ops."):]
    # 去掉末尾分号
    if expr.endswith(";"):
        expr = expr[:-1]

    # 解析函数名与参数
    import ast
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return False, f"operator 语法错误: {e}"
    # 容忍无括号的裸 operator(如 object.delete → object.delete())
    if isinstance(tree.body, (ast.Attribute, ast.Name)):
        expr = f"{expr}()"
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            return False, f"operator 语法错误: {e}"
    if not isinstance(tree.body, ast.Call):
        return False, "operator 必须是调用表达式,如 object.delete"
    call = tree.body
    if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name):
        return False, "operator 必须是 module.opname 形式"
    module_name = call.func.value.id
    op_name = call.func.attr

    # 求值参数(只允许字面量)
    try:
        kwargs = {
            kw.arg: _literal_eval(kw.value)
            for kw in call.keywords if kw.arg is not None
        }
    except ValueError as e:
        return False, f"operator 参数仅支持字面量: {e}"

    module = getattr(bpy.ops, module_name, None)
    if module is None:
        return False, f"未知 operator 模块: {module_name}"
    op = getattr(module, op_name, None)
    if op is None:
        return False, f"未知 operator: {module_name}.{op_name}"
    try:
        result = op(**kwargs)
        if _is_cancelled(result):
            return False, "操作被取消"
        return True, ""
    except TypeError as e:
        return False, f"operator 参数错误: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _is_cancelled(result):
    """判断 bpy.ops 返回值是否为取消。

    bpy.ops 返回 set(如 {'FINISHED'} / {'CANCELLED'}),或带 FINISHED/CANCELLED
    属性的对象(测试桩)。两者都兼容处理。
    """
    try:
        if isinstance(result, set):
            return "CANCELLED" in result
        if hasattr(result, "CANCELLED"):
            return bool(result.CANCELLED) and not bool(result.FINISHED)
    except Exception:
        pass
    return False


def _literal_eval(node):
    """安全求值 AST 字面量(数字/字符串/元组/列表/字典/布尔/None)。"""
    import ast
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_literal_eval(e) for e in node.elts)
    if isinstance(node, ast.List):
        return [_literal_eval(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _literal_eval(k): _literal_eval(v)
            for k, v in zip(node.keys, node.values)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_literal_eval(node.operand)
    raise ValueError(f"不支持的参数: {type(node).__name__}")
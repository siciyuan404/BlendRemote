@file:OptIn(ExperimentalLayoutApi::class, ExperimentalFoundationApi::class)

package com.blendremote.client.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.calculateCentroid
import androidx.compose.foundation.gestures.calculateCentroidSize
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.ViewInAr
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.blendremote.client.BlendRemoteViewModel
import com.blendremote.client.ControlLayout
import com.blendremote.client.LayoutButton
import com.blendremote.client.TAB_TOUCHPAD
import kotlin.math.abs
import kotlin.math.roundToInt

/** 触控板 tab 页名固定在最前,其余按此顺序(缺失页自动跳过) */
private val TAB_ORDER = listOf(
    TAB_TOUCHPAD, "view", "object", "anim", "render", "custom",
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ControlScreen(
    vm: BlendRemoteViewModel,
    onDisconnect: () -> Unit,
) {
    val layout by vm.controlLayout.collectAsState()
    val renderState by vm.renderState.collectAsState()
    val tabs = layout?.keys?.toList()?.sortedBy { TAB_ORDER.indexOf(it).let { i -> if (i < 0) 999 else i } } ?: emptyList()

    var tab by remember { mutableStateOf(TAB_TOUCHPAD) }
    var editing by remember { mutableStateOf(false) }
    var editingButton by remember { mutableStateOf<LayoutButton?>(null) }

    // 首次连接后拉取布局;若尚未加载,显示加载中
    val currentLayout = layout
    if (currentLayout == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
        return
    }

    // 当前 tab 的按钮
    val currentButtons = currentLayout[tab] ?: emptyList()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (editing) "编辑布局" else "Blender 控制台") },
                actions = {
                    if (editing) {
                        TextButton(onClick = { editing = false }) { Text("完成") }
                    } else {
                        TextButton(onClick = onDisconnect) { Text("断开") }
                    }
                },
            )
        },
        bottomBar = {
            if (tabs.isNotEmpty()) {
                NavigationBar {
                    tabs.forEach { t ->
                        NavigationBarItem(
                            selected = tab == t,
                            onClick = { tab = t },
                            icon = {
                                if (t == TAB_TOUCHPAD) Icon(Icons.Default.ViewInAr, null)
                                else Text(t.first().uppercase())
                            },
                            label = { Text(tabLabel(t)) },
                        )
                    }
                }
            }
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            if (renderState.running) {
                LinearProgressIndicator(
                    progress = { renderState.percent.coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "渲染中 ${renderState.frame}/${renderState.frameTotal} (${(renderState.percent * 100).roundToInt()}%)",
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                )
            }

            if (tab == TAB_TOUCHPAD) {
                TouchpadPage(vm, currentButtons, editing, onEditLayout = { editing = true }, onRequestEdit = { editingButton = it })
            } else {
                ButtonsPage(vm, currentButtons, editing, onEditLayout = { editing = true }, onRequestEdit = { editingButton = it })
            }

            if (editing) {
                EditBar(
                    vm = vm,
                    tab = tab,
                    layout = currentLayout,
                    onAdd = {
                        // 添加默认按钮到当前页
                        val updated = currentLayout.toMutableMap()
                        val list = (updated[tab] ?: emptyList()).toMutableList()
                        val newBtn = LayoutButton(
                            id = "btn_${System.currentTimeMillis()}",
                            label = "新按钮",
                            method = "object.add",
                            params = org.json.JSONObject().put("type", "Cube"),
                            style = "outlined",
                        )
                        list.add(newBtn)
                        updated[tab] = list
                        vm.saveLayout(updated)
                        editingButton = newBtn
                    },
                )
            }
        }
    }

    // 按钮编辑对话框
    editingButton?.let { btn ->
        EditButtonDialog(
            btn = btn,
            onDismiss = { editingButton = null },
            onSave = { label, method ->
                updateButton(vm, btn) { it.copy(label = label, method = method) }
                editingButton = null
            },
        )
    }
}

private fun tabLabel(tab: String): String = when (tab) {
    TAB_TOUCHPAD -> "触控板"
    "view" -> "视图"
    "object" -> "对象"
    "anim" -> "动画"
    "render" -> "渲染"
    "custom" -> "自定义"
    else -> tab
}

// ==================== 触控板页(全屏手势 + 快捷按钮栏) ====================

@Composable
private fun TouchpadPage(
    vm: BlendRemoteViewModel,
    buttons: List<LayoutButton>,
    editing: Boolean,
    onEditLayout: () -> Unit,
    onRequestEdit: (LayoutButton) -> Unit,
) {
    Column(Modifier.fillMaxSize()) {
        // 全屏手势区
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .background(MaterialTheme.colorScheme.surfaceVariant)
                .pointerInput(Unit) {
                    var lastTapTime = 0L
                    var lastTapPos: Offset? = null
                    awaitEachGesture {
                        val down = awaitFirstDown()
                        val downTime = down.uptimeMillis
                        val downPos = down.position
                        var lastCentroid: Offset? = null
                        var lastDist = 0f
                        var initialDist = 0f
                        var accPan = 0.0
                        var mode = 0 // 0=未定 1=旋转 2=平移 3=缩放
                        var wasMulti = false
                        var endPos: Offset? = null
                        var endTime = downTime

                        while (true) {
                            val event = awaitPointerEvent()
                            val pressed = event.changes.count { it.pressed }
                            if (pressed == 0) {
                                endPos = event.calculateCentroid()
                                endTime = event.uptimeMillis
                                break
                            }
                            if (pressed >= 2) wasMulti = true

                            val centroid = event.calculateCentroid()
                            val dist = if (pressed >= 2) event.calculateCentroidSize() else 0f
                            val prevCentroid = lastCentroid
                            val prevDist = lastDist
                            lastCentroid = centroid
                            lastDist = dist

                            if (prevCentroid == null) {
                                initialDist = dist
                                continue
                            }

                            val dx = (centroid.x - prevCentroid.x).toDouble()
                            val dy = (centroid.y - prevCentroid.y).toDouble()
                            val distDelta = dist - prevDist

                            if (pressed == 1) {
                                mode = 1
                                if (dx != 0.0 || dy != 0.0) {
                                    vm.viewOrbit(dx * 0.4, dy * 0.4)
                                }
                            } else {
                                accPan += abs(dx) + abs(dy)
                                if (mode == 0) {
                                    val distRatio = if (initialDist > 1f) {
                                        abs(dist - initialDist) / initialDist
                                    } else 0f
                                    if (distRatio > 0.08f) {
                                        mode = 3
                                    } else if (accPan > 12f) {
                                        mode = 2
                                    }
                                }
                                when (mode) {
                                    2 -> if (dx != 0.0 || dy != 0.0) vm.viewPan(dx * 1.5, dy * 1.5)
                                    3 -> if (abs(distDelta) >= 0.3f) vm.viewZoom(distDelta * 0.06)
                                }
                            }
                        }

                        // 双击检测:单指、位移小、时间短 记为一次轻点;300ms 内再次轻点触发聚焦选中
                        val displacement = endPos?.let { (it - downPos).getDistance() } ?: Float.MAX_VALUE
                        if (!wasMulti && displacement < 30f && (endTime - downTime) < 250L) {
                            val doubleTap = (downTime - lastTapTime) < 300L &&
                                (lastTapPos?.let { (it - downPos).getDistance() < 60f } ?: false)
                            lastTapTime = downTime
                            lastTapPos = downPos
                            if (doubleTap) vm.viewFrameSelected()
                        }
                    }
                },
            contentAlignment = Alignment.Center,
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text("👆 单指旋转", fontWeight = FontWeight.Medium)
                Text("✌️ 双指拖动平移 · 双指捏合缩放", style = MaterialTheme.typography.bodySmall)
                Text("👆👆 双击聚焦选中物体", style = MaterialTheme.typography.bodySmall)
            }
        }

        // 底部快捷按钮栏(数据驱动)
        if (buttons.isNotEmpty()) {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(12.dp),
            ) {
                buttons.forEach { btn ->
                    LayoutButtonView(btn, vm, editing, onEditLayout, onRequestEdit)
                }
            }
        }
    }
}

// ==================== 普通按钮页(数据驱动,无滚动) ====================

@Composable
private fun ButtonsPage(
    vm: BlendRemoteViewModel,
    buttons: List<LayoutButton>,
    editing: Boolean,
    onEditLayout: () -> Unit,
    onRequestEdit: (LayoutButton) -> Unit,
) {
    if (buttons.isEmpty()) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(
                if (editing) "点击右上角 + 添加按钮" else "本页暂无按钮",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        return
    }
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp),
    ) {
        // 用 FlowRow 网格一屏排布,避免滚动
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            buttons.forEach { btn ->
                LayoutButtonView(btn, vm, editing, onEditLayout, onRequestEdit)
            }
        }
    }
}

// ==================== 数据驱动按钮 ====================

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun LayoutButtonView(
    btn: LayoutButton,
    vm: BlendRemoteViewModel,
    editing: Boolean,
    onEditLayout: () -> Unit,
    onRequestEdit: (LayoutButton) -> Unit = {},
) {
    val isFilled = btn.style == "filled"
    val shape = RoundedCornerShape(12.dp)
    val width = if (btn.label.length >= 6) 150.dp else 104.dp
    val containerColor = if (isFilled) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant
    val contentColor = if (isFilled) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface

    val onClick = if (editing) { { onRequestEdit(btn) } } else { { vm.sendFromLayout(btn) } }
    val onLongClick = if (editing) { { removeButton(vm, btn) } } else { onEditLayout }

    Surface(
        modifier = Modifier
            .width(width)
            .height(56.dp)
            .combinedClickable(onClick = onClick, onLongClick = onLongClick),
        shape = shape,
        color = containerColor,
        contentColor = contentColor,
        border = if (!isFilled) BorderStroke(1.dp, MaterialTheme.colorScheme.outline) else null,
    ) {
        Box(Modifier.fillMaxSize().padding(horizontal = 8.dp), contentAlignment = Alignment.Center) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (editing) {
                    Icon(Icons.Default.Edit, null, modifier = Modifier.size(14.dp), tint = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(4.dp))
                }
                Text(btn.label, fontWeight = FontWeight.Medium, fontSize = 13.sp, maxLines = 1)
            }
        }
    }
}

// ==================== 编辑操作 ====================

private fun removeButton(vm: BlendRemoteViewModel, btn: LayoutButton) {
    val current = vm.controlLayout.value ?: return
    val updated = current.toMutableMap()
    for ((tab, list) in updated) {
        val newList = list.filterNot { it.id == btn.id }
        if (newList.size != list.size) {
            updated[tab] = newList
            break
        }
    }
    vm.saveLayout(updated)
}

/** 更新指定按钮的字段(当前 tab) */
fun updateButton(vm: BlendRemoteViewModel, btn: LayoutButton, transform: (LayoutButton) -> LayoutButton) {
    val current = vm.controlLayout.value ?: return
    val updated = current.toMutableMap()
    for ((tab, list) in updated) {
        val newList = list.map {
            if (it.id == btn.id) transform(it) else it
        }
        if (newList.any { it.id == btn.id }) {
            updated[tab] = newList
            break
        }
    }
    vm.saveLayout(updated)
}

@Composable
private fun EditButtonDialog(
    btn: LayoutButton,
    onDismiss: () -> Unit,
    onSave: (String, String) -> Unit,
) {
    var label by remember { mutableStateOf(btn.label) }
    var method by remember { mutableStateOf(btn.method) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("编辑按钮") },
        text = {
            Column {
                OutlinedTextField(
                    value = label,
                    onValueChange = { label = it },
                    label = { Text("标签") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = method,
                    onValueChange = { method = it },
                    label = { Text("命令 (如 object.delete)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "命令需在插件 commands.REGISTRY 中注册",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            Button(onClick = { onSave(label, method) }) { Text("保存") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("取消") }
        },
    )
}

// ==================== 编辑工具栏 ====================

@Composable
private fun EditBar(
    vm: BlendRemoteViewModel,
    tab: String,
    layout: ControlLayout,
    onAdd: () -> Unit,
) {
    Surface(color = MaterialTheme.colorScheme.surfaceVariant) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "长按按钮删除 · 点击编辑",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f),
            )
            OutlinedButton(onClick = { vm.resetLayout() }, contentPadding = PaddingValues(horizontal = 10.dp)) {
                Text("恢复默认")
            }
            Spacer(Modifier.width(8.dp))
            FilledTonalButton(onClick = onAdd, contentPadding = PaddingValues(horizontal = 10.dp)) {
                Icon(Icons.Default.Add, null, modifier = Modifier.size(16.dp))
                Text("添加")
            }
        }
    }
}

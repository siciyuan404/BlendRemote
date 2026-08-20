@file:OptIn(ExperimentalLayoutApi::class)

package com.blendremote.client.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ViewInAr
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.blendremote.client.BlendRemoteViewModel
import com.blendremote.client.CustomButton
import kotlin.math.roundToInt

private val VIEW_PRESETS = listOf(
    "front", "back", "left", "right", "top", "bottom", "camera",
)
private val SHADINGS = listOf("wireframe", "solid", "material", "rendered")
private val MODES = listOf("OBJECT", "EDIT", "SCULPT", "POSE", "TEXTURE", "WEIGHT")
private val OBJECT_TYPES = listOf("Cube", "Sphere", "Cylinder", "Plane", "Empty", "Light")
private val RENDER_ENGINES = listOf("BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES")

enum class ControlTab(val label: String) {
    VIEW("视图"),
    OBJECT("对象"),
    ANIM("动画"),
    RENDER("渲染"),
    CUSTOM("自定义"),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ControlScreen(
    vm: BlendRemoteViewModel,
    onDisconnect: () -> Unit,
) {
    var tab by remember { mutableStateOf(ControlTab.VIEW) }
    val renderState by vm.renderState.collectAsState()
    val customButtons by vm.customButtons.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Blender 控制台") },
                actions = {
                    TextButton(onClick = onDisconnect) {
                        Text("断开")
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                ControlTab.entries.forEach { t ->
                    NavigationBarItem(
                        selected = tab == t,
                        onClick = { tab = t },
                        icon = {
                            if (t == ControlTab.VIEW) Icon(Icons.Default.ViewInAr, null) else Text(t.label.first().toString())
                        },
                        label = { Text(t.label) },
                    )
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
            when (tab) {
                ControlTab.VIEW -> ViewPanel(vm)
                ControlTab.OBJECT -> ObjectPanel(vm)
                ControlTab.ANIM -> AnimPanel(vm)
                ControlTab.RENDER -> RenderPanel(vm)
                ControlTab.CUSTOM -> CustomPanel(vm, customButtons)
            }
        }
    }
}

// ==================== 视图 ====================

@Composable
private fun ViewPanel(vm: BlendRemoteViewModel) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(rememberScrollState()),
    ) {
        // 手势板:单指拖动=旋转,双指捏合=缩放
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(260.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant)
                .pointerInput(Unit) {
                    detectTransformGestures { _, pan, zoom, _ ->
                        val dx = pan.x.toDouble()
                        val dy = pan.y.toDouble()
                        if (zoom != 1f) {
                            // 缩放
                            val delta = (zoom - 1f) * 100.0
                            vm.viewZoom(delta)
                        } else if (dx != 0.0 || dy != 0.0) {
                            // 旋转
                            vm.viewOrbit(dx * 0.3, dy * 0.3)
                        }
                    }
                },
            contentAlignment = Alignment.Center,
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text("👆 拖动旋转", fontWeight = FontWeight.Medium)
                Text("✌️ 双指缩放", style = MaterialTheme.typography.bodySmall)
            }
        }

        Spacer(Modifier.height(16.dp))

        Text("预设视角", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(8.dp))
        FlowRowButtons(VIEW_PRESETS) { vm.viewPreset(it) }

        Spacer(Modifier.height(12.dp))
        Text("着色方式", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(8.dp))
        FlowRowButtons(SHADINGS) { vm.viewShading(it) }

        Spacer(Modifier.height(12.dp))
        Row {
            Button(onClick = { vm.viewFrameAll() }, modifier = Modifier.weight(1f)) {
                Text("框选全部")
            }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { vm.viewTogglePersp() }, modifier = Modifier.weight(1f)) {
                Text("透视切换")
            }
        }
    }
}

@Composable
private fun FlowRowButtons(items: List<String>, onClick: (String) -> Unit) {
    // 自适应换行的按钮流(非滚动,避免与父 verticalScroll 嵌套导致崩溃)
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        items.forEach { item ->
            OutlinedButton(onClick = { onClick(item) }, modifier = Modifier.widthIn(min = 88.dp)) {
                Text(item)
            }
        }
    }
}

// ==================== 对象 ====================

@Composable
private fun ObjectPanel(vm: BlendRemoteViewModel) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(rememberScrollState()),
    ) {
        Text("模式", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(8.dp))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            MODES.forEach { mode ->
                OutlinedButton(onClick = { vm.setMode(mode) }, modifier = Modifier.widthIn(min = 96.dp)) {
                    Text(mode)
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        OutlinedButton(onClick = { vm.toggleEditMode() }, modifier = Modifier.fillMaxWidth()) {
            Text("切换编辑模式")
        }

        Spacer(Modifier.height(16.dp))
        Text("添加对象", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(8.dp))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            OBJECT_TYPES.forEach { type ->
                OutlinedButton(onClick = { vm.objectAdd(type) }, modifier = Modifier.widthIn(min = 96.dp)) {
                    Text(type)
                }
            }
        }

        Spacer(Modifier.height(16.dp))
        Text("编辑", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(8.dp))
        Row {
            Button(onClick = { vm.objectDelete() }, modifier = Modifier.weight(1f)) {
                Text("删除")
            }
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.objectDuplicate() }, modifier = Modifier.weight(1f)) {
                Text("复制")
            }
        }
        Spacer(Modifier.height(8.dp))
        Button(onClick = { vm.objectSelectAll() }, modifier = Modifier.fillMaxWidth()) {
            Text("全选")
        }
    }
}

// ==================== 动画 ====================

@Composable
private fun AnimPanel(vm: BlendRemoteViewModel) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(rememberScrollState()),
    ) {
        Row {
            Button(onClick = { vm.animGotoStart() }, modifier = Modifier.weight(1f)) {
                Text("⏮ 首帧")
            }
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.animStep(-1) }, modifier = Modifier.weight(1f)) {
                Text("◀")
            }
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.animPlay() }, modifier = Modifier.weight(1f)) {
                Text("▶ 播放")
            }
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.animStep(1) }, modifier = Modifier.weight(1f)) {
                Text("▶")
            }
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.animGotoEnd() }, modifier = Modifier.weight(1f)) {
                Text("末帧 ⏭")
            }
        }
        Spacer(Modifier.height(8.dp))
        Row {
            OutlinedButton(onClick = { vm.animPause() }, modifier = Modifier.weight(1f)) {
                Text("暂停")
            }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { vm.animKeyframeInsert() }, modifier = Modifier.weight(1f)) {
                Text("插入关键帧")
            }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { vm.animKeyframePrev() }, modifier = Modifier.weight(1f)) {
                Text("◀ 上一关键帧")
            }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { vm.animKeyframeNext() }, modifier = Modifier.weight(1f)) {
                Text("下一关键帧 ▶")
            }
        }
    }
}

// ==================== 渲染 ====================

@Composable
private fun RenderPanel(vm: BlendRemoteViewModel) {
    var frameInput by remember { mutableStateOf("") }
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(rememberScrollState()),
    ) {
        Row {
            Button(onClick = { vm.renderStill() }, modifier = Modifier.weight(1f)) {
                Text("渲染当前帧")
            }
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.renderAnimation() }, modifier = Modifier.weight(1f)) {
                Text("渲染动画")
            }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { vm.renderCancel() }, modifier = Modifier.weight(1f)) {
                Text("取消")
            }
        }

        Spacer(Modifier.height(16.dp))
        Text("渲染引擎", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(8.dp))
        FlowRowButtons(RENDER_ENGINES) { vm.renderEngine(it) }
    }
}

// ==================== 自定义按钮 ====================

@Composable
private fun CustomPanel(
    vm: BlendRemoteViewModel,
    customButtons: List<CustomButton>,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp),
    ) {
        Text(
            "自定义按钮来自 Blender 插件偏好设置中的配置",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))
        if (customButtons.isEmpty()) {
            Text(
                "暂无自定义按钮。在 Blender 插件 N 面板中添加。",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            LazyVerticalGrid(
                columns = GridCells.Fixed(2),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                items(customButtons) { btn ->
                    Button(
                        onClick = { vm.runCustomButton(btn.name) },
                        modifier = Modifier.fillMaxWidth().height(72.dp),
                        shape = RoundedCornerShape(10.dp),
                    ) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(btn.name, fontWeight = FontWeight.Medium)
                            Text(
                                btn.operator,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.8f),
                                maxLines = 1,
                            )
                        }
                    }
                }
            }
        }
    }
}
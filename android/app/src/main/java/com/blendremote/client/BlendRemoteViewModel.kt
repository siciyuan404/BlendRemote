package com.blendremote.client

import android.content.Context
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicReference

sealed class ConnectionState {
    object Disconnected : ConnectionState()
    object Connecting : ConnectionState()
    data class Connected(val serverAddr: String) : ConnectionState()
    data class Error(val message: String) : ConnectionState()
}

data class PairingRequiredState(
    val serverAddr: String,
    val clientName: String,
    val errorMessage: String? = null,
)

/** 自定义按钮(从服务端 custom.list 拉取) */
data class CustomButton(
    val name: String,
    val operator: String,
    val icon: String = "",
)

/** 布局按钮定义(数据驱动渲染) */
data class LayoutButton(
    val id: String,
    val label: String,
    val method: String,
    val params: JSONObject,
    val style: String = "outlined",
)

/** 控制面板布局:页名 → 按钮列表 */
typealias ControlLayout = Map<String, List<LayoutButton>>

const val TAB_TOUCHPAD = "touchpad"
const val TAB_VIEW = "view"
const val TAB_OBJECT = "object"
const val TAB_ANIM = "anim"
const val TAB_RENDER = "render"
const val TAB_CUSTOM = "custom"

/** 渲染任务进度状态 */
data class RenderState(
    val running: Boolean,
    val percent: Float = 0f,
    val frame: Int = 0,
    val frameTotal: Int = 0,
)

/** 地址规范化:去空格、去 scheme、裸 IP/主机名自动补默认端口 */
fun normalizeAddress(input: String, defaultPort: Int = 28900): String? {
    var s = input.trim()
    if (s.isEmpty()) return null
    s = s.removePrefix("http://").removePrefix("https://").trimEnd('/')
    if (s.isEmpty()) return null
    return if (s.contains(':')) {
        val host = s.substringBeforeLast(':')
        val port = s.substringAfterLast(':').toIntOrNull()
        if (host.isBlank() || port == null || port !in 1..65535) null else "$host:$port"
    } else {
        "$s:$defaultPort"
    }
}

class BlendRemoteViewModel : ViewModel() {

    companion object {
        private const val TAG = "BlendRemote/VM"
        private const val PREFS_NAME = "blendremote_client_prefs"
        private const val KEY_HISTORY = "history_addr"
        private const val KEY_LAST_ADDR = "last_addr"
        private const val MAX_HISTORY = 5
        private const val STATUS_POLL_MS = 1000L
    }

    private val _connectionState = MutableStateFlow<ConnectionState>(ConnectionState.Disconnected)
    val connectionState: StateFlow<ConnectionState> = _connectionState.asStateFlow()

    private val _pairingRequired = MutableStateFlow<PairingRequiredState?>(null)
    val pairingRequired: StateFlow<PairingRequiredState?> = _pairingRequired.asStateFlow()

    private val _pairingSubmitting = MutableStateFlow(false)
    val pairingSubmitting: StateFlow<Boolean> = _pairingSubmitting.asStateFlow()

    private val _historyAddresses = MutableStateFlow<List<String>>(emptyList())
    val historyAddresses: StateFlow<List<String>> = _historyAddresses.asStateFlow()

    private val _lastAddr = MutableStateFlow("")
    val lastAddr: StateFlow<String> = _lastAddr.asStateFlow()

    private val _discoveredServers = MutableStateFlow<Set<DiscoveredServer>>(emptySet())
    val discoveredServers: StateFlow<Set<DiscoveredServer>> = _discoveredServers.asStateFlow()

    /** 最新 Blender 状态快照(JSON 文本) */
    private val _blenderStatus = MutableStateFlow<JSONObject?>(null)
    val blenderStatus: StateFlow<JSONObject?> = _blenderStatus.asStateFlow()

    /** 渲染状态(从 status 快照派生) */
    private val _renderState = MutableStateFlow(RenderState(false))
    val renderState: StateFlow<RenderState> = _renderState.asStateFlow()

    private val _customButtons = MutableStateFlow<List<CustomButton>>(emptyList())
    val customButtons: StateFlow<List<CustomButton>> = _customButtons.asStateFlow()

    private val _updateState = MutableStateFlow<UpdateState>(UpdateState.Idle)
    val updateState: StateFlow<UpdateState> = _updateState.asStateFlow()

    private val _controlLayout = MutableStateFlow<ControlLayout?>(null)
    val controlLayout: StateFlow<ControlLayout?> = _controlLayout.asStateFlow()

    private var updateChecker: UpdateChecker? = null
    private var pendingApkPath: String? = null

    private var context: Context? = null
    private var mdnsDiscovery: MdnsDiscovery? = null

    @Volatile
    private var initialized: Boolean = false

    private var statusJob: Job? = null

    fun init(context: Context) {
        if (initialized) return
        initialized = true
        this.context = context.applicationContext
        loadHistory()

        try {
            val stateDir = context.applicationContext.filesDir.absolutePath
            NativeBridge.nativeSetStateDir(stateDir)
        } catch (e: UnsatisfiedLinkError) {
            Log.w(TAG, "nativeSetStateDir 失败: ${e.message}")
        }

        val discovery = MdnsDiscovery(context.applicationContext) { clientPubkeyB64() }
        mdnsDiscovery = discovery
        viewModelScope.launch {
            discovery.servers.collect { servers ->
                _discoveredServers.value = servers
            }
        }

        updateChecker = UpdateChecker(context.applicationContext)

        autoReconnectLastPc()
    }

    private fun loadHistory() {
        val prefs = context?.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val saved = prefs?.getStringSet(KEY_HISTORY, emptySet()) ?: emptySet()
        _historyAddresses.value = saved.toList().take(MAX_HISTORY)
        _lastAddr.value = prefs?.getString(KEY_LAST_ADDR, "") ?: ""
    }

    private fun saveHistory(address: String) {
        val prefs = context?.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val current = _historyAddresses.value.toMutableList()
        current.remove(address)
        current.add(0, address)
        val limited = current.take(MAX_HISTORY)
        _historyAddresses.value = limited
        _lastAddr.value = address
        prefs?.edit()?.apply {
            putStringSet(KEY_HISTORY, limited.toSet())
            putString(KEY_LAST_ADDR, address)
            apply()
        }
    }

    /** 读取本客户端公钥(供 serverinfo pair_status 查询);失败返回空串 */
    fun clientPubkeyB64(): String {
        return try {
            if (NativeBridge.isLoaded()) NativeBridge.nativeGetClientPubkeyB64() else ""
        } catch (e: UnsatisfiedLinkError) {
            ""
        }
    }

    fun startDiscovery() {
        mdnsDiscovery?.startDiscovery()
    }

    fun stopDiscovery() {
        mdnsDiscovery?.stopDiscovery()
    }

    private fun autoReconnectLastPc() {
        if (_connectionState.value != ConnectionState.Disconnected) return
        val addr = _lastAddr.value.takeIf { it.isNotBlank() } ?: return
        if (normalizeAddress(addr) == null) return

        _connectionState.value = ConnectionState.Connecting
        viewModelScope.launch(Dispatchers.IO) {
            if (!NativeBridge.isLoaded()) {
                _connectionState.value = ConnectionState.Disconnected
                return@launch
            }
            when (val result = nativeConnectAttempt(addr, "Android-Client")) {
                1 -> {
                    _connectionState.value = ConnectionState.Connected(addr)
                    saveHistory(addr)
                    startStatusPolling()
                }
                2 -> {
                    _connectionState.value = ConnectionState.Disconnected
                    _pairingRequired.value = PairingRequiredState(addr, "Android-Client")
                }
                else -> {
                    _connectionState.value = ConnectionState.Disconnected
                }
            }
        }
    }

    private suspend fun nativeConnectAttempt(addr: String, clientName: String): Int? =
        withContext(Dispatchers.Default) {
            val ref = AtomicReference<Int?>(null)
            val thread = Thread {
                try {
                    ref.set(NativeBridge.nativeConnect(addr, clientName))
                } catch (e: UnsatisfiedLinkError) {
                    Log.e(TAG, "Native 错误", e)
                    ref.set(0)
                }
            }
            thread.start()
            thread.join(15000)
            if (thread.isAlive) {
                thread.interrupt()
                null
            } else {
                ref.get()
            }
        }

    fun connect(serverAddr: String, clientName: String = "Android-Client") {
        if (_connectionState.value is ConnectionState.Connecting) return
        if (_pairingSubmitting.value) return

        val normalized = normalizeAddress(serverAddr)
        if (normalized == null) {
            _connectionState.value = ConnectionState.Error("地址格式无效,示例:192.168.1.12 或 192.168.1.12:28900")
            return
        }

        // 尝试连接即记录到历史(下次直接点击复用,无需重输)
        saveHistory(normalized)
        _connectionState.value = ConnectionState.Connecting
        _pairingRequired.value = null

        viewModelScope.launch(Dispatchers.IO) {
            if (!NativeBridge.isLoaded()) {
                _connectionState.value = ConnectionState.Error("libblendremote.so 未加载")
                return@launch
            }
            handleConnectResult(nativeConnectAttempt(normalized, clientName), normalized, clientName)
        }
    }

    private fun handleConnectResult(result: Int?, addr: String, clientName: String) {
        when (result) {
            null -> _connectionState.value = ConnectionState.Error("连接超时,请检查地址或网络")
            1 -> {
                _connectionState.value = ConnectionState.Connected(addr)
                saveHistory(addr)
                startStatusPolling()
            }
            2 -> {
                _connectionState.value = ConnectionState.Disconnected
                _pairingRequired.value = PairingRequiredState(addr, clientName)
            }
            3 -> _connectionState.value = ConnectionState.Error("地址格式无效,示例:192.168.1.12 或 192.168.1.12:28900")
            4 -> _connectionState.value = ConnectionState.Error("无法连接到 PC(超时):请确认 PC 在线、与手机在同一网络")
            5 -> _connectionState.value = ConnectionState.Error("连接被拒绝:blendremote-server 未启动或端口错误(默认 28900)")
            else -> _connectionState.value = ConnectionState.Error("连接失败,检查地址或防火墙")
        }
    }

    fun completePairing(pin: String) {
        val pending = _pairingRequired.value ?: return
        if (_pairingSubmitting.value) return

        _pairingSubmitting.value = true
        viewModelScope.launch(Dispatchers.IO) {
            val result = nativeCompletePairingAttempt(pin)
            _pairingSubmitting.value = false
            when (result) {
                1 -> {
                    _pairingRequired.value = null
                    _connectionState.value = ConnectionState.Connected(pending.serverAddr)
                    saveHistory(pending.serverAddr)
                    startStatusPolling()
                }
                6 -> _pairingRequired.value = pending.copy(errorMessage = "PIN 不正确,请重新输入")
                7 -> _pairingRequired.value = pending.copy(errorMessage = "配对响应超时,请重试")
                else -> {
                    _pairingRequired.value = null
                    _connectionState.value = ConnectionState.Error("配对连接已断开,请重新连接")
                }
            }
        }
    }

    private suspend fun nativeCompletePairingAttempt(pin: String): Int? =
        withContext(Dispatchers.Default) {
            val ref = AtomicReference<Int?>(null)
            val thread = Thread {
                try {
                    ref.set(NativeBridge.nativeCompletePairing(pin))
                } catch (e: UnsatisfiedLinkError) {
                    Log.e(TAG, "Native 错误", e)
                    ref.set(0)
                }
            }
            thread.start()
            thread.join(12000)
            if (thread.isAlive) {
                thread.interrupt()
                null
            } else {
                ref.get()
            }
        }

    fun cancelPairing() {
        viewModelScope.launch(Dispatchers.IO) {
            try {
                NativeBridge.nativeCancelPairing()
            } catch (e: UnsatisfiedLinkError) {
                Log.w(TAG, "nativeCancelPairing 失败: ${e.message}")
            }
            _pairingRequired.value = null
            _pairingSubmitting.value = false
            _connectionState.value = ConnectionState.Disconnected
        }
    }

    fun disconnect() {
        statusJob?.cancel()
        statusJob = null
        viewModelScope.launch(Dispatchers.IO) {
            try {
                NativeBridge.nativeDisconnect()
            } catch (e: UnsatisfiedLinkError) {
                Log.w(TAG, "nativeDisconnect 失败: ${e.message}")
            }
        }
        _connectionState.value = ConnectionState.Disconnected
    }

    // ==================== 状态轮询 ====================

    private fun startStatusPolling() {
        fetchLayout()
        statusJob?.cancel()
        statusJob = viewModelScope.launch(Dispatchers.IO) {
            while (_connectionState.value is ConnectionState.Connected) {
                try {
                    val status = NativeBridge.pollStatus()
                    if (status != null) {
                        _blenderStatus.value = status
                        updateRenderState(status)
                        refreshCustomButtons(status)
                    }
                } catch (e: UnsatisfiedLinkError) {
                    break
                }
                delay(STATUS_POLL_MS)
            }
        }
    }

    private fun updateRenderState(status: JSONObject) {
        val render = status.optJSONObject("render")
        if (render == null) {
            _renderState.value = RenderState(false)
            return
        }
        val running = render.optBoolean("running", false)
        _renderState.value = RenderState(
            running = running,
            percent = render.optDouble("percent", 0.0).toFloat(),
            frame = render.optInt("frame", 0),
            frameTotal = render.optInt("frame_total", 0),
        )
    }

    private fun refreshCustomButtons(status: JSONObject) {
        val buttons = status.optJSONArray("custom_buttons") ?: return
        val list = mutableListOf<CustomButton>()
        for (i in 0 until buttons.length()) {
            val obj = buttons.optJSONObject(i) ?: continue
            list.add(
                CustomButton(
                    name = obj.optString("name", ""),
                    operator = obj.optString("operator", ""),
                    icon = obj.optString("icon", ""),
                )
            )
        }
        _customButtons.value = list
    }

    // ==================== 命令分发 ====================

    /** 发送命令(通用入口);在 IO 线程执行 */
    fun send(method: String, params: JSONObject = JSONObject(), onResult: ((JSONObject) -> Unit)? = null) {
        viewModelScope.launch(Dispatchers.IO) {
            val result = NativeBridge.sendCommand(method, params)
            onResult?.let { viewModelScope.launch { it(result) } }
        }
    }

    /** 发送无参数命令并返回是否成功 */
    fun fire(method: String) {
        viewModelScope.launch(Dispatchers.IO) {
            NativeBridge.fire(method)
        }
    }

    /** 异步发送命令(不等待响应),用于高频手势,降低控制延迟 */
    fun sendAsync(method: String, params: JSONObject = JSONObject()) {
        viewModelScope.launch(Dispatchers.IO) {
            NativeBridge.sendAsync(method, params)
        }
    }

    // ----- 视图(手势类高频命令走异步,避免每次等待 RTT) -----
    fun viewOrbit(dx: Double, dy: Double) =
        sendAsync("view3d.orbit", JSONObject().put("dx", dx).put("dy", dy))
    fun viewPan(dx: Double, dy: Double) =
        sendAsync("view3d.pan", JSONObject().put("dx", dx).put("dy", dy))
    fun viewZoom(delta: Double) =
        sendAsync("view3d.zoom", JSONObject().put("delta", delta))
    fun viewPreset(preset: String) =
        send("view3d.preset", JSONObject().put("preset", preset))
    fun viewTogglePersp() = fire("view3d.toggle_persp")
    fun viewShading(shading: String) =
        send("view3d.shading", JSONObject().put("shading", shading))
    fun viewFrameAll() = fire("view3d.frame_all")
    fun viewFrameSelected() = fire("view3d.frame_selected")
    fun viewReset() = fire("view3d.reset")

    // ----- 模式 -----
    fun setMode(mode: String) = send("mode.set", JSONObject().put("mode", mode))
    fun toggleEditMode() = fire("mode.toggle_edit")

    // ----- 对象 -----
    fun objectAdd(type: String) = send("object.add", JSONObject().put("type", type))
    fun objectDelete() = fire("object.delete")
    fun objectDuplicate() = fire("object.duplicate")
    fun objectSelectAll() = fire("object.select_all")
    fun objectSelectByName(name: String) =
        send("object.select_by_name", JSONObject().put("name", name))
    fun objectTransform(name: String, x: Double, y: Double, z: Double) =
        send("object.transform", JSONObject().put("name", name).put("x", x).put("y", y).put("z", z))
    fun objectSetVisibility(name: String, visible: Boolean) =
        send("object.visibility", JSONObject().put("name", name).put("visible", visible))
    fun objectSetOrigin(name: String, mode: String) =
        send("object.origin", JSONObject().put("name", name).put("mode", mode))

    // ----- 动画 -----
    fun animPlay() = fire("anim.play")
    fun animPause() = fire("anim.pause")
    fun animFrameJump(frame: Int) = send("anim.frame_jump", JSONObject().put("frame", frame))
    fun animStep(delta: Int) = send("anim.frame_step", JSONObject().put("delta", delta))
    fun animGotoStart() = fire("anim.goto_start")
    fun animGotoEnd() = fire("anim.goto_end")
    fun animKeyframeInsert() = fire("anim.keyframe_insert")
    fun animKeyframePrev() = fire("anim.keyframe_prev")
    fun animKeyframeNext() = fire("anim.keyframe_next")

    // ----- 渲染 -----
    fun renderStill() = fire("render.still")
    fun renderAnimation() = fire("render.animation")
    fun renderEngine(engine: String) = send("render.engine", JSONObject().put("engine", engine))
    fun renderResolution(w: Int, h: Int) =
        send("render.resolution", JSONObject().put("width", w).put("height", h))
    fun renderSamples(samples: Int) = send("render.samples", JSONObject().put("samples", samples))
    fun renderCancel() = fire("render.cancel")

    // ----- 自定义按钮 -----
    fun runCustomButton(name: String) = send("custom.run", JSONObject().put("name", name))

    // ==================== 控制面板布局(数据驱动) ====================

    /** 执行布局按钮(method + params) */
    fun sendFromLayout(btn: LayoutButton) {
        if (btn.method.isEmpty()) return
        sendAsync(btn.method, btn.params)
    }

    /** 拉取控制面板布局(配置存 PC 端插件偏好设置);失败时用本地默认布局兜底,避免 UI 死锁 */
    fun fetchLayout() {
        viewModelScope.launch(Dispatchers.IO) {
            var layout: ControlLayout? = null
            try {
                val result = NativeBridge.sendCommand("layout.get", JSONObject())
                if (result.optBoolean("ok", false)) {
                    layout = result.optJSONObject("result")?.optJSONObject("layout")?.let { parseLayout(it) }
                }
            } catch (e: Exception) {
                Log.w(TAG, "拉取布局失败: ${e.message}")
            }
            _controlLayout.value = layout ?: defaultLayout()
        }
    }

    /** 本地默认布局(拉取失败时兜底,避免转圈) */
    private fun defaultLayout(): ControlLayout {
        fun btn(
            id: String,
            label: String,
            method: String,
            style: String = "outlined",
            p: Array<Pair<String, Any>> = emptyArray(),
        ) = LayoutButton(id, label, method, JSONObject().apply { p.forEach { put(it.first, it.second) } }, style)

        val map = linkedMapOf<String, List<LayoutButton>>()
        map[TAB_TOUCHPAD] = listOf(
            btn("frame_all", "框选全部", "view3d.frame_all", "filled"),
            btn("frame_sel", "聚焦选中", "view3d.frame_selected", "filled"),
            btn("reset_view", "复位", "view3d.reset", "filled"),
            btn("toggle_persp", "透视", "view3d.toggle_persp"),
            btn("shading_solid", "着色", "view3d.shading", p = arrayOf("shading" to "solid")),
            btn("shading_wire", "线框", "view3d.shading", p = arrayOf("shading" to "wireframe")),
            btn("preset_front", "前", "view3d.preset", p = arrayOf("preset" to "front")),
            btn("preset_top", "顶", "view3d.preset", p = arrayOf("preset" to "top")),
            btn("preset_camera", "相机", "view3d.preset", p = arrayOf("preset" to "camera")),
        )
        map[TAB_VIEW] = listOf(
            btn("frame_all", "框选全部", "view3d.frame_all", "filled"),
            btn("frame_sel", "聚焦选中", "view3d.frame_selected", "filled"),
            btn("reset_view", "复位", "view3d.reset", "filled"),
            btn("toggle_persp", "透视切换", "view3d.toggle_persp"),
            btn("shading_solid", "实体", "view3d.shading", p = arrayOf("shading" to "solid")),
            btn("shading_wire", "线框", "view3d.shading", p = arrayOf("shading" to "wireframe")),
            btn("preset_front", "前", "view3d.preset", p = arrayOf("preset" to "front")),
            btn("preset_top", "顶", "view3d.preset", p = arrayOf("preset" to "top")),
            btn("preset_camera", "相机", "view3d.preset", p = arrayOf("preset" to "camera")),
        )
        map[TAB_OBJECT] = listOf(
            btn("mode_object", "对象模式", "mode.set", "filled", p = arrayOf("mode" to "OBJECT")),
            btn("mode_edit", "编辑模式", "mode.set", p = arrayOf("mode" to "EDIT")),
            btn("add_cube", "立方体", "object.add", p = arrayOf("type" to "Cube")),
            btn("add_sphere", "球体", "object.add", p = arrayOf("type" to "Sphere")),
            btn("delete", "删除", "object.delete", "filled"),
            btn("duplicate", "复制", "object.duplicate"),
            btn("select_all", "全选", "object.select_all"),
        )
        map[TAB_ANIM] = listOf(
            btn("play", "▶ 播放", "anim.play", "filled"),
            btn("pause", "⏸ 暂停", "anim.pause", "filled"),
            btn("goto_start", "⏮ 首帧", "anim.goto_start"),
            btn("goto_end", "末帧 ⏭", "anim.goto_end"),
            btn("key_insert", "关键帧", "anim.keyframe_insert", "filled"),
            btn("key_prev", "◀ 上关键帧", "anim.keyframe_prev"),
            btn("key_next", "下关键帧 ▶", "anim.keyframe_next"),
        )
        map[TAB_RENDER] = listOf(
            btn("still", "渲染当前帧", "render.still", "filled"),
            btn("animation", "渲染动画", "render.animation", "filled"),
            btn("cancel", "取消", "render.cancel", "filled"),
            btn("engine_eevee", "EEVEE", "render.engine", p = arrayOf("engine" to "EEVEE")),
            btn("engine_cycles", "Cycles", "render.engine", p = arrayOf("engine" to "CYCLES")),
        )
        map[TAB_CUSTOM] = emptyList()
        return map
    }

    /** 保存布局到 PC 端 */
    fun saveLayout(layout: ControlLayout) {
        viewModelScope.launch(Dispatchers.IO) {
            val payload = JSONObject()
            for ((tab, buttons) in layout) {
                val arr = org.json.JSONArray()
                for (b in buttons) {
                    arr.put(
                        JSONObject()
                            .put("id", b.id)
                            .put("label", b.label)
                            .put("method", b.method)
                            .put("params", b.params)
                            .put("style", b.style)
                    )
                }
                payload.put(tab, arr)
            }
            NativeBridge.sendCommand("layout.set", JSONObject().put("layout", payload))
            _controlLayout.value = layout
        }
    }

    /** 恢复默认布局 */
    fun resetLayout() {
        viewModelScope.launch(Dispatchers.IO) {
            NativeBridge.sendCommand("layout.reset", JSONObject())
            fetchLayout()
        }
    }

    private fun parseLayout(layoutObj: JSONObject): ControlLayout? {
        val map = linkedMapOf<String, List<LayoutButton>>()
        val keys = layoutObj.keys()
        while (keys.hasNext()) {
            val tab = keys.next()
            val arr = layoutObj.optJSONArray(tab) ?: continue
            val buttons = mutableListOf<LayoutButton>()
            for (i in 0 until arr.length()) {
                val b = arr.optJSONObject(i) ?: continue
                buttons.add(
                    LayoutButton(
                        id = b.optString("id", "btn_$i"),
                        label = b.optString("label", "按钮"),
                        method = b.optString("method", ""),
                        params = b.optJSONObject("params") ?: JSONObject(),
                        style = b.optString("style", "outlined"),
                    )
                )
            }
            map[tab] = buttons
        }
        return map
    }

    // ==================== 自动更新 ====================

    /** 当前应用版本名(来自 PackageInfo) */
    fun currentVersion(): String = updateChecker?.getCurrentVersion() ?: "0.0.0"

    /** 检查 GitHub 最新 Release */
    fun checkForUpdate() {
        val checker = updateChecker ?: run {
            _updateState.value = UpdateState.Error("更新检查器未初始化")
            return
        }
        viewModelScope.launch {
            _updateState.value = UpdateState.Checking
            _updateState.value = checker.checkLatest()
        }
    }

    /** 下载最新 APK。仅当状态为 Available 时可调用。 */
    fun downloadUpdate() {
        val checker = updateChecker ?: return
        val state = _updateState.value
        val url = (state as? UpdateState.Available)?.downloadUrl ?: run {
            _updateState.value = UpdateState.Error("无可下载的更新")
            return
        }
        viewModelScope.launch {
            _updateState.value = UpdateState.Downloading(0)
            try {
                val path = checker.downloadApk(url) { progress ->
                    _updateState.value = UpdateState.Downloading(progress)
                }
                pendingApkPath = path
                _updateState.value = UpdateState.ReadyToInstall(path)
            } catch (e: Exception) {
                Log.w(TAG, "下载更新失败", e)
                _updateState.value = UpdateState.Error(e.message ?: "下载失败")
            }
        }
    }

    /** 调起系统安装器。仅当状态为 ReadyToInstall 时可调用。 */
    fun installUpdate() {
        val checker = updateChecker ?: return
        val path = pendingApkPath ?: return
        try {
            checker.installApk(path)
        } catch (e: Exception) {
            Log.w(TAG, "调起安装器失败", e)
            _updateState.value = UpdateState.Error(e.message ?: "无法启动安装器")
        }
    }

    /** 重置更新状态(从 Error 返回 Idle) */
    fun resetUpdateState() {
        _updateState.value = UpdateState.Idle
    }
}
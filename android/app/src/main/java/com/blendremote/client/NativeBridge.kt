package com.blendremote.client

import org.json.JSONObject

/**
 * Rust JNI 桥接
 *
 * Native 方法对应 android/rust-core/src/lib.rs 中的实现。
 * 加载的 so 库名为 libblendremote.so(由 Cargo.toml 的 [lib].name 指定)。
 */
object NativeBridge {
    @Volatile
    private var loaded: Boolean = false

    init {
        try {
            System.loadLibrary("blendremote")
            loaded = true
        } catch (e: UnsatisfiedLinkError) {
            // so 库未加载(可能未编译),会在 native 调用时抛错
            android.util.Log.w("BlendRemote", "libblendremote.so 未加载: ${e.message}")
        }
    }

    fun isLoaded(): Boolean = loaded

    /**
     * 设置配对状态文件所在目录(由 Kotlin 通过 Context.getFilesDir() 传入)
     * 必须在 nativeConnect 之前调用
     */
    external fun nativeSetStateDir(path: String)

    /**
     * 连接到服务端
     * @param serverAddr 形如 "192.168.1.100:28900" (control 端口,serverinfo HTTP 为 +4)
     * @param clientName 客户端名称,用于服务端日志识别
     * @return 0=失败, 1=已连接, 2=需要配对(等待 nativeCompletePairing),
     *         3=地址无效, 4=主机不可达(TCP 超时), 5=连接被拒绝(服务未启动/端口错误)
     */
    external fun nativeConnect(serverAddr: String, clientName: String): Int

    /**
     * 直接用已配对身份连接(跳过 Hello,直接发送 HelloPaired)
     * @return 0=失败, 1=已连接
     */
    external fun nativeConnectPaired(serverAddr: String, clientName: String): Int

    /**
     * 完成配对(用户输入 PIN 后调用)
     * @param pin 用户输入的 PIN(显示在 Blender 插件 N 面板)
     * @return 0=失败(连接已坏,需重新连接), 1=配对成功并已连接,
     *         6=配对被拒绝(PIN 错误等,可用新 PIN 重试),
     *         7=等待响应超时(可重试)
     */
    external fun nativeCompletePairing(pin: String): Int

    /** 取消 pending 配对状态(断开连接) */
    external fun nativeCancelPairing()

    /**
     * 查询是否已配对该服务端
     * @param serverPubkeyB64 服务端公钥的 base64 字符串
     */
    external fun nativeIsServerPaired(serverPubkeyB64: String): Boolean

    /**
     * 获取本客户端的 Ed25519 公钥(base64)
     * 用途:轮询 /serverinfo?pubkey=<此值> 查询服务端侧配对状态(pair_status)
     */
    external fun nativeGetClientPubkeyB64(): String

    /** 检查 TCP 控制连接是否存活(由后台事件循环维护) */
    external fun nativeIsConnected(): Boolean

    /**
     * 发送 Blender 命令
     * @param method 命令方法名(如 "view3d.orbit")
     * @param paramsJson 命令参数 JSON 字符串(如 {"dx":1.5})
     * @return JSON: {"ok":bool, "result":..., "error":"..."}
     */
    external fun nativeSendBlenderCommand(method: String, paramsJson: String): String

    /** 发送 Blender 命令且不等待响应(fire-and-forget,用于高频手势) */
    external fun nativeSendBlenderCommandAsync(method: String, paramsJson: String)

    /** 轮询最新 Blender 状态快照 JSON(无更新时返回 "") */
    external fun nativePollStatus(): String

    /** 优雅断开 */
    external fun nativeDisconnect()

    /** 统计 JSON: {"cmd_sent":N} */
    external fun nativeGetStats(): String

    // ============ 便捷方法 ============

    /**
     * 发送命令并解析 JSON 结果
     * @return JSONObject {"ok":bool, "result":..., "error":"..."};调用失败返回含 ok=false 的对象
     */
    fun sendCommand(method: String, params: JSONObject = JSONObject()): JSONObject {
        return try {
            JSONObject(nativeSendBlenderCommand(method, params.toString()))
        } catch (e: Exception) {
            JSONObject().put("ok", false).put("error", "调用失败: ${e.message}")
        }
    }

    /** 发送无参数命令 */
    fun fire(method: String): Boolean = sendCommand(method).optBoolean("ok")

    /** 异步发送命令(不等待响应),用于高频手势;失败静默忽略 */
    fun sendAsync(method: String, params: JSONObject = JSONObject()) {
        try {
            nativeSendBlenderCommandAsync(method, params.toString())
        } catch (e: Exception) {
            // 高频调用,失败静默(连接断开由状态轮询感知)
        }
    }

    /** 获取最新 Blender 状态;返回 JSONObject 或 null */
    fun pollStatus(): JSONObject? {
        return try {
            val raw = nativePollStatus()
            if (raw.isBlank()) null else JSONObject(raw)
        } catch (e: Exception) {
            null
        }
    }
}
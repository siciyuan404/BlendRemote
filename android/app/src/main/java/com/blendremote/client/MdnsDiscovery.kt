package com.blendremote.client

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import kotlin.jvm.Synchronized

/** 服务端在线状态(三态轮询) */
enum class ServerStatus {
    UNKNOWN,
    ONLINE,
    OFFLINE,
}

/**
 * 发现到的 BlendRemote 服务端
 *
 * @param serviceName mDNS 服务实例名(唯一,用于 onServiceLost 匹配)
 * @param name        显示名(来自 mDNS TXT 记录的 name 字段)
 * @param host        服务端 IP
 * @param port        control TCP 端口(serverinfo HTTP 端口为 port+4)
 * @param status      当前在线状态
 * @param pubkey      服务端 Ed25519 公钥 base64(身份标识,来自 mDNS TXT 的 pk 字段)
 * @param paired      本客户端是否已配对该服务端;null=未知
 */
data class DiscoveredServer(
    val serviceName: String,
    val name: String,
    val host: String,
    val port: Int,
    val status: ServerStatus = ServerStatus.UNKNOWN,
    val pubkey: String = "",
    val paired: Boolean? = null,
) {
    /** 完整连接地址,形如 "192.168.1.100:28900" */
    val addrString: String get() = "$host:$port"

    /** serverinfo HTTP 探测 URL(control 端口 + 4) */
    val serverInfoUrl: String get() = "http://$host:${port + 4}/serverinfo"
}

/**
 * mDNS 自动发现 + 三态轮询
 *
 * 监听局域网 `_blendremote._tcp.` 服务,自动发现运行 blendremote-server 的 PC。
 * 发现后周期性 HTTP 探测 `/serverinfo`,在 UNKNOWN/ONLINE/OFFLINE 间切换。
 */
class MdnsDiscovery(
    context: Context,
    private val clientPubkeyProvider: () -> String = { "" },
) {

    companion object {
        private const val TAG = "BlendRemote/Mdns"
        /** 与 Rust crates/net/src/discovery.rs 的 SERVICE_TYPE 保持一致 */
        private const val SERVICE_TYPE = "_blendremote._tcp."
        private const val POLL_INTERVAL_MS = 3000L
        private const val OFFLINE_FAILURE_THRESHOLD = 2
    }

    private val nsdManager: NsdManager =
        context.getSystemService(Context.NSD_SERVICE) as NsdManager

    private val _servers = MutableStateFlow<Set<DiscoveredServer>>(emptySet())
    val servers: StateFlow<Set<DiscoveredServer>> = _servers.asStateFlow()

    private val pendingResolve = ConcurrentHashMap<String, Boolean>()
    private var discovering = false

    private val failureCount = ConcurrentHashMap<String, AtomicInteger>()

    private val pollScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var pollJob: Job? = null

    private val discoveryListener = object : NsdManager.DiscoveryListener {
        override fun onDiscoveryStarted(serviceType: String) {
            Log.i(TAG, "mDNS 发现已启动: $serviceType")
        }

        override fun onDiscoveryStopped(serviceType: String) {
            Log.i(TAG, "mDNS 发现已停止: $serviceType")
            _servers.value = emptySet()
            pendingResolve.clear()
            failureCount.clear()
        }

        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
            Log.e(TAG, "mDNS 启动失败 code=$errorCode")
            discovering = false
        }

        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
            Log.e(TAG, "mDNS 停止失败 code=$errorCode")
        }

        override fun onServiceFound(serviceInfo: NsdServiceInfo) {
            if (serviceInfo.serviceType != SERVICE_TYPE) return
            val key = serviceInfo.serviceName + serviceInfo.serviceType
            if (pendingResolve.putIfAbsent(key, true) != null) return
            resolveService(serviceInfo)
        }

        override fun onServiceLost(serviceInfo: NsdServiceInfo) {
            Log.d(TAG, "服务丢失: ${serviceInfo.serviceName}")
            _servers.value = _servers.value.filterNot { it.serviceName == serviceInfo.serviceName }.toSet()
            pendingResolve.remove(serviceInfo.serviceName + serviceInfo.serviceType)
            failureCount.remove(serviceInfo.serviceName)
        }
    }

    private fun resolveService(serviceInfo: NsdServiceInfo) {
        val resolveListener = object : NsdManager.ResolveListener {
            override fun onServiceResolved(info: NsdServiceInfo) {
                pendingResolve.remove(info.serviceName + info.serviceType)
                val host = info.host ?: return
                val hostStr = host.hostAddress ?: return
                if (host.isLoopbackAddress) return

                val name = info.attributes["name"]?.toString(Charsets.UTF_8) ?: info.serviceName
                val txtPubkey = info.attributes["pk"]?.toString(Charsets.UTF_8) ?: ""

                val server = DiscoveredServer(
                    serviceName = info.serviceName,
                    name = name,
                    host = hostStr,
                    port = info.port,
                    status = ServerStatus.UNKNOWN,
                    pubkey = txtPubkey,
                )
                val current = _servers.value.toMutableSet()
                current.removeAll { it.serviceName == server.serviceName }
                current.add(server)
                _servers.value = current
            }

            override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                pendingResolve.remove(info.serviceName + info.serviceType)
                Log.w(TAG, "resolve 失败: ${info.serviceName} code=$errorCode")
            }
        }
        try {
            nsdManager.resolveService(serviceInfo, resolveListener)
        } catch (e: IllegalArgumentException) {
            pendingResolve.remove(serviceInfo.serviceName + serviceInfo.serviceType)
        }
    }

    /** 启动 mDNS 服务发现 + 状态轮询。可重复调用,内部有 discovering 标志防重入。 */
    fun startDiscovery() {
        if (discovering) return
        discovering = true
        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
        } catch (e: Exception) {
            Log.e(TAG, "启动 mDNS 发现异常", e)
            discovering = false
            return
        }
        if (pollJob == null || pollJob?.isActive != true) {
            pollJob = pollScope.launch { pollLoop() }
        }
    }

    /** 停止发现并清空列表,同时取消轮询协程 */
    fun stopDiscovery() {
        if (discovering) {
            discovering = false
            try {
                nsdManager.stopServiceDiscovery(discoveryListener)
            } catch (e: IllegalArgumentException) {
            }
        }
        pollJob?.cancel()
        pollJob = null
        _servers.value = emptySet()
        pendingResolve.clear()
        failureCount.clear()
    }

    private suspend fun CoroutineScope.pollLoop() {
        while (discovering && isActive) {
            val snapshot = _servers.value
            if (snapshot.isEmpty()) {
                delay(POLL_INTERVAL_MS)
                continue
            }
            val toProbe = snapshot.filter { it.status != ServerStatus.OFFLINE }
            for (server in toProbe) {
                launch {
                    probeOnce(server)
                }
            }
            delay(POLL_INTERVAL_MS)
        }
    }

    private suspend fun probeOnce(server: DiscoveredServer) {
        val result = withContext(Dispatchers.IO) {
            ServerInfoProber.probe(server.serverInfoUrl, clientPubkeyProvider())
        }
        updateStatus(server.serviceName) { current ->
            if (result != null) {
                failureCount.remove(server.serviceName)
                current.copy(
                    status = ServerStatus.ONLINE,
                    name = result.name.takeIf { it.isNotBlank() } ?: current.name,
                    pubkey = result.serverPubkeyB64.takeIf { it.isNotBlank() } ?: current.pubkey,
                    paired = result.pairStatus ?: current.paired,
                )
            } else {
                val count = failureCount
                    .computeIfAbsent(server.serviceName) { AtomicInteger(0) }
                    .incrementAndGet()
                if (count >= OFFLINE_FAILURE_THRESHOLD) {
                    current.copy(status = ServerStatus.OFFLINE)
                } else {
                    current
                }
            }
        }
    }

    @Synchronized
    private fun updateStatus(
        serviceName: String,
        transform: (DiscoveredServer) -> DiscoveredServer,
    ) {
        val current = _servers.value
        val target = current.firstOrNull { it.serviceName == serviceName } ?: return
        val newServer = transform(target)
        if (newServer == target) return
        val updated = current.toMutableSet()
        updated.removeAll { it.serviceName == serviceName }
        updated.add(newServer)
        _servers.value = updated
    }
}
package com.blendremote.client

import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * /serverinfo HTTP 探测器(MdnsDiscovery 与手动连接共用)
 *
 * BlendRemote 服务端在 control 端口 +4 暴露 /serverinfo(0.0.0.0:28904)。
 * URL 带 `?pubkey=<客户端公钥>` 时,服务端额外返回 `pair_status`
 * (该客户端是否已配对)。
 */
object ServerInfoProber {

    private const val TAG = "BlendRemote/Prober"

    const val DEFAULT_CONNECT_TIMEOUT_MS = 1500
    const val DEFAULT_READ_TIMEOUT_MS = 2000

    data class ServerInfoResult(
        val name: String,
        val hostname: String,
        val version: Int,
        val state: String,
        val connectedClients: Int,
        val maxClients: Int,
        val uptimeSecs: Long,
        val serverPubkeyB64: String,
        val pairStatus: Boolean?,
        val addonConnected: Boolean?,
    )

    /**
     * 探测一次 /serverinfo。
     *
     * @param serverInfoUrl   形如 "http://192.168.1.12:28904/serverinfo"
     * @param clientPubkeyB64 客户端公钥(base64);非空时附加 ?pubkey= 查询 pair_status
     * @return 成功返回 [ServerInfoResult],任何失败返回 null
     */
    fun probe(
        serverInfoUrl: String,
        clientPubkeyB64: String = "",
        connectTimeoutMs: Int = DEFAULT_CONNECT_TIMEOUT_MS,
        readTimeoutMs: Int = DEFAULT_READ_TIMEOUT_MS,
    ): ServerInfoResult? {
        val url = if (clientPubkeyB64.isNotEmpty()) {
            "$serverInfoUrl?pubkey=${URLEncoder.encode(clientPubkeyB64, "UTF-8")}"
        } else {
            serverInfoUrl
        }
        return try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
                requestMethod = "GET"
                useCaches = false
                instanceFollowRedirects = false
            }
            conn.connect()
            try {
                if (conn.responseCode == 200) {
                    val body = conn.inputStream.bufferedReader().use { it.readText() }
                    parse(body)
                } else {
                    Log.w(TAG, "serverinfo HTTP ${conn.responseCode} for $serverInfoUrl")
                    null
                }
            } finally {
                conn.disconnect()
            }
        } catch (e: Exception) {
            Log.d(TAG, "serverinfo 探测失败 $serverInfoUrl: ${e.message}")
            null
        }
    }

    private fun parse(body: String): ServerInfoResult? {
        return try {
            val json = JSONObject(body)
            ServerInfoResult(
                name = json.optString("name", ""),
                hostname = json.optString("hostname", ""),
                version = json.optInt("version", 1),
                state = json.optString("state", "ONLINE"),
                connectedClients = json.optInt("connected_clients", 0),
                maxClients = json.optInt("max_clients", 1),
                uptimeSecs = json.optLong("uptime_secs", 0),
                serverPubkeyB64 = json.optString("server_pubkey_b64", ""),
                pairStatus = if (json.has("pair_status")) json.optBoolean("pair_status") else null,
                addonConnected = if (json.has("addon_connected")) json.optBoolean("addon_connected") else null,
            )
        } catch (e: Exception) {
            Log.w(TAG, "解析 serverinfo 失败: ${e.message}")
            null
        }
    }
}
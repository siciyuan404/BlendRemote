package com.blendremote.client.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.SettingsEthernet
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.blendremote.client.BlendRemoteViewModel
import com.blendremote.client.ConnectionState
import com.blendremote.client.DiscoveredServer
import com.blendremote.client.ServerStatus
import com.blendremote.client.UpdateState
import com.blendremote.client.normalizeAddress

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConnectScreen(
    vm: BlendRemoteViewModel,
    onConnected: () -> Unit,
) {
    val context = LocalContext.current
    val connectionState by vm.connectionState.collectAsState()
    val historyAddresses by vm.historyAddresses.collectAsState()
    val lastAddr by vm.lastAddr.collectAsState()
    val servers by vm.discoveredServers.collectAsState()
    val pairingRequired by vm.pairingRequired.collectAsState()
    val pairingSubmitting by vm.pairingSubmitting.collectAsState()

    var serverAddr by remember {
        mutableStateOf(lastAddr.ifBlank { historyAddresses.firstOrNull() ?: "" })
    }
    var clientName by remember { mutableStateOf("Android-Client") }
    var showPinDialog by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        vm.init(context)
    }

    // Android 13+: 使用 NsdManager 需要运行时授予 NEARBY_WIFI_DEVICES
    val nearbyWifiPermission = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.NEARBY_WIFI_DEVICES else null
    val hasNearbyWifiPermission = nearbyWifiPermission == null ||
        ContextCompat.checkSelfPermission(context, nearbyWifiPermission) == PackageManager.PERMISSION_GRANTED
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ -> }
    LaunchedEffect(Unit) {
        if (nearbyWifiPermission != null && !hasNearbyWifiPermission) {
            permissionLauncher.launch(nearbyWifiPermission)
        }
    }

    DisposableEffect(Unit) {
        vm.startDiscovery()
        onDispose { vm.stopDiscovery() }
    }

    LaunchedEffect(connectionState) {
        if (connectionState is ConnectionState.Connected) {
            onConnected()
        }
    }

    // 配对弹窗
    val pendingPairing = pairingRequired
    LaunchedEffect(pendingPairing) {
        showPinDialog = pendingPairing != null
    }

    if (showPinDialog && pendingPairing != null) {
        PinDialog(
            errorMessage = pendingPairing.errorMessage,
            submitting = pairingSubmitting,
            onConfirm = { pin ->
                vm.completePairing(pin)
            },
            onDismiss = {
                showPinDialog = false
                vm.cancelPairing()
            },
        )
    }

    Box(modifier = Modifier.fillMaxSize().padding(24.dp)) {
        Column(
            modifier = Modifier.fillMaxSize(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Spacer(Modifier.height(24.dp))
            Icon(
                imageVector = Icons.Default.Computer,
                contentDescription = null,
                modifier = Modifier.size(56.dp),
                tint = MaterialTheme.colorScheme.primary,
            )
            Spacer(Modifier.height(8.dp))
            Text("BlendRemote", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(
                "通过局域网远程控制 Blender",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(16.dp))

            // 手动连接
            OutlinedTextField(
                value = serverAddr,
                onValueChange = { serverAddr = it },
                label = { Text("PC 地址(IP 或 IP:端口)") },
                singleLine = true,
                leadingIcon = { Icon(Icons.Default.SettingsEthernet, null) },
                isError = serverAddr.isNotBlank() && normalizeAddress(serverAddr) == null,
                supportingText = {
                    if (serverAddr.isNotBlank() && normalizeAddress(serverAddr) == null) {
                        Text("示例:192.168.1.12 或 192.168.1.12:28900")
                    }
                },
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = clientName,
                onValueChange = { clientName = it },
                label = { Text("设备名称(可选)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(16.dp))

            Button(
                onClick = {
                    val name = clientName.trim().ifBlank { "Android-Client" }
                    vm.connect(serverAddr, name)
                },
                enabled = connectionState !is ConnectionState.Connecting,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    when (connectionState) {
                        is ConnectionState.Connecting -> "连接中…"
                        else -> "连接"
                    }
                )
            }

            connectionState.let { state ->
                when (state) {
                    is ConnectionState.Error -> {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            state.message,
                            color = MaterialTheme.colorScheme.error,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    else -> {}
                }
            }

            Spacer(Modifier.height(24.dp))

            // 自动发现
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("局域网自动发现", style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.weight(1f))
                if (servers.isEmpty()) {
                    Text(
                        "未发现服务,请确认已启动 blendremote-server",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            Spacer(Modifier.height(8.dp))

            if (servers.isNotEmpty()) {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(servers.sortedBy { it.name }) { server ->
                        DiscoveredServerRow(
                            server = server,
                            onClick = { vm.connect(server.addrString) },
                        )
                    }
                    item { UpdatePanel(vm, modifier = Modifier.fillMaxWidth()) }
                    item { Spacer(Modifier.height(24.dp)) }
                }
            } else {
                Spacer(Modifier.weight(1f))
                UpdatePanel(vm, modifier = Modifier.fillMaxWidth())
            }
        }
    }
}

@Composable
private fun DiscoveredServerRow(
    server: DiscoveredServer,
    onClick: () -> Unit,
) {
    val statusColor = when (server.status) {
        ServerStatus.ONLINE -> Color(0xFF22C55E)
        ServerStatus.OFFLINE -> MaterialTheme.colorScheme.error
        ServerStatus.UNKNOWN -> Color(0xFFFACC15)
    }
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
    ) {
        Row(
            modifier = Modifier.padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(10.dp)
                    .clip(CircleShape)
                    .background(statusColor),
            )
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(server.name, fontWeight = FontWeight.Medium)
                Text(
                    server.addrString,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (server.paired == true) {
                AssistChip(onClick = {}, label = { Text("已配对") })
            } else if (server.paired == false) {
                AssistChip(onClick = {}, label = { Text("未配对") })
            }
        }
    }
}

@Composable
private fun PinDialog(
    errorMessage: String?,
    submitting: Boolean,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var pin by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = { if (!submitting) onDismiss() },
        title = { Text("配对") },
        text = {
            Column {
                Text("请输入 Blender 插件 N 面板上显示的 6 位 PIN:")
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(
                    value = pin,
                    onValueChange = { pin = it.filter { c -> c.isDigit() }.take(6) },
                    label = { Text("PIN") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                    visualTransformation = PasswordVisualTransformation(),
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                if (errorMessage != null) {
                    Spacer(Modifier.height(8.dp))
                    Text(errorMessage, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(pin) },
                enabled = pin.length == 6 && !submitting,
            ) {
                Text(if (submitting) "配对中…" else "确认")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !submitting) {
                Text("取消")
            }
        },
    )
}

/**
 * 更新面板:显示版本号 + 检查更新 + 下载进度 + 安装
 */
@Composable
private fun UpdatePanel(vm: BlendRemoteViewModel, modifier: Modifier = Modifier) {
    val updateState by vm.updateState.collectAsState()
    val currentVer = remember { vm.currentVersion() }

    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = "v$currentVer",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                when (val s = updateState) {
                    is UpdateState.Idle, is UpdateState.UpToDate -> {
                        TextButton(
                            onClick = { vm.checkForUpdate() },
                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp),
                        ) {
                            Text("检查更新", fontSize = 12.sp)
                        }
                    }
                    is UpdateState.Checking -> {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            strokeWidth = 2.dp,
                        )
                        Spacer(Modifier.width(8.dp))
                        Text("检查中...", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    is UpdateState.Available -> {
                        Button(
                            onClick = { vm.downloadUpdate() },
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                            shape = RoundedCornerShape(8.dp),
                        ) {
                            Text("下载 v${s.version}", fontSize = 12.sp)
                        }
                    }
                    is UpdateState.Downloading -> {
                        Text(
                            "下载中 ${s.progress}%",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    is UpdateState.ReadyToInstall -> {
                        Button(
                            onClick = { vm.installUpdate() },
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                            shape = RoundedCornerShape(8.dp),
                        ) {
                            Text("安装更新", fontSize = 12.sp)
                        }
                    }
                    is UpdateState.Error -> {
                        Text(
                            s.message,
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.weight(1f),
                            maxLines = 1,
                            overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis,
                        )
                        TextButton(
                            onClick = { vm.checkForUpdate() },
                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp),
                        ) {
                            Text("重试", fontSize = 12.sp)
                        }
                    }
                }
            }

            // 下载进度条
            (updateState as? UpdateState.Downloading)?.let { d ->
                Spacer(Modifier.height(6.dp))
                LinearProgressIndicator(
                    progress = { d.progress / 100f },
                    modifier = Modifier.fillMaxWidth().height(3.dp),
                )
            }

            // 更新日志
            (updateState as? UpdateState.Available)?.let { a ->
                if (a.notes.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Text(
                        a.notes,
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 3,
                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}
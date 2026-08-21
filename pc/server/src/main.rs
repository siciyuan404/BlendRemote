//! BlendRemote 服务端守护进程
//!
//! 职责:
//! - 局域网网关:监听 TCP control 端口,处理握手/配对/Blender 命令
//! - mDNS 广播 `_blendremote._tcp.`,供手机自动发现
//! - 本地 HTTP /pairing 接口(127.0.0.1:base+5),供 Blender 插件面板显示 PIN / 已配对设备
//! - 局域网 HTTP /serverinfo 接口(0.0.0.0:base+4),供手机轮询在线状态
//! - 转发 Blender 命令到插件本地桥(127.0.0.1:addon_port),
//!   并周期轮询插件 /status 广播给所有已连接客户端
//!
//! 插件桥端口默认 29390,可通过 --addon-port 覆盖。

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use clap::Parser;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::RwLock;

use blendremote_net::pairing::PairingManager;
use blendremote_net::{
    MdnsAdvertiser, PortLayout, Server, ServerEvent,
};
use blendremote_protocol::{ControlMessage, monotonic_ns};

/// 插件桥默认端口(Blender 插件本地 HTTP 服务)
pub const DEFAULT_ADDON_PORT: u16 = 29390;

#[derive(Parser, Debug)]
#[command(name = "blendremote-server", version, about = "BlendRemote 服务端 - Blender 手机外设网关")]
struct Cli {
    /// 监听地址
    #[arg(long, default_value = "0.0.0.0")]
    bind: String,
    /// 基础端口(control=base, serverinfo=base+4, pairing=base+5)
    #[arg(long, default_value_t = PortLayout::DEFAULT_BASE)]
    port: u16,
    /// Blender 插件本地桥端口
    #[arg(long, default_value_t = DEFAULT_ADDON_PORT)]
    addon_port: u16,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "blendremote=info,warn".into()),
        )
        .with_target(false)
        .init();

    let cli = Cli::parse();
    let ports = PortLayout::from_base(cli.port);
    let bind = cli.bind.clone();

    tracing::info!("BlendRemote 服务端启动中...");
    tracing::info!(
        "端口分配: control={}(TCP) serverinfo={}(HTTP) pairing={}(HTTP本地)",
        ports.control,
        cli.port + 4,
        cli.port + 5
    );
    tracing::info!("插件桥: http://127.0.0.1:{}/", cli.addon_port);

    list_local_ips(ports.control);

    // 加载或创建配对管理器(mDNS TXT 要携带服务端公钥,需先初始化)
    let pairing_dir = pairing_state_dir();
    let pairing_manager = match PairingManager::load_or_create(Some(pairing_dir.clone())) {
        Ok(pm) => {
            let pm = Arc::new(pm);
            let pin = pm.ensure_pin().await;
            tracing::info!(
                "配对机制已启用,当前 PIN: {} (状态文件: {})",
                pin,
                pairing_dir.join("pairing.json").display()
            );
            Some(pm)
        }
        Err(e) => {
            tracing::warn!("配对管理器初始化失败(配对机制禁用): {}", e);
            None
        }
    };

    // 启动 mDNS 广播
    let host_name = hostname_string();
    let mdns_pubkey = pairing_manager.as_ref().map(|pm| pm.server_pubkey_b64());
    let _mdns_advertiser = match MdnsAdvertiser::register(
        Some("BlendRemote-Server"),
        Some(&host_name),
        ports.control,
        None,
        mdns_pubkey.as_deref(),
    ) {
        Ok(a) => {
            tracing::info!("mDNS 广播已启动: 服务类型=_blendremote._tcp. 端口={}", ports.control);
            Some(a)
        }
        Err(e) => {
            tracing::warn!("mDNS 广播启动失败(自动发现将不可用): {}", e);
            None
        }
    };

    let server = if let Some(ref pm) = pairing_manager {
        Server::new(ports).with_pairing(pm.clone())
    } else {
        Server::new(ports)
    };
    let (event_tx, mut event_rx) = tokio::sync::mpsc::channel::<ServerEvent>(256);

    // 活跃客户端表:client_id → 出站通道(用于命令响应回写 + 状态广播)
    let clients: Arc<RwLock<HashMap<u32, blendremote_net::ClientConn>>> =
        Arc::new(RwLock::new(HashMap::new()));

    // 插件桥客户端
    let bridge = Arc::new(BridgeClient::new(cli.addon_port));

    // 启动 HTTP /pairing 服务(127.0.0.1:base+5,仅本机)
    let pairing_port = cli.port + 5;
    let pairing_addr: SocketAddr = format!("127.0.0.1:{}", pairing_port).parse()?;
    let pairing_for_http = pairing_manager.clone();
    let pairing_http = pairing_for_http.clone();
    tokio::spawn(async move {
        run_pairing_server(pairing_http, pairing_addr).await;
    });
    tracing::info!("pairing HTTP 监听: http://{}/pairing", pairing_addr);

    // 启动 HTTP /serverinfo 服务(0.0.0.0:base+4,对局域网开放)
    let serverinfo_port = cli.port + 4;
    let serverinfo_addr: SocketAddr = format!("0.0.0.0:{}", serverinfo_port).parse()?;
    let serverinfo_pairing = pairing_manager.clone();
    let clients_for_info = clients.clone();
    let hostname_for_info = host_name.clone();
    let started_ns = monotonic_ns();
    tokio::spawn(async move {
        run_serverinfo_server(
            serverinfo_pairing,
            clients_for_info,
            hostname_for_info,
            serverinfo_addr,
            started_ns,
        )
        .await;
    });
    tracing::info!("serverinfo HTTP 监听: http://{}/serverinfo", serverinfo_addr);

    // 状态轮询任务:每 1s 拉取插件 /status,广播给所有客户端
    let clients_for_status = clients.clone();
    let bridge_for_status = bridge.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_millis(1000)).await;
            match bridge_for_status.fetch_status().await {
                Ok(json) => {
                    let conns = clients_for_status.read().await;
                    for conn in conns.values() {
                        let _ = conn
                            .tx
                            .send(ControlMessage::BlenderStatus { json: json.clone() })
                            .await;
                    }
                }
                Err(e) => {
                    // 插件未运行(Blender 未启动/插件未启用),静默降级
                    tracing::trace!("插件状态拉取失败: {}", e);
                }
            }
        }
    });

    // 事件循环
    let server_handle = tokio::spawn(async move {
        if let Err(e) = server.run(&bind, event_tx).await {
            tracing::error!("服务端错误: {}", e);
        }
    });

    while let Some(event) = event_rx.recv().await {
        match event {
            ServerEvent::ClientConnected { conn } => {
                tracing::info!(
                    "✓ 客户端已连接 id={} peer={} name={}",
                    conn.client_id,
                    conn.peer,
                    conn.client_name
                );
                clients.write().await.insert(conn.client_id, conn.clone());
                // 连接建立后立即推送一次状态
                if let Ok(json) = bridge.fetch_status().await {
                    let _ = conn.tx.send(ControlMessage::BlenderStatus { json }).await;
                }
            }
            ServerEvent::ClientDisconnected {
                client_id,
                client_pubkey_b64,
            } => {
                tracing::info!(
                    "✗ 客户端断开 id={} pubkey={:?}",
                    client_id,
                    client_pubkey_b64
                );
                clients.write().await.remove(&client_id);
            }
            ServerEvent::ClientPaired { client_name } => {
                tracing::info!("✓ 客户端配对成功: {}", client_name);
            }
            ServerEvent::BlenderCommand {
                client_id,
                id,
                method,
                params,
            } => {
                // 并发转发到插件桥,不阻塞事件循环。
                // 手势类命令高频到达时,串行 await 会积压 → 延迟叠加(参考 MeowMic 触摸通道的设计:
                // 高频输入不能等每次完整往返)。每个命令在独立 task 中执行 HTTP 请求。
                let bridge_for_cmd = bridge.clone();
                let clients_for_cmd = clients.clone();
                tokio::spawn(async move {
                    let result = bridge_for_cmd.execute(&method, &params).await;
                    // 回写客户端
                    let conn = {
                        let guard = clients_for_cmd.read().await;
                        guard.get(&client_id).cloned()
                    };
                    if let Some(conn) = conn {
                        let (ok, result_json, error) = match result {
                            Ok((ok, result_json)) => (ok, result_json, String::new()),
                            Err(e) => (false, String::new(), e.to_string()),
                        };
                        let _ = conn
                            .tx
                            .send(ControlMessage::BlenderCommandResult {
                                id,
                                ok,
                                result: result_json,
                                error,
                            })
                            .await;
                    } else {
                        tracing::warn!("命令响应失败: 客户端已断开 id={}", client_id);
                    }
                });
            }
            ServerEvent::Error(e) => {
                tracing::warn!("服务端事件错误: {}", e);
            }
        }
    }

    server_handle.await?;
    Ok(())
}

// ============================================================================
// 插件桥客户端(HTTP → 127.0.0.1:addon_port)
// ============================================================================

/// 与 Blender 插件本地桥通信的 HTTP 客户端
pub struct BridgeClient {
    base: String,
    http: reqwest_lite::Client,
}

impl BridgeClient {
    pub fn new(addon_port: u16) -> Self {
        Self {
            base: format!("http://127.0.0.1:{}", addon_port),
            http: reqwest_lite::Client::new(),
        }
    }

    /// 执行命令
    /// 返回 (ok, result_json)
    pub async fn execute(&self, method: &str, params: &str) -> Result<(bool, String)> {
        let body = serde_json::json!({ "method": method, "params": parse_params(params) });
        let (status, text) = self.http.post_json(&format!("{}/cmd", self.base), &body).await?;
        if status != 200 {
            return Ok((false, format!("插件桥返回 HTTP {}", status)));
        }
        let parsed: serde_json::Value = serde_json::from_str(&text)?;
        let ok = parsed.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
        let result = parsed
            .get("result")
            .map(|v| v.to_string())
            .unwrap_or_else(|| "null".into());
        let error = parsed
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if ok {
            Ok((true, result))
        } else {
            Ok((false, error))
        }
    }

    /// 拉取状态快照 JSON(字符串)
    pub async fn fetch_status(&self) -> Result<String> {
        let (status, text) = self.http.get(&format!("{}/status", self.base)).await?;
        if status != 200 {
            anyhow::bail!("插件桥 /status 返回 HTTP {}", status);
        }
        Ok(text)
    }
}

/// params 字符串(JSON 对象)解析:空串或非法时返回空对象
fn parse_params(params: &str) -> serde_json::Value {
    if params.is_empty() {
        return serde_json::Value::Object(Default::default());
    }
    serde_json::from_str(params).unwrap_or(serde_json::Value::Object(Default::default()))
}

// ============================================================================
// 极简 HTTP 客户端(reqwest 太重,这里用手写的 tokio 实现)
// ============================================================================

mod reqwest_lite {
    use anyhow::Result;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    pub struct Client;

    impl Client {
        pub fn new() -> Self {
            Self
        }

        pub async fn get(&self, url: &str) -> Result<(u16, String)> {
            self.request("GET", url, None).await
        }

        pub async fn post_json(&self, url: &str, body: &serde_json::Value) -> Result<(u16, String)> {
            self.request("POST", url, Some(body.to_string())).await
        }

        async fn request(
            &self,
            method: &str,
            url: &str,
            body: Option<String>,
        ) -> Result<(u16, String)> {
            // 解析 http://host:port/path
            let rest = url
                .strip_prefix("http://")
                .ok_or_else(|| anyhow::anyhow!("仅支持 http://: {}", url))?;
            let (host_port, path) = match rest.split_once('/') {
                Some((hp, p)) => (hp, format!("/{}", p)),
                None => (rest, "/".to_string()),
            };

            let mut stream = tokio::time::timeout(
                std::time::Duration::from_secs(2),
                TcpStream::connect(host_port),
            )
            .await??;

            let body_bytes = body.clone().unwrap_or_default();
            let content_len = body_bytes.len();
            let mut req = format!(
                "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nAccept: */*\r\n",
                method, path, host_port
            );
            if let Some(ref b) = body {
                req.push_str(&format!(
                    "Content-Type: application/json\r\nContent-Length: {}\r\n",
                    b.len()
                ));
            }
            req.push_str("\r\n");
            stream.write_all(req.as_bytes()).await?;
            if !body_bytes.is_empty() {
                stream.write_all(body_bytes.as_bytes()).await?;
            }
            stream.flush().await?;

            let mut buf = Vec::with_capacity(4096);
            let mut tmp = [0u8; 4096];
            loop {
                match stream.read(&mut tmp).await {
                    Ok(0) => break,
                    Ok(n) => buf.extend_from_slice(&tmp[..n]),
                    Err(e) => return Err(e.into()),
                }
            }
            let text = String::from_utf8_lossy(&buf).into_owned();
            let status = parse_status(&text).unwrap_or(0);
            let body = parse_body(&text);
            // 防未读:body 可能被截断,仅作降级处理
            let _ = content_len;
            Ok((status, body))
        }
    }

    fn parse_status(text: &str) -> Option<u16> {
        let first_line = text.lines().next()?;
        let mut parts = first_line.split_whitespace();
        parts.next()?;
        parts.next()?.parse().ok()
    }

    fn parse_body(text: &str) -> String {
        match text.find("\r\n\r\n") {
            Some(idx) => text[idx + 4..].to_string(),
            None => String::new(),
        }
    }
}

// ============================================================================
// HTTP /pairing 服务(仅本机,供 Blender 插件面板使用)
// ============================================================================

async fn run_pairing_server(pairing: Option<Arc<PairingManager>>, addr: SocketAddr) {
    let listener = match TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!("pairing HTTP 绑定 {} 失败: {}", addr, e);
            return;
        }
    };
    loop {
        let (mut stream, _peer) = match listener.accept().await {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("pairing accept 失败: {}", e);
                continue;
            }
        };
        let pairing = pairing.clone();
        tokio::spawn(async move {
            let mut buf = [0u8; 2048];
            let n = match stream.read(&mut buf).await {
                Ok(n) if n > 0 => n,
                _ => return,
            };
            let req = String::from_utf8_lossy(&buf[..n]);
            let first_line = req.lines().next().unwrap_or("");
            let mut parts = first_line.split_whitespace();
            let method = parts.next().unwrap_or("");
            let raw_path = parts.next().unwrap_or("");
            let (path, _query) = match raw_path.split_once('?') {
                Some((p, q)) => (p, q),
                None => (raw_path, ""),
            };

            let (status, body) = match pairing {
                Some(pm) => {
                    if method == "GET" && path == "/pairing" {
                        let pin = pm.current_pin().await.unwrap_or_default();
                        let clients = pm.paired_clients().await;
                        let clients_json = serde_json::to_string(&clients).unwrap_or_else(|_| "[]".into());
                        let body = format!(
                            r#"{{"pin":"{}","paired_clients":{}}}"#,
                            pin, clients_json
                        );
                        ("200 OK", body)
                    } else if method == "POST" && path == "/pairing/refresh" {
                        let pin = pm.refresh_pin().await;
                        tracing::info!("HTTP /pairing/refresh: 新 PIN={}", pin);
                        ("200 OK", format!(r#"{{"pin":"{}"}}"#, pin))
                    } else if method == "POST" && path == "/pairing/reset" {
                        match pm.reset_paired_clients().await {
                            Ok(()) => {
                                tracing::info!("HTTP /pairing/reset: 已清空已配对客户端");
                                ("200 OK", r#"{"ok":true}"#.to_string())
                            }
                            Err(e) => {
                                ("500 Internal Server Error", format!(r#"{{"error":"{}"}}"#, e))
                            }
                        }
                    } else {
                        ("404 Not Found", r#"{"error":"not found"}"#.to_string())
                    }
                }
                None => ("503 Service Unavailable", r#"{"error":"pairing disabled"}"#.to_string()),
            };

            let response = format!(
                "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                status,
                body.len(),
                body
            );
            let _ = stream.write_all(response.as_bytes()).await;
            let _ = stream.shutdown().await;
        });
    }
}

// ============================================================================
// HTTP /serverinfo 服务(局域网,供手机轮询)
// ============================================================================

async fn run_serverinfo_server(
    pairing: Option<Arc<PairingManager>>,
    clients: Arc<RwLock<HashMap<u32, blendremote_net::ClientConn>>>,
    hostname: String,
    addr: SocketAddr,
    started_ns: u64,
) {
    const MAX_CLIENTS: usize = 4;
    let listener = match TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!("serverinfo HTTP 绑定 {} 失败: {}", addr, e);
            return;
        }
    };
    loop {
        let (mut stream, _peer) = match listener.accept().await {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("serverinfo accept 失败: {}", e);
                continue;
            }
        };
        let pairing = pairing.clone();
        let clients = clients.clone();
        let hostname = hostname.clone();
        tokio::spawn(async move {
            let mut buf = [0u8; 2048];
            let n = match stream.read(&mut buf).await {
                Ok(n) if n > 0 => n,
                _ => return,
            };
            let req = String::from_utf8_lossy(&buf[..n]);
            let first_line = req.lines().next().unwrap_or("");
            let mut parts = first_line.split_whitespace();
            let method = parts.next().unwrap_or("");
            let raw_path = parts.next().unwrap_or("");
            let (path, query) = match raw_path.split_once('?') {
                Some((p, q)) => (p, q),
                None => (raw_path, ""),
            };

            let (status, body) = if method == "GET" && path == "/serverinfo" {
                let pubkey_b64 = pairing
                    .as_ref()
                    .map(|p| p.server_pubkey_b64())
                    .unwrap_or_default();
                let pair_status_field =
                    match (extract_query_param(query, "pubkey"), pairing.as_ref()) {
                        (Some(client_pk_b64), Some(pm)) => {
                            let decoded = url_decode(&client_pk_b64);
                            let paired = pm.is_client_paired_b64(&decoded).await;
                            format!(",\"pair_status\":{}", paired)
                        }
                        _ => String::new(),
                    };
                let connected = clients.read().await.len();
                let uptime = monotonic_ns().saturating_sub(started_ns) / 1_000_000_000;
                let body = format!(
                    r#"{{"name":"BlendRemote-Server","hostname":"{}","version":{},"app_version":"{}","state":"ONLINE","connected_clients":{},"max_clients":{},"uptime_secs":{},"server_pubkey_b64":"{}"{}}}"#,
                    hostname,
                    blendremote_net::PROTOCOL_VERSION,
                    env!("CARGO_PKG_VERSION"),
                    connected,
                    MAX_CLIENTS,
                    uptime,
                    pubkey_b64,
                    pair_status_field,
                );
                ("200 OK", body)
            } else {
                ("404 Not Found", r#"{"error":"not found"}"#.to_string())
            };

            let response = format!(
                "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                status,
                body.len(),
                body
            );
            let _ = stream.write_all(response.as_bytes()).await;
            let _ = stream.shutdown().await;
        });
    }
}

// ============================================================================
// 工具函数
// ============================================================================

fn extract_query_param(query: &str, key: &str) -> Option<String> {
    for pair in query.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            if k == key {
                return Some(v.to_string());
            }
        }
    }
    None
}

fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = |b: u8| -> Option<u8> {
                match b {
                    b'0'..=b'9' => Some(b - b'0'),
                    b'a'..=b'f' => Some(b - b'a' + 10),
                    b'A'..=b'F' => Some(b - b'A' + 10),
                    _ => None,
                }
            };
            if let (Some(h), Some(l)) = (hex(bytes[i + 1]), hex(bytes[i + 2])) {
                out.push(h * 16 + l);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn list_local_ips(control_port: u16) {
    tracing::info!("本机网络地址:");
    match local_ip_address::list_afinet_netifas() {
        Ok(interfaces) => {
            for (_name, ip) in interfaces {
                if ip.is_ipv4() && !ip.is_loopback() {
                    tracing::info!("  {} (端口 {})", ip, control_port);
                }
            }
        }
        Err(e) => tracing::warn!("获取本机 IP 失败: {}", e),
    }
}

/// 配对状态文件目录:
/// - Windows: %APPDATA%/blendremote
/// - 其他: ~/.config/blendremote
fn pairing_state_dir() -> PathBuf {
    #[cfg(windows)]
    {
        if let Ok(appdata) = std::env::var("APPDATA") {
            return PathBuf::from(appdata).join("blendremote");
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join(".config").join("blendremote");
    }
    std::env::temp_dir().join("blendremote")
}

fn hostname_string() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "BlendRemote-Host".into())
}
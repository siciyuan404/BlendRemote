//! 服务端网络层
//!
//! 监听:
//! - TCP Control 端口:握手、配对、Blender 命令收发、状态推送
//!
//! 与 MeowMic 不同:BlendRemote 只有低频控制命令,不需要
//! 触摸/音频/视频 UDP 通道,因此服务端只保留 TCP 控制通道。

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;

use blendremote_protocol::{ControlMessage, decode_control, encode_control, monotonic_ns};

use crate::pairing::{PairingManager, generate_nonce};
use crate::{NetError, PortLayout};

/// 客户端连接句柄(事件循环用它向客户端写回消息 / 广播状态)
#[derive(Debug, Clone)]
pub struct ClientConn {
    pub client_id: u32,
    pub peer: SocketAddr,
    /// 客户端公钥 base64(HelloPaired 成功后登记;普通连接为 None)
    pub client_pubkey_b64: Option<String>,
    /// 客户端名称
    pub client_name: String,
    /// 向该客户端写回控制消息的通道(由连接任务负责落盘到 TCP)
    pub tx: mpsc::Sender<ControlMessage>,
}

/// 服务端收到的事件
#[derive(Debug)]
pub enum ServerEvent {
    /// 客户端已连接(握手完成)
    ClientConnected { conn: ClientConn },
    /// 客户端断开
    ClientDisconnected {
        client_id: u32,
        client_pubkey_b64: Option<String>,
    },
    /// 客户端配对成功(通知 UI 更新)
    ClientPaired { client_name: String },
    /// 收到 Blender 命令(事件循环转发给插件桥后,
    /// 通过 `conn.tx.send(BlenderCommandResult{...})` 回写客户端)
    BlenderCommand {
        client_id: u32,
        id: u64,
        method: String,
        params: String,
    },
    /// 错误
    Error(NetError),
}

pub struct Server {
    ports: PortLayout,
    /// 配对管理器(可选:测试或无配对需求时为 None)
    pairing: Option<Arc<PairingManager>>,
}

impl Server {
    pub fn new(ports: PortLayout) -> Self {
        Self {
            ports,
            pairing: None,
        }
    }

    /// 启用配对机制
    pub fn with_pairing(mut self, pairing: Arc<PairingManager>) -> Self {
        self.pairing = Some(pairing);
        self
    }

    /// 启动服务端,返回事件接收端
    ///
    /// - bind_addr:监听地址(如 "0.0.0.0")
    pub async fn run(
        self,
        bind_addr: &str,
        event_tx: mpsc::Sender<ServerEvent>,
    ) -> Result<(), NetError> {
        let control_addr = format!("{}:{}", bind_addr, self.ports.control);
        let tcp = TcpListener::bind(&control_addr).await?;
        tracing::info!("BlendRemote 服务端启动: control={} (TCP)", control_addr);

        // TCP 接受循环
        let pairing = self.pairing.clone();
        loop {
            match tcp.accept().await {
                Ok((stream, peer)) => {
                    set_keepalive(&stream);
                    let event_tx = event_tx.clone();
                    let pairing = pairing.clone();
                    tokio::spawn(async move {
                        if let Err(e) = handle_control_conn(stream, peer, event_tx, pairing).await
                        {
                            tracing::warn!("控制连接处理失败: {}", e);
                        }
                    });
                }
                Err(e) => {
                    tracing::error!("TCP accept 失败: {}", e);
                }
            }
        }
    }
}

/// 跨平台设置 TCP keepalive(Windows/Android/Linux)
fn set_keepalive(stream: &tokio::net::TcpStream) {
    let keepalive = socket2::TcpKeepalive::new()
        .with_time(Duration::from_secs(15))
        .with_interval(Duration::from_secs(5));

    #[cfg(windows)]
    {
        use std::os::windows::io::{AsRawSocket, FromRawSocket};
        let raw = stream.as_raw_socket();
        // safety: raw 来自活着的 TcpStream,drop 时 mem::forget 防止 double-close
        let sock = unsafe { socket2::Socket::from_raw_socket(raw) };
        let _ = sock.set_tcp_keepalive(&keepalive);
        std::mem::forget(sock);
    }
    #[cfg(unix)]
    {
        use std::os::unix::io::{AsRawFd, FromRawFd};
        let raw = stream.as_raw_fd();
        // safety: 同上
        let sock = unsafe { socket2::Socket::from_raw_fd(raw) };
        let _ = sock.set_tcp_keepalive(&keepalive);
        std::mem::forget(sock);
    }
}

/// 控制连接处理:读循环 + 出站写任务
async fn handle_control_conn(
    stream: TcpStream,
    peer: SocketAddr,
    event_tx: mpsc::Sender<ServerEvent>,
    pairing: Option<Arc<PairingManager>>,
) -> Result<(), NetError> {
    tracing::info!("控制连接来自 {}", peer);

    // 分离读写半部:read 循环独占 read_half,写任务独占 write_half
    let (mut read_half, mut write_half) = stream.into_split();

    // 出站通道:事件循环/main 通过 tx 发送消息 → 写任务落盘
    let (out_tx, mut out_rx) = mpsc::channel::<ControlMessage>(64);

    let mut client_id: u32 = 0;
    let mut client_name: String = String::new();
    // 每个连接独立 nonce(用于本次 PairRequired 握手)
    let mut pending_nonce: Option<u64> = None;
    // 本连接成功完成 HelloPaired 后的客户端公钥 base64
    let mut conn_pubkey_b64: Option<String> = None;
    // 是否已通过握手(未通过前拒绝业务命令)
    let mut handshaken = false;

    // 出站写任务:独占 write_half
    let writer = tokio::spawn(async move {
        loop {
            match out_rx.recv().await {
                Some(msg) => {
                    let mut frame = Vec::with_capacity(256);
                    if let Err(e) = encode_control(&msg, &mut frame) {
                        tracing::warn!("出站消息编码失败: {}", e);
                        continue;
                    }
                    if write_half.write_all(&frame).await.is_err() {
                        break;
                    }
                }
                None => break,
            }
        }
    });

    // 不活跃超时:客户端每 10s 发心跳,服务端 35s 无消息则断开
    const IDLE_TIMEOUT: Duration = Duration::from_secs(35);

    let read_result: Result<(), NetError> = async {
        let mut read_buf = Vec::with_capacity(4096);
        loop {
            let mut tmp = [0u8; 4096];
            let idle_sleep = tokio::time::sleep(IDLE_TIMEOUT);
            tokio::pin!(idle_sleep);

            let n = tokio::select! {
                r = read_half.read(&mut tmp) => r?,
                () = &mut idle_sleep => {
                    tracing::warn!("客户端不活跃超时({}s),断开: {}", IDLE_TIMEOUT.as_secs(), peer);
                    return Err(NetError::Io(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        format!("客户端 {}s 未发送消息", IDLE_TIMEOUT.as_secs()),
                    )));
                }
            };
            if n == 0 {
                return Ok(());
            }
            read_buf.extend_from_slice(&tmp[..n]);

            // 解析所有完整消息
            loop {
                if read_buf.len() < 4 {
                    break;
                }
                let len = u32::from_le_bytes(read_buf[..4].try_into().unwrap()) as usize;
                if read_buf.len() < 4 + len {
                    break;
                }
                let msg_bytes = read_buf[..4 + len].to_vec();
                read_buf.drain(..4 + len);

                match decode_control(&msg_bytes) {
                    Ok((msg, _)) => {
                        if let Some(resp) = handle_control_msg(
                            &msg,
                            &mut client_id,
                            &mut client_name,
                            &peer,
                            &event_tx,
                            pairing.as_deref(),
                            &mut pending_nonce,
                            &mut conn_pubkey_b64,
                            &mut handshaken,
                            &out_tx,
                        )
                        .await
                        {
                            if out_tx.send(resp).await.is_err() {
                                return Err(NetError::Disconnected);
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!("控制消息解码失败: {}", e);
                    }
                }
            }
        }
    }
    .await;

    // 关闭出站通道(写任务退出)
    drop(out_tx);
    let _ = writer.await;

    // 通知事件循环断开(无论正常/异常)
    let _ = event_tx
        .send(ServerEvent::ClientDisconnected {
            client_id,
            client_pubkey_b64: conn_pubkey_b64.clone(),
        })
        .await;
    tracing::info!("控制连接关闭: {} ({})", peer, client_name);

    read_result
}

#[allow(clippy::too_many_arguments)]
async fn handle_control_msg(
    msg: &ControlMessage,
    client_id: &mut u32,
    client_name: &mut String,
    peer: &SocketAddr,
    event_tx: &mpsc::Sender<ServerEvent>,
    pairing: Option<&PairingManager>,
    pending_nonce: &mut Option<u64>,
    conn_pubkey_b64: &mut Option<String>,
    handshaken: &mut bool,
    out_tx: &mpsc::Sender<ControlMessage>,
) -> Option<ControlMessage> {
    match msg {
        ControlMessage::Hello {
            client_name: name,
            protocol_version,
        } => {
            // 若启用配对机制,普通 Hello 必须先走配对流程
            if let Some(pm) = pairing {
                let nonce = generate_nonce();
                *pending_nonce = Some(nonce);
                tracing::info!(
                    "握手: client={} 未配对,要求先完成配对 nonce={}",
                    name,
                    nonce
                );
                return Some(ControlMessage::PairRequired {
                    server_pubkey: pm.server_pubkey().to_vec(),
                    server_nonce: nonce,
                });
            }
            *client_id = monotonic_ns() as u32;
            *client_name = name.clone();
            *handshaken = true;
            tracing::info!(
                "握手: client={} proto={} (无配对模式)",
                name,
                protocol_version
            );
            notify_connected(event_tx, *client_id, *peer, name.clone(), None, out_tx).await;
            Some(ControlMessage::HelloAck {
                server_name: "BlendRemote-Server".into(),
                protocol_version: 1,
                client_id: *client_id,
            })
        }
        ControlMessage::HelloPaired {
            client_name: name,
            protocol_version,
            client_pubkey,
            nonce,
            signature,
        } => {
            let pm = pairing?;
            // 验证签名 + 白名单
            match pm
                .verify_paired_hello(client_pubkey, name, *nonce, signature)
                .await
            {
                Ok(()) => {
                    *client_id = monotonic_ns() as u32;
                    *client_name = name.clone();
                    *handshaken = true;
                    use base64::Engine as _;
                    let pk_b64 = base64::engine::general_purpose::STANDARD.encode(client_pubkey);
                    *conn_pubkey_b64 = Some(pk_b64.clone());
                    tracing::info!(
                        "已配对握手成功: client={} proto={} pubkey_registered",
                        name,
                        protocol_version
                    );
                    notify_connected(
                        event_tx,
                        *client_id,
                        *peer,
                        name.clone(),
                        Some(pk_b64),
                        out_tx,
                    )
                    .await;
                    Some(ControlMessage::HelloAck {
                        server_name: "BlendRemote-Server".into(),
                        protocol_version: 1,
                        client_id: *client_id,
                    })
                }
                Err(e) => {
                    tracing::warn!("已配对握手失败: client={} err={}", name, e);
                    Some(ControlMessage::PairResponse {
                        success: false,
                        server_pubkey: pm.server_pubkey().to_vec(),
                        error_msg: format!("认证失败: {}", e),
                    })
                }
            }
        }
        ControlMessage::PairRequest {
            client_pubkey,
            client_name: name,
            pin,
            server_nonce,
            signature,
        } => {
            let pm = pairing?;
            // 校验 nonce 是否匹配本连接发出的
            if let Some(expected) = *pending_nonce {
                if expected != *server_nonce {
                    return Some(ControlMessage::PairResponse {
                        success: false,
                        server_pubkey: pm.server_pubkey().to_vec(),
                        error_msg: "nonce 不匹配".into(),
                    });
                }
            }
            let (success, server_pubkey, err) = pm
                .handle_pair_request(
                    client_pubkey.clone(),
                    name.clone(),
                    pin.clone(),
                    *server_nonce,
                    signature.clone(),
                )
                .await;
            if success {
                let _ = event_tx
                    .send(ServerEvent::ClientPaired {
                        client_name: name.clone(),
                    })
                    .await;
                // 清除 nonce(本次配对完成)
                *pending_nonce = None;
            }
            Some(ControlMessage::PairResponse {
                success,
                server_pubkey,
                error_msg: err,
            })
        }
        ControlMessage::SyncReq { client_ts_ns } => {
            let server_recv_ts_ns = monotonic_ns();
            let server_send_ts_ns = monotonic_ns();
            Some(ControlMessage::SyncResp {
                client_ts_ns: *client_ts_ns,
                server_recv_ts_ns,
                server_send_ts_ns,
            })
        }
        ControlMessage::Bye => {
            tracing::info!("客户端主动断开");
            None
        }
        ControlMessage::Ping => Some(ControlMessage::Pong),
        ControlMessage::Pong => None,
        ControlMessage::BlenderCommand { id, method, params } => {
            // 未握手前拒绝业务命令
            if !*handshaken {
                return Some(ControlMessage::BlenderCommandResult {
                    id: *id,
                    ok: false,
                    result: String::new(),
                    error: "未完成握手".into(),
                });
            }
            let _ = event_tx
                .send(ServerEvent::BlenderCommand {
                    client_id: *client_id,
                    id: *id,
                    method: method.clone(),
                    params: params.clone(),
                })
                .await;
            None
        }
        ControlMessage::BlenderCommandResult { .. } | ControlMessage::BlenderStatus { .. } => None,
        ControlMessage::HelloAck { .. }
        | ControlMessage::PairRequired { .. }
        | ControlMessage::PairResponse { .. }
        | ControlMessage::SyncResp { .. } => None,
    }
}

async fn notify_connected(
    event_tx: &mpsc::Sender<ServerEvent>,
    client_id: u32,
    peer: SocketAddr,
    client_name: String,
    client_pubkey_b64: Option<String>,
    out_tx: &mpsc::Sender<ControlMessage>,
) {
    let _ = event_tx
        .send(ServerEvent::ClientConnected {
            conn: ClientConn {
                client_id,
                peer,
                client_pubkey_b64,
                client_name,
                tx: out_tx.clone(),
            },
        })
        .await;
}


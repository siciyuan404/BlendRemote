//! 客户端网络层
//!
//! 连接服务端:
//! - TCP Control:握手 + 配对 + 发送 Blender 命令 + 接收状态推送
//!
//! 参考 MeowMic 客户端设计,但精简为仅控制通道(无触摸/音频/视频 UDP)。

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{Mutex, mpsc};

use blendremote_protocol::{ControlMessage, decode_control, encode_control};

use crate::NetError;

/// TCP 连接超时(秒)。不设置时依赖 OS 默认,会让上层只能给出模糊的"连接超时"。
/// 缩短为 3s,使"主机不可达"能快速、准确地上报(映射为 io TimedOut)。
pub const CONNECT_TIMEOUT_SECS: u64 = 3;

/// 客户端事件(从服务端收到)
#[derive(Debug)]
pub enum ClientEvent {
    HelloAck { client_id: u32 },
    /// 服务端要求客户端先完成配对(收到 PairRequired)
    PairRequired {
        server_pubkey: Vec<u8>,
        server_nonce: u64,
    },
    /// 配对响应(收到 PairResponse)
    PairResponse {
        success: bool,
        server_pubkey: Vec<u8>,
        error_msg: String,
    },
    /// Blender 命令执行结果
    BlenderCommandResult {
        id: u64,
        ok: bool,
        result: String,
        error: String,
    },
    /// Blender 状态快照推送
    BlenderStatus { json: String },
    Disconnected,
    Error(NetError),
}

pub struct Client {
    /// TCP 控制流写入端(线程安全,read 端由后台 task 独占)
    control_write: Arc<Mutex<tokio::net::tcp::OwnedWriteHalf>>,
    /// TCP 控制连接是否存活(run_control_recv 退出时自动置 false)
    is_connected: Arc<std::sync::atomic::AtomicBool>,
    /// 下一次命令 id
    cmd_seq: Arc<std::sync::atomic::AtomicU64>,
    /// 等待响应的命令(id → 响应通道)
    pending: Arc<Mutex<std::collections::HashMap<u64, mpsc::Sender<ClientEvent>>>>,
}

impl Client {
    /// 连接到服务端(发送普通 Hello)
    pub async fn connect(
        server_control_addr: &str,
        client_name: &str,
        event_tx: mpsc::Sender<ClientEvent>,
    ) -> Result<Self, NetError> {
        let msg = ControlMessage::Hello {
            client_name: client_name.into(),
            protocol_version: 1,
        };
        Self::connect_with_first_msg(server_control_addr, msg, event_tx).await
    }

    /// 以已配对身份连接服务端(发送 HelloPaired)
    ///
    /// - `client_pubkey`:客户端 Ed25519 公钥(32 字节)
    /// - `nonce`:随机 nonce(每次连接不同)
    /// - `signature`:客户端私钥对 SHA256(client_name || client_pubkey || nonce_le) 的签名(64 字节)
    pub async fn connect_paired(
        server_control_addr: &str,
        client_name: &str,
        client_pubkey: Vec<u8>,
        nonce: u64,
        signature: Vec<u8>,
        event_tx: mpsc::Sender<ClientEvent>,
    ) -> Result<Self, NetError> {
        let msg = ControlMessage::HelloPaired {
            client_name: client_name.into(),
            protocol_version: 1,
            client_pubkey,
            nonce,
            signature,
        };
        Self::connect_with_first_msg(server_control_addr, msg, event_tx).await
    }

    /// 内部:连接服务端并发送第一条控制消息,启动后台任务
    async fn connect_with_first_msg(
        server_control_addr: &str,
        first_msg: ControlMessage,
        event_tx: mpsc::Sender<ClientEvent>,
    ) -> Result<Self, NetError> {
        let control_addr: SocketAddr = server_control_addr
            .parse()
            .map_err(|_| NetError::Handshake(format!("无效地址: {}", server_control_addr)))?;

        let stream = match tokio::time::timeout(
            Duration::from_secs(CONNECT_TIMEOUT_SECS),
            TcpStream::connect(control_addr),
        )
        .await
        {
            Ok(Ok(s)) => s,
            Ok(Err(e)) => return Err(NetError::Io(e)),
            Err(_) => {
                return Err(NetError::Io(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    format!("连接 {} 超时({}s)", control_addr, CONNECT_TIMEOUT_SECS),
                )))
            }
        };
        stream.set_nodelay(true)?;

        // TCP keepalive:通过 socket2 设置 SO_KEEPALIVE,防止 NAT/路由闲置断开
        set_keepalive(&stream);

        // 分离读写半部:read_half 独占给后台接收 task,write_half 用 Mutex 保护
        let (read_half, write_half) = stream.into_split();

        let is_connected = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let cmd_seq = Arc::new(std::sync::atomic::AtomicU64::new(1));
        let pending: Arc<Mutex<std::collections::HashMap<u64, mpsc::Sender<ClientEvent>>>> =
            Arc::new(Mutex::new(std::collections::HashMap::new()));

        let client = Self {
            control_write: Arc::new(Mutex::new(write_half)),
            is_connected: is_connected.clone(),
            cmd_seq: cmd_seq.clone(),
            pending: pending.clone(),
        };

        // 发送第一条消息(Hello 或 HelloPaired)
        client.send_control(first_msg).await?;

        // 启动控制消息接收循环
        let event_tx_clone = event_tx.clone();
        let is_conn = is_connected.clone();
        tokio::spawn(async move {
            run_control_recv(read_half, event_tx_clone, pending).await;
            is_conn.store(false, std::sync::atomic::Ordering::Relaxed);
        });

        // 启动应用层心跳:每 10 秒发送一次 Ping(参考 Sunshine 10s Ping 超时)
        let heartbeat_write = client.control_write.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(10)).await;
                let mut frame = Vec::with_capacity(32);
                if encode_control(&ControlMessage::Ping, &mut frame).is_err() {
                    break;
                }
                let mut stream = heartbeat_write.lock().await;
                if stream.write_all(&frame).await.is_err() {
                    break;
                }
            }
        });

        Ok(client)
    }

    /// 发送控制消息
    pub async fn send_control(&self, msg: ControlMessage) -> Result<(), NetError> {
        let mut stream = self.control_write.lock().await;
        let mut frame = Vec::with_capacity(256);
        encode_control(&msg, &mut frame).map_err(NetError::Protocol)?;
        stream.write_all(&frame).await?;
        Ok(())
    }

    /// 优雅断开
    pub async fn disconnect(&self) -> Result<(), NetError> {
        let _ = self.send_control(ControlMessage::Bye).await;
        Ok(())
    }

    /// 发送配对请求(首次连接收到 PairRequired 后调用)
    ///
    /// - `client_pubkey`:客户端 Ed25519 公钥(32 字节)
    /// - `client_name`:客户端设备名
    /// - `pin`:服务端显示的 6 位 PIN
    /// - `server_nonce`:服务端在 PairRequired 中返回的 nonce
    /// - `signature`:客户端私钥对 server_nonce.to_le_bytes() 的 Ed25519 签名(64 字节)
    pub async fn send_pair_request(
        &self,
        client_pubkey: Vec<u8>,
        client_name: String,
        pin: String,
        server_nonce: u64,
        signature: Vec<u8>,
    ) -> Result<(), NetError> {
        self.send_control(ControlMessage::PairRequest {
            client_pubkey,
            client_name,
            pin,
            server_nonce,
            signature,
        })
        .await
    }

    /// 执行 Blender 命令(异步等待结果)
    ///
    /// - `method`:命令名(如 "view3d.orbit")
    /// - `params`:JSON 对象字符串
    ///
    /// 返回 (ok, result_json, error_msg)
    pub async fn send_blender_command(
        &self,
        method: &str,
        params: &str,
    ) -> Result<(bool, String, String), NetError> {
        let id = self.cmd_seq.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (resp_tx, mut resp_rx) = mpsc::channel::<ClientEvent>(1);
        {
            let mut pending = self.pending.lock().await;
            pending.insert(id, resp_tx);
        }
        if let Err(e) = self
            .send_control(ControlMessage::BlenderCommand {
                id,
                method: method.into(),
                params: params.into(),
            })
            .await
        {
            self.pending.lock().await.remove(&id);
            return Err(e);
        }
        // 等待响应(10s 超时)
        match tokio::time::timeout(Duration::from_secs(10), resp_rx.recv()).await {
            Ok(Some(ClientEvent::BlenderCommandResult {
                id: resp_id,
                ok,
                result,
                error,
            })) => {
                debug_assert_eq!(resp_id, id);
                self.pending.lock().await.remove(&id);
                Ok((ok, result, error))
            }
            Ok(Some(_)) | Ok(None) => {
                self.pending.lock().await.remove(&id);
                Err(NetError::Disconnected)
            }
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(NetError::Handshake("命令响应超时".into()))
            }
        }
    }

    /// 发送 Blender 命令且不等待响应(fire-and-forget)
    ///
    /// 用于高频手势命令(orbit/pan/zoom 等),避免每次等待完整 RTT + 服务端执行。
    /// 与 Moonlight/Sunshine 的低延迟输入流思路一致:只管下发,不阻塞 UI 手势。
    pub async fn send_blender_command_async(
        &self,
        method: &str,
        params: &str,
    ) -> Result<(), NetError> {
        let id = self.cmd_seq.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        self.send_control(ControlMessage::BlenderCommand {
            id,
            method: method.into(),
            params: params.into(),
        })
        .await
    }

    /// 检查 TCP 控制连接是否存活
    pub fn is_connected(&self) -> bool {
        self.is_connected.load(std::sync::atomic::Ordering::Relaxed)
    }
}

async fn run_control_recv(
    mut read_half: tokio::net::tcp::OwnedReadHalf,
    event_tx: mpsc::Sender<ClientEvent>,
    pending: Arc<Mutex<std::collections::HashMap<u64, mpsc::Sender<ClientEvent>>>>,
) {
    let mut read_buf = Vec::with_capacity(4096);
    loop {
        let mut tmp = [0u8; 4096];
        match read_half.read(&mut tmp).await {
            Ok(n) => {
                if n == 0 {
                    let _ = event_tx.send(ClientEvent::Disconnected).await;
                    return;
                }
                read_buf.extend_from_slice(&tmp[..n]);
            }
            Err(e) => {
                let _ = event_tx.send(ClientEvent::Error(NetError::Io(e))).await;
                return;
            }
        }

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

            if let Ok((msg, _)) = decode_control(&msg_bytes) {
                match msg {
                    ControlMessage::HelloAck { client_id, .. } => {
                        let _ = event_tx.send(ClientEvent::HelloAck { client_id }).await;
                    }
                    ControlMessage::PairRequired {
                        server_pubkey,
                        server_nonce,
                    } => {
                        let _ = event_tx
                            .send(ClientEvent::PairRequired {
                                server_pubkey,
                                server_nonce,
                            })
                            .await;
                    }
                    ControlMessage::PairResponse {
                        success,
                        server_pubkey,
                        error_msg,
                    } => {
                        let _ = event_tx
                            .send(ClientEvent::PairResponse {
                                success,
                                server_pubkey,
                                error_msg,
                            })
                            .await;
                    }
                    ControlMessage::BlenderCommandResult {
                        id,
                        ok,
                        result,
                        error,
                    } => {
                        // 优先投递给等待该 id 的调用方
                        let delivered = {
                            let mut pending = pending.lock().await;
                            if let Some(tx) = pending.remove(&id) {
                                let _ = tx
                                    .send(ClientEvent::BlenderCommandResult {
                                        id,
                                        ok,
                                        result: result.clone(),
                                        error: error.clone(),
                                    })
                                    .await;
                                true
                            } else {
                                false
                            }
                        };
                        if !delivered {
                            let _ = event_tx
                                .send(ClientEvent::BlenderCommandResult {
                                    id,
                                    ok,
                                    result: result.clone(),
                                    error: error.clone(),
                                })
                                .await;
                        }
                    }
                    ControlMessage::BlenderStatus { json } => {
                        let _ = event_tx.send(ClientEvent::BlenderStatus { json }).await;
                    }
                    ControlMessage::SyncResp { .. } => {}
                    ControlMessage::Pong => {}
                    _ => {}
                }
            }
        }
    }
}

/// 跨平台设置 TCP keepalive
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
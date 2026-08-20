//! BlendRemote 协议定义
//!
//! 设计原则(参考 MeowMic):
//! - 全部走 TCP 控制通道(低频控制命令,可靠传递)
//! - 控制消息用 bincode(变长消息,4 字节 u32 LE 长度前缀)
//! - 命令负载(method + params)使用 JSON 字符串,方便 Blender Python 端解析
//!
//! 通道模型:
//! - Control: TCP,控制/配对/命令/状态推送
//!
//! 数据流:
//! - 手机 → 服务端: BlenderCommand(执行 Blender 操作)
//! - 服务端 → 手机: BlenderCommandResult(命令结果)
//! - 服务端 → 手机: BlenderStatus(周期推送 Blender 状态快照)

#![forbid(unsafe_op_in_unsafe_fn)]

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// 协议错误
#[derive(Debug, Error)]
pub enum ProtocolError {
    #[error("包过短: 需要 {need} 字节, 实际 {got}")]
    TooShort { need: usize, got: usize },
    #[error("bincode 序列化失败: {0}")]
    Bincode(#[from] bincode::Error),
}

// ============================================================================
// TCP 控制消息:变长,bincode 序列化(4 字节 u32 LE 长度前缀)
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ControlMessage {
    /// 客户端首次连接(未配对)
    Hello {
        client_name: String,
        protocol_version: u32,
    },
    /// 服务端确认连接
    HelloAck {
        server_name: String,
        protocol_version: u32,
        client_id: u32,
    },
    /// 已配对客户端的 Hello:带公钥 + 签名证明身份
    ///
    /// 签名内容 = SHA256(client_name || client_pubkey || nonce_le_bytes)
    /// 服务端查白名单匹配 client_pubkey,验证签名后放行
    HelloPaired {
        client_name: String,
        protocol_version: u32,
        client_pubkey: Vec<u8>,
        nonce: u64,
        signature: Vec<u8>,
    },
    /// 服务端要求客户端先完成配对
    /// server_pubkey:服务端 Ed25519 公钥(原始 32 字节)
    /// server_nonce:服务端生成的随机 nonce,客户端需在 PairRequest 中回传签名
    PairRequired {
        server_pubkey: Vec<u8>,
        server_nonce: u64,
    },
    /// 客户端发起配对请求(首次连接或重新配对)
    ///
    /// - client_pubkey:客户端 Ed25519 公钥(32 字节)
    /// - client_name:设备名(用于服务端白名单显示)
    /// - pin:用户在 PC 端看到的 6 位 PIN
    /// - signature:对 server_nonce 的 Ed25519 签名(用客户端私钥)
    PairRequest {
        client_pubkey: Vec<u8>,
        client_name: String,
        pin: String,
        server_nonce: u64,
        signature: Vec<u8>,
    },
    /// 服务端配对响应
    ///
    /// - success=true:配对成功,server_pubkey 可用于后续验证服务端身份
    /// - success=false:配对失败(PIN 错误/已配对满),error_msg 描述原因
    PairResponse {
        success: bool,
        server_pubkey: Vec<u8>,
        error_msg: String,
    },
    /// 时钟同步请求/响应
    SyncReq { client_ts_ns: u64 },
    SyncResp {
        client_ts_ns: u64,
        server_recv_ts_ns: u64,
        server_send_ts_ns: u64,
    },
    /// 心跳
    Ping,
    Pong,
    /// 优雅断开
    Bye,
    /// 执行 Blender 命令(手机 → 服务端)
    ///
    /// - id:调用方自增 id,用于匹配响应
    /// - method:命令名(如 "view3d.orbit"、"object.delete")
    /// - params:JSON 对象字符串(如 "{\"dx\":12.5,\"dy\":-3.2}")
    BlenderCommand {
        id: u64,
        method: String,
        params: String,
    },
    /// Blender 命令执行结果(服务端 → 手机)
    ///
    /// - ok=true:result 为 JSON 对象字符串
    /// - ok=false:error 描述失败原因
    BlenderCommandResult {
        id: u64,
        ok: bool,
        result: String,
        error: String,
    },
    /// Blender 状态快照推送(服务端周期轮询插件桥后广播给所有客户端)
    BlenderStatus { json: String },
}

/// 控制消息长度前缀编码(4 字节 u32 LE + payload)
pub fn encode_control(msg: &ControlMessage, dst: &mut Vec<u8>) -> Result<(), ProtocolError> {
    dst.clear();
    dst.extend_from_slice(&[0u8; 4]);
    bincode::serialize_into(&mut *dst, msg)?;
    let len = (dst.len() - 4) as u32;
    dst[..4].copy_from_slice(&len.to_le_bytes());
    Ok(())
}

/// 解码控制消息,返回 (消息, 消耗字节数)
pub fn decode_control(buf: &[u8]) -> Result<(ControlMessage, usize), ProtocolError> {
    if buf.len() < 4 {
        return Err(ProtocolError::TooShort {
            need: 4,
            got: buf.len(),
        });
    }
    let len = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
    let total = 4 + len;
    if buf.len() < total {
        return Err(ProtocolError::TooShort {
            need: total,
            got: buf.len(),
        });
    }
    let msg: ControlMessage = bincode::deserialize(&buf[4..total])?;
    Ok((msg, total))
}

// ============================================================================
// 时钟同步
// ============================================================================

#[derive(Debug, Clone, Copy, Default)]
pub struct ClockOffset {
    pub offset_ns: i64,
    pub rtt_ns: u64,
}

impl ClockOffset {
    pub fn from_sync(
        client_ts_ns: u64,
        server_recv_ts_ns: u64,
        server_send_ts_ns: u64,
        client_recv_ts_ns: u64,
    ) -> Self {
        let rtt_ns = client_recv_ts_ns.saturating_sub(client_ts_ns)
            - server_send_ts_ns.saturating_sub(server_recv_ts_ns);
        let offset_ns = (server_recv_ts_ns as i64 - client_ts_ns as i64
            + server_send_ts_ns as i64
            - client_recv_ts_ns as i64)
            / 2;
        Self { offset_ns, rtt_ns }
    }

    pub fn client_to_server(&self, client_ns: u64) -> u64 {
        (client_ns as i64 - self.offset_ns) as u64
    }

    pub fn server_to_client(&self, server_ns: u64) -> u64 {
        (server_ns as i64 + self.offset_ns) as u64
    }
}

/// 单调时钟纳秒
pub fn monotonic_ns() -> u64 {
    use std::sync::OnceLock;
    use std::time::Instant;
    static EPOCH: OnceLock<Instant> = OnceLock::new();
    let epoch = EPOCH.get_or_init(Instant::now);
    epoch.elapsed().as_nanos() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_message_roundtrip() {
        let msg = ControlMessage::Hello {
            client_name: "Pixel-8".into(),
            protocol_version: 1,
        };
        let mut buf = Vec::new();
        encode_control(&msg, &mut buf).unwrap();
        let (decoded, consumed) = decode_control(&buf).unwrap();
        assert_eq!(consumed, buf.len());
        match decoded {
            ControlMessage::Hello {
                client_name, ..
            } => assert_eq!(client_name, "Pixel-8"),
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn blender_command_roundtrip() {
        let msg = ControlMessage::BlenderCommand {
            id: 7,
            method: "view3d.orbit".into(),
            params: r#"{"dx":1.5,"dy":-2.0}"#.into(),
        };
        let mut buf = Vec::new();
        encode_control(&msg, &mut buf).unwrap();
        let (decoded, _) = decode_control(&buf).unwrap();
        match decoded {
            ControlMessage::BlenderCommand { id, method, .. } => {
                assert_eq!(id, 7);
                assert_eq!(method, "view3d.orbit");
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn blender_command_result_roundtrip() {
        let msg = ControlMessage::BlenderCommandResult {
            id: 7,
            ok: true,
            result: r#"{"frame":42}"#.into(),
            error: String::new(),
        };
        let mut buf = Vec::new();
        encode_control(&msg, &mut buf).unwrap();
        let (decoded, _) = decode_control(&buf).unwrap();
        match decoded {
            ControlMessage::BlenderCommandResult { id, ok, result, .. } => {
                assert_eq!(id, 7);
                assert!(ok);
                assert_eq!(result, r#"{"frame":42}"#);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn short_buffer_rejected() {
        let buf = [0u8, 1, 2];
        assert!(decode_control(&buf).is_err());
    }

    #[test]
    fn clock_offset_symmetric() {
        let offset = ClockOffset::from_sync(1_000_000_000, 50, 50, 1_000_000_100);
        assert_eq!(offset.rtt_ns, 100);
        assert_eq!(offset.offset_ns, -1_000_000_000);
    }
}
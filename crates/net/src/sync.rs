//! 时钟同步(客户端侧)
//!
//! 参考 MeowMic:通过 SyncReq/SyncResp 往返计算时钟偏移(EWMA 平滑)。

use std::sync::Arc;
use std::time::Duration;

use blendremote_protocol::ClockOffset;
use tokio::sync::RwLock;

/// 客户端时钟同步器
#[derive(Clone, Default)]
pub struct ClockSynchronizer {
    inner: Arc<RwLock<SyncInner>>,
}

#[derive(Default)]
struct SyncInner {
    offset: ClockOffset,
    /// 上一次同步的时间(用于 EWMA 衰减判断)
    last_sync_ns: u64,
}

impl ClockSynchronizer {
    pub fn new() -> Self {
        Self::default()
    }

    /// 处理服务端 SyncResp,更新时钟偏移(EWMA 平滑)
    pub async fn handle_sync_resp(
        &self,
        client_ts_ns: u64,
        server_recv_ts_ns: u64,
        server_send_ts_ns: u64,
    ) {
        let client_recv_ts_ns = blendremote_protocol::monotonic_ns();
        let sample = ClockOffset::from_sync(
            client_ts_ns,
            server_recv_ts_ns,
            server_send_ts_ns,
            client_recv_ts_ns,
        );
        let mut inner = self.inner.write().await;
        // EWMA:新样本权重 0.25,历史权重 0.75
        if inner.last_sync_ns != 0 {
            inner.offset.offset_ns =
                (inner.offset.offset_ns * 3 + sample.offset_ns) / 4;
            inner.offset.rtt_ns =
                (inner.offset.rtt_ns * 3 + sample.rtt_ns) / 4;
        } else {
            inner.offset = sample;
        }
        inner.last_sync_ns = client_recv_ts_ns;
    }

    /// 当前时钟偏移状态
    pub async fn state(&self) -> SyncState {
        let inner = self.inner.read().await;
        SyncState {
            offset: inner.offset,
        }
    }
}

/// 时钟同步状态快照
#[derive(Debug, Clone, Copy)]
pub struct SyncState {
    pub offset: ClockOffset,
}

/// 发送同步请求的间隔(由调用方的心跳循环控制,此处仅导出常量)
pub const SYNC_INTERVAL: Duration = Duration::from_secs(2);
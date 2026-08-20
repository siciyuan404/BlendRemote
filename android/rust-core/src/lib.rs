//! BlendRemote Android JNI Core
//!
//! 暴露给 Kotlin 的 native 方法,封装在 `com.blendremote.client.NativeBridge` 类。
//! 加载的 so 库名为 libblendremote.so(由 Cargo.toml 的 [lib].name 指定)。
//!
//! 架构(参考 MeowMic):
//! - 全局 tokio runtime + Client 实例
//! - 命令发送用 block_on(低频控制命令,同步等待结果)
//! - 状态推送在后台 task 接收,存最新快照供 Kotlin 轮询
//!
//! 配对流程:
//! 1. nativeConnect 返回 1=已连接 / 2=需要配对 / 0=失败
//!    (3=地址无效 / 4=主机不可达 / 5=连接被拒绝)
//! 2. 若需要配对,Kotlin 弹出 PIN 输入框,调用 nativeCompletePairing(pin)
//! 3. 配对成功后自动重连(发送 HelloPaired),返回 1=已连接 / 0=失败
//! 4. 后续连接若已配对该服务端,nativeConnect 自动发送 HelloPaired

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};

/// 安全获取 Mutex 锁:即使锁被中毒也恢复数据而非 panic。
/// JNI 函数中 panic 会触发 abort()→SIGABRT→进程死亡。
fn lock_or_recover<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

use base64::Engine;
use jni::objects::{JClass, JString};
use jni::sys::{jboolean, jint, jstring, JNI_FALSE, JNI_TRUE};
use jni::JNIEnv;
use log::LevelFilter;
use once_cell::sync::OnceCell;
use tokio::runtime::Runtime;

use blendremote_net::pairing::ClientPairingState;
use blendremote_net::{Client, ClientEvent};

/// base64 标准编码引擎
const B64: base64::engine::general_purpose::GeneralPurpose =
    base64::engine::general_purpose::STANDARD;

/// 已连接状态
struct State {
    rt: Runtime,
    client: Arc<Client>,
    /// 命令发送计数(统计用)
    cmd_sent: AtomicU64,
}

/// 配对中状态(收到 PairRequired 后保存,等待用户输入 PIN)
struct PendingPairing {
    client: Arc<Client>,
    event_rx: tokio::sync::mpsc::Receiver<ClientEvent>,
    server_nonce: u64,
    client_state: ClientPairingState,
    server_addr: String,
    client_name: String,
}

static STATE: OnceLock<Mutex<Option<State>>> = OnceLock::new();
static PENDING_PAIRING: OnceLock<Mutex<Option<PendingPairing>>> = OnceLock::new();
static STATE_DIR: OnceCell<PathBuf> = OnceCell::new();
static LOGGER_INIT: OnceCell<()> = OnceCell::new();
/// 最新 Blender 状态快照(Kotlin 通过 nativePollStatus 轮询)
static STATUS_CACHE: OnceLock<Mutex<Option<String>>> = OnceLock::new();

fn init_logger() {
    LOGGER_INIT.get_or_init(|| {
        android_logger::init_once(
            android_logger::Config::default()
                .with_tag("BlendRemote")
                .with_max_level(LevelFilter::Info),
        );
    });
}

fn state() -> &'static Mutex<Option<State>> {
    STATE.get_or_init(|| Mutex::new(None))
}

fn pending() -> &'static Mutex<Option<PendingPairing>> {
    PENDING_PAIRING.get_or_init(|| Mutex::new(None))
}

fn status_cache() -> &'static Mutex<Option<String>> {
    STATUS_CACHE.get_or_init(|| Mutex::new(None))
}

fn state_dir() -> Option<PathBuf> {
    STATE_DIR.get().cloned()
}

/// 客户端配对状态文件路径
fn client_state_path() -> PathBuf {
    state_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join("client-pairing.json")
}

fn load_client_state() -> Option<ClientPairingState> {
    ClientPairingState::load_or_create(&client_state_path()).ok()
}

// ============================================================================
// 连接
// ============================================================================

/// 连接到服务端。若客户端已配对该服务端公钥,自动走 HelloPaired。
///
/// 返回:0=通用失败, 1=已连接, 2=需要配对, 3=地址无效, 4=主机不可达, 5=连接被拒绝
fn connect_internal(server_addr: &str, client_name: &str, paired: bool) -> jint {
    init_logger();
    let rt = match Runtime::new() {
        Ok(rt) => rt,
        Err(e) => {
            log::error!("tokio runtime 创建失败: {}", e);
            return 0;
        }
    };

    // 地址校验
    if server_addr.parse::<std::net::SocketAddr>().is_err() {
        log::warn!("无效地址: {}", server_addr);
        return 3;
    }

    let client_state = load_client_state();
    let (event_tx, event_rx) = tokio::sync::mpsc::channel::<ClientEvent>(32);

    // 决定第一条消息:已配对该服务端 → HelloPaired
    let result = if paired {
        match &client_state {
            Some(cs) => {
                let nonce = blendremote_net::generate_nonce();
                match cs.sign_paired_hello(client_name, nonce) {
                    Ok((pubkey, sig)) => rt.block_on(Client::connect_paired(
                        server_addr,
                        client_name,
                        pubkey,
                        nonce,
                        sig,
                        event_tx.clone(),
                    )),
                    Err(e) => {
                        log::warn!("HelloPaired 签名失败: {}", e);
                        return 0;
                    }
                }
            }
            None => {
                log::warn!("强制配对但本地无客户端密钥");
                return 0;
            }
        }
    } else if let Some(cs) = &client_state {
        if !cs.paired_servers.is_empty() {
            // 尝试 HelloPaired,失败回退普通 Hello
            let nonce = blendremote_net::generate_nonce();
            match cs.sign_paired_hello(client_name, nonce) {
                Ok((pubkey, sig)) => {
                    match rt.block_on(Client::connect_paired(
                        server_addr,
                        client_name,
                        pubkey,
                        nonce,
                        sig,
                        event_tx.clone(),
                    )) {
                        Ok(c) => Ok(c),
                        Err(e) => {
                            log::info!("HelloPaired 被拒,回退普通 Hello: {}", e);
                            rt.block_on(Client::connect(server_addr, client_name, event_tx.clone()))
                        }
                    }
                }
                Err(e) => {
                    log::warn!("HelloPaired 签名失败,回退普通 Hello: {}", e);
                    rt.block_on(Client::connect(server_addr, client_name, event_tx.clone()))
                }
            }
        } else {
            rt.block_on(Client::connect(server_addr, client_name, event_tx.clone()))
        }
    } else {
        rt.block_on(Client::connect(server_addr, client_name, event_tx.clone()))
    };

    let client = match result {
        Ok(c) => Arc::new(c),
        Err(e) => {
            log::warn!("连接失败: {} err={}", server_addr, e);
            return match e {
                blendremote_net::NetError::Io(io)
                    if io.kind() == std::io::ErrorKind::TimedOut =>
                {
                    4
                }
                blendremote_net::NetError::Io(io)
                    if io.kind() == std::io::ErrorKind::ConnectionRefused =>
                {
                    5
                }
                _ => 0,
            };
        }
    };

    // 若服务端要求配对,会立刻收到 PairRequired(有配对管理器时,普通 Hello 必回)
    // 等待一小段时间以区分"已连接"与"需要配对"
    let mut event_rx = event_rx;
    let mut pair_required = None;
    for _ in 0..50 {
        match event_rx.try_recv() {
            Ok(ClientEvent::PairRequired {
                server_pubkey,
                server_nonce,
            }) => {
                pair_required = Some((server_pubkey, server_nonce));
                break;
            }
            Ok(ClientEvent::HelloAck { .. }) => break,
            Ok(_) => continue,
            Err(tokio::sync::mpsc::error::TryRecvError::Empty) => {
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            Err(_) => break,
        }
    }

    if let Some((_server_pubkey, server_nonce)) = pair_required {
        let cs = match client_state {
            Some(cs) => cs,
            None => {
                log::error!("需要配对但客户端密钥状态不可用");
                return 0;
            }
        };
        *lock_or_recover(pending()) = Some(PendingPairing {
            client: client.clone(),
            event_rx,
            server_nonce,
            client_state: cs,
            server_addr: server_addr.into(),
            client_name: client_name.into(),
        });
        log::info!("需要配对: {}", server_addr);
        return 2;
    }

    // 已连接
    spawn_event_loop(event_rx, &rt);
    *lock_or_recover(state()) = Some(State {
        rt,
        client: client.clone(),
        cmd_sent: AtomicU64::new(0),
    });
    log::info!("已连接: {}", server_addr);
    1
}

/// 后台事件循环:处理 BlenderStatus 推送,写入状态缓存
fn spawn_event_loop(mut event_rx: tokio::sync::mpsc::Receiver<ClientEvent>, rt: &Runtime) {
    let cache = status_cache();
    rt.spawn(async move {
        while let Some(event) = event_rx.recv().await {
            match event {
                ClientEvent::BlenderStatus { json } => {
                    *lock_or_recover(&cache) = Some(json);
                }
                ClientEvent::Disconnected => break,
                _ => {}
            }
        }
    });
}

// ============================================================================
// JNI 入口
// ============================================================================

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeSetStateDir(
    mut env: JNIEnv,
    _class: JClass,
    path: JString,
) {
    let path: String = env.get_string(&path).map(|s| s.into()).unwrap_or_default();
    let _ = STATE_DIR.set(PathBuf::from(path));
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeConnect(
    mut env: JNIEnv,
    _class: JClass,
    server_addr: JString,
    client_name: JString,
) -> jint {
    let addr: String = env.get_string(&server_addr).map(|s| s.into()).unwrap_or_default();
    let name: String = env.get_string(&client_name).map(|s| s.into()).unwrap_or_default();
    connect_internal(&addr, &name, false)
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeConnectPaired(
    mut env: JNIEnv,
    _class: JClass,
    server_addr: JString,
    client_name: JString,
) -> jint {
    let addr: String = env.get_string(&server_addr).map(|s| s.into()).unwrap_or_default();
    let name: String = env.get_string(&client_name).map(|s| s.into()).unwrap_or_default();
    connect_internal(&addr, &name, true)
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeCompletePairing(
    mut env: JNIEnv,
    _class: JClass,
    pin: JString,
) -> jint {
    let pin: String = env.get_string(&pin).map(|s| s.into()).unwrap_or_default();
    let pp = lock_or_recover(pending()).take();
    let pp = match pp {
        Some(pp) => pp,
        None => return 0,
    };

    let signature = match pp.client_state.sign_server_nonce(pp.server_nonce) {
        Ok(sig) => sig,
        Err(e) => {
            log::error!("nonce 签名失败: {}", e);
            return 0;
        }
    };
    let pubkey = match pp.client_state.pubkey() {
        Ok(pk) => pk.to_vec(),
        Err(e) => {
            log::error!("读取客户端公钥失败: {}", e);
            return 0;
        }
    };

    // 发送 PairRequest,等待 PairResponse(通过 PendingPairing 保存的事件通道接收)
    let mut event_rx = pp.event_rx;
    let _ = pp.client.send_pair_request(
        pubkey,
        pp.client_name.clone(),
        pin,
        pp.server_nonce,
        signature,
    );

    // 等待配对响应(最长 8s)
    let mut response = None;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(8);
    while std::time::Instant::now() < deadline {
        match event_rx.try_recv() {
            Ok(ClientEvent::PairResponse { success, server_pubkey, .. }) => {
                response = Some((success, server_pubkey));
                break;
            }
            Ok(_) => {}
            Err(tokio::sync::mpsc::error::TryRecvError::Empty) => {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => break,
        }
    }

    match response {
        Some((true, server_pubkey)) => {
            // 配对成功:保存服务端公钥,重连(HelloPaired)
            let mut cs = pp.client_state;
            cs.add_paired_server(B64.encode(&server_pubkey));
            if let Err(e) = cs.save(&client_state_path()) {
                log::warn!("配对状态保存失败: {}", e);
            }
            // 重连
            let result = connect_internal(&pp.server_addr, &pp.client_name, true);
            if result == 1 {
                1
            } else {
                log::warn!("配对后重连失败: {}", result);
                0
            }
        }
        Some((false, _)) => {
            log::warn!("配对被拒绝(PIN 错误等)");
            6
        }
        None => {
            log::warn!("配对响应超时");
            7
        }
    }
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeCancelPairing(
    _env: JNIEnv,
    _class: JClass,
) {
    *lock_or_recover(pending()) = None;
    *lock_or_recover(state()) = None;
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeIsServerPaired(
    mut env: JNIEnv,
    _class: JClass,
    server_pubkey_b64: JString,
) -> jboolean {
    let pk: String = env
        .get_string(&server_pubkey_b64)
        .map(|s| s.into())
        .unwrap_or_default();
    match load_client_state() {
        Some(cs) => {
            if cs.is_paired_with(&pk) {
                JNI_TRUE
            } else {
                JNI_FALSE
            }
        }
        None => JNI_FALSE,
    }
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeGetClientPubkeyB64(
    env: JNIEnv,
    _class: JClass,
) -> jstring {
    let result = match load_client_state() {
        Some(cs) => cs.pubkey_b64().unwrap_or_default(),
        None => String::new(),
    };
    env.new_string(&result).map(|s| s.into_raw()).unwrap_or(std::ptr::null_mut())
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeIsConnected(
    _env: JNIEnv,
    _class: JClass,
) -> jboolean {
    let guard = lock_or_recover(state());
    match guard.as_ref() {
        Some(s) if s.client.is_connected() => JNI_TRUE,
        _ => JNI_FALSE,
    }
}

/// 发送 Blender 命令,返回 JSON: {"ok": bool, "result": ..., "error": "..."}
#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeSendBlenderCommand(
    mut env: JNIEnv,
    _class: JClass,
    method: JString,
    params_json: JString,
) -> jstring {
    let method: String = env.get_string(&method).map(|s| s.into()).unwrap_or_default();
    let params: String = env.get_string(&params_json).map(|s| s.into()).unwrap_or_default();

    let (ok, result, error) = {
        let guard = lock_or_recover(state());
        match guard.as_ref() {
            Some(s) => {
                s.cmd_sent.fetch_add(1, Ordering::Relaxed);
                let client = s.client.clone();
                let rt = &s.rt;
                match rt.block_on(client.send_blender_command(&method, &params)) {
                    Ok((ok, result, error)) => (ok, result, error),
                    Err(e) => (false, String::new(), format!("连接错误: {}", e)),
                }
            }
            None => (false, String::new(), "未连接".into()),
        }
    };

    let json = serde_json::json!({
        "ok": ok,
        "result": serde_json::from_str::<serde_json::Value>(&result).unwrap_or(serde_json::Value::Null),
        "error": error,
    })
    .to_string();
    env.new_string(&json).map(|s| s.into_raw()).unwrap_or(std::ptr::null_mut())
}

/// 轮询最新 Blender 状态快照 JSON(无更新时返回 "")
#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativePollStatus(
    env: JNIEnv,
    _class: JClass,
) -> jstring {
    let value = lock_or_recover(status_cache())
        .clone()
        .unwrap_or_default();
    env.new_string(&value).map(|s| s.into_raw()).unwrap_or(std::ptr::null_mut())
}

#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeDisconnect(
    _env: JNIEnv,
    _class: JClass,
) {
    let mut guard = lock_or_recover(state());
    if let Some(s) = guard.take() {
        let _ = s.rt.block_on(s.client.disconnect());
    }
    *lock_or_recover(pending()) = None;
}

/// 统计 JSON: {"cmd_sent": N}
#[no_mangle]
pub extern "system" fn Java_com_blendremote_client_NativeBridge_nativeGetStats(
    env: JNIEnv,
    _class: JClass,
) -> jstring {
    let cmd_sent = lock_or_recover(state())
        .as_ref()
        .map(|s| s.cmd_sent.load(Ordering::Relaxed))
        .unwrap_or(0);
    let json = format!(r#"{{"cmd_sent":{}}}"#, cmd_sent);
    env.new_string(&json).map(|s| s.into_raw()).unwrap_or(std::ptr::null_mut())
}

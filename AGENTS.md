# BlendRemote 开发指南

## 项目结构

```
crates/protocol  协议层:ControlMessage 编解码(u32 LE 长度前缀 + bincode)
crates/net       网络层:配对(Ed25519+PIN)、mDNS 发现、TCP server/client、状态同步
pc/server        blendremote-server 守护进程(Windows/Linux)
blender-addon    Blender Python 插件(本地 HTTP 命令桥 + N 面板 UI)
android/rust-core  Android JNI 核心(libblendremote.so)
android/app      Android 客户端(Kotlin Compose)
.github/workflows CI:addon 打包 / server 构建 / APK 构建
```

## 常用命令

```powershell
# Rust 全量检查 + 测试(Windows 本机)
cargo build            # 工作区构建(不含 android/rust-core,已 exclude)
cargo test             # protocol + net 单元测试
cargo build --release -p blendremote-server

# Android JNI 核心(独立于工作区)
cd android/rust-core
cargo check            # host 平台语法检查
cargo ndk -t arm64-v8a -o ../app/src/main/jniLibs build --release   # CI 用

# Blender 插件语法检查(无需 bpy)
python -m py_compile blender-addon/*.py
```

## 端口约定(不可随意更改)

| 端口 | 用途 | 绑定 | 说明 |
|---|---|---|---|
| 28900 (base) | control TCP(bincode) | 0.0.0.0 | 手机↔server 控制通道 |
| base+4 = 28904 | /serverinfo HTTP | 0.0.0.0 | 局域网探测 + pair_status |
| base+5 = 28905 | /pairing HTTP | 127.0.0.1 | 仅插件 N 面板读取 PIN |
| 29390 | addon 命令桥 HTTP | 127.0.0.1 | server→Blender:POST /cmd、GET /status、GET /health |

## 协议要点

- TCP 帧:`u32 LE 长度 + bincode(ControlMessage)`
- 配对:服务端首次启动生成 Ed25519 密钥对,持久化 `%APPDATA%/blendremote/pairing.json`;
  启动时生成 6 位 PIN(插件面板展示);客户端 PIN + 对 server_nonce 签名完成配对;
  后续连接走 HelloPaired(签名内容 = SHA256(client_name || client_pubkey || nonce_le))。
- mDNS:`_blendremote._tcp.`,TXT 字段 v/name/pk;serverinfo 端口 = control + 4。
- Blender 命令:JSON 负载 {method, params};方法表见 blender-addon/commands.py 的 REGISTRY。

## 新增命令流程

1. `blender-addon/commands.py` REGISTRY 增加分发函数
2. 手机端 `BlendRemoteViewModel` 增加对应 wrapper(send/fire)
3. 若需 UI 按钮:在 `ui/ControlScreen.kt` 对应面板添加
4. 修改后重载插件(不要仅改文件,需在 Blender 里禁用/启用)

## 注意事项

- bpy 只能在主线程调用:bridge.py 用 `bpy.app.timers` + 线程队列编组,禁止在 HTTP 线程直接调 bpy。
- JNI 函数禁止 panic:`lock_or_recover` 模式防 JNI abort;native 阻塞调用在 Kotlin 侧用独立线程 + 看门狗。
- Android 构建需要 cargo-ndk + Android SDK/NDK,本机无 SDK 时用 CI 验证。
- 版本发布:打 tag `v0.1.0` → CI 自动注入版本并发布(zip addon / exe / apk)。
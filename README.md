# BlendRemote

通过局域网手机远程控制 Blender:视图导航、对象操作、动画播放、渲染控制、自定义按钮。

## 架构

```
┌─────────────┐  TCP(bincode)  ┌──────────────────┐  HTTP(127.0.0.1:29390)  ┌─────────────┐
│ Android 手机 │◄──────────────►│ blendremote-server │◄───────────────────────►│ Blender 插件 │
└─────────────┘  mDNS + 配对   └──────────────────┘    POST /cmd、GET /status  └─────────────┘
```

- **Blender 插件**(blender-addon):N 面板显示配对 PIN、启停服务;本地 HTTP 桥把命令编组到 bpy 主线程执行
- **blendremote-server**(Rust):局域网网关,负责 mDNS 发现、Ed25519 + PIN 配对、命令转发、状态轮询广播
- **Android 客户端**(Kotlin Compose + Rust JNI):mDNS 自动发现 PC、PIN 配对、手势/按钮控制

## 功能

| 分类 | 命令 |
|---|---|
| 视图 | orbit/pan/zoom、预设视角(front/top/...)、着色方式、透视切换、框选全部 |
| 模式/对象 | OBJECT/EDIT/SCULPT... 模式切换、添加/删除/复制/全选对象 |
| 动画 | 播放/暂停、跳帧、首末帧、插入/前后关键帧 |
| 渲染 | 渲染当前帧/动画、引擎切换、取消 |
| 自定义 | 插件偏好设置里定义 operator 字符串,手机端生成按钮一键执行 |

## 安装

### 1. Blender 插件

1. 下载 `blendremote-addon-<version>.zip`(GitHub Releases)
2. Blender:编辑 → 偏好设置 → 插件 → 安装 → 选择 zip
3. 在 3D 视口 N 面板找到 **BlendRemote** 标签,设置 blendremote-server 路径并启动
4. 记下显示的 6 位 PIN

### 2. blendremote-server

Windows 直接下载 exe;Linux 用 cargo 构建:

```bash
cargo build --release -p blendremote-server
./target/release/blendremote-server
```

### 3. Android 客户端

安装 `blendremote-<version>.apk`,打开后:
- 自动发现局域网内运行的 server,或手动输入 `PC_IP`(默认端口 28900)
- 首次连接输入 N 面板上的 PIN 完成配对(Ed25519 密钥对持久化,之后免密)

## 开发

见 [AGENTS.md](AGENTS.md):端口约定、协议要点、新增命令流程。

## 端口

| 端口 | 用途 |
|---|---|
| 28900 | control TCP(手机 ↔ server) |
| 28904 | /serverinfo HTTP(局域网探测) |
| 28905 | /pairing HTTP(仅本机,插件读 PIN) |
| 29390 | addon 命令桥 HTTP(仅本机) |
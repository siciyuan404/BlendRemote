"""本地 HTTP 命令桥。

运行在 127.0.0.1:{bridge_port}(默认 29390),仅供本机 Rust 服务访问。
由于 bpy 只能在 Blender 主线程调用,HTTP 线程只负责收请求/回响应,
真正的命令执行通过队列交给主线程的 bpy.app.timers 驱动。

端点:
- POST /cmd    {"method": "...", "params": {...}} → {"ok": bool, "result": ..., "error": str}
- GET  /status → 最近一次主线程刷新的状态快照
- GET  /health → {"ok": true}
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import commands
from . import status as status_module


class CommandExecutor:
    """命令执行器:HTTP 线程入队,主线程 timer 处理。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue = []
        self._status = {}
        self._status_ready = False
        # 状态刷新节流:高频执行命令,低频重建状态快照(降低 bpy 主线程开销)
        self._last_status_time = 0.0
        self._status_interval = 0.5

    def enqueue(self, method, params, timeout=8.0):
        """入队并等待主线程执行完成,返回 {"ok":..,"result":..,"error":..}。"""
        event = threading.Event()
        item = {
            "method": method,
            "params": params,
            "event": event,
            "result": {"ok": False, "error": "命令未执行"},
        }
        with self._lock:
            self._queue.append(item)
        event.wait(timeout)
        return item["result"]

    def process(self):
        """主线程 timer 回调:执行命令队列 + 节流刷新状态缓存。"""
        import time
        with self._lock:
            items = self._queue
            self._queue = []
        for item in items:
            try:
                item["result"] = commands.dispatch(item["method"], item["params"])
            except Exception as e:  # 兜底,dispatch 内部已捕获大部分异常
                item["result"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            finally:
                item["event"].set()
        # 节流刷新状态缓存(每 status_interval 秒一次,避免高频率 timer 拖慢 Blender)
        now = time.monotonic()
        if now - self._last_status_time >= self._status_interval:
            self._last_status_time = now
            snap = status_module.build_status()
            if snap is not None:
                with self._lock:
                    self._status = snap
                    self._status_ready = True

    def status(self):
        with self._lock:
            if not self._status_ready:
                return {"ok": False, "error": "状态尚未就绪"}
            return self._status


# 全局执行器(由 __init__ 创建)
executor = CommandExecutor()


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True, "addon": "blendremote"})
        elif self.path == "/status":
            self._send_json(200, executor.status())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/cmd":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8"))
            method = data.get("method", "")
            params = data.get("params") or {}
            if not method:
                self._send_json(400, {"ok": False, "error": "缺少 method"})
                return
            result = executor.enqueue(method, params)
            self._send_json(200, result)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "JSON 解析失败"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


class BridgeServer:
    """HTTP 桥服务(daemon 线程,不阻塞 Blender 主线程)。"""

    def __init__(self, port):
        self._port = port
        self._httpd = None
        self._thread = None

    def start(self):
        if self._httpd is not None:
            return True, "已在运行"
        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), BridgeHandler)
        except OSError as e:
            return False, f"绑定 127.0.0.1:{self._port} 失败: {e}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name="blendremote-bridge",
        )
        self._thread.start()
        return True, ""

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def port(self):
        return self._port
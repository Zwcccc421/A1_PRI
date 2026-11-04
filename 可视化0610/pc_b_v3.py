"""电脑 B：同时提供大屏页面、静态媒体资源和局域网 WebRTC 设备发现。

使用方法：
1. 将本文件和“大屏可视化v22.html”放到原大屏 HTML/视频/音频资源所在目录。
2. 运行本文件。
3. 浏览器只打开 http://127.0.0.1:8080/ ，不要再双击 HTML(file://)。
4. 调试通话可直接打开 http://127.0.0.1:8080/?call=1
"""
from __future__ import annotations

import json
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

TARGET_PC_A_IP = "10.113.28.88"
HTTP_PORT = 8080
DASHBOARD_NAME = "大屏可视化v22.html"
BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_FILE = BASE_DIR / DASHBOARD_NAME


def probe_a(ip: str) -> dict | None:
    """通过 TCP 8765 验证指定电脑 A 是否可连接。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.2)
            sock.connect((ip, 8765))
            return {"ip": ip, "name": f"通话设备 - {ip}", "port": 8765}
    except OSError:
        return None


def discover_hosts() -> list[dict]:
    """只检测配置的电脑 A，不再扫描局域网中的其他地址。"""
    print(f"[定向扫描] 正在检测电脑 A：{TARGET_PC_A_IP}:8765")
    result = probe_a(TARGET_PC_A_IP)
    if result is None:
        print(f"[定向扫描] 未连接到 {TARGET_PC_A_IP}:8765")
        return []

    print(f"[定向扫描] 已发现：{TARGET_PC_A_IP}")
    return [result]


class Handler(SimpleHTTPRequestHandler):
    """根路径提供大屏；其余视频/音频文件从当前目录静态提供。"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/discover":
            body = json.dumps(discover_hosts(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/call":
            # 顶层通话调试入口，不使用 iframe。
            self.send_response(302)
            self.send_header("Location", "/?call=1")
            self.end_headers()
            return

        if path in ("/", "/dashboard", "/dashboard.html"):
            if not DASHBOARD_FILE.exists():
                message = (
                    f"找不到 {DASHBOARD_NAME}。请把 HTML 和本 Python 文件放到同一目录。"
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)
                return

            body = DASHBOARD_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return

        # MP4/MP3/图片等资源沿用原文件名，从同目录读取。
        super().do_GET()

    def end_headers(self):
        # 本服务只监听 localhost；允许本页面使用摄像头/麦克风。
        # 顶层页面本身不受 iframe Permissions Policy 限制。
        self.send_header("Permissions-Policy", "camera=(self), microphone=(self)")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format, *args):
        # 静态媒体请求非常多，默认不刷屏。
        pass


def main() -> None:
    if not DASHBOARD_FILE.exists():
        print(f"[错误] 未找到：{DASHBOARD_FILE}")
        print("请把两个新文件放到原大屏 HTML 和所有 MP4/MP3 资源所在目录。")
        raise SystemExit(1)

    handler = partial(Handler, directory=str(BASE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    server.daemon_threads = True

    print("=" * 60)
    print("PC_B 顶层直连版已启动")
    print(f"大屏入口：http://127.0.0.1:{HTTP_PORT}/")
    print(f"通话直测：http://127.0.0.1:{HTTP_PORT}/?call=1")
    print("注意：不要再双击 HTML 文件，也不要使用旧 iframe 版本。")
    print("按 Ctrl+C 退出")
    print("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

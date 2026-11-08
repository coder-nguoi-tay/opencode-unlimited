#!/usr/bin/env python3
"""
OpenCode Unlimited - Local Proxy Bridge
Tác giả: coder-nguoi-tay
Mục đích:
- Tạo local proxy bridge thuần Python (100% standard library, không cần cài GOST hay thư viện ngoài).
- Hoạt động trơn tru đa nền tảng (macOS, Linux, Windows 10 & 11).
- Lắng nghe tại 127.0.0.1:local_port và chuyển tiếp toàn bộ traffic sang Upstream Proxy.
"""

import asyncio
import socket
import urllib.request
import urllib.error
import threading
import time
import logging
import platform
import sys

# Đặt timeout toàn cục cho socket để không bao giờ bị treo vĩnh viễn
socket.setdefaulttimeout(10.0)

# Fix mã hóa Unicode tiếng Việt trên Windows Console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class ProxyBridge:
    def __init__(self, upstream_host, upstream_port, local_host="127.0.0.1", local_port=28080, upstream_auth=None,
                 logger=None, idle_timeout=90.0):
        # (host, port, auth) đổi được khi đang chạy qua set_upstream(); kết nối mới dùng giá trị mới nhất
        self._upstream = (upstream_host, int(upstream_port), upstream_auth)
        self.local_host = local_host
        self.local_port = int(local_port)
        # Đóng kết nối zombie khi CẢ HAI chiều cùng im lặng quá idle_timeout giây
        self.idle_timeout = float(idle_timeout)
        self.logger = logger or logging.getLogger("ProxyBridge")
        self.server = None
        self.loop = None
        self.thread = None
        self.running = False

    @property
    def upstream_host(self):
        return self._upstream[0]

    @property
    def upstream_port(self):
        return self._upstream[1]

    @property
    def upstream_auth(self):
        return self._upstream[2]

    def set_upstream(self, host, port, auth=None):
        """Đổi Upstream Proxy cho các kết nối mới (VD: API xoay IP trả về cổng proxy mới).
        Kết nối đang mở giữ nguyên upstream cũ. Trả về True nếu upstream thực sự thay đổi."""
        new = (host, int(port), auth)
        changed = new != self._upstream
        self._upstream = new
        return changed

    @staticmethod
    def _inject_proxy_auth(data, auth):
        """Chèn Proxy-Authorization vào request đầu tiên (CONNECT hoặc HTTP) gửi tới upstream."""
        if data.startswith(b"CONNECT ") or data.startswith(b"GET ") or data.startswith(b"POST "):
            import base64
            auth_str = f"Proxy-Authorization: Basic {base64.b64encode(auth.encode()).decode()}\r\n".encode()
            if b"\r\n\r\n" in data:
                header_part, rest = data.split(b"\r\n\r\n", 1)
                data = header_part + b"\r\n" + auth_str + b"\r\n" + rest
        return data

    async def _pipe(self, reader, writer, activity, inject_auth=None):
        """Chuyển dữ liệu một chiều reader -> writer.
        activity[0] là thời điểm có dữ liệu gần nhất ở BẤT KỲ chiều nào của kết nối: chỉ đóng khi cả hai
        chiều cùng im lặng quá idle_timeout. Trước đây mỗi chiều tự tính timeout riêng nên một response
        stream dài (client không gửi gì thêm, VD: LLM trả lời > 90s) bị cắt ngang giữa chừng."""
        first_chunk = True
        try:
            while self.running and not reader.at_eof():
                remaining = self.idle_timeout - (time.monotonic() - activity[0])
                if remaining <= 0:
                    break  # Cả hai chiều cùng im lặng quá lâu: kết nối zombie
                try:
                    data = await asyncio.wait_for(reader.read(16384), timeout=remaining)
                except asyncio.TimeoutError:
                    continue  # Kiểm tra lại: chiều kia có thể vừa có dữ liệu
                if not data:
                    break
                activity[0] = time.monotonic()
                if first_chunk and inject_auth:
                    data = self._inject_proxy_auth(data, inject_auth)
                first_chunk = False
                writer.write(data)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_connection(self, client_reader, client_writer):
        upstream_host, upstream_port, upstream_auth = self._upstream
        remote_reader = None
        remote_writer = None
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(upstream_host, upstream_port),
                timeout=10.0
            )
        except Exception:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            return

        activity = [time.monotonic()]
        await asyncio.gather(
            self._pipe(client_reader, remote_writer, activity, inject_auth=upstream_auth),
            self._pipe(remote_reader, client_writer, activity),
            return_exceptions=True
        )

    def _run_server(self):
        # Thiết lập Event Loop riêng cho background thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def start():
            # Trên Windows tránh reuse_address=True vì socket semantics khác Unix
            use_reuse = platform.system() != "Windows"
            self.server = await asyncio.start_server(
                self._handle_connection,
                self.local_host,
                self.local_port,
                reuse_address=use_reuse
            )
            self.running = True
            async with self.server:
                await self.server.serve_forever()

        try:
            self.loop.run_until_complete(start())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        except Exception:
            pass
        finally:
            self.running = False
            try:
                # Dọn dẹp an toàn các task còn treo trên loop trước khi đóng
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending and not self.loop.is_closed():
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                if not self.loop.is_closed():
                    self.loop.close()
            except Exception:
                pass

    def start(self):
        """Khởi động bridge trong background thread."""
        if self.running:
            return
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()
        time.sleep(0.5)

    def stop(self):
        """Dừng proxy bridge an toàn."""
        self.running = False
        if self.loop and self.server:
            try:
                self.loop.call_soon_threadsafe(self.server.close)
            except Exception:
                pass
            if self.loop.is_running():
                try:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                except Exception:
                    pass

    def check_health(self, timeout=5):
        """Kiểm tra xem kết nối qua bridge có sống và lấy IP Public không (ưu tiên HTTP nhanh, dự phòng HTTPS)."""
        endpoints = [
            "http://api.ipify.org",
            "http://icanhazip.com",
            "https://api.ipify.org",
            "https://icanhazip.com",
            "http://ifconfig.me/ip"
        ]
        proxy_url = f"http://{self.local_host}:{self.local_port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        for ep in endpoints:
            try:
                req = urllib.request.Request(ep, headers={"User-Agent": "Mozilla/5.0"})
                with opener.open(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        ip = resp.read().decode("utf-8").strip()
                        if ("." in ip or ":" in ip) and len(ip) <= 45:
                            return ip
            except Exception:
                continue
        return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    upstream = sys.argv[1] if len(sys.argv) > 1 else "160.250.166.20:10427"
    if "://" in upstream:
        upstream = upstream.split("://", 1)[1]
    if "@" in upstream:
        upstream = upstream.split("@", 1)[1]
    parts = upstream.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8080

    bridge = ProxyBridge(host, port, local_port=28080)
    print(f"🚀 Khởi động Proxy Bridge tại 127.0.0.1:28080 -> {host}:{port}")
    bridge.start()
    try:
        ip = bridge.check_health(timeout=3)
        print(f"🌐 Health Check Public IP: {ip or 'Disconnect'}")
    finally:
        bridge.stop()
        print("🛑 Đã dừng bridge.")

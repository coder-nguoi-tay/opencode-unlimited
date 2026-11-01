#!/usr/bin/env python3
"""
OpenCode Unlimited - Local Proxy Bridge
Tác giả: coder-nguoi-tay
Mục đích:
- Tạo local proxy bridge thuần Python (100% standard library, không cần cài GOST hay thư viện ngoài).
- Hoạt động đa nền tảng (macOS, Linux, Windows).
- Lắng nghe tại 127.0.0.1:local_port và chuyển tiếp toàn bộ traffic sang Upstream Proxy.
"""

import asyncio
import socket
import urllib.request
import urllib.error
import threading
import time
import logging

class ProxyBridge:
    def __init__(self, upstream_host, upstream_port, local_host="127.0.0.1", local_port=28080, logger=None):
        self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        self.local_host = local_host
        self.local_port = int(local_port)
        self.logger = logger or logging.getLogger("ProxyBridge")
        self.server = None
        self.loop = None
        self.thread = None
        self.running = False

    async def _pipe(self, reader, writer):
        try:
            while self.running and not reader.at_eof():
                data = await reader.read(16384)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
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
        remote_reader = None
        remote_writer = None
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port),
                timeout=10.0
            )
        except Exception as e:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            return

        await asyncio.gather(
            self._pipe(client_reader, remote_writer),
            self._pipe(remote_reader, client_writer),
            return_exceptions=True
        )

    def _run_server(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def start():
            self.server = await asyncio.start_server(
                self._handle_connection,
                self.local_host,
                self.local_port,
                reuse_address=True
            )
            self.running = True
            async with self.server:
                await self.server.serve_forever()

        try:
            self.loop.run_until_complete(start())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            self.running = False

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
            self.loop.call_soon_threadsafe(self.server.close)
            if self.loop.is_running():
                self.loop.stop()

    def check_health(self, timeout=7):
        """Kiểm tra xem kết nối qua bridge có sống và lấy IP Public không."""
        try:
            proxy_url = f"http://{self.local_host}:{self.local_port}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            req = urllib.request.Request(
                "https://api.ipify.org",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    ip = resp.read().decode("utf-8").strip()
                    if "." in ip or ":" in ip:
                        return ip
        except Exception:
            pass
        return None

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    upstream = sys.argv[1] if len(sys.argv) > 1 else "160.250.166.20:10427"
    host, port = upstream.split(":")
    bridge = ProxyBridge(host, int(port), local_port=28080)
    print(f"🚀 Khởi động Proxy Bridge tại 127.0.0.1:28080 -> {upstream}")
    bridge.start()
    try:
        while True:
            ip = bridge.check_health()
            print(f"🌐 Health Check Public IP: {ip or 'Disconnect'}")
            time.sleep(10)
    except KeyboardInterrupt:
        bridge.stop()
        print("\n🛑 Đã dừng bridge.")

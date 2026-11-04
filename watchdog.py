#!/usr/bin/env python3
"""
OpenCode Unlimited - Watchdog & Auto-Resume Engine
Tác giả: coder-nguoi-tay
Mục đích:
- Giám sát file opencode.log trong thời gian thực trên mọi hệ điều hành (macOS, Linux, Windows 10 & 11).
- Phát hiện tức thì các lỗi: 429, 503, QuotaExceeded, today's free messages, ECONNRESET.
- Tự động gọi rotator để cấp IP mới sạch (không trùng lặp).
- Chờ đường truyền và hệ thống ổn định hoàn toàn.
- TỰ ĐỘNG GỬI PROMPT TIẾP TỤC (RESUME) cho phiên làm việc hiện tại mà không cần thao tác tay.
"""

import os
import sys
import time
import re
import json
import sqlite3
import datetime
import threading
import subprocess
import platform
import shutil
import glob

# Fix mã hóa Unicode tiếng Việt trên Windows Console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from spoofer import get_opencode_dir

class OpenCodeWatchdog:
    def __init__(self, rotator, stabilize_wait_seconds=60, auto_resume=True,
                 prompt_resume_text="Tiếp tục thực hiện các bước tiếp theo trong công việc đang làm dở."):
        self.rotator = rotator
        self.stabilize_wait_seconds = int(stabilize_wait_seconds)
        self.auto_resume = auto_resume
        self.prompt_resume_text = prompt_resume_text

        self.opencode_dir = get_opencode_dir()
        self.log_file = os.path.join(self.opencode_dir, "log", "opencode.log")
        self.autoreset_log = os.path.join(self.opencode_dir, "log", "opencode-autoreset.log")
        self.db_file = os.path.join(self.opencode_dir, "opencode.db")

        self.opencode_bin = self._find_opencode_binary()
        self.lock = threading.Lock()
        self.is_handling = False
        self.running = False
        self.current_ip_expire_time = 0

    def _find_opencode_binary(self):
        """Tìm đường dẫn binary thực thi của OpenCode trên hệ thống."""
        # 1. Thử qua PATH
        for cmd_name in ["opencode", "opencode-cli", "OpenCode"]:
            found = shutil.which(cmd_name)
            if found:
                return found

        home = os.path.expanduser("~")
        candidates = []

        # 2. Các đường dẫn đặc thù của Windows
        if platform.system() == "Windows":
            local_app = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
            roaming_app = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
            prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")

            # Tìm trong CLI bundled của OpenCode Desktop (Roaming)
            cli_glob = os.path.join(roaming_app, "ai.opencode.desktop", "cli", "*", "opencode-cli.exe")
            matching_clis = sorted(glob.glob(cli_glob), reverse=True)
            candidates.extend(matching_clis)

            # Tìm trong Programs của OpenCode Desktop
            candidates.append(os.path.join(local_app, "Programs", "ai.opencode.desktop", "resources", "opencode-cli.exe"))
            candidates.append(os.path.join(local_app, "Programs", "ai.opencode.desktop", "OpenCode.exe"))
            candidates.append(os.path.join(local_app, "Programs", "opencode", "opencode.exe"))
            candidates.append(os.path.join(local_app, "Programs", "opencode", "resources", "opencode-cli.exe"))

            # Tìm npm / scoop / chocolatey
            candidates.append(os.path.join(roaming_app, "npm", "opencode.cmd"))
            candidates.append(os.path.join(roaming_app, "npm", "opencode-cli.cmd"))
            candidates.append(os.path.join(home, "scoop", "shims", "opencode.exe"))
            candidates.append(os.path.join(prog_files, "OpenCode", "opencode.exe"))

        # 3. Các đường dẫn mặc định trên macOS / Linux / XDG
        candidates.extend([
            os.path.join(home, ".local", "bin", "opencode"),
            os.path.join(home, ".opencode", "bin", "opencode"),
            os.path.join(home, ".opencode", "bin", "opencode.exe"),
            "/usr/local/bin/opencode",
            "/opt/homebrew/bin/opencode",
        ])

        for c in candidates:
            if os.path.exists(c):
                return c

        return "opencode"

    def _run_cmd(self, args, timeout=10):
        """Chạy lệnh binary của OpenCode tương thích Windows (.cmd / .exe) và Unix."""
        is_windows = platform.system() == "Windows"
        is_batch = is_windows and self.opencode_bin.lower().endswith((".cmd", ".bat"))

        if is_batch:
            full_cmd = ["cmd.exe", "/c", self.opencode_bin] + args[1:]
        else:
            full_cmd = args

        return subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout
        )

    def log(self, msg):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}"
        print(line, flush=True)
        try:
            with open(self.autoreset_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def get_active_sessions(self):
        """Lấy danh sách các session đang chạy từ API của OpenCode."""
        try:
            res = self._run_cmd([self.opencode_bin, "api", "get", "/api/session/active"], timeout=5)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                active = data.get("data", {})
                if isinstance(active, dict):
                    return list(active.keys())
        except Exception:
            pass
        return []

    def get_latest_session_id(self):
        """Lấy session ID gần nhất từ database SQLite của OpenCode (mở chế độ read-only an toàn)."""
        if not os.path.exists(self.db_file):
            return None
        conn = None
        try:
            # Trên Windows dùng URI read-only để tránh conflict với SQLite WAL mode
            db_uri = f"file:{os.path.abspath(self.db_file)}?mode=ro"
            try:
                conn = sqlite3.connect(db_uri, uri=True, timeout=3)
            except Exception:
                conn = sqlite3.connect(self.db_file, timeout=3)

            cursor = conn.cursor()
            # Thử bảng session_v2 (OpenCode hiện đại)
            try:
                cursor.execute("SELECT id FROM session_v2 ORDER BY time_updated DESC LIMIT 1;")
                row = cursor.fetchone()
                if row:
                    return row[0]
            except Exception:
                pass

            # Fallback bảng session cũ nếu có
            try:
                cursor.execute("SELECT id FROM session ORDER BY updated_at DESC LIMIT 1;")
                row = cursor.fetchone()
                if row:
                    return row[0]
            except Exception:
                pass
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return None

    def send_resume_prompt(self, session_id):
        """Gửi prompt tiếp tục tới session qua OpenCode API."""
        self.log(f"💬 Đang tự động gửi prompt tiếp tục tới session: {session_id}...")
        try:
            body = json.dumps({
                "text": self.prompt_resume_text,
                "delivery": "steer",
                "resume": True
            }, ensure_ascii=False)

            cmd = [
                self.opencode_bin, "api", "post",
                f"/api/session/{session_id}/prompt",
                "-d", body
            ]
            res = self._run_cmd(cmd, timeout=15)
            if res.returncode == 0:
                self.log(f"✅ Gửi lệnh tiếp tục thành công! Phiên {session_id} đang chạy tiếp.")
                return True, res.stdout
            else:
                self.log(f"⚠️ Phản hồi khi gửi lệnh tiếp tục (code {res.returncode}): {res.stderr or res.stdout}")
                return False, res.stderr or res.stdout
        except Exception as e:
            self.log(f"⚠️ Ngoại lệ khi gửi lệnh tiếp tục: {e}")
            return False, str(e)

    def handle_limit_and_resume(self, reason, session_hint=None, delay_seconds=0):
        with self.lock:
            if self.is_handling:
                self.log("⏳ Đang trong quá trình reset/resume phiên trước, bỏ qua trigger trùng lặp.")
                return
            self.is_handling = True

        try:
            self.log("========================================================")
            self.log(f"🚨 PHÁT HIỆN LỖI/HẠN MỨC TRONG OPENCODE!")
            self.log(f"   Lý do: {reason}")

            # 1. Đánh dấu IP hiện tại vào blacklist
            failing_ip = self.rotator.check_health()
            if failing_ip:
                self.log(f"🚫 Đánh dấu IP {failing_ip} vào danh sách đã dùng (bị limit: {reason})")
                self.rotator.mark_ip_used(failing_ip, reason=f"Chặn limit: {reason}")

            # 2. Xác định session ID mục tiêu
            target_session = session_hint
            if not target_session:
                active_list = self.get_active_sessions()
                if active_list:
                    target_session = active_list[0]
            if not target_session:
                target_session = self.get_latest_session_id()

            self.log(f"   Target Session: {target_session or 'Chưa xác định'}")

            # 3. Đợi nếu hệ thống yêu cầu retry delay
            if delay_seconds > 0:
                self.log(f"⏳ Cần chờ {delay_seconds}s trước khi reset...")
                time.sleep(delay_seconds)

            # 4. Kích hoạt xoay IP lọc trùng
            self.rotator.notify("OpenCode Auto-Resume", "Đang tự động đổi IP sạch mới...")
            success, new_ip, remaining_seconds = self.rotator.rotate(force=True, reason=f"Hết quota trên session {target_session or 'active'}")

            if not success or not new_ip:
                self.log("❌ Không thể kết nối IP mới sau khi xoay. Tạm hoãn gửi prompt.")
                return

            if remaining_seconds and remaining_seconds > 0:
                self.current_ip_expire_time = time.time() + remaining_seconds

            # 5. Đợi đường truyền ổn định với cơ chế Dynamic Fast Health Probe (tiết kiệm thời gian)
            self.log(f"⏳ Đã có IP mới ({new_ip}). Đang thăm dò độ ổn định đường truyền...")
            self.rotator.notify("OpenCode Auto-Resume", f"Đã có IP mới ({new_ip}). Đang kiểm tra kết nối...")

            # Đợi tối thiểu 10s để modem/trạm phát 4G hoàn tất định tuyến
            time.sleep(10)
            probe_success = False
            for probe_i in range(1, 4):
                probe_ip = self.rotator.check_health()
                if probe_ip:
                    self.log(f"⚡ Đường truyền đã thông suốt và ổn định (Thăm dò {probe_i}/3 thành công, IP: {probe_ip})!")
                    probe_success = True
                    break
                self.log(f"   ⏱️ Đường truyền chưa thông, đang đợi thêm 10s (lần {probe_i}/3)...")
                time.sleep(10)

            if not probe_success:
                fallback_wait = min(15, self.stabilize_wait_seconds)
                self.log(f"⚠️ Thăm dò chưa có phản hồi ngay, đợi thêm {fallback_wait}s an toàn...")
                time.sleep(fallback_wait)

            # 6. Tự động gửi prompt tiếp tục nếu được bật
            if self.auto_resume and target_session:
                self.log(f"🚀 Hết thời gian chờ! Tự động gửi lệnh tiếp tục phiên {target_session}...")
                ok, _ = self.send_resume_prompt(target_session)
                if ok:
                    self.rotator.notify("OpenCode Auto-Resume", f"Đã tự động tiếp tục code phiên {target_session[:12]}...")
                else:
                    time.sleep(5)
                    self.send_resume_prompt(target_session)
            elif not target_session:
                self.log("⚠️ Không tìm thấy session nào để tự động resume.")

            self.log("========================================================")
        except Exception as e:
            self.log(f"❌ Lỗi trong luồng xử lý: {e}")
        finally:
            with self.lock:
                self.is_handling = False

    def start_monitoring(self):
        """Bắt đầu theo dõi opencode.log theo thời gian thực."""
        self.running = True
        self.log("👀 OpenCode Watchdog đang giám sát log thời gian thực...")
        self.log(f"   File log: {self.log_file}")
        self.log(f"   Binary:   {self.opencode_bin}")

        if not os.path.exists(self.log_file):
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                pass

        try:
            f = open(self.log_file, "r", encoding="utf-8", errors="ignore")
            # Quét kiểm tra các dòng cuối cùng xem có lỗi limit/disconnect gần nhất chưa được xử lý không
            try:
                curr_size = os.path.getsize(self.log_file)
                f.seek(max(0, curr_size - 15000), os.SEEK_SET)
                startup_lines = f.readlines()
                for s_line in startup_lines[-10:]:
                    s_lower = s_line.lower()
                    if ("free usage limit reached" in s_lower or "quotaexceeded" in s_lower or 
                        "free messages" in s_lower or "econnreset" in s_lower) and ("failed to drain" in s_lower or "limit reached" in s_lower):
                        s_hint = None
                        m = re.search(r"(?:sessionID|sessionId|session\.id)=([a-zA-Z0-9_-]+)", s_line)
                        if m:
                            s_hint = m.group(1)
                        self.log("⚠️ Phát hiện OpenCode dính lỗi hạn mức/kết nối gần nhất. Tự động chuẩn bị xử lý...")
                        threading.Thread(
                            target=self.handle_limit_and_resume,
                            args=("Lỗi hạn mức phát hiện khi khởi động", s_hint, 2),
                            daemon=True
                        ).start()
                        break
            except Exception:
                pass

            f.seek(0, os.SEEK_END)
            last_trigger_time = 0
            last_pos = f.tell()

            while self.running:
                # Kiểm tra nếu file bị xoay hoặc truncate
                try:
                    curr_size = os.path.getsize(self.log_file)
                    if curr_size < last_pos:
                        f.seek(0, os.SEEK_SET)
                        last_pos = 0
                except FileNotFoundError:
                    time.sleep(1)
                    try:
                        f.close()
                        f = open(self.log_file, "r", encoding="utf-8", errors="ignore")
                        f.seek(0, os.SEEK_END)
                        last_pos = f.tell()
                    except Exception:
                        pass
                except Exception:
                    pass

                try:
                    line = f.readline()
                except Exception:
                    time.sleep(0.5)
                    continue

                if not line:
                    try:
                        f.seek(f.tell())  # Xóa EOF indicator trên Windows C runtime
                    except Exception:
                        pass
                    last_pos = f.tell()

                    # Kiểm tra Proactive TTL: Nếu proxy 4G sắp hết hạn sống (còn < 45s) và OpenCode đang nhàn rỗi
                    now = time.time()
                    if self.current_ip_expire_time > 0 and now > (self.current_ip_expire_time - 45):
                        if (now - last_trigger_time) > 45 and not self.is_handling:
                            self.log("⏱️ Proxy 4G sắp hết hạn sống (còn < 45s). Đang chủ động xoay IP trước để tránh gián đoạn...")
                            last_trigger_time = now
                            self.current_ip_expire_time = 0
                            t = threading.Thread(
                                target=self.handle_limit_and_resume,
                                args=("Chủ động xoay IP trước khi proxy hết hạn (Proactive TTL)", None, 0),
                                daemon=True
                            )
                            t.start()

                    time.sleep(0.4)
                    continue

                line_lower = line.lower()
                is_trigger = False
                reason = ""

                if "free usage limit reached" in line_lower:
                    is_trigger = True
                    reason = "Free usage limit reached"
                elif "quotaexceeded" in line_lower:
                    is_trigger = True
                    reason = "QuotaExceeded"
                elif "today's free messages" in line_lower or "free messages" in line_lower:
                    is_trigger = True
                    reason = "today's free messages"
                elif "freeusagelimiterror" in line_lower:
                    is_trigger = True
                    reason = "FreeUsageLimitError"
                elif "provider.free-limit" in line_lower:
                    is_trigger = True
                    reason = "provider.free-limit"
                elif "resource_exhausted" in line_lower or "usage limit exceeded" in line_lower:
                    is_trigger = True
                    reason = "Resource Exhausted / Usage Limit Exceeded"
                elif 'status":429' in line or 'status": 429' in line or "http 429" in line_lower or "ratelimit" in line_lower:
                    is_trigger = True
                    reason = "HTTP 429 RateLimit"
                elif "http 503" in line_lower or 'status":503' in line or 'status": 503' in line or "service unavailable" in line_lower:
                    is_trigger = True
                    reason = "HTTP 503 Service Unavailable"
                elif "econnreset" in line_lower or "socket connection was closed unexpectedly" in line_lower:
                    is_trigger = True
                    reason = "ECONNRESET / Socket Reset"

                if is_trigger:
                    now = time.time()
                    if now - last_trigger_time < 25:
                        continue
                    last_trigger_time = now

                    session_hint = None
                    m1 = re.search(r"(?:sessionID|sessionId|session\.id)=([a-zA-Z0-9_-]+)", line)
                    if m1:
                        session_hint = m1.group(1)
                    else:
                        m2 = re.search(r"\b(ses_[a-zA-Z0-9]{15,})\b", line)
                        if m2:
                            session_hint = m2.group(1)

                    delay = 0
                    dm = re.search(r"retryAfterMs\":\s*(\d+)", line)
                    if dm:
                        delay = int(dm.group(1)) / 1000.0

                    t = threading.Thread(
                        target=self.handle_limit_and_resume,
                        args=(reason, session_hint, delay),
                        daemon=True
                    )
                    t.start()
        except Exception as e:
            self.log(f"❌ Watchdog gặp lỗi: {e}")
        finally:
            self.running = False
            try:
                f.close()
            except Exception:
                pass

if __name__ == "__main__":
    from rotator import ProxyRotator
    rotator = ProxyRotator("https://proxyxoay.shop/api/get.php?key=test")
    watchdog = OpenCodeWatchdog(rotator)
    print(f"Binary: {watchdog.opencode_bin}")
    print(f"Log: {watchdog.log_file}")
    print(f"Latest Session: {watchdog.get_latest_session_id()}")
    print(f"Active Sessions: {watchdog.get_active_sessions()}")

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

MAX_CONSECUTIVE_SESSION_RETRIES = 3
MAX_CONSECUTIVE_GLOBAL_LIMITS = 3
GLOBAL_LIMIT_COOLDOWN_SECONDS = 900  # 15 phút nếu dính hạn mức toàn cầu

# Tên tiến trình của OpenCode Desktop (OpenCode.exe) và CLI/backend (opencode, opencode-cli)
OPENCODE_PROCESS_NAMES = ("opencode", "opencode-cli")
PRESENCE_CHECK_INTERVAL_SECONDS = 30   # Tần suất kiểm tra OpenCode còn chạy khi đang tạm dừng xoay IP
STARTUP_PENDING_CHECK_SECONDS = 5      # Tần suất kiểm tra OpenCode đã mở chưa để xử lý lỗi cũ lúc khởi động
STARTUP_PENDING_WINDOW_SECONDS = 120   # Quá thời gian này mà OpenCode chưa mở thì bỏ qua lỗi cũ trong log

# Proxy Health Monitor: khi proxy chết, OpenCode chỉ tự thử lại ("Attempt N - retrying") mà không
# ghi gì vào log cho tới khi bỏ cuộc - có thể mất vài phút. Nên watchdog tự thăm dò proxy định kỳ.
PROXY_HEALTH_INTERVAL_SECONDS = 20     # Tần suất thăm dò proxy khi OpenCode đang mở
PROXY_HEALTH_RETRY_SECONDS = 5         # Thăm dò lại nhanh sau một lần lỗi
PROXY_DEAD_THRESHOLD = 2               # Số lần lỗi liên tiếp để coi proxy đã chết
PROXY_DEAD_BACKOFF_MAX_SECONDS = 600   # Xoay IP thất bại liên tục (VD: key API hỏng) thì giãn dần, tối đa 10 phút
RECENT_ROTATION_SECONDS = 120          # ECONNRESET tới trong khoảng này sau khi vừa xoay xong là lỗi của IP cũ

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
        self.handling_kind = None             # "resume" (xoay + gửi tiếp tục) | "rotate_only" (xoay nền)
        self.pending_resume = None            # (reason, session_hint) bị lỗi trong lúc đang xoay nền
        self.running = False
        self.current_ip_expire_time = 0
        self.last_rotation_ok_at = 0
        self._probe_running = False
        self._proxy_fail_count = 0
        self._dead_proxy_failures = 0
        self._next_probe_at = 0
        self.session_retries = {}             # session_id -> int
        self.circuit_broken_sessions = set()  # Các session bị khóa auto-resume để chống lặp vô tận
        self.consecutive_global_limits = 0
        self.global_limit_locked_until = 0
        self._presence_checked_at = None
        self._presence_cache = True

    def is_opencode_running(self):
        """Kiểm tra OpenCode (Desktop hoặc CLI) có đang chạy không.
        Nếu không kiểm tra được thì trả về True để giữ hành vi cũ (không chặn xoay IP)."""
        try:
            if platform.system() == "Windows":
                res = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, errors="ignore", timeout=10,
                    # Không bật cửa sổ console khi service chạy bằng pythonw.exe
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if res.returncode != 0:
                    return True
                targets = {f"{n}.exe" for n in OPENCODE_PROCESS_NAMES}
                for line in res.stdout.splitlines():
                    image = line.split(",", 1)[0].strip().strip('"').lower()
                    if image in targets:
                        return True
                return False

            pattern = "|".join(OPENCODE_PROCESS_NAMES)
            res = subprocess.run(["pgrep", "-i", "-x", pattern], capture_output=True, timeout=10)
            if res.returncode in (0, 1):  # 0 = có tiến trình, 1 = không có
                return res.returncode == 0
            return True
        except Exception:
            return True

    def _opencode_running_cached(self, max_age):
        """Như is_opencode_running() nhưng chỉ kiểm tra lại sau mỗi max_age giây."""
        now = time.time()
        if self._presence_checked_at is None or now - self._presence_checked_at >= max_age:
            self._presence_cache = self.is_opencode_running()
            self._presence_checked_at = now
        return self._presence_cache

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

    def get_latest_session_id(self, max_idle_seconds=900):
        """Lấy session ID gần nhất từ database SQLite của OpenCode (mở chế độ read-only an toàn).
        Chỉ trả về session nếu vừa được cập nhật trong max_idle_seconds (mặc định 15 phút)
        và chưa hoàn tất thành công (không resume session đã xong)."""
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
                now_ms = int(time.time() * 1000)
                min_time_updated = now_ms - (max_idle_seconds * 1000)
                cursor.execute("""
                    SELECT id FROM session_v2 
                    WHERE time_archived IS NULL 
                      AND (idle_outcome IS NULL OR idle_outcome != 'completed')
                      AND time_updated >= ?
                    ORDER BY time_updated DESC 
                    LIMIT 1;
                """, (min_time_updated,))
                row = cursor.fetchone()
                if row:
                    return row[0]
            except Exception:
                pass

            # Fallback bảng session cũ nếu có
            try:
                cursor.execute("SELECT id FROM session WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 1;", (time.time() - max_idle_seconds,))
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

    def _record_rotation(self, remaining_seconds):
        """Ghi nhận một lần xoay IP thành công (hẹn giờ Proactive TTL + mốc vừa xoay)."""
        self.last_rotation_ok_at = time.time()
        if remaining_seconds and remaining_seconds > 0:
            self.current_ip_expire_time = time.time() + remaining_seconds

    def restore_ip_expiry(self):
        """Khôi phục hạn sống của IP đang dùng sau khi tool khởi động lại.
        Không có bước này thì Proactive TTL bị tắt cho tới lần xoay kế tiếp và IP cứ thế chết."""
        state = self.rotator.load_ip_state()
        ip, expire_at = state.get("ip"), state.get("expire_at")
        if not ip or not isinstance(expire_at, (int, float)):
            return
        current_ip = self.rotator.check_health(timeout=3, quick=True)
        if current_ip != ip:
            # IP đã đổi (hoặc proxy đang chết) - hạn đã lưu không còn đúng, Health Monitor sẽ lo
            return
        self.current_ip_expire_time = float(expire_at)
        left = int(expire_at - time.time())
        if left > 0:
            self.log(f"⏱️ Khôi phục hạn sống IP {ip}: còn ~{left}s (~{left/60:.1f} phút).")
        else:
            self.log(f"⏱️ IP {ip} đã quá hạn sống {-left}s - sẽ chủ động xoay IP mới khi OpenCode đang mở.")

    def _rotate_in_background(self, headline, reason, notice):
        """Xoay IP nền (Proactive TTL / proxy chết): chỉ đổi IP và cập nhật hẹn giờ, tuyệt đối không
        tự ý bơm prompt resume vào các session đang rảnh hoặc đã kết thúc.
        Nếu trong lúc xoay mà OpenCode báo một session bị lỗi (bỏ cuộc), lệnh tiếp tục cho session
        đó được gửi ngay khi xoay xong (thay vì bị bỏ qua như trigger trùng lặp).
        Trả về True nếu xoay thành công, None nếu đang bận xử lý việc khác."""
        with self.lock:
            if self.is_handling:
                return None
            self.is_handling = True
            self.handling_kind = "rotate_only"

        success = False
        try:
            self.log("========================================================")
            self.log(headline)
            self.rotator.notify(*notice)
            ok, new_ip, remaining_seconds = self.rotator.rotate(force=True, reason=reason)
            success = bool(ok and new_ip)

            if success:
                self._record_rotation(remaining_seconds)
                self.log(f"✅ Đã cập nhật IP mới ({new_ip}) thành công! Mọi kết nối sẽ tự động định tuyến qua IP mới.")
            else:
                self.log("⚠️ Không thể đổi IP mới, sẽ thử lại ở chu kỳ tiếp theo.")
            self.log("========================================================")
        except Exception as e:
            self.log(f"❌ Lỗi khi xoay IP nền: {e}")
        finally:
            with self.lock:
                pending = self.pending_resume
                self.pending_resume = None
                if pending:
                    self.handling_kind = "resume"  # Giữ nguyên khóa, chuyển thẳng sang gửi lệnh tiếp tục
                else:
                    self.is_handling = False
                    self.handling_kind = None
            if pending:
                self.log("▶️ Đã xoay IP xong - xử lý tiếp phiên bị lỗi trong lúc đang xoay.")
                self._limit_and_resume_body(pending[0], pending[1], 0, skip_rotation=success)
        return success

    def handle_proactive_ttl(self):
        """Chủ động xoay IP khi proxy 4G sắp hết hạn sống (TTL)."""
        return self._rotate_in_background(
            "⏱️ [PROACTIVE TTL] Proxy 4G sắp hết hạn sống (< 45s). Đang chủ động xoay IP mới...",
            "Chủ động xoay IP (Proactive TTL)",
            ("OpenCode Proactive TTL", "Đang chủ động đổi IP sạch mới trước khi proxy hết hạn..."),
        )

    def handle_dead_proxy(self):
        """Proxy không còn phản hồi: xoay IP ngay, không chờ OpenCode thử lại tới lúc bỏ cuộc."""
        return self._rotate_in_background(
            f"💀 [PROXY MONITOR] Proxy không phản hồi {PROXY_DEAD_THRESHOLD} lần liên tiếp (IP đã chết). "
            "Đang xoay IP mới ngay, không chờ OpenCode thử lại...",
            "Proxy mất kết nối (Health Monitor)",
            ("OpenCode Proxy Monitor", "Proxy mất kết nối. Đang đổi IP mới..."),
        )

    def _probe_proxy_health(self):
        """Chạy trong thread riêng: thăm dò proxy, chết PROXY_DEAD_THRESHOLD lần liên tiếp thì xoay IP."""
        try:
            ip = self.rotator.check_health(timeout=4, quick=True)
            now = time.time()
            if self.is_handling:
                # Đang xoay IP / resume - kết quả thăm dò lúc này không có ý nghĩa
                self._proxy_fail_count = 0
                self._next_probe_at = now + PROXY_HEALTH_INTERVAL_SECONDS
                return
            if ip:
                if self._proxy_fail_count:
                    self.log(f"✅ Proxy đã phản hồi lại bình thường (IP: {ip}).")
                self._proxy_fail_count = 0
                self._dead_proxy_failures = 0
                self._next_probe_at = now + PROXY_HEALTH_INTERVAL_SECONDS
                return

            self._proxy_fail_count += 1
            if self._proxy_fail_count < PROXY_DEAD_THRESHOLD:
                self.log(f"⚠️ Proxy không phản hồi (lần {self._proxy_fail_count}/{PROXY_DEAD_THRESHOLD}), "
                         f"thăm dò lại sau {PROXY_HEALTH_RETRY_SECONDS}s...")
                self._next_probe_at = now + PROXY_HEALTH_RETRY_SECONDS
                return

            self._proxy_fail_count = 0
            self.current_ip_expire_time = 0  # IP đã chết - bỏ hẹn giờ TTL cũ
            result = self.handle_dead_proxy()
            if result is False:
                self._dead_proxy_failures += 1
                backoff = min(PROXY_DEAD_BACKOFF_MAX_SECONDS, 60 * (2 ** (self._dead_proxy_failures - 1)))
                self.log(f"⏳ Xoay IP thất bại {self._dead_proxy_failures} lần liên tiếp - kiểm tra lại "
                         f"API key / proxy. Thăm dò lại sau {backoff}s.")
                self._next_probe_at = time.time() + backoff
            else:
                if result:
                    self._dead_proxy_failures = 0
                self._next_probe_at = time.time() + PROXY_HEALTH_INTERVAL_SECONDS
        except Exception as e:
            self.log(f"❌ Lỗi khi thăm dò proxy: {e}")
            self._next_probe_at = time.time() + PROXY_HEALTH_INTERVAL_SECONDS
        finally:
            self._probe_running = False

    def handle_limit_and_resume(self, reason, session_hint=None, delay_seconds=0):
        with self.lock:
            if self.is_handling:
                if self.handling_kind == "rotate_only":
                    self.pending_resume = (reason, session_hint)
                    self.log(f"⏳ Đang xoay IP nền - sẽ xử lý lỗi '{reason}' ngay khi xoay xong.")
                else:
                    self.log("⏳ Đang trong quá trình reset/resume phiên trước, bỏ qua trigger trùng lặp.")
                return
            self.is_handling = True
            self.handling_kind = "resume"
        self._limit_and_resume_body(reason, session_hint, delay_seconds)

    def _limit_and_resume_body(self, reason, session_hint=None, delay_seconds=0, skip_rotation=False):
        """Thân xử lý lỗi: xoay IP (trừ khi skip_rotation) rồi gửi lệnh tiếp tục.
        Phải được gọi khi đang giữ cờ is_handling; tự nhả cờ khi xong."""
        try:
            now = time.time()
            # 0. Kiểm tra khóa Global Quota
            if now < self.global_limit_locked_until:
                wait_min = max(1, int((self.global_limit_locked_until - now) / 60))
                self.log(f"🛑 [GLOBAL QUOTA LOCK] Toàn bộ IP đều dính hạn mức toàn cầu của OpenCode. Đang tạm ngưng xoay IP trong {wait_min} phút nữa.")
                return

            self.log("========================================================")
            self.log(f"🚨 PHÁT HIỆN LỖI/HẠN MỨC TRONG OPENCODE!")
            self.log(f"   Lý do: {reason}")

            # IP vừa được xoay xong mà OpenCode mới báo ngắt kết nối: lỗi thuộc về IP cũ,
            # không đốt thêm một IP mới nếu IP hiện tại vẫn chạy tốt.
            if not skip_rotation and "ECONNRESET" in reason and now - self.last_rotation_ok_at < RECENT_ROTATION_SECONDS:
                skip_rotation = True

            current_ip = None
            if skip_rotation:
                current_ip = self.rotator.check_health(timeout=3, quick=True)
                if current_ip:
                    self.log(f"ℹ️ IP vừa xoay ({current_ip}) đang hoạt động tốt - không xoay lại, chỉ gửi lệnh tiếp tục.")
                else:
                    self.log("⚠️ IP hiện tại không phản hồi - tiến hành xoay IP mới.")
                    skip_rotation = False

            # 1. Đánh dấu IP hiện tại vào blacklist
            if not skip_rotation:
                failing_ip = self.rotator.check_health(timeout=3, quick=True)
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
                target_session = self.get_latest_session_id(max_idle_seconds=900)

            self.log(f"   Target Session: {target_session or 'Chưa xác định'}")

            # Kiểm tra Circuit Breaker đối với session này
            if target_session and target_session in self.circuit_broken_sessions:
                self.log(f"⛔ [CIRCUIT BREAKER] Phiên {target_session} đang bị tạm khóa auto-resume do lỗi liên tiếp {MAX_CONSECUTIVE_SESSION_RETRIES} lần.")
                self.log("   Bỏ qua trigger tự động để tránh lặp vô tận và tiết kiệm proxy.")
                self.log("========================================================")
                return

            if skip_rotation:
                new_ip = current_ip
            else:
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

                self._record_rotation(remaining_seconds)

            # 5. Đợi đường truyền ổn định với cơ chế Dynamic Fast Health Probe (tiết kiệm thời gian)
            #    (bỏ qua khi không xoay: IP hiện tại vừa được thăm dò thành công ở trên)
            if not skip_rotation:
                self.log(f"⏳ Đã có IP mới ({new_ip}). Đang thăm dò độ ổn định đường truyền...")
                self.rotator.notify("OpenCode Auto-Resume", f"Đã có IP mới ({new_ip}). Đang kiểm tra kết nối...")

                time.sleep(5)
                probe_success = False
                for probe_i in range(1, 4):
                    probe_ip = self.rotator.check_health(timeout=3, quick=True)
                    if probe_ip:
                        self.log(f"⚡ Đường truyền đã thông suốt và ổn định (Thăm dò {probe_i}/3 thành công, IP: {probe_ip})!")
                        probe_success = True
                        break
                    self.log(f"   ⏱️ Đường truyền chưa thông, đang đợi thêm 5s (lần {probe_i}/3)...")
                    time.sleep(5)

                if not probe_success:
                    fallback_wait = min(15, self.stabilize_wait_seconds)
                    self.log(f"⚠️ Thăm dò chưa có phản hồi ngay, đợi thêm {fallback_wait}s an toàn...")
                    time.sleep(fallback_wait)

            # 6. Tự động gửi prompt tiếp tục nếu được bật
            if self.auto_resume and target_session:
                cur_retries = self.session_retries.get(target_session, 0) + 1
                self.session_retries[target_session] = cur_retries

                if cur_retries >= MAX_CONSECUTIVE_SESSION_RETRIES:
                    self.circuit_broken_sessions.add(target_session)
                    self.log(f"⛔ [CIRCUIT BREAKER KÍCH HOẠT] Phiên {target_session} đã thử auto-resume {cur_retries} lần liên tiếp.")
                    self.log(f"   Tạm dừng auto-resume cho phiên này để chống vòng lặp vô tận. Vui lòng kiểm tra lại task.")
                    self.rotator.notify(
                        "OpenCode Circuit Breaker",
                        f"Tạm dừng auto-resume phiên {target_session[:8]} do lỗi 3 lần liên tiếp."
                    )
                else:
                    self.log(f"🚀 Hết thời gian chờ! Tự động gửi lệnh tiếp tục phiên {target_session} (Thử lần {cur_retries}/{MAX_CONSECUTIVE_SESSION_RETRIES})...")
                    ok, _ = self.send_resume_prompt(target_session)
                    if ok:
                        self.rotator.notify("OpenCode Auto-Resume", f"Đã tự động tiếp tục code phiên {target_session[:12]}...")
                    else:
                        time.sleep(4)
                        self.send_resume_prompt(target_session)
            elif not target_session:
                self.log("ℹ️ Không có session nào cần resume. Proxy đã sẵn sàng.")

            self.log("========================================================")
        except Exception as e:
            self.log(f"❌ Lỗi trong luồng xử lý: {e}")
        finally:
            with self.lock:
                self.is_handling = False
                self.handling_kind = None

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

        # Lỗi hạn mức cũ tìm thấy lúc khởi động: (session_hint, hạn chót) - chỉ xử lý nếu OpenCode mở trong hạn
        pending_startup = None
        ttl_paused = False

        try:
            self.restore_ip_expiry()
        except Exception:
            pass
        self.log(f"🩺 Proxy Health Monitor: thăm dò proxy mỗi {PROXY_HEALTH_INTERVAL_SECONDS}s khi OpenCode đang mở, "
                 "proxy chết thì xoay IP ngay.")

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
                        self.log("⚠️ Phát hiện OpenCode dính lỗi hạn mức/kết nối gần nhất. Sẽ xử lý ngay khi OpenCode đang chạy...")
                        pending_startup = (s_hint, time.time() + STARTUP_PENDING_WINDOW_SECONDS)
                        break
            except Exception:
                pass

            f.seek(0, os.SEEK_END)
            last_trigger_time = 0  # Chống trigger trùng từ log (riêng, để xoay TTL không nuốt mất lỗi thật)
            last_ttl_time = 0
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

                    now = time.time()

                    # Lỗi hạn mức cũ lúc khởi động: chỉ xoay IP + resume khi OpenCode thực sự đang chạy
                    if pending_startup is not None:
                        if self._opencode_running_cached(STARTUP_PENDING_CHECK_SECONDS):
                            s_hint = pending_startup[0]
                            pending_startup = None
                            threading.Thread(
                                target=self.handle_limit_and_resume,
                                args=("Lỗi hạn mức phát hiện khi khởi động", s_hint, 2),
                                daemon=True
                            ).start()
                        elif now > pending_startup[1]:
                            pending_startup = None
                            self.log("ℹ️ OpenCode không được mở sau khi khởi động - bỏ qua lỗi hạn mức cũ trong log, không xoay IP.")

                    # Kiểm tra Proactive TTL: Nếu proxy 4G sắp hết hạn sống (còn < 45s)
                    # Chỉ xoay khi OpenCode đang chạy - tắt OpenCode thì tạm dừng, mở lại thì xoay tiếp
                    if self.current_ip_expire_time > 0 and now > (self.current_ip_expire_time - 45):
                        if (now - last_ttl_time) > 45 and not self.is_handling:
                            if self._opencode_running_cached(PRESENCE_CHECK_INTERVAL_SECONDS):
                                if ttl_paused:
                                    ttl_paused = False
                                    self.log("▶️ OpenCode đã mở lại - tiếp tục xoay IP chủ động.")
                                last_ttl_time = now
                                self.current_ip_expire_time = 0
                                threading.Thread(
                                    target=self.handle_proactive_ttl,
                                    daemon=True
                                ).start()
                            elif not ttl_paused:
                                ttl_paused = True
                                self.log("⏸️ OpenCode đã tắt - tạm dừng xoay IP chủ động cho tới khi OpenCode mở lại.")

                    # Proxy Health Monitor: OpenCode đang mở mà proxy chết thì xoay IP ngay
                    if now >= self._next_probe_at and not self.is_handling and not self._probe_running:
                        if self._opencode_running_cached(PRESENCE_CHECK_INTERVAL_SECONDS):
                            self._probe_running = True
                            threading.Thread(target=self._probe_proxy_health, daemon=True).start()
                        else:
                            self._proxy_fail_count = 0
                            self._next_probe_at = now + PROXY_HEALTH_INTERVAL_SECONDS

                    time.sleep(0.4)
                    continue

                line_lower = line.lower()

                # Bỏ qua các log hệ thống định kỳ không liên quan đến phiên code của người dùng
                if any(ign in line_lower for ign in [
                    "failed to fetch models.dev",
                    "models.opencode.ai",
                    "api.github.com",
                    "cli starting",
                    "telemetry",
                    "analytics"
                ]):
                    continue

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
                    # Chỉ trigger nếu lỗi ngắt kết nối xảy ra trong một phiên làm việc thực tế
                    if "session" in line_lower or "sessionid=" in line_lower or "failed to drain" in line_lower:
                        is_trigger = True
                        reason = "ECONNRESET / Socket Reset (trong phiên làm việc)"

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

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
import csv

# Fix mã hóa Unicode tiếng Việt trên Windows Console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from spoofer import get_opencode_dir

MAX_CONSECUTIVE_SESSION_RETRIES = 3
SESSION_RETRY_WINDOW_SECONDS = 600     # Lỗi tới sau lần tự tiếp tục trước quá 10 phút = phiên đã chạy ổn, đếm lại từ đầu
CIRCUIT_BREAKER_LOCK_SECONDS = 1800    # Phiên bị khóa auto-resume được mở lại sau 30 phút
MAX_CONSECUTIVE_GLOBAL_LIMITS = 3
GLOBAL_LIMIT_COOLDOWN_SECONDS = 900  # 15 phút nếu dính hạn mức toàn cầu

# Tên tiến trình của OpenCode Desktop (OpenCode.exe) và CLI/backend (opencode, opencode-cli)
OPENCODE_PROCESS_NAMES = ("opencode", "opencode-cli")
PRESENCE_CHECK_INTERVAL_SECONDS = 30   # Tần suất kiểm tra OpenCode còn chạy khi đang tạm dừng xoay IP
OPEN_CONFIRM_SECONDS = 8               # Tiến trình OpenCode mới phải còn sống ít nhất chừng này mới tính là "đã mở"
STARTUP_PENDING_CHECK_SECONDS = 5      # Tần suất kiểm tra OpenCode đã mở chưa để xử lý lỗi cũ lúc khởi động
STARTUP_PENDING_WINDOW_SECONDS = 120   # Quá thời gian này mà OpenCode chưa mở thì bỏ qua lỗi cũ trong log
STARTUP_ERROR_MAX_AGE_SECONDS = 600    # Lỗi cũ trong log OpenCode quá 10 phút lúc khởi động thì không tự xử lý nữa

# Proxy Health Monitor: khi proxy chết, OpenCode chỉ tự thử lại ("Attempt N - retrying") mà không
# ghi gì vào log cho tới khi bỏ cuộc - có thể mất vài phút. Nên watchdog tự thăm dò proxy định kỳ.
PROXY_HEALTH_INTERVAL_SECONDS = 20     # Tần suất thăm dò proxy khi OpenCode đang mở
PROXY_HEALTH_RETRY_SECONDS = 5         # Thăm dò lại nhanh sau một lần lỗi
PROXY_DEAD_THRESHOLD = 2               # Số lần lỗi liên tiếp để coi proxy đã chết
PROXY_DEAD_BACKOFF_MAX_SECONDS = 600   # Xoay IP thất bại liên tục (VD: key API hỏng) thì giãn dần, tối đa 10 phút
RECENT_ROTATION_SECONDS = 120          # ECONNRESET tới trong khoảng này sau khi vừa xoay xong là lỗi của IP cũ

# Chống xoay IP liên tục khi proxy chết ngay sau mỗi lần xoay (VD: nhà cung cấp chỉ cho kết nối mới
# ~60s sau mỗi lần gọi API). Xoay thêm cũng chết tiếp, chỉ đốt IP và cắt luôn kết nối OpenCode đang chạy.
PROXY_FLAP_WINDOW_SECONDS = 180        # Proxy chết trong khoảng này sau khi xoay = "xoay xong lại chết ngay"
PROXY_FLAP_THRESHOLD = 2               # Số lần liên tiếp như vậy thì tạm ngừng tự xoay theo Health Monitor
PROXY_FLAP_HOLD_BASE_SECONDS = 300     # Thời gian tạm ngừng lần đầu, gấp đôi mỗi lần tái diễn...
PROXY_FLAP_HOLD_MAX_SECONDS = 1800     # ...tối đa 30 phút


def opencode_log_line_time(line):
    """Thời điểm (epoch) của một dòng opencode.log ("timestamp=2026-11-08T18:51:56.794Z ..."), None nếu không đọc được."""
    m = re.search(r"timestamp=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z", line or "")
    if not m:
        return None
    s = m.group(1)
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
        return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def _command_args(cmdline):
    """Các tham số đứng sau đường dẫn file thực thi (đường dẫn có thể nằm trong ngoặc kép), viết thường."""
    s = (cmdline or "").strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        rest = s[end + 1:] if end != -1 else ""
    else:
        parts = s.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    return [a.strip('"').lower() for a in rest.split()]


def is_background_opencode_process(cmdline):
    """True nếu tiến trình OpenCode này chạy ngầm, không có nghĩa là người dùng đang mở OpenCode:
    - "serve --service": backend mà OpenCode Desktop để lại chạy ngầm sau khi đóng cửa sổ
    - "api ...": lệnh ngắn do chính watchdog gọi (hỏi session, gửi lệnh tiếp tục)"""
    args = _command_args(cmdline)
    subcommand = next((a for a in args if not a.startswith("-")), None)
    if subcommand == "api":
        return True
    return subcommand == "serve" and "--service" in args


def _toolhelp_process_pids(exe_names):
    """{pid} của các tiến trình có tên file .exe (viết thường) thuộc exe_names, đọc thẳng từ Windows qua
    Toolhelp32 - chỉ mất vài ms, không phụ thuộc tasklist/WMI (có lúc chậm quá 10s khi máy bận)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    TH32CS_SNAPPROCESS = 0x00000002
    ERROR_NO_MORE_FILES = 18
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        pids = set()
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in exe_names:
                pids.add(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        err = ctypes.get_last_error()
        if err != ERROR_NO_MORE_FILES:
            raise ctypes.WinError(err)
        return pids
    finally:
        kernel32.CloseHandle(snap)


def _tasklist_process_pids(exe_names):
    """Như _toolhelp_process_pids nhưng qua tasklist (dự phòng)."""
    res = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="ignore", timeout=15,
        # Không bật cửa sổ console khi service chạy bằng pythonw.exe
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if res.returncode != 0:
        raise RuntimeError(f"tasklist lỗi (exit {res.returncode})")
    pids = set()
    for row in csv.reader(res.stdout.splitlines()):
        if len(row) >= 2 and row[0].strip().lower() in exe_names and row[1].strip().isdigit():
            pids.add(int(row[1]))
    return pids


def _windows_opencode_pids():
    exe_names = {f"{n}.exe" for n in OPENCODE_PROCESS_NAMES}
    try:
        return _toolhelp_process_pids(exe_names)
    except Exception:
        return _tasklist_process_pids(exe_names)


def _windows_command_lines(pids):
    """{pid: command line} của các PID còn sống ("" nếu không đọc được, VD: tiến trình chạy quyền admin)."""
    flt = " OR ".join(f"ProcessId={int(p)}" for p in pids)
    ps_cmd = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        f"Get-CimInstance Win32_Process -Filter '{flt}' | "
        "ForEach-Object { [string]$_.ProcessId + [char]9 + [string]$_.CommandLine }"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if res.returncode != 0:
        raise RuntimeError(f"Get-CimInstance lỗi (exit {res.returncode})")
    out = {}
    for line in res.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit():
            out[int(pid)] = cmd.strip()
    return out


def _posix_command_lines(pids):
    """{pid: command line} của các PID còn sống."""
    res = subprocess.run(
        ["ps", "-o", "pid=", "-o", "args=", "-p", ",".join(str(int(p)) for p in pids)],
        capture_output=True, text=True, errors="ignore", timeout=10,
    )
    if res.returncode not in (0, 1):  # 1 = không còn PID nào
        raise RuntimeError(f"ps lỗi (exit {res.returncode})")
    out = {}
    for line in res.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            out[int(pid)] = cmd.strip()
    return out


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
        # Mốc lỗi gần nhất watchdog đã thấy trong opencode.log (giữ qua các lần khởi động lại), để lúc khởi
        # động không xử lý lại (xoay IP + gửi lệnh tiếp tục) một lỗi mà lần chạy trước đã xử lý rồi.
        self.state_file = os.path.join(self.opencode_dir, "watchdog_state.json")

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
        self._flap_count = 0                  # Số lần liên tiếp proxy chết ngay sau khi vừa xoay
        self._next_probe_at = 0
        self.session_retries = {}             # session_id -> (số lần tự tiếp tục liên tiếp, thời điểm lần gần nhất)
        self.circuit_broken_sessions = {}     # session_id -> thời điểm bị khóa auto-resume (chống lặp vô tận)
        self.consecutive_global_limits = 0
        self.global_limit_locked_until = 0
        self._presence_checked_at = None
        self._presence_cache = True
        self._presence_recheck_at = 0         # Kiểm tra lại sớm để xác nhận tiến trình OpenCode vừa xuất hiện
        self._presence_error_logged = False
        self._open_reason = ""                # Tiến trình làm watchdog coi là OpenCode đang mở (để ghi log)
        # pid -> {"bg": True/False/None (chưa đọc được command line), "cmd": str, "first_seen": time}
        self._pid_info = {}

    def is_opencode_running(self):
        """Kiểm tra người dùng có đang mở OpenCode (cửa sổ Desktop hoặc CLI) không.
        - True: có tiến trình OpenCode không chạy ngầm, đã sống ít nhất OPEN_CONFIRM_SECONDS.
        - False: không có.
        - None: chưa chắc (vừa thấy tiến trình mới, cần xác nhận lại) hoặc không kiểm tra được.
          Khi đó giữ nguyên trạng thái trước, KHÔNG tự coi là đang mở - trước đây chỉ một lần
          tasklist chậm/lỗi là watchdog tưởng OpenCode mở lại và xoay IP dù không ai mở.
        Bỏ qua tiến trình chạy ngầm (xem is_background_opencode_process): đóng cửa sổ Desktop thì
        backend "opencode-cli serve --service" vẫn còn nhưng OpenCode coi như đã tắt."""
        try:
            if platform.system() == "Windows":
                pids = _windows_opencode_pids()
                result = self._has_foreground_opencode(pids, _windows_command_lines)
            else:
                pattern = "|".join(OPENCODE_PROCESS_NAMES)
                res = subprocess.run(["pgrep", "-i", "-x", pattern], capture_output=True, text=True, timeout=10)
                if res.returncode not in (0, 1):  # 1 = không có tiến trình
                    raise RuntimeError(f"pgrep lỗi (exit {res.returncode})")
                pids = {int(p) for p in res.stdout.split() if p.isdigit()}
                result = self._has_foreground_opencode(pids, _posix_command_lines)
            self._presence_error_logged = False
            return result
        except Exception as e:
            if not self._presence_error_logged:
                self._presence_error_logged = True
                self.log(f"⚠️ Không kiểm tra được OpenCode có đang mở không ({type(e).__name__}: {e}) - "
                         "giữ nguyên trạng thái hiện tại.")
            return None

    def _has_foreground_opencode(self, pids, fetch_command_lines):
        """True/False/None như is_opencode_running(). Chỉ đọc command line của PID chưa biết."""
        now = time.time()
        self._pid_info = {p: info for p, info in self._pid_info.items() if p in pids}
        for p in pids:
            self._pid_info.setdefault(p, {"bg": None, "cmd": "", "first_seen": now})

        unknown = [p for p in pids if self._pid_info[p]["bg"] is None]
        if unknown:
            cmdlines = fetch_command_lines(unknown)
            for pid in unknown:
                if pid not in cmdlines:  # PID không có trong kết quả = đã thoát
                    del self._pid_info[pid]
                    continue
                cmd = cmdlines[pid]
                if cmd:
                    self._pid_info[pid].update(bg=is_background_opencode_process(cmd), cmd=cmd)
                # Không đọc được command line (VD: tiến trình đang thoát, chạy quyền admin): đọc lại lần sau

        pending = False
        for pid, info in self._pid_info.items():
            if info["bg"] is True:
                continue
            if now - info["first_seen"] >= OPEN_CONFIRM_SECONDS:
                # Đã sống đủ lâu: tiến trình thật của người dùng (đọc mãi không ra command line cũng tính là mở)
                self._open_reason = f"PID {pid}: {info['cmd'] or '(không đọc được command line)'}"
                return True
            pending = True  # Mới xuất hiện: chờ xác nhận, bỏ qua lệnh chạy chớp nhoáng (VD: --version)
        return None if pending else False

    def _opencode_running_cached(self, max_age):
        """Như is_opencode_running() nhưng chỉ kiểm tra lại sau mỗi max_age giây. Ghi log khi OpenCode đóng/mở."""
        now = time.time()
        due = self._presence_checked_at is None or now - self._presence_checked_at >= max_age
        if due or (self._presence_recheck_at and now >= self._presence_recheck_at):
            running = self.is_opencode_running()
            self._presence_checked_at = now
            self._presence_recheck_at = now + OPEN_CONFIRM_SECONDS if running is None else 0
            if running is not None and running != self._presence_cache:
                if running:
                    self.log("▶️ OpenCode đã mở lại - tiếp tục thăm dò proxy, xoay IP và tự gửi lệnh tiếp tục. "
                             f"[{self._open_reason[:200]}]")
                else:
                    self.log("⏸️ OpenCode đã đóng - tạm dừng thăm dò proxy, xoay IP và tự gửi lệnh tiếp tục "
                             "cho tới khi OpenCode mở lại (Local Bridge vẫn chạy).")
                self._presence_cache = running
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

    def _load_last_seen_error_at(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                value = json.load(f).get("last_seen_error_at")
            return float(value) if isinstance(value, (int, float)) else None
        except Exception:
            return None

    def _remember_error_line(self, line):
        """Ghi lại mốc thời gian của dòng lỗi vừa thấy trong opencode.log (chỉ tiến lên, không lùi)."""
        err_at = opencode_log_line_time(line)
        if err_at is None:
            return
        last = self._load_last_seen_error_at()
        if last is not None and err_at <= last:
            return
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_seen_error_at": err_at}, f)
            os.replace(tmp, self.state_file)
        except Exception:
            pass

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
        và chưa hoàn tất thành công (không resume session đã xong).
        idle_outcome: NULL khi đang chạy, 'succeeded' / 'failed' khi dừng (OpenCode 2.x; bản cũ ghi 'completed')."""
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
                      AND (idle_outcome IS NULL OR idle_outcome NOT IN ('succeeded', 'completed'))
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
        """Chạy trong thread riêng: thăm dò proxy, chết PROXY_DEAD_THRESHOLD lần liên tiếp thì xoay IP.
        Nếu proxy cứ chết ngay sau mỗi lần xoay (PROXY_FLAP_*) thì tạm ngừng tự xoay, giãn dần thời gian
        thăm dò: lỗi kiểu này nằm ở nhà cung cấp, xoay thêm chỉ đốt IP. Lỗi OpenCode ghi trong log
        (hết hạn mức, ECONNRESET...) vẫn được xử lý bình thường (xoay IP + gửi lệnh tiếp tục)."""
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
                # Chỉ coi là hết "xoay xong chết ngay" khi IP sống qua được PROXY_FLAP_WINDOW_SECONDS
                # (ngay sau khi xoay thì proxy lúc nào cũng phản hồi, chưa nói lên được gì)
                if self._flap_count and now - self.last_rotation_ok_at >= PROXY_FLAP_WINDOW_SECONDS:
                    self.log(f"✅ IP {ip} đã sống ổn định quá {PROXY_FLAP_WINDOW_SECONDS}s sau khi xoay - "
                             "Health Monitor tự xoay IP trở lại như bình thường.")
                    self._flap_count = 0
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

            since_rotation = now - self.last_rotation_ok_at
            if self.last_rotation_ok_at and since_rotation < PROXY_FLAP_WINDOW_SECONDS:
                self._flap_count += 1
                if self._flap_count >= PROXY_FLAP_THRESHOLD:
                    hold = min(PROXY_FLAP_HOLD_MAX_SECONDS,
                               PROXY_FLAP_HOLD_BASE_SECONDS * 2 ** (self._flap_count - PROXY_FLAP_THRESHOLD))
                    self.log(f"🛑 [PROXY MONITOR] Proxy lại chết chỉ ~{int(since_rotation)}s sau khi xoay "
                             f"({self._flap_count} lần liên tiếp) - nhiều khả năng lỗi phía nhà cung cấp proxy "
                             "(không nhận kết nối mới), không phải do IP. Xoay thêm chỉ đốt IP và cắt kết nối "
                             f"OpenCode đang chạy, nên tạm ngừng tự xoay {hold // 60} phút. Lỗi OpenCode báo trong "
                             "log vẫn được xử lý (xoay IP + gửi lệnh tiếp tục). Nên kiểm tra key/gói proxy.")
                    self.rotator.notify(
                        "OpenCode Proxy Monitor",
                        f"Proxy chết ngay sau mỗi lần xoay. Tạm ngừng tự xoay {hold // 60} phút - kiểm tra key proxy.",
                    )
                    self._next_probe_at = now + hold
                    return

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

    def _is_circuit_broken(self, session_id, now):
        """True nếu phiên đang bị khóa auto-resume. Khóa hết hạn sau CIRCUIT_BREAKER_LOCK_SECONDS: phiên
        bị khóa không tự lỗi lại được (không còn ai gửi lệnh tiếp tục), nên lỗi mới tới sau đó là do người
        dùng đã cho phiên chạy lại - xử lý như một chuỗi lỗi mới."""
        broken_at = self.circuit_broken_sessions.get(session_id)
        if broken_at is None:
            return False
        if now - broken_at < CIRCUIT_BREAKER_LOCK_SECONDS:
            return True
        del self.circuit_broken_sessions[session_id]
        self.log(f"🔓 [CIRCUIT BREAKER] Hết thời gian khóa - mở lại auto-resume cho phiên {session_id}.")
        return False

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
            if target_session and self._is_circuit_broken(target_session, now):
                left_min = max(1, int((self.circuit_broken_sessions[target_session]
                                       + CIRCUIT_BREAKER_LOCK_SECONDS - now) / 60))
                self.log(f"⛔ [CIRCUIT BREAKER] Phiên {target_session} đang bị tạm khóa auto-resume do lỗi liên tiếp "
                         f"{MAX_CONSECUTIVE_SESSION_RETRIES} lần (mở lại sau ~{left_min} phút).")
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
                attempt_at = time.time()
                prev_retries, prev_at = self.session_retries.get(target_session, (0, 0))
                if attempt_at - prev_at > SESSION_RETRY_WINDOW_SECONDS:
                    # Lần tự tiếp tục trước đã lâu (phiên chạy ổn ở giữa) - không phải lỗi liên tiếp
                    prev_retries = 0

                if prev_retries >= MAX_CONSECUTIVE_SESSION_RETRIES:
                    # Đã tự tiếp tục đủ MAX lần, lần nào cũng lỗi lại ngay - dừng để chống vòng lặp vô tận
                    self.circuit_broken_sessions[target_session] = attempt_at
                    self.session_retries.pop(target_session, None)
                    self.log(f"⛔ [CIRCUIT BREAKER KÍCH HOẠT] Phiên {target_session} vẫn lỗi sau {prev_retries} lần "
                             "tự gửi lệnh tiếp tục liên tiếp.")
                    self.log(f"   Tạm dừng auto-resume cho phiên này {CIRCUIT_BREAKER_LOCK_SECONDS // 60} phút để chống "
                             "vòng lặp vô tận. Vui lòng kiểm tra lại task.")
                    self.rotator.notify(
                        "OpenCode Circuit Breaker",
                        f"Tạm dừng auto-resume phiên {target_session[:8]} do lỗi {prev_retries} lần liên tiếp."
                    )
                else:
                    cur_retries = prev_retries + 1
                    self.session_retries[target_session] = (cur_retries, attempt_at)
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
                last_seen_error_at = self._load_last_seen_error_at()
                for s_line in reversed(startup_lines[-10:]):  # Lỗi mới nhất trước
                    s_lower = s_line.lower()
                    if ("free usage limit reached" in s_lower or "quotaexceeded" in s_lower or
                        "free messages" in s_lower or "econnreset" in s_lower) and ("failed to drain" in s_lower or "limit reached" in s_lower):
                        err_at = opencode_log_line_time(s_line)
                        err_time = (datetime.datetime.fromtimestamp(err_at).strftime("%H:%M:%S")
                                    if err_at is not None else "?")
                        if err_at is not None and last_seen_error_at is not None and err_at <= last_seen_error_at:
                            self.log(f"ℹ️ Lỗi gần nhất trong log OpenCode (lúc {err_time}) đã được lần chạy trước "
                                     "xử lý - bỏ qua, không xoay IP / gửi lệnh tiếp tục lại.")
                            break
                        if err_at is not None and time.time() - err_at > STARTUP_ERROR_MAX_AGE_SECONDS:
                            self.log(f"ℹ️ Lỗi gần nhất trong log OpenCode (lúc {err_time}) đã cũ hơn "
                                     f"{STARTUP_ERROR_MAX_AGE_SECONDS // 60} phút - bỏ qua, không tự xử lý.")
                            break
                        s_hint = None
                        m = re.search(r"(?:sessionID|sessionId|session\.id)=([a-zA-Z0-9_-]+)", s_line)
                        if m:
                            s_hint = m.group(1)
                        self.log(f"⚠️ Phát hiện OpenCode dính lỗi hạn mức/kết nối gần nhất (lúc {err_time}) chưa được "
                                 "xử lý. Sẽ xử lý ngay khi OpenCode đang chạy...")
                        pending_startup = (s_hint, time.time() + STARTUP_PENDING_WINDOW_SECONDS)
                        self._remember_error_line(s_line)
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
                                last_ttl_time = now
                                self.current_ip_expire_time = 0
                                threading.Thread(
                                    target=self.handle_proactive_ttl,
                                    daemon=True
                                ).start()

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
                    # Ghi mốc lỗi đã thấy (kể cả khi bỏ qua bên dưới) để lần khởi động sau không xử lý lại
                    self._remember_error_line(line)
                    now = time.time()
                    if now - last_trigger_time < 25:
                        continue
                    last_trigger_time = now

                    # Đã đóng OpenCode (phiên chỉ còn chạy trong backend ngầm): không đốt IP, không tự gửi lệnh tiếp tục
                    if not self._opencode_running_cached(0):
                        self.log(f"ℹ️ OpenCode đang đóng - bỏ qua lỗi '{reason}': không xoay IP, không gửi lệnh tiếp tục.")
                        continue

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

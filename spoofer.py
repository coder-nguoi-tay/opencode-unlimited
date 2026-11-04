#!/usr/bin/env python3
"""
OpenCode Unlimited - Cross-Platform Device ID Spoofer
Tác giả: coder-nguoi-tay
Mục đích:
- Tạo và làm mới Device ID (IOPlatformUUID / MachineGuid / Machine ID) ngẫu nhiên mỗi khi đổi IP.
- Tự động thích ứng trên macOS, Linux và Windows (10 & 11).
"""

import os
import sys
import uuid
import platform
import subprocess
import shutil

# Fix mã hóa Unicode tiếng Việt trên Windows Console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def get_opencode_dir():
    """Lấy đường dẫn thư mục dữ liệu của OpenCode theo từng hệ điều hành."""
    system = platform.system()
    home = os.path.expanduser("~")
    candidates = []

    # 1. Đường dẫn XDG chuẩn (được OpenCode CLI sử dụng trên mọi nền tảng bao gồm cả Windows)
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        candidates.append(os.path.join(xdg_data, "opencode"))
    candidates.append(os.path.join(home, ".local", "share", "opencode"))

    # 2. Đường dẫn AppData đặc thù của Windows
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "opencode"))
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(os.path.join(localappdata, "opencode"))

    # Ưu tiên thư mục đã có file database hoặc thư mục log thực tế
    for c in candidates:
        if os.path.exists(os.path.join(c, "opencode.db")) or os.path.exists(os.path.join(c, "log")):
            return c

    # Nếu chưa có file dữ liệu, chọn thư mục đã tồn tại đầu tiên
    for c in candidates:
        if os.path.exists(c):
            return c

    # Mặc định tạo thư mục theo chuẩn XDG / OpenCode
    default_dir = candidates[0] if candidates else os.path.join(home, ".local", "share", "opencode")
    os.makedirs(default_dir, exist_ok=True)
    return default_dir

def get_fake_uuid_file():
    opencode_dir = get_opencode_dir()
    os.makedirs(opencode_dir, exist_ok=True)
    return os.path.join(opencode_dir, "fake_uuid")

def get_windows_machine_guid():
    """Đọc MachineGuid hiện tại từ Registry trên Windows."""
    if platform.system() != "Windows":
        return None
    try:
        res = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
            capture_output=True, text=True, errors="ignore"
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if "MachineGuid" in line and "REG_SZ" in line:
                    parts = line.split("REG_SZ")
                    if len(parts) >= 2:
                        return parts[1].strip()
    except Exception:
        pass
    return None

def set_windows_machine_guid(new_guid):
    """Cập nhật MachineGuid mới vào Registry trên Windows (yêu cầu quyền ghi HKLM)."""
    if platform.system() != "Windows":
        return False
    try:
        # Sao lưu MachineGuid ban đầu nếu chưa có file backup
        backup_file = os.path.join(get_opencode_dir(), "original_machine_guid.txt")
        if not os.path.exists(backup_file):
            current = get_windows_machine_guid()
            if current:
                try:
                    with open(backup_file, "w", encoding="utf-8") as f:
                        f.write(current + "\n")
                except Exception:
                    pass

        # Ghi MachineGuid mới (định dạng GUID chuẩn)
        guid_str = str(new_guid).lower()
        res = subprocess.run(
            ["reg", "add", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid", "/t", "REG_SZ", "/d", guid_str, "/f"],
            capture_output=True, text=True, errors="ignore"
        )
        return res.returncode == 0
    except Exception:
        return False

def restore_original_machine_guid():
    """Khôi phục MachineGuid ban đầu từ file backup trên Windows."""
    if platform.system() != "Windows":
        return False
    backup_file = os.path.join(get_opencode_dir(), "original_machine_guid.txt")
    if not os.path.exists(backup_file):
        return False
    try:
        with open(backup_file, "r", encoding="utf-8") as f:
            original = f.read().strip()
        if original:
            ok = set_windows_machine_guid(original)
            if ok:
                try:
                    os.remove(backup_file)
                except Exception:
                    pass
            return ok
    except Exception:
        pass
    return False

def generate_new_device_id():
    """Sinh một Device UUID mới và lưu vào fake_uuid file, đồng thời cập nhật Registry trên Windows."""
    new_uuid = str(uuid.uuid4()).upper()
    fake_file = get_fake_uuid_file()
    try:
        with open(fake_file, "w", encoding="utf-8") as f:
            f.write(new_uuid + "\n")
    except Exception:
        pass

    # Trên Windows: cập nhật Registry MachineGuid
    if platform.system() == "Windows":
        set_windows_machine_guid(new_uuid)

    return new_uuid

def setup_macos_ioreg_wrapper():
    """Trên macOS: cài đặt ioreg wrapper để đánh lừa IOPlatformUUID của OpenCode."""
    if platform.system() != "Darwin":
        return

    fake_file = get_fake_uuid_file()
    user_bin = os.path.expanduser("~/.local/bin")
    os.makedirs(user_bin, exist_ok=True)
    wrapper_path = os.path.join(user_bin, "ioreg")

    script_content = f"""#!/bin/bash
FAKE_UUID_FILE="{fake_file}"
if [ -f "$FAKE_UUID_FILE" ]; then
    FAKE_UUID=$(cat "$FAKE_UUID_FILE" 2>/dev/null | tr -d '[:space:]')
fi
if [ -z "$FAKE_UUID" ]; then
    FAKE_UUID="FCB63DB1-3599-5770-8F02-CC81AA7B69A8"
fi
/usr/sbin/ioreg "$@" | sed -E "s/\\\"IOPlatformUUID\\\" = \\\".*\\\"/\\\"IOPlatformUUID\\\" = \\\"$FAKE_UUID\\\"/"
"""
    try:
        with open(wrapper_path, "w", encoding="utf-8") as f:
            f.write(script_content)
        os.chmod(wrapper_path, 0o755)
    except Exception:
        pass

def setup_spoofer():
    """Cài đặt môi trường spoof ban đầu tuỳ theo OS."""
    system = platform.system()
    if system == "Darwin":
        setup_macos_ioreg_wrapper()
    return generate_new_device_id()

if __name__ == "__main__":
    if "--restore" in sys.argv:
        if restore_original_machine_guid():
            print("✅ Đã khôi phục thành công MachineGuid ban đầu của Windows!")
        else:
            print("⚠️ Không tìm thấy file sao lưu hoặc không thể khôi phục MachineGuid.")
        sys.exit(0)

    device_id = setup_spoofer()
    print(f"🔑 Device ID đã được cập nhật thành công: {device_id}")
    print(f"📁 Thư mục OpenCode: {get_opencode_dir()}")
    print(f"📁 Đường dẫn lưu fake_uuid: {get_fake_uuid_file()}")
    if platform.system() == "Windows":
        current_reg = get_windows_machine_guid()
        print(f"🪟 Windows MachineGuid hiện tại: {current_reg}")

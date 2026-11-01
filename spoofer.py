#!/usr/bin/env python3
"""
OpenCode Unlimited - Cross-Platform Device ID Spoofer
Tác giả: coder-nguoi-tay
Mục đích:
- Tạo và làm mới Device ID (IOPlatformUUID / Machine ID) ngẫu nhiên mỗi khi đổi IP.
- Tự động thích ứng trên macOS, Linux và Windows.
"""

import os
import sys
import uuid
import platform
import subprocess
import shutil

def get_opencode_dir():
    """Lấy đường dẫn thư mục dữ liệu của OpenCode theo từng hệ điều hành."""
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            path = os.path.join(appdata, "opencode")
            if os.path.exists(path):
                return path
        return os.path.expanduser("~/.local/share/opencode")
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return os.path.join(xdg_data, "opencode")
        return os.path.expanduser("~/.local/share/opencode")

def get_fake_uuid_file():
    opencode_dir = get_opencode_dir()
    os.makedirs(opencode_dir, exist_ok=True)
    return os.path.join(opencode_dir, "fake_uuid")

def generate_new_device_id():
    """Sinh một Device UUID mới và lưu vào fake_uuid file."""
    new_uuid = str(uuid.uuid4()).upper()
    fake_file = get_fake_uuid_file()
    try:
        with open(fake_file, "w", encoding="utf-8") as f:
            f.write(new_uuid + "\n")
    except Exception:
        pass
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
    device_id = setup_spoofer()
    print(f"🔑 Device ID đã được cập nhật thành công: {device_id}")
    print(f"📁 Đường dẫn lưu: {get_fake_uuid_file()}")

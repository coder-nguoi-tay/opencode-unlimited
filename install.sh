#!/usr/bin/env bash
# OpenCode Unlimited - 1-Line Installer & Runner (macOS & Linux)

set -e

REPO_URL="https://github.com/coder-nguoi-tay/opencode-unlimited.git"
INSTALL_DIR="$HOME/.opencode-unlimited"

echo "========================================================"
echo " 🚀 BẮT ĐẦU CÀI ĐẶT OPENCODE UNLIMITED (1 LỆNH DUY NHẤT)"
echo "========================================================"

# Kiểm tra Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ Lỗi: Chưa tìm thấy Python 3 trên hệ thống."
    echo "👉 Hãy cài đặt Python 3 trước: brew install python (macOS) hoặc apt install python3 (Linux)"
    exit 1
fi

# Nếu đang ở trong thư mục git của dự án
if [ -f "main.py" ] && [ -f "bridge.py" ]; then
    TARGET_DIR="$(pwd)"
else
    # Clone hoặc cập nhật từ GitHub
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "🔄 Đang cập nhật code mới nhất tại: $INSTALL_DIR..."
        cd "$INSTALL_DIR"
        git pull --quiet || true
    else
        echo "📥 Đang tải code từ GitHub về: $INSTALL_DIR..."
        git clone --quiet "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
    TARGET_DIR="$INSTALL_DIR"
fi

cd "$TARGET_DIR"
chmod +x run.sh *.py 2>/dev/null || true

# Tạo shortcut toàn cục trong ~/.local/bin nếu có
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat << EOF > "$BIN_DIR/opencode-unlimited"
#!/usr/bin/env bash
exec python3 "$TARGET_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/opencode-unlimited"

echo "✅ Đã tạo lệnh gọi tắt toàn cục: opencode-unlimited"
echo "🚀 Đang khởi động chương trình..."
echo "========================================================"

exec python3 main.py "$@"

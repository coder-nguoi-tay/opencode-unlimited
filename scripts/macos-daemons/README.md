# 🍏 macOS Standalone Daemons & Scripts

Thư mục này chứa toàn bộ các script độc lập và cấu hình LaunchAgents đang được sử dụng trực tiếp trên macOS:

| File | Chức năng |
| :--- | :--- |
| `proxy-rotate` | Script tự động xoay IP qua API proxyxoay.shop, xử lý cooldown, chống trùng IP trong ngày |
| `opencode-autoreset.py` | Daemon theo dõi thời gian thực file log của OpenCode, phát hiện limit và tự động gửi prompt resume |
| `opencode-reset` | Lệnh CLI kích hoạt xoay IP và gửi prompt resume thủ công khi cần |
| `ioreg` | Wrapper can thiệp lệnh ioreg của macOS để thay thế IOPlatformUUID bằng fake_uuid |
| `opencode-wrapper.sh` | Wrapper tự động gắn biến môi trường HTTP_PROXY / HTTPS_PROXY vào `127.0.0.1:28080` khi gõ lệnh `opencode` |
| `smart-proxy-bridge.mjs` | Proxy bridge viết bằng Node.js với cơ chế hold request khi proxy đang xoay |
| `com.user.gost-bridge.plist` | LaunchAgent chạy ngầm GOST bridge |
| `com.user.proxy-rotator.plist` | LaunchAgent chạy ngầm proxy-rotate daemon |
| `com.user.opencode-autoreset.plist` | LaunchAgent chạy ngầm opencode-autoreset watchdog |

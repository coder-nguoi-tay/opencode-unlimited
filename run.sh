#!/usr/bin/env bash
# OpenCode Unlimited - Quick Run Script (macOS / Linux)

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if command -v python3 >/dev/null 2>&1; then
    exec python3 main.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python main.py "$@"
else
    echo "❌ Lỗi: Máy tính của bạn chưa cài đặt Python 3!"
    echo "👉 Hãy cài Python 3 trước: https://www.python.org/downloads/"
    exit 1
fi

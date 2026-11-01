#!/bin/bash
# Wrapper for OpenCode to automatically route via local HTTP-to-SOCKS5 bridge

export HTTPS_PROXY="http://127.0.0.1:28080"
export HTTP_PROXY="http://127.0.0.1:28080"
export ALL_PROXY="http://127.0.0.1:28080"
export NO_PROXY="localhost,127.0.0.1,::1"

exec "$HOME/.opencode/bin/opencode" "$@"

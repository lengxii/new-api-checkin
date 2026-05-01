#!/bin/bash
# 启动 bb-browser 环境（Chrome + daemon）
# Usage: bash /root/scripts/start-bb-browser.sh

set -e

CHROME_BIN=$(find /root/.agent-browser/browsers -name "chrome" -type f 2>/dev/null | head -1)
CDP_PORT=19825
DISPLAY_NUM=99

# 检查 Xvfb
if ! pgrep -f "Xvfb :$DISPLAY_NUM" > /dev/null; then
    echo "Starting Xvfb on :$DISPLAY_NUM..."
    Xvfb :$DISPLAY_NUM -screen 0 1280x720x24 &
    sleep 1
fi

# 检查 Chrome
if ! curl -s http://127.0.0.1:$CDP_PORT/json/version > /dev/null 2>&1; then
    echo "Starting Chrome on port $CDP_PORT..."
    DISPLAY=:$DISPLAY_NUM $CHROME_BIN \
        --remote-debugging-port=$CDP_PORT \
        --user-data-dir=/tmp/chrome-cdp-profile \
        --no-first-run \
        --no-default-browser-check \
        --no-sandbox \
        --disable-gpu \
        --window-size=1280,720 &
    sleep 3
fi

# 检查 daemon
if ! bb-browser status 2>/dev/null | grep -q "Daemon running: yes"; then
    echo "Starting bb-browser daemon..."
    bb-browser daemon --host 127.0.0.1 &
    sleep 2
fi

# 验证
echo "=== Status ==="
bb-browser status
echo ""
echo "=== Ready ==="
echo "Chrome CDP: http://127.0.0.1:$CDP_PORT"
echo "bb-browser daemon: running"

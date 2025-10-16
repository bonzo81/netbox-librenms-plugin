#!/bin/bash

echo "🔍 DevContainer Startup Diagnostics"
echo "=================================="

echo "📍 Current working directory: $(pwd)"
echo "👤 Current user: $(whoami)"
echo "🆔 User ID: $(id)"

echo ""
echo "🐳 Container Environment:"
echo "  - NETBOX_VERSION: ${NETBOX_VERSION:-not set}"
echo "  - DEBUG: ${DEBUG:-not set}"
echo "  - SECRET_KEY: ${SECRET_KEY:0:20}... (truncated)"
echo "  - DB_HOST: ${DB_HOST:-not set}"
echo "  - DB_NAME: ${DB_NAME:-not set}"
echo "  - DB_USER: ${DB_USER:-not set}"
echo "  - REDIS_HOST: ${REDIS_HOST:-not set}"
echo "  - SUPERUSER_NAME: ${SUPERUSER_NAME:-not set}"

echo ""
echo "🔗 Service Connectivity:"
echo "  - PostgreSQL: $(timeout 3 bash -c 'cat < /dev/null > /dev/tcp/postgres/5432' 2>/dev/null && echo 'Connected' || echo 'Not reachable')"
echo "  - Redis: $(timeout 3 bash -c 'cat < /dev/null > /dev/tcp/redis/6379' 2>/dev/null && echo 'Connected' || echo 'Not reachable')"

echo ""
echo "🗂️ File System:"
echo "  - NetBox venv: $(test -f /opt/netbox/venv/bin/activate && echo 'Exists' || echo 'Missing')"
echo "  - Plugin directory: $(test -d /workspaces/netbox-librenms-plugin && echo 'Exists' || echo 'Missing')"
echo "  - Setup script: $(test -f /workspaces/netbox-librenms-plugin/.devcontainer/scripts/setup.sh && echo 'Exists' || echo 'Missing')"
echo "  - Start script: $(test -f /workspaces/netbox-librenms-plugin/.devcontainer/scripts/start-netbox.sh && echo 'Exists' || echo 'Missing')"
echo "  - Start script executable: $(test -x /workspaces/netbox-librenms-plugin/.devcontainer/scripts/start-netbox.sh && echo 'Yes' || echo 'No')"
echo "  - Plugin config: $(test -f /workspaces/netbox-librenms-plugin/.devcontainer/plugin-config.py && echo 'Found' || echo 'Missing (using defaults)')"
echo "  - NetBox config path: /opt/netbox/netbox/netbox/configuration.py"

echo ""
echo "🚀 Process Status:"
if [ -f /tmp/netbox.pid ]; then
  PID=$(cat /tmp/netbox.pid)
  if kill -0 $PID 2>/dev/null; then
    echo "  - NetBox server: Running (PID: $PID)"
  else
    echo "  - NetBox server: PID file exists but process not running"
    echo "    (PID $PID is dead - NetBox may have crashed)"
  fi
else
  echo "  - NetBox server: Not started"
fi

# Check port listening
echo ""
echo "🌍 Port Check:"
if command -v netstat >/dev/null 2>&1; then
  echo "  - Port 8000: $(netstat -tuln 2>/dev/null | grep :8000 >/dev/null && echo 'Listening' || echo 'Not listening')"
elif command -v ss >/dev/null 2>&1; then
  echo "  - Port 8000: $(ss -tuln 2>/dev/null | grep :8000 >/dev/null && echo 'Listening' || echo 'Not listening')"
else
  echo "  - Port 8000: $(cat /proc/net/tcp 2>/dev/null | awk '$2 ~ /:1F40$/ {print "Listening"; exit}' | grep -q "Listening" && echo 'Listening' || echo 'Not listening')"
fi

echo ""
echo "✅ Diagnostic complete!"

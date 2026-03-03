#!/bin/bash

# Check if we should run in background or foreground
BACKGROUND=false
if [ "$1" = "--background" ] || [ "$1" = "-b" ]; then
  BACKGROUND=true
fi

echo "🌐 Starting NetBox development server..."

# Set required environment variables
export DEBUG="${DEBUG:-True}"

# Detect Codespaces and set access URL
if [ "$CODESPACES" = "true" ] && [ -n "$CODESPACE_NAME" ]; then
  GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  ACCESS_URL="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  echo "🔗 GitHub Codespaces detected"
else
  ACCESS_URL="http://localhost:8000"
  echo "🐛 Debug: ACCESS_URL is set to: $ACCESS_URL"
fi

# Load shared process management helpers
if ! source "$(dirname "$0")/process-helpers.sh"; then
  echo "ERROR: Failed to load process-helpers.sh" >&2
  exit 1
fi

# Kill any orphaned processes (not tracked by PID file)
echo "🧹 Cleaning up orphaned processes..."
if pgrep -f "python.*rqworker" >/dev/null 2>&1; then
  echo "   Found orphaned RQ workers, killing..."
  graceful_kill_pattern "python.*rqworker"
fi

if pgrep -f "python.*runserver.*8000" >/dev/null 2>&1; then
  echo "   Found orphaned NetBox servers, killing..."
  graceful_kill_pattern "python.*runserver.*8000"
fi

# Stop any tracked processes from PID files
if [ -f /tmp/netbox.pid ]; then
  OLD_PID=$(cat /tmp/netbox.pid 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    if is_expected_pid "$OLD_PID" "python.*runserver.*8000"; then
      graceful_kill_pid "$OLD_PID"
    else
      echo "⚠️  Skipping stale /tmp/netbox.pid (PID $OLD_PID is not NetBox runserver)"
    fi
  fi
  rm -f /tmp/netbox.pid
fi

if [ -f /tmp/rqworker.pid ]; then
  OLD_PID=$(cat /tmp/rqworker.pid 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    if is_expected_pid "$OLD_PID" "python.*rqworker"; then
      graceful_kill_pid "$OLD_PID"
    else
      echo "⚠️  Skipping stale /tmp/rqworker.pid (PID $OLD_PID is not rqworker)"
    fi
  fi
  rm -f /tmp/rqworker.pid
fi

# Activate NetBox virtual environment
source /opt/netbox/venv/bin/activate

# Navigate to NetBox directory
cd /opt/netbox/netbox

# Start RQ worker in background
echo "⚙️  Starting RQ worker..."
(
  source /opt/netbox/venv/bin/activate
  cd /opt/netbox/netbox
  python manage.py rqworker --verbosity=1
) > /tmp/rqworker.log 2>&1 &

RQ_PID=$!
echo $RQ_PID > /tmp/rqworker.pid
echo "✅ RQ worker started (PID: $RQ_PID)"

if [ "$BACKGROUND" = true ]; then
  echo "🚀 Starting NetBox in background"
  (
    export DEBUG="${DEBUG:-True}"
    source /opt/netbox/venv/bin/activate
    cd /opt/netbox/netbox
    python manage.py runserver 0.0.0.0:8000 --verbosity=0
  ) > /tmp/netbox.log 2>&1 &

  NETBOX_PID=$!
  echo $NETBOX_PID > /tmp/netbox.pid
  echo "✅ NetBox started in background (PID: $NETBOX_PID)"
  echo "📍 Access NetBox at: $ACCESS_URL"
  echo "💡 If clicking the URL opens 0.0.0.0:8000, manually type: localhost:8000"
  echo "📄 View logs with: netbox-logs"
  echo "🛑 Stop NetBox with: netbox-stop"
else
  echo "🌍 Starting NetBox in foreground"
  echo "📍 Access NetBox at: $ACCESS_URL"
  echo "💡 If clicking the URL opens 0.0.0.0:8000, manually type: localhost:8000"
  echo ""
  python manage.py runserver 0.0.0.0:8000
fi

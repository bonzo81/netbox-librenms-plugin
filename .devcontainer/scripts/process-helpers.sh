#!/bin/bash
# Shared process management helpers.
# Sourced by load-aliases.sh and start-netbox.sh.

# Graceful termination: SIGTERM, wait, then SIGKILL if still alive.
graceful_kill_pid() {
  local pid="$1"
  kill -15 "$pid" 2>/dev/null || true
  sleep 2
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
}

graceful_kill_pattern() {
  local pattern="$1"
  pkill -15 -f "$pattern" 2>/dev/null || true
  sleep 2
  pgrep -f "$pattern" >/dev/null 2>&1 && pkill -9 -f "$pattern" 2>/dev/null || true
}

# Verify a PID matches the expected process before killing it
is_expected_pid() {
  local pid="$1" pattern="$2"
  ps -p "$pid" -o args= 2>/dev/null | grep -Eq "$pattern"
}

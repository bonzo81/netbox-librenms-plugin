#!/bin/bash
# Quick alias loader for current session
# Usage: source .devcontainer/scripts/load-aliases.sh

export PATH="/opt/netbox/venv/bin:$PATH"
export DEBUG="${DEBUG:-True}"
PLUGIN_DIR="/workspaces/netbox-librenms-plugin"

alias netbox-run-bg="$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh --background"
alias netbox-run="$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh"

# Robust stop command that kills both tracked and orphaned processes
alias netbox-stop='echo "🛑 Stopping NetBox and RQ workers..."; \
  if [ -f /tmp/netbox.pid ]; then \
    PID=$(cat /tmp/netbox.pid 2>/dev/null); \
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then \
      kill "$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null; \
      echo "   Stopped NetBox (PID: $PID)"; \
    fi; \
    rm -f /tmp/netbox.pid; \
  fi; \
  if [ -f /tmp/rqworker.pid ]; then \
    PID=$(cat /tmp/rqworker.pid 2>/dev/null); \
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then \
      kill "$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null; \
      echo "   Stopped RQ worker (PID: $PID)"; \
    fi; \
    rm -f /tmp/rqworker.pid; \
  fi; \
  if pgrep -f "python.*rqworker" >/dev/null 2>&1; then \
    ORPHAN_COUNT=$(pgrep -cf "python.*rqworker" 2>/dev/null || echo 0); \
    pkill -9 -f "python.*rqworker" 2>/dev/null; \
    echo "   Killed $ORPHAN_COUNT orphaned RQ worker(s)"; \
  fi; \
  if pgrep -f "python.*runserver.*8000" >/dev/null 2>&1; then \
    pkill -9 -f "python.*runserver.*8000" 2>/dev/null; \
    echo "   Killed orphaned NetBox server(s)"; \
  fi; \
  echo "✅ All processes stopped"'

alias netbox-restart="netbox-stop && sleep 1 && netbox-run-bg"
alias netbox-reload="cd $PLUGIN_DIR && (command -v uv >/dev/null 2>&1 && uv pip install -e . || pip install -e .) && netbox-restart"

alias netbox-logs="tail -f /tmp/netbox.log"
alias netbox-status="[ -f /tmp/netbox.pid ] && kill -0 \$(cat /tmp/netbox.pid) 2>/dev/null && echo 'NetBox is running (PID: '\$(cat /tmp/netbox.pid)')' || echo 'NetBox is not running'; [ -f /tmp/rqworker.pid ] && kill -0 \$(cat /tmp/rqworker.pid) 2>/dev/null && echo 'RQ worker is running (PID: '\$(cat /tmp/rqworker.pid)')' || echo 'RQ worker is not running'"
alias rq-logs="tail -f /tmp/rqworker.log"
alias rq-status="[ -f /tmp/rqworker.pid ] && kill -0 \$(cat /tmp/rqworker.pid) 2>/dev/null && echo 'RQ worker is running (PID: '\$(cat /tmp/rqworker.pid)')' || echo 'RQ worker is not running'"
alias netbox-shell="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell"
alias netbox-test="cd $PLUGIN_DIR && source /opt/netbox/venv/bin/activate && python -m pytest"
alias netbox-manage="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py"
alias plugin-install="cd $PLUGIN_DIR && (command -v uv >/dev/null 2>&1 && uv pip install -e . || pip install -e .)"
alias ruff-check="cd $PLUGIN_DIR && ruff check ."
alias ruff-format="cd $PLUGIN_DIR && ruff format ."
alias ruff-fix="cd $PLUGIN_DIR && ruff check --fix ."
alias diagnose="$PLUGIN_DIR/.devcontainer/scripts/diagnose.sh"
alias plugins-install='if [ -f "$PLUGIN_DIR/.devcontainer/extra-requirements.txt" ]; then source /opt/netbox/venv/bin/activate && pip install -r "$PLUGIN_DIR/.devcontainer/extra-requirements.txt"; else echo "No .devcontainer/extra-requirements.txt found"; fi'

# RQ job inspection commands
alias rq-stats="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py rqstats"
alias rq-jobs="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell -c \"from django_rq import get_queue; q = get_queue('default'); print(f'Jobs in queue: {len(q)}'); [print(f'  {job.id[:8]}: {job.func_name} - {job.get_status()}') for job in q.jobs[:10]]\""
alias rq-failed="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell -c \"from django_rq import get_failed_queue; q = get_failed_queue(); print(f'Failed jobs: {len(q)}'); [print(f'  {job.id[:8]}: {job.func_name}') for job in q.jobs[:10]]\""
alias rq-recent="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell -c \"from core.models import Job; jobs = Job.objects.all().order_by('-created')[:10]; [print(f'{j.id}: {j.name[:50]} - {getattr(j.status, \\\"value\\\", j.status)} ({j.user})') for j in jobs]\""

echo "✅ Aliases loaded! Try: rq-status, rq-stats, rq-recent"

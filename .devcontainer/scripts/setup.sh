#!/bin/bash
set -e

echo "🚀 Setting up NetBox LibreNMS Plugin development environment..."
echo "📍 Current working directory: $(pwd)"
echo "👤 Current user: $(whoami)"
NETBOX_VERSION=${NETBOX_VERSION:-"latest"}
echo "📦 Using NetBox Docker image: netboxcommunity/netbox:${NETBOX_VERSION}"

# Verify NetBox virtual environment exists
if [ ! -f "/opt/netbox/venv/bin/activate" ]; then
    echo "❌ NetBox virtual environment not found at /opt/netbox/venv/"
    echo "This might indicate an issue with the NetBox Docker image."
    exit 1
fi

echo "🐍 Activating NetBox virtual environment..."
source /opt/netbox/venv/bin/activate

# Choose installer (uv if available, else pip)
if command -v uv >/dev/null 2>&1; then
  PIP_CMD="uv pip"
else
  PIP_CMD="pip"
fi

# Install dev tools
echo "🔧 Installing development dependencies..."
apt-get update -qq
apt-get install -y -qq net-tools
$PIP_CMD install pytest pytest-django ruff

# Detect plugin workspace directory (must contain pyproject.toml)
if [ -f "$PWD/pyproject.toml" ]; then
  PLUGIN_WS_DIR="$PWD"
elif [ -d "/workspaces/netbox-librenms-plugin" ] && [ -f "/workspaces/netbox-librenms-plugin/pyproject.toml" ]; then
  PLUGIN_WS_DIR="/workspaces/netbox-librenms-plugin"
else
  CANDIDATE_DIR=$(find /workspaces -maxdepth 2 -type f -name pyproject.toml 2>/dev/null | head -n1 | xargs dirname || true)
  if [ -n "$CANDIDATE_DIR" ] && [ -f "$CANDIDATE_DIR/pyproject.toml" ]; then
    PLUGIN_WS_DIR="$CANDIDATE_DIR"
  else
    echo "❌ Could not locate plugin workspace directory (pyproject.toml not found)."
    echo "   Checked: $PWD and /workspaces/*"
    exit 1
  fi
fi
echo "📂 Plugin workspace: $PLUGIN_WS_DIR"

# Install this plugin in development mode
echo "📦 Installing plugin in development mode from: $PLUGIN_WS_DIR"
if [ ! -f "$PLUGIN_WS_DIR/pyproject.toml" ] && [ ! -f "$PLUGIN_WS_DIR/setup.py" ]; then
  echo "❌ Neither pyproject.toml nor setup.py found in $PLUGIN_WS_DIR"
  ls -la "$PLUGIN_WS_DIR" || true
  exit 2
fi
cd "$PLUGIN_WS_DIR"
$PIP_CMD install -e .

CONF_FILE="/opt/netbox/netbox/netbox/configuration.py"

# Optional extras
if [ -f "$PLUGIN_WS_DIR/.devcontainer/extra-requirements.txt" ]; then
  echo "📦 Installing extra packages from extra-requirements.txt..."
  $PIP_CMD install -r "$PLUGIN_WS_DIR/.devcontainer/extra-requirements.txt"
fi


# Inject plugin loader into standard NetBox configuration if present
if [ -f "$CONF_FILE" ]; then
  if ! grep -q "# Devcontainer Plugins Loader" "$CONF_FILE" 2>/dev/null; then
    {
      echo "";
      echo "# Devcontainer Plugins Loader";
      echo "# Import PLUGINS/PLUGINS_CONFIG and optional extras dynamically from the workspace";
      echo "import importlib.util, os";
      echo "PLUGINS = ['netbox_librenms_plugin']";
      echo "PLUGINS_CONFIG = {'netbox_librenms_plugin': {}}";
      echo "_pc_path = '/workspaces/netbox-librenms-plugin/.devcontainer/config/plugin-config.py'";
      echo "if os.path.isfile(_pc_path):";
      echo "    _spec = importlib.util.spec_from_file_location('workspace_plugin_config', _pc_path)";
      echo "    _mod = importlib.util.module_from_spec(_spec)";
      echo "    try:";
      echo "        _spec.loader.exec_module(_mod)  # type: ignore[attr-defined]";
      echo "        PLUGINS = getattr(_mod, 'PLUGINS', PLUGINS)";
      echo "        PLUGINS_CONFIG = getattr(_mod, 'PLUGINS_CONFIG', PLUGINS_CONFIG)";
      echo "    except Exception as e:";
      echo "        print(f'⚠️  Failed to load plugin-config.py: {e}')";
      echo "else:";
      echo "    print('ℹ️ plugin-config.py not found; using defaults')";

      echo "# Import optional extra NetBox configuration (uppercase settings)";
      echo "_xc_path = '/workspaces/netbox-librenms-plugin/.devcontainer/config/extra-configuration.py'";
      echo "if os.path.isfile(_xc_path):";
      echo "    _xc_spec = importlib.util.spec_from_file_location('workspace_extra_configuration', _xc_path)";
      echo "    _xc_mod = importlib.util.module_from_spec(_xc_spec)";
      echo "    try:";
      echo "        _xc_spec.loader.exec_module(_xc_mod)  # type: ignore[attr-defined]";
      echo "        for _name in dir(_xc_mod):";
      echo "            if _name.isupper():";
      echo "                globals()[_name] = getattr(_xc_mod, _name)";
      echo "    except Exception as e:";
      echo "        print(f'⚠️  Failed to apply extra-configuration.py: {e}')";

      echo "# Import Codespaces configuration when applicable (uppercase settings)";
      echo "_cs_path = '/workspaces/netbox-librenms-plugin/.devcontainer/config/codespaces-configuration.py'";
      echo "if os.environ.get('CODESPACES') == 'true' and os.path.isfile(_cs_path):";
      echo "    _cs_spec = importlib.util.spec_from_file_location('workspace_codespaces_configuration', _cs_path)";
      echo "    _cs_mod = importlib.util.module_from_spec(_cs_spec)";
      echo "    try:";
      echo "        _cs_spec.loader.exec_module(_cs_mod)  # type: ignore[attr-defined]";
      echo "        for _name in dir(_cs_mod):";
      echo "            if _name.isupper():";
      echo "                globals()[_name] = getattr(_cs_mod, _name)";
      echo "    except Exception as e:";
      echo "        print(f'⚠️  Failed to apply codespaces-configuration.py: {e}')";

  echo "# Ensure SECRET_KEY exists: prefer environment, fallback to a dev placeholder";
  echo "if 'SECRET_KEY' not in globals() or not SECRET_KEY:";
  echo "    SECRET_KEY = os.environ.get('SECRET_KEY', 'dummydummydummydummydummydummydummydummydummydummydummydummy')";
    } >> "$CONF_FILE"
  fi

  if grep -q "netbox_librenms_plugin" "$CONF_FILE" 2>/dev/null; then
    echo "✅ Plugin configuration exists in NetBox settings"
  fi


else
  echo "⚠️  Warning: $CONF_FILE not found"
  echo "Plugin configuration may need to be added manually"
fi

# Run migrations and collectstatic
cd /opt/netbox/netbox

# Wait briefly for DB (compose healthchecks should ensure availability)
export DEBUG="${DEBUG:-True}"

echo "🗃️  Applying database migrations..."
python manage.py migrate 2>&1 | grep -E "(Operations to perform|Running migrations|Apply all migrations|No migrations to apply|\s+Applying|\s+OK)" || true

echo "🔐 Creating superuser (if not exists)..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
username = '${SUPERUSER_NAME:-admin}'
email = '${SUPERUSER_EMAIL:-admin@example.com}'
password = '${SUPERUSER_PASSWORD:-admin}'
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, email, password)
    print(f'Created superuser: {username}/{password}')
else:
    print(f'Superuser {username} already exists')
" 2>/dev/null || true

echo "📊 Collecting static files..."
python manage.py collectstatic --noinput >/dev/null 2>&1 || true

# Ensure scripts are executable
chmod +x "$PLUGIN_WS_DIR/.devcontainer/scripts/start-netbox.sh" || true
chmod +x "$PLUGIN_WS_DIR/.devcontainer/scripts/diagnose.sh" || true

# Aliases for convenience
cat >> ~/.bashrc << EOF
# NetBox LibreNMS Plugin Development Aliases
export PATH="/opt/netbox/venv/bin:\$PATH"
export DEBUG="\${DEBUG:-True}"
PLUGIN_DIR="$PLUGIN_WS_DIR"
alias netbox-run-bg="\$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh --background"
alias netbox-run="\$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh"
alias netbox-restart="netbox-stop && sleep 1 && netbox-run-bg"
alias netbox-reload="cd \$PLUGIN_DIR && (command -v uv >/dev/null 2>&1 && uv pip install -e . || pip install -e .) && netbox-restart"
alias netbox-stop="([ -f /tmp/netbox.pid ] && kill \\\$(cat /tmp/netbox.pid) && rm /tmp/netbox.pid && echo 'NetBox stopped' || echo 'NetBox not running'); ([ -f /tmp/rqworker.pid ] && kill \\\$(cat /tmp/rqworker.pid) && rm /tmp/rqworker.pid && echo 'RQ worker stopped' || echo 'RQ worker not running')"
alias netbox-logs="tail -f /tmp/netbox.log"
alias netbox-status="[ -f /tmp/netbox.pid ] && kill -0 \\\$(cat /tmp/netbox.pid) 2>/dev/null && echo 'NetBox is running (PID: '\\\$(cat /tmp/netbox.pid)')' || echo 'NetBox is not running'; [ -f /tmp/rqworker.pid ] && kill -0 \\\$(cat /tmp/rqworker.pid) 2>/dev/null && echo 'RQ worker is running (PID: '\\\$(cat /tmp/rqworker.pid)')' || echo 'RQ worker is not running'"
alias rq-logs="tail -f /tmp/rqworker.log"
alias rq-status="[ -f /tmp/rqworker.pid ] && kill -0 \\\$(cat /tmp/rqworker.pid) 2>/dev/null && echo 'RQ worker is running (PID: '\\\$(cat /tmp/rqworker.pid)')' || echo 'RQ worker is not running'"
alias netbox-shell="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell"
alias netbox-test="cd \$PLUGIN_DIR && source /opt/netbox/venv/bin/activate && python -m pytest"
alias netbox-manage="cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py"
alias plugin-install="cd \$PLUGIN_DIR && (command -v uv >/dev/null 2>&1 && uv pip install -e . || pip install -e .)"
alias ruff-check="cd \$PLUGIN_DIR && ruff check ."
alias ruff-format="cd \$PLUGIN_DIR && ruff format ."
alias ruff-fix="cd \$PLUGIN_DIR && ruff check --fix ."
alias diagnose="\$PLUGIN_DIR/.devcontainer/scripts/diagnose.sh"
alias plugins-install='if [ -f "$PLUGIN_DIR/.devcontainer/extra-requirements.txt" ]; then source /opt/netbox/venv/bin/activate && pip install -r "$PLUGIN_DIR/.devcontainer/extra-requirements.txt"; else echo "No .devcontainer/extra-requirements.txt found"; fi'
# Help
alias dev-help='echo "🎯 NetBox LibreNMS Plugin Development Commands:"; echo ""; echo "📊 NetBox Server Management:"; echo "  netbox-run-bg       : Start NetBox in background"; echo "  netbox-run          : Start NetBox in foreground (for debugging)"; echo "  netbox-stop         : Stop NetBox and RQ worker"; echo "  netbox-restart      : Restart NetBox and RQ worker"; echo "  netbox-reload       : Reinstall plugin and restart NetBox"; echo "  netbox-status       : Check if NetBox and RQ worker are running"; echo "  netbox-logs         : View NetBox server logs"; echo ""; echo "⚙️  Background Jobs (RQ Worker):"; echo "  rq-status           : Check if RQ worker is running"; echo "  rq-logs             : View RQ worker logs"; echo ""; echo "🛠️  Development Tools:"; echo "  netbox-shell        : Open NetBox Django shell"; echo "  netbox-test         : Run plugin tests"; echo "  netbox-manage       : Run Django management commands"; echo "  plugin-install      : Reinstall plugin in development mode"; echo ""; echo "🧹 Code Quality:"; echo "  ruff-check          : Check code with Ruff"; echo "  ruff-format         : Format code with Ruff"; echo "  ruff-fix            : Auto-fix code issues with Ruff"; echo ""; echo "🔎 Diagnostics:"; echo "  diagnose            : Run startup diagnostics"; echo "  dev-help           : Show this help message"; echo ""; echo "📖 NetBox available at: http://localhost:8000 (admin/admin)"; echo ""'

# Show welcome message for new terminals
bash $PLUGIN_DIR/.devcontainer/scripts/welcome.sh
EOF

# Fix Git remote URLs for dev container compatibility
echo "🔧 Checking Git remote configuration..."
cd "$PLUGIN_WS_DIR"
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [[ "$CURRENT_REMOTE" == git@github.com:* ]]; then
  # Convert SSH URL to HTTPS for dev container compatibility
  HTTPS_URL=$(echo "$CURRENT_REMOTE" | sed 's|git@github.com:|https://github.com/|')
  git remote set-url origin "$HTTPS_URL"
  echo "✅ Converted Git remote from SSH to HTTPS: $HTTPS_URL"
  echo "   This ensures compatibility with GitHub CLI authentication in dev containers"
elif [[ "$CURRENT_REMOTE" == https://github.com/* ]]; then
  echo "✅ Git remote already uses HTTPS: $CURRENT_REMOTE"
else
  echo "ℹ️  Git remote URL: $CURRENT_REMOTE (no changes needed)"
fi

# Final validation
cd /opt/netbox/netbox
if python -c "import netbox_librenms_plugin; print('✅ Plugin import successful')" 2>/dev/null | grep -q "✅ Plugin import successful"; then
  echo "✅ Plugin is properly installed and importable"
else
  echo "⚠️  Warning: Plugin may not be properly installed"
fi

echo "🚀 NetBox LibreNMS Plugin Dev Environment Ready!"

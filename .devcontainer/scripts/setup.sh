#!/bin/bash
set -e

echo "🚀 Setting up NetBox LibreNMS Plugin development environment..."
echo "📍 Current working directory: $(pwd)"
echo "👤 Current user: $(whoami)"
NETBOX_VERSION=${NETBOX_VERSION:-"latest"}
echo "📦 Using NetBox Docker image: netboxcommunity/netbox:${NETBOX_VERSION}"

# ---------------------------------------------------------------------------
# Detect plugin workspace directory (must contain pyproject.toml).
# Prints the resolved path to stdout on success, or an empty string on
# failure.  Always exits 0 — callers must check for an empty result.
# ---------------------------------------------------------------------------
detect_plugin_workspace() {
  if [ -f "$PWD/pyproject.toml" ]; then
    echo "$PWD"
  elif [ -d "/workspaces/netbox-librenms-plugin" ] && [ -f "/workspaces/netbox-librenms-plugin/pyproject.toml" ]; then
    echo "/workspaces/netbox-librenms-plugin"
  else
    local candidate
    candidate=$(find /workspaces -maxdepth 2 -type f -name pyproject.toml 2>/dev/null | head -n1 | xargs -r dirname || true)
    if [ -n "$candidate" ] && [ -f "$candidate/pyproject.toml" ]; then
      echo "$candidate"
    else
      echo ""
    fi
  fi
}

# Configure proxy for apt and pip if proxy environment variables are set
if [ -n "$HTTP_PROXY" ] || [ -n "$HTTPS_PROXY" ]; then
  echo "🌐 Configuring proxy settings..."

  # Configure apt proxy
  if [ -n "$HTTP_PROXY" ]; then
    echo "Acquire::http::Proxy \"$HTTP_PROXY\";" > /etc/apt/apt.conf.d/80proxy
    echo "  ✓ apt HTTP proxy: $HTTP_PROXY"
  fi
  if [ -n "$HTTPS_PROXY" ]; then
    echo "Acquire::https::Proxy \"$HTTPS_PROXY\";" >> /etc/apt/apt.conf.d/80proxy
    echo "  ✓ apt HTTPS proxy: $HTTPS_PROXY"
  fi

  # Configure pip proxy via environment (already set, but ensure it's exported)
  export HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy

  # Install custom CA certificate into the system trust store (for MITM proxies)
  PLUGIN_WS_DIR_EARLY="$(detect_plugin_workspace)"
  [ -z "$PLUGIN_WS_DIR_EARLY" ] && PLUGIN_WS_DIR_EARLY="/workspaces/netbox-librenms-plugin"
  CA_BUNDLE_SRC="$PLUGIN_WS_DIR_EARLY/ca-bundle.crt"
  if [ -f "$CA_BUNDLE_SRC" ]; then
    echo "🔐 Installing custom CA certificate into system trust store..."
    mkdir -p /usr/local/share/ca-certificates
    cp "$CA_BUNDLE_SRC" /usr/local/share/ca-certificates/proxy-ca-bundle.crt
    update-ca-certificates 2>/dev/null
    echo "  ✓ CA certificate installed into system trust store"
    # Point environment variables to the system bundle
    export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
  else
    echo "  ℹ️  No ca-bundle.crt found at $CA_BUNDLE_SRC, skipping CA install"
    # Only disable git SSL verification if explicitly opted-in via ALLOW_GIT_SSL_DISABLE.
    # Silently disabling SSL is a security risk; prefer providing a CA bundle instead.
    if [ "${ALLOW_GIT_SSL_DISABLE:-false}" = "true" ]; then
      git config --global http.sslVerify false
      echo "  ⚠️  git SSL verification disabled globally (ALLOW_GIT_SSL_DISABLE=true)"
    else
      echo "  ⚠️  No CA bundle found and git SSL verification was NOT disabled."
      echo "     If you need to disable it, set ALLOW_GIT_SSL_DISABLE=true in .devcontainer/.env"
      echo "     Preferred: provide a ca-bundle.crt in the workspace root instead."
    fi
  fi
fi

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
apt-get install -y -qq net-tools git
$PIP_CMD install pytest pytest-django ruff pre-commit

# Install GitHub CLI (gh)
# NOTE: The chained && commands below mean a partial failure (e.g. wget succeeds
# but apt-get install gh fails) may leave artifacts (keyring, sources list, temp
# file).  This is acceptable here because it only runs during container build —
# a rebuild will retry from scratch.  If this block is ever moved to a runtime
# script, consider adding a trap or explicit cleanup on error.
if ! command -v gh >/dev/null 2>&1; then
  echo "🔧 Installing GitHub CLI..."
  (type -p wget >/dev/null || apt-get install -y -qq wget) \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && out=$(mktemp) \
    && wget -qO "$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && cat "$out" | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update -qq \
    && apt-get install -y -qq gh \
    && rm -f "$out" \
    && echo "  ✓ GitHub CLI installed: $(gh --version | head -1)" \
    || echo "⚠️  GitHub CLI installation failed (non-fatal)"
fi

# Detect plugin workspace directory using the shared helper
PLUGIN_WS_DIR="$(detect_plugin_workspace)"
if [ -z "$PLUGIN_WS_DIR" ]; then
  echo "❌ Could not locate plugin workspace directory (pyproject.toml not found)."
  echo "   Checked: $PWD and /workspaces/*"
  exit 1
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

# Set up pre-commit hooks
echo "🪝 Installing pre-commit hooks..."
cd "$PLUGIN_WS_DIR"
git config --global --add safe.directory "$PLUGIN_WS_DIR"
pre-commit install --install-hooks 2>/dev/null || echo "⚠️  Pre-commit hook installation failed (may already be installed)"

# Ensure scripts are executable
chmod +x "$PLUGIN_WS_DIR/.devcontainer/scripts/start-netbox.sh" || true
chmod +x "$PLUGIN_WS_DIR/.devcontainer/scripts/diagnose.sh" || true
chmod +x "$PLUGIN_WS_DIR/.devcontainer/scripts/load-aliases.sh" || true

# Load aliases and welcome message from the canonical source (load-aliases.sh).
# Appended to .bashrc so every interactive shell gets them automatically.
# Guard with a sentinel so rerunning setup.sh doesn't create duplicate entries.
BASHRC_SENTINEL="# NetBox LibreNMS Plugin — source aliases from the single canonical file"
if ! grep -qF "$BASHRC_SENTINEL" ~/.bashrc 2>/dev/null; then
  cat >> ~/.bashrc << EOF
$BASHRC_SENTINEL
source "$PLUGIN_WS_DIR/.devcontainer/scripts/load-aliases.sh"

# Show welcome message for new terminals
bash "$PLUGIN_WS_DIR/.devcontainer/scripts/welcome.sh"
EOF
fi

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

echo ""
echo "🚀 NetBox LibreNMS Plugin Dev Environment Ready!"

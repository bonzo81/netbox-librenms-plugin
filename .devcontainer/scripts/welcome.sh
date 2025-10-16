#!/bin/bash

echo ""
echo "🎯 NetBox LibreNMS Plugin Development Environment"

if [ ! -f "/workspaces/netbox-librenms-plugin/.devcontainer/config/plugin-config.py" ]; then
  echo ""
  echo "⚠️  Plugin configuration not found: .devcontainer/config/plugin-config.py"
  echo "   Create it first: cp .devcontainer/config/plugin-config.py.example .devcontainer/config/plugin-config.py"
  echo "   Then edit it and set your plugin values (e.g. LibreNMS server URL/token)"
fi

# Check GitHub CLI authentication status
echo ""
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    # Get the authenticated user info
    GH_USER=$(gh api user --jq '.login' 2>/dev/null || echo "unknown")
    echo "✅ GitHub authenticated as: $GH_USER"
    echo "   Git is configured for GitHub operations"
  else
    echo "🔑 GitHub CLI available but not authenticated"
    echo "   Run 'gh auth login' to authenticate with GitHub"
    echo "   This will automatically configure Git for pushing/pulling"
  fi
else
  echo "⚠️  GitHub CLI not available"
fi

echo ""
if [ -n "$CODESPACES" ]; then
  echo "🌐 GitHub Codespaces Environment:"
  echo "   NetBox will be available via automatic port forwarding"
  echo "   Check the 'Ports' panel for the forwarded port labeled 'NetBox Web Interface'"
  if [ -n "$CODESPACE_NAME" ]; then
    # Try to construct the likely URL (GitHub Codespaces pattern)
    CODESPACE_URL="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-preview.app.github.dev}"
    echo "   Expected URL: $CODESPACE_URL"
  fi
  echo "   💡 Click the link in the Ports panel or look for the 'Open in Browser' button"
else
  echo "🖥️  Local Development Environment:"
  echo "   NetBox will be available at: http://localhost:8000 (paste into you browser)"
fi

echo ""
echo "🚀 Quick start:"
echo "   • Type 'netbox-run' to start the development server"
echo "   • Type 'dev-help' to see all available commands"
echo "   • Edit code in the workspace - auto-reload is enabled"
echo ""
# NetBox LibreNMS Plugin - Development Container

The Dev container was created to help aid with development without the need for a full NetBox installation locally. It provides a complete development environment using the official NetBox Docker images with PostgreSQL and Redis.

This directory contains the development container configuration for the NetBox LibreNMS Plugin.

## Table of contents

- [Prerequisites](#-prerequisites)
- [Quick Start](#-quick-start)
- [Out-of-the-box defaults](#out-of-the-box-defaults)
- [Configuration](#-configuration)
   - [NetBox Version and Environment](#netbox-version-and-environment-use-devcontainerenv)
   - [Changing NetBox Versions](#-changing-netbox-versions)
   - [Other environment variables](#other-environment-variables)
- [Other configurations](#other-configurations)
   - [NetBox Configuration](#netbox-configuration)
   - [Additional packages](#additional-packages-including-other-netbox-plugins)
- [Git Setup](#-git-setup)
- [Commands](#-commands-aliases)
- [Troubleshooting](#-troubleshooting)
- [Cleanup](#-cleanup-remove-the-dev-containers)

## 📋 Prerequisites

### For Local Development (VS Code)

To use this dev container locally, you need:

- **[Docker](https://docs.docker.com/get-docker/)** (Docker Engine + Docker Compose)
- **[Visual Studio Code](https://code.visualstudio.com/)**
- **[Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)** for VS Code

### For GitHub Codespaces

If using GitHub Codespaces, all prerequisites are automatically available - just click "Code" → "Create codespace" in the GitHub repository.

**⚠️ Network Limitation with Codespaces:** GitHub Codespaces runs in the cloud and can only access publicly available LibreNMS servers.

If you need to test with a LibreNMS instance on a private network (local lab, corporate network, etc.), you'll need to use the local dev container instead.


## 🚀 Quick Start

1. Fork and Clone: fork the plugin repo in Github and clone locally
2. Open in VS Code and choose "Reopen in Container" (or Ctrl+Shift+P → Dev Containers: Reopen in Container)
2. Wait for setup (~5min on first run or when new NetBox image is used). The container will install the plugin and prep NetBox
3. Set up GitHub access: `gh auth login` (for pushing/pulling code changes)
4. Create your plugin config — see [Plugin configuration](#plugin-configuration):
   - `cp .devcontainer/config/plugin-config.py.example .devcontainer/config/plugin-config.py`
   - Edit it with your server details (tokens/URLs)
5. Start NetBox with `netbox-run` (or `netbox-run-bg` in background) (see [Commands](#-commands-aliases))
6. Access NetBox at http://localhost:8000
   - Username: `admin`
   - Password: `admin`

### 🔄 Code changes and Committing
8. Edit code in the repo root.  Check out [contributing docs](../docs/contributing.md)
9. Use `netbox-logs` to follow log output on screen
6. Commit changes and contribute as normal by submitting a PR on GitHub.

### Quick Tips
- **Auto-reload**: Works for most code changes when `DEBUG=True`
- **Config changes**: Always restart NetBox after changing plugin settings
- **GitHub CLI**: Automatically configured for easy PR submission
- **Logs**: Use `netbox-logs` to debug issues in real-time

## Out-of-the-box defaults

Below are the dev container defaults. The field name to change these defaults is listed below each line.

- NetBox image: `netboxcommunity/netbox:${NETBOX_VERSION:-latest}` (default `latest`)
   - .env: `NETBOX_VERSION`
- DB: PostgreSQL 15 (db: `netbox`, user: `netbox`, password: `netbox`)
   - .env: `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- Redis: 7-alpine
   - .env: `REDIS_HOST`, `REDIS_PASSWORD`
- NetBox DEBUG: `True` (dev only)
   - .env: `DEBUG`
- Secret key: dev placeholder (not for production)
   - .env: `SECRET_KEY` (optional). If unset, a dev-safe default is used inside the container.
- Superuser: `admin` / `admin`
   - .env: `SUPERUSER_NAME`, `SUPERUSER_EMAIL`, `SUPERUSER_PASSWORD`, `SKIP_SUPERUSER`
- Plugin loader: enabled; reads `.devcontainer/config/plugin-config.py` if present
- If `plugin-config.py` is missing: plugin is enabled with empty config (features won’t work until configured)

## 🔧 Configuration

### NetBox Version and Environment (use .devcontainer/.env)

The default NetBox docker image version is set to `latest`.

To update the NetBox image version create `.devcontainer/.env` using `NETBOX_VERSION` Example:

If you don’t have an env file yet, create it in the `.devcontainer/` folder from the example and customize:

```bash
cp .devcontainer/.env.example .devcontainer/.env
```

Change the NetBox image version in the `.env` file:
```bash
# .devcontainer/.env
NETBOX_VERSION=v4.2-3.3.4
```

After changing `.devcontainer/.env`, rebuild the dev container to apply it (Command Palette → Dev Containers: Rebuild Container).

See NetBox Docker tag docs for available tags:
https://hub.docker.com/r/netboxcommunity/netbox/#container-image-tags

### 🔄 Changing NetBox Versions

You might experience issues with database schemas and migrations when changing NetBox version. Since this is a development container, the simplest way to handle NetBox version changes is to reset the database completely.

**To change NetBox versions:**

1. **Update the version** in `.devcontainer/.env`:
   ```bash
   NETBOX_VERSION=v4.1-3.1.1  # or whatever version you need
   ```

2. **Reset the development environment:**
   ```bash
   # Stop containers and remove volumes (removes all dev data)
   docker compose down -v

   # Rebuild container with new NetBox version
   # VS Code: Ctrl+Shift+P → "Dev Containers: Rebuild Container"
   ```

3. **Start fresh:** The container will automatically set up the new NetBox version with a clean database.

**Note:** This removes all development data (test devices, configurations, etc.), but that's typically fine for development and testing scenarios.

### Other environment variables:

- Core: `NETBOX_VERSION`, `DEBUG`, `SECRET_KEY`
- Database: `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- Redis: `REDIS_HOST`, `REDIS_PASSWORD`
- Superuser: `SUPERUSER_NAME`, `SUPERUSER_EMAIL`, `SUPERUSER_PASSWORD`, `SKIP_SUPERUSER`


After any `.env` change, rebuild the dev container to apply environment updates.
   - VS Code: “Dev Containers: Rebuild Container” (from the Command Palette)


## Other configurations

###  NetBox Configuration:
- Create `.devcontainer/config/extra-configuration.py` for additional NetBox settings (TIME_ZONE, banners, logging, etc)
   - After changes: run `netbox-restart` (see [Commands](#-commands-aliases))

### Additional packages (including other netbox plugins)
- Create `.devcontainer/extra-requirements.txt` for extra Python packages. Example: `.devcontainer/extra-requirements.txt.example`.
   - After changes: run `plugins-install` to install packages, then `netbox-restart` (see [Commands](#-commands-aliases))

## 🔧 Git Setup

The dev container includes Git and GitHub CLI pre-installed. You'll need to configure authentication for commits and pushes:

### Recommended: GitHub CLI (Easiest)
```bash
# Authenticate with GitHub (handles Git credentials automatically)
gh auth login

# Verify authentication
gh auth status
```

The GitHub CLI automatically configures Git to use your GitHub credentials for this repository.

### GitHub Codespaces
In Codespaces, GitHub authentication is often pre-configured, but you can verify with:
```bash
# Check current status
gh auth status

# If needed, authenticate
gh auth login
```

### Manual Git Setup (Alternative)
If you prefer manual setup or need non-GitHub authentication:

#### Local Dev Container
```bash
# Set your Git identity
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"

# Optional: Set default branch name
git config --global init.defaultBranch main
```

#### SSH Key Setup (for private repositories)
```bash
# Generate SSH key (if you don't have one)
ssh-keygen -t ed25519 -C "your.email@example.com"

# Add to SSH agent
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519

# Display public key to add to GitHub
cat ~/.ssh/id_ed25519.pub
```

> **💡 Authentication Persistence:**
> - **GitHub CLI**: Authentication persists across container rebuilds (stored in persistent volume)
> - **Manual Git Config**: Git identity settings are **NOT persistent** across rebuilds
> - **GitHub Codespaces**: Authentication is automatically handled by the Codespaces platform
>
> **Recommendation**: Use `gh auth login` for the best experience - it's persistent and handles everything automatically.

## 📋 Commands (aliases)

- `netbox-run-bg` - start NetBox in background
- `netbox-run` - start NetBox in foreground (with Django logs showing)
- `netbox-stop` - stop background server
- `netbox-restart` - restart NetBox
- `netbox-reload` - reinstall plugin and restart
- `netbox-status` - show server status
- `netbox-logs` - tail server logs
- `netbox-shell` - Django shell
- `netbox-manage` - Django manage.py
- `netbox-test` - run tests
- `plugin-install` - reinstall plugin
- `ruff-check|format|fix` - Ruff helpers

## 🐛 Troubleshooting

- Rebuild container if setup fails (Ctrl+Shift+P → Rebuild Container)
- Check logs `docker-compose logs postgres redis devcontainer`
- Ensure plugin is importable inside container: `python -c "import netbox_librenms_plugin"`
- Run `diagnose` to see whether `plugin-config.py` was detected and the NetBox config path

## 🧹 Cleanup: remove the dev containers

Data warning: removing volumes deletes all dev data (PostgreSQL DB, Redis AOF, NetBox media/static).

1) Close the VS Code Dev Container session first (Command Palette → Dev Containers: Close Remote)
2) From the repo root:

```bash
# Stop and remove containers
docker compose -f .devcontainer/docker-compose.yml down

# Also remove named volumes (DB/media/static) — irreversible
docker compose -f .devcontainer/docker-compose.yml down -v

# Optional: reclaim image space built/pulled for this project
docker compose -f .devcontainer/docker-compose.yml down --rmi local -v
```

Alternatively, run from inside the .devcontainer folder without -f:

```bash
cd .devcontainer
docker compose down           # containers only
docker compose down -v        # containers + volumes
```

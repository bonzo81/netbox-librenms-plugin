#!/bin/bash
# Build the NetBox image with the plugin wheel and start the end-to-end stack.
# Usage: NETBOX_CONTAINER_TAG=v4.7 tests/compose_e2e/setup.sh [path/to/plugin.whl]
# Without a wheel argument, the script takes the one wheel in dist/.
set -euo pipefail

docker_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/docker" && pwd)"
repo_root="$(cd "$docker_dir/../../.." && pwd)"
: "${NETBOX_CONTAINER_TAG:?set NETBOX_CONTAINER_TAG to a netboxcommunity/netbox image tag}"

if [ $# -ge 1 ]; then
    wheel="$1"
else
    shopt -s nullglob
    wheels=("$repo_root"/dist/*.whl)
    shopt -u nullglob
    if [ "${#wheels[@]}" -ne 1 ]; then
        echo "Expected exactly one wheel in dist/, found ${#wheels[@]}. Remove dist/ and run 'uv build' again." >&2
        exit 1
    fi
    wheel="${wheels[0]}"
fi
whl_file="$(basename "$wheel")"
# The build context must hold only this wheel.
rm -f "$docker_dir"/*.whl
cp "$wheel" "$docker_dir/$whl_file"
# BuildKit reads a build secret only from inside the project.
cp "${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}" "$docker_dir/host_ca.crt"

# Later compose commands (logs, down) read the same values from .env.
{
    echo "NETBOX_CONTAINER_TAG=$NETBOX_CONTAINER_TAG"
    echo "WHL_FILE=$whl_file"
    echo "NETBOX_PORT=${NETBOX_PORT:-8000}"
    if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
        echo "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME"
    fi
} > "$docker_dir/.env"

cd "$docker_dir"
build_args=()
# Compose does not pass the host proxy to a build. A name without a value reads the environment.
for name in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
    build_args+=(--build-arg "$name")
done
docker compose build "${build_args[@]}"
docker compose up --detach --wait --wait-timeout 900

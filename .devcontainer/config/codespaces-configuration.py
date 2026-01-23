# GitHub Codespaces NetBox Configuration
# CSRF/hosts setup for Codespaces URLs

import os

codespace_name = os.environ.get("CODESPACE_NAME")
port_domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")

if codespace_name:
    codespaces_url = f"https://{codespace_name}-8000.{port_domain}"
    CSRF_TRUSTED_ORIGINS = [
        codespaces_url,
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    ALLOWED_HOSTS = [
        f"{codespace_name}-8000.{port_domain}",
        "localhost",
        "127.0.0.1",
        "*",
    ]
    print(f"🔗 Codespaces detected: {codespace_name}")
    print(f"🔒 CSRF Trusted Origins: {CSRF_TRUSTED_ORIGINS}")
    print(f"🌐 Allowed Hosts: {ALLOWED_HOSTS}")
else:
    CSRF_TRUSTED_ORIGINS = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    ALLOWED_HOSTS = ["*"]

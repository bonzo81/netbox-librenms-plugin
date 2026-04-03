"""Conftest for e2e tests — no Django initialization needed."""

import os

# Prevent pytest-django from trying to initialize Django
os.environ.pop("DJANGO_SETTINGS_MODULE", None)

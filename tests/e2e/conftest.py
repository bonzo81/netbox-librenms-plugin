"""Conftest for end-to-end Playwright tests.

These tests are excluded from the default pytest discovery via ``testpaths``
in ``pyproject.toml`` and are intended to be invoked explicitly:

    python -m pytest tests/e2e/test_module_install.py -v -s

Note: ``pyproject.toml`` sets ``DJANGO_SETTINGS_MODULE = "netbox.settings"``
under ``[tool.pytest.ini_options]``, which pytest-django reads directly from
the config file (not from the environment).  Popping the env var here would
have no effect on pytest-django's initialisation.  The e2e tests do not
import or use Django models — they drive a running NetBox over HTTP — so
pytest-django's auto-loading is harmless and we leave it alone.  If you ever
need to skip pytest-django entirely for this suite, invoke pytest with
``-p no:django``.
"""

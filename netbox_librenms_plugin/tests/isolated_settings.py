"""NetBox test settings that require caller-selected database and Redis targets."""

import importlib
import os
import runpy
from pathlib import Path

from netbox_librenms_plugin.tests.parallel import isolated_redis_databases


# Strip once and export the cleaned value: exporting the raw one let a padded host pass this
# check and then fail later at connection time instead of here.
def _required_environment(name):
    """Return a required test environment variable, or explain what is missing."""
    try:
        return os.environ[name]
    except KeyError:
        raise RuntimeError(
            f"{name} must be set for this settings module. It is the DJANGO_SETTINGS_MODULE in "
            "pyproject.toml, so every pytest run needs TEST_DB_NAME and TEST_REDIS_HOST."
        ) from None


_test_redis_host = _required_environment("TEST_REDIS_HOST").strip()
if not _test_redis_host:
    raise ValueError("TEST_REDIS_HOST must not be empty.")
_tasks_redis_database, _cache_redis_database = isolated_redis_databases(os.environ.get("PYTEST_XDIST_WORKER"))
os.environ["REDIS_HOST"] = _test_redis_host
os.environ["REDIS_CACHE_HOST"] = _test_redis_host
os.environ["REDIS_DATABASE"] = str(_tasks_redis_database)
os.environ["REDIS_CACHE_DATABASE"] = str(_cache_redis_database)

_configuration_name = os.environ.get("NETBOX_CONFIGURATION", "netbox.configuration")
_configuration = importlib.import_module(_configuration_name)
# Take the servers from the file CI installs, not from the developer's own NetBox configuration.
_ci_configuration = Path(__file__).resolve().parents[2] / "media" / "configuration.testing.py"
_plugin_config = runpy.run_path(str(_ci_configuration))["PLUGINS_CONFIG"]["netbox_librenms_plugin"]
_configuration.PLUGINS = ["netbox_librenms_plugin"]
_configuration.PLUGINS_CONFIG = {"netbox_librenms_plugin": _plugin_config}
os.environ["NETBOX_CONFIGURATION"] = _configuration.__name__

from netbox.settings import *  # noqa: E402

TEST_DB_NAME_PREFIX = "test_"

_test_database_name = _required_environment("TEST_DB_NAME")
if not _test_database_name.startswith(TEST_DB_NAME_PREFIX):
    raise ValueError(f"TEST_DB_NAME must start with '{TEST_DB_NAME_PREFIX}'.")

DATABASES["default"].setdefault("TEST", {})["NAME"] = _test_database_name

# Apply this before connection setup, including separate wrappers created inside workers.
for _database_config in DATABASES.values():
    _database_config.setdefault("OPTIONS", {})["connect_timeout"] = 5

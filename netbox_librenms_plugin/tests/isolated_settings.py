"""NetBox test settings that require caller-selected database and Redis targets."""

import os

from netbox_librenms_plugin.tests.parallel import isolated_redis_databases


# Strip once and export the cleaned value: exporting the raw one let a padded host pass this
# check and then fail later at connection time instead of here.
_test_redis_host = os.environ["TEST_REDIS_HOST"].strip()
if not _test_redis_host:
    raise ValueError("TEST_REDIS_HOST must not be empty.")
_tasks_redis_database, _cache_redis_database = isolated_redis_databases(os.environ.get("PYTEST_XDIST_WORKER"))
os.environ["REDIS_HOST"] = _test_redis_host
os.environ["REDIS_CACHE_HOST"] = _test_redis_host
os.environ["REDIS_DATABASE"] = str(_tasks_redis_database)
os.environ["REDIS_CACHE_DATABASE"] = str(_cache_redis_database)

from netbox.settings import *  # noqa: E402, F403


TEST_DB_NAME_PREFIX = "test_"

_test_database_name = os.environ["TEST_DB_NAME"]
if not _test_database_name.startswith(TEST_DB_NAME_PREFIX):
    raise ValueError(f"TEST_DB_NAME must start with '{TEST_DB_NAME_PREFIX}'.")

DATABASES["default"].setdefault("TEST", {})["NAME"] = _test_database_name  # noqa: F405

"""Root conftest: pytest only calls `pytest_xdist_auto_num_workers` from a startup conftest."""

from netbox_librenms_plugin.tests.parallel import pytest_xdist_auto_num_workers  # noqa: F401

# Testing Guide

This guide explains how to run the test suite, write new tests, and debug failures.

## Quick Start

Outside the devcontainer and CI, install the browser the Playwright suite drives:

```bash
python -m playwright install chromium
```

The devcontainer setup script and the CI browser job install it for you.

Run all tests with a single command:

```bash
make test
```

Run the Django suite without the separate browser suite:

```bash
make unittest
```

Run the Playwright browser suite:

```bash
make browser
```

## Test Structure

The test suite covers all major plugin functionality. Tests are organized by the module they verify:

| Test File | What It Tests |
|-----------|---------------|
| [test_librenms_api.py](../../netbox_librenms_plugin/tests/test_librenms_api.py) | LibreNMS API client—connections, device operations, locations, ports, and error handling |
| [test_import_utils.py](../../netbox_librenms_plugin/tests/test_import_utils.py) | Device import logic—filtering, validation, and data transformation |
| [test_import_validation_helpers.py](../../netbox_librenms_plugin/tests/test_import_validation_helpers.py) | Field validation for sites, roles, platforms, and device types |
| [test_utils.py](../../netbox_librenms_plugin/tests/test_utils.py) | General utilities—name matching, speed conversion, and data formatting |
| [test_background_jobs.py](../../netbox_librenms_plugin/tests/test_background_jobs.py) | Background job execution and view decision logic |
| [test_vlan_sync.py](../../netbox_librenms_plugin/tests/test_vlan_sync.py) | VLAN sync—API fetching, comparison logic, CSS class utilities, and sync actions |
| [test_interface_vlan_sync.py](../../netbox_librenms_plugin/tests/test_interface_vlan_sync.py) | Interface VLAN assignments—group resolution, mode detection, and per-interface VLAN assignment |
| [test_librenms_id.py](../../netbox_librenms_plugin/tests/test_librenms_id.py) | Multi-server librenms_id helpers—get/set/find/migrate and boolean rejection |
| [test_mixins.py](../../netbox_librenms_plugin/tests/test_mixins.py) | View mixins—CacheMixin key generation, LibreNMSAPIMixin lazy init |
| [test_sync_devices.py](../../netbox_librenms_plugin/tests/test_sync_devices.py) | Device sync views—field updates, platform creation |
| [test_sync_interfaces.py](../../netbox_librenms_plugin/tests/test_sync_interfaces.py) | Interface sync—port matching, attribute updates, MAC handling, librenms_id assignment |
| [test_virtual_chassis.py](../../netbox_librenms_plugin/tests/test_virtual_chassis.py) | Virtual chassis detection—VC member naming patterns and name generation |
| [test_sync_view_mismatch.py](../../netbox_librenms_plugin/tests/test_sync_view_mismatch.py) | Sync page context—device type mismatch detection and badge rendering |
| [test_coverage_device_fields.py](../../netbox_librenms_plugin/tests/test_coverage_device_fields.py) | Device field sync view—field update logic and device field mapping |
| [test_coverage_list.py](../../netbox_librenms_plugin/tests/test_coverage_list.py) | Import list view—background job decision, job result loading, and GET handler |
| [test_coverage_api.py](../../netbox_librenms_plugin/tests/test_coverage_api.py) | LibreNMS API client—malformed payload guards, error paths, and edge cases |
| [test_coverage_api2.py](../../netbox_librenms_plugin/tests/test_coverage_api2.py) | API views—device status, background job management, VM status endpoints |
| [test_coverage_base_views.py](../../netbox_librenms_plugin/tests/test_coverage_base_views.py) | Base view coverage tests—sync table views, context data, and data pipeline |
| [test_coverage_base_views2.py](../../netbox_librenms_plugin/tests/test_coverage_base_views2.py) | Additional base view coverage—IP address sync, cable matching, edge cases |
| [test_coverage_cache.py](../../netbox_librenms_plugin/tests/test_coverage_cache.py) | Import cache helpers—cache key generation, active search tracking, metadata |
| [test_coverage_device_operations.py](../../netbox_librenms_plugin/tests/test_coverage_device_operations.py) | Device validation—type matching, serial handling, VC detection, role lookup |
| [test_coverage_forms.py](../../netbox_librenms_plugin/tests/test_coverage_forms.py) | Import forms—filter form choices, background-job option guards, field validation |
| [test_coverage_mixins.py](../../netbox_librenms_plugin/tests/test_coverage_mixins.py) | View mixins—VLAN group scope resolution, VlanAssignmentMixin, scope priority |
| [test_coverage_sync_interfaces.py](../../netbox_librenms_plugin/tests/test_coverage_sync_interfaces.py) | Interface sync view—port caching, attribute updates, MAC handling, VC member routing |
| [test_coverage_sync_view.py](../../netbox_librenms_plugin/tests/test_coverage_sync_view.py) | Sync view base class—context preparation and tab rendering |
| [test_coverage_sync_views.py](../../netbox_librenms_plugin/tests/test_coverage_sync_views.py) | Sync action views—cables, IP addresses, VLAN sync action handlers |
| [test_coverage_sync_views2.py](../../netbox_librenms_plugin/tests/test_coverage_sync_views2.py) | Additional sync action view coverage—device fields, device name/type sync |
| [test_coverage_sync_views3.py](../../netbox_librenms_plugin/tests/test_coverage_sync_views3.py) | Further sync action view coverage—location sync, VLAN assignment edge cases |
| [test_coverage_actions.py](../../netbox_librenms_plugin/tests/test_coverage_actions.py) | Import action views—bulk import, device role/cluster/rack update, validation details |
| [test_coverage_filters.py](../../netbox_librenms_plugin/tests/test_coverage_filters.py) | Import filter logic—filter form processing and device count helpers |
| [test_init.py](../../netbox_librenms_plugin/tests/test_init.py) | Plugin startup—`_ensure_librenms_id_custom_field` creation, type migration, and multi-DB alias handling |
| [test_coverage_tables.py](../../netbox_librenms_plugin/tests/test_coverage_tables.py) | Sync tables—column rendering, row data, interface and cable table helpers |
| [test_coverage_utils.py](../../netbox_librenms_plugin/tests/test_coverage_utils.py) | Utility function coverage—name matching, speed conversion, site/platform lookup |
| [test_coverage_virtual_chassis.py](../../netbox_librenms_plugin/tests/test_coverage_virtual_chassis.py) | Virtual chassis coverage—VC creation, position conflict handling, member naming |
| [test_coverage_vlans_table.py](../../netbox_librenms_plugin/tests/test_coverage_vlans_table.py) | VLAN sync table—column rendering, group assignment, VLAN comparison rows |
| [test_sync_modules.py](../../netbox_librenms_plugin/tests/test_sync_modules.py) | Module sync—inventory matching, module type resolution, and normalization rules |
| [test_modules_view.py](../../netbox_librenms_plugin/tests/test_modules_view.py) | Module sync view—context preparation, table rendering, and module bay mapping |
| [test_tables_modules.py](../../netbox_librenms_plugin/tests/test_tables_modules.py) | Module tables—column rendering, row formatting, and action buttons |
| [test_permissions.py](../../netbox_librenms_plugin/tests/test_permissions.py) | Permission enforcement—mixin contracts, object-level permissions, and write guards |
| [test_vm_operations.py](../../netbox_librenms_plugin/tests/test_vm_operations.py) | VM operations—virtual machine sync, interface handling, and VM-specific views |
| [test_integration_sync.py](../../netbox_librenms_plugin/tests/test_integration_sync.py) | Integration tests—API client against local mock HTTP server |
| [test_integration_virtual_chassis.py](../../netbox_librenms_plugin/tests/test_integration_virtual_chassis.py) | Integration tests—VC detection, negative cache, multi-server cache isolation |
| [test_view_wiring.py](../../netbox_librenms_plugin/tests/test_view_wiring.py) | Smoke tests—view class MRO, mixin wiring, permission contracts, and template syntax |
| [test_platform_mapping.py](../../netbox_librenms_plugin/tests/test_platform_mapping.py) | PlatformMapping model—clean validation, YAML serialization, table/form/filterset, and find_matching_platform integration |
| [test_module_replace.py](../../netbox_librenms_plugin/tests/test_module_replace.py) | Module replacement—module swapping, bay reindexing, and replacement validation |

Supporting files:

| File | Purpose |
|------|---------|
| [conftest.py](../../netbox_librenms_plugin/tests/conftest.py) | Shared pytest fixtures |
| [test_librenms_api_helpers.py](../../netbox_librenms_plugin/tests/test_librenms_api_helpers.py) | Auto-use fixture for API configuration mocking |
| [mock_librenms_server.py](../../netbox_librenms_plugin/tests/mock_librenms_server.py) | Minimal HTTP mock server for integration tests |

## Running Tests

### Running Specific Tests

```bash
# Run a specific test file
pytest netbox_librenms_plugin/tests/test_librenms_api.py -v

# Run a specific test class
pytest netbox_librenms_plugin/tests/test_librenms_api.py::TestLibreNMSAPIConnection -v

# Run a specific test method
pytest netbox_librenms_plugin/tests/test_librenms_api.py::TestLibreNMSAPIConnection::test_connection_success -v
```

### Running Tests by Area

```bash
# API client tests
pytest netbox_librenms_plugin/tests/test_librenms_api.py netbox_librenms_plugin/tests/test_coverage_api.py netbox_librenms_plugin/tests/test_coverage_api2.py -v

# Import and validation tests
pytest netbox_librenms_plugin/tests/test_import_utils.py netbox_librenms_plugin/tests/test_import_validation_helpers.py netbox_librenms_plugin/tests/test_utils.py -v

# Background job tests
pytest netbox_librenms_plugin/tests/test_background_jobs.py -v

# Multi-server librenms_id tests
pytest netbox_librenms_plugin/tests/test_librenms_id.py -v

# Sync view tests (devices, interfaces, modules)
pytest netbox_librenms_plugin/tests/test_sync_devices.py netbox_librenms_plugin/tests/test_sync_interfaces.py netbox_librenms_plugin/tests/test_sync_modules.py -v

# Integration tests (API client against mock HTTP server)
pytest netbox_librenms_plugin/tests/test_integration_*.py -v

# Sync view mismatch detection and permission enforcement
pytest netbox_librenms_plugin/tests/test_sync_view_mismatch.py netbox_librenms_plugin/tests/test_permissions.py -v

# View wiring and template syntax smoke tests
pytest netbox_librenms_plugin/tests/test_view_wiring.py -v
```

### Debugging Failed Tests

```bash
# Show full traceback
pytest netbox_librenms_plugin/tests/ -v --tb=long

# Show print statements during tests
pytest netbox_librenms_plugin/tests/ -v -s

# Stop on first failure
pytest netbox_librenms_plugin/tests/ -v -x

# Re-run only failed tests from last run
pytest netbox_librenms_plugin/tests/ -v --lf

# Re-run only failed browser tests from last run
pytest -c netbox_librenms_plugin/tests/browser/pytest.ini netbox_librenms_plugin/tests/browser --lf
```

## Testing Philosophy

A test is worth what it actually runs. Rank the options in this order:

1. **End to end** — a real request through the real view, the real permission gate, the real ORM, and the real response.
2. **Integration against real dependencies** — a real test database, real NetBox models, real forms and serializers.
3. **Narrow unit tests** — only for pure functions with no I/O.

Reserve mocks for boundaries you cannot run locally: the LibreNMS HTTP API, an error a local
database cannot produce (an `IntegrityError` on save, a lock `DatabaseError`), or a timing seam that
injects one deterministic concurrent write into an otherwise real flow.

A `MagicMock` answers any attribute and any method, so a test built on mocked models stays green
while the real query path is broken. It re-asserts the author's assumptions rather than the code.

- **No external network access**: Tests never call external services. The LibreNMS API is exercised through a local loopback HTTP server (`mock_librenms_server.py`), so the real API client runs against realistic HTTP responses without a running LibreNMS instance.
- **Isolated**: Each test is independent. Database tests run in a transaction that is rolled back.
- **Coverage exclusions**: Test files themselves are excluded from coverage reports (see `[tool.coverage.run]` omit list in `pyproject.toml`).

The database-backed suite needs a test database, so the first run pays a one-off migration cost.
Pass `--reuse-db` on later runs to skip it.

## Writing New Tests

### Basic Test Template

Never set `pytest_plugins` in a test module. pytest registers that plugin for the whole
session, so any autouse fixture it carries also applies to every test file collected after
it. Import the fixture, or bind it as a module attribute, in the module that needs it.

New tests should follow this structure:

```python
import pytest


@pytest.mark.django_db
class TestFeatureName:
    """Tests for [feature description]."""

    def test_specific_behavior(self, settings):
        """Describe what this test verifies."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import (
            make_request as make_view_request,
            make_user_with_perms as make_view_user,
            message_texts as view_message_texts,
            post as post_view,
        )
        from netbox_librenms_plugin.views.imports.actions import SomeActionView

        # Arrange - real rows, a real user, real grants
        device = make_device("r01", serial="SN1")
        user = make_view_user("feature-user", [("change", Device)])
        request = make_view_request("post", {"action": "sync_name"}, user=user)

        # Act - drive the real view with the request bound as dispatch() binds it
        response = post_view(SomeActionView(), request, device_id=device.pk)

        # Assert - on the response and on the persisted state
        assert response.status_code == 200
        assert Device.objects.get(pk=device.pk).name == "expected-name"
        assert view_message_texts(request, "error") == []
```

### Key Testing Conventions

**Use inline imports** inside test methods to avoid Django initialization at module load time:

```python
def test_something(self):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    api = LibreNMSAPI(server_key="default")
```

**Create real NetBox objects** with the `conftest.py` factories instead of mocking models:

```python
device = make_device("test-device", serial="SN1")
ip = make_ip("192.168.1.1/24", assigned_to=make_interface(device, "mgmt0"))
```

**Deny a permission by withholding it**, not by stubbing the gate. Build a user who genuinely lacks
the permission, then assert the response the gate actually returns (302 for a normal POST, 200 with
an `HX-Redirect` header for an HTMX POST) and re-read the object to prove no write landed.

**Drive the LibreNMS API through the loopback stub** rather than mocking the client:

```python
with run_librenms_server() as server:
    configure_test_servers(settings, {key: {"librenms_url": server.url, "api_token": "t"}})
    server.device_info_response(device_id=42, hostname="r01", serial="SN1")
    view._librenms_api = LibreNMSAPI(server_key=key)
```

**Patch at the source module**, not where the function is imported:

```python
# Correct - patch where the function is defined
@patch("netbox_librenms_plugin.import_utils.process_device_filters")

# Incorrect - patching the import location
@patch("netbox_librenms_plugin.views.imports.list.process_device_filters")
```

### Available Fixtures

These fixtures are defined in [conftest.py](../../netbox_librenms_plugin/tests/conftest.py):

- `make_device`, `make_vm`, `make_interface`, `make_ip`, `make_cluster`, `make_superuser`: real rows
- `mock_response_factory` — Factory for creating mock HTTP responses
- `mock_netbox_device`, `mock_netbox_vm` — mock NetBox objects, kept for the unconverted legacy
  tests. Do not use them in new tests.

[view_test_helpers.py](../../netbox_librenms_plugin/tests/view_test_helpers.py) supplies the request
and permission side: `make_request`, `make_user_with_perms`, `grant`, `post`, `get`,
`message_texts`, `assert_locked_before_update`.

`mock_librenms_config` is defined in
[test_librenms_api_helpers.py](../../netbox_librenms_plugin/tests/test_librenms_api_helpers.py).
Bind it at module scope in each test module that needs it. Its `autouse=True` setting
applies only to modules where the fixture is available.

### Common Assertion Patterns

```python
# Methods returning (success, data) tuples
success, data = api.get_device_info(123)
assert success is True
assert data["hostname"] == "expected-hostname"

# Methods returning dicts with error flags
result = api.test_connection()
assert "error" not in result

# Verifying exceptions are raised
with pytest.raises(ValueError, match="Invalid configuration"):
    api.method_that_should_fail()

# Verifying persisted state after a view action
assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == {"default": 42}

# Verifying the messages a view queued
assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
```

Assert on persisted state and on the response, not on recorded mock calls. A `assert_called_once()`
passes whenever the call is made, including when the call did the wrong thing.

## CI/CD Compatibility

The tests do not use production services:

- Database-backed tests require a test database connection. Other tests do not require one.
- No external network access needed (integration tests use local loopback only)
- Fast execution suitable for pre-commit hooks
- Clear failure messages for debugging
- Works in containerized environments

This makes the test suite suitable for GitHub Actions, pre-commit hooks, or any CI pipeline you choose to implement.

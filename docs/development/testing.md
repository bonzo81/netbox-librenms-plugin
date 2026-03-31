# Testing Guide

This guide explains how to run the test suite, write new tests, and debug failures.

## Quick Start

Run all tests with a single command:

```bash
make unittest
```

Or run pytest directly:

```bash
pytest netbox_librenms_plugin/tests/ -v
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
```

## Testing Philosophy

The test suite prioritizes speed and isolation so you can run tests frequently during development:

- **Mock-based**: Unit tests use `MagicMock` instead of real database objects. No Django database setup required.
- **Fast execution**: The full suite runs in approximately 15-20 seconds (varies by environment).
- **Isolated**: Each test is independent with no shared state between tests.
- **No external network access**: Tests never call external services. Integration tests use a local loopback HTTP server (`mock_librenms_server.py`) to exercise the real API client against realistic HTTP responses without requiring a running LibreNMS instance.
- **Coverage exclusions**: Test files themselves are excluded from coverage reports (see `[tool.coverage.run]` omit list in `pyproject.toml`).

This approach means tests work identically in your local development environment, in the devcontainer, and in CI pipelines.

## Writing New Tests

### Basic Test Template

New tests should follow this structure:

```python
from unittest.mock import MagicMock, patch


class TestFeatureName:
    """Tests for [feature description]."""

    pytest_plugins = ["tests.test_librenms_api_helpers"]

    @patch("netbox_librenms_plugin.module_name.external_dependency")
    def test_specific_behavior(self, mock_dependency, mock_librenms_config):
        """Describe what this test verifies."""
        # Arrange - set up test data and mocks
        mock_dependency.return_value = {"expected": "response"}

        # Act - call the code being tested
        from netbox_librenms_plugin.module_name import function_to_test
        result = function_to_test(input_data)

        # Assert - verify the results
        assert result == expected_value
        mock_dependency.assert_called_once_with(expected_args)
```

### Key Testing Conventions

**Use inline imports** inside test methods to avoid Django initialization at module load time:

```python
def test_something(self):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    api = LibreNMSAPI(server_key="default")
```

**Mock NetBox models** with `MagicMock()` instead of creating real database objects:

```python
device = MagicMock()
device.name = "test-device"
device.primary_ip.address.ip = "192.168.1.1"
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

- `mock_librenms_config` — Automatically mocks plugin configuration for all tests
- `mock_response_factory` — Factory for creating mock HTTP responses
- `mock_netbox_device` — Pre-configured mock NetBox Device object
- `mock_netbox_vm` — Pre-configured mock NetBox VM object

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

# Verifying mock calls
mock_get.assert_called_once()
mock_post.assert_called_with(expected_url, headers=expected_headers, json=expected_data)
mock_delete.assert_not_called()
```

## CI/CD Compatibility

The tests run in any environment without external dependencies:

- No database connection required
- No external network access needed (integration tests use local loopback only)
- Fast execution suitable for pre-commit hooks
- Clear failure messages for debugging
- Works in containerized environments

This makes the test suite suitable for GitHub Actions, pre-commit hooks, or any CI pipeline you choose to implement.

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
| [test_mixins.py](../../netbox_librenms_plugin/tests/test_mixins.py) | View mixins—CacheMixin key generation, LibreNMSAPIMixin lazy init, permission checks |
| [test_sync_devices.py](../../netbox_librenms_plugin/tests/test_sync_devices.py) | Device sync views—field updates, platform creation, server mapping, legacy ID conversion |
| [test_sync_interfaces.py](../../netbox_librenms_plugin/tests/test_sync_interfaces.py) | Interface sync—port matching, attribute updates, MAC handling, librenms_id assignment |
| [test_sync_view_mismatch.py](../../netbox_librenms_plugin/tests/test_sync_view_mismatch.py) | Sync page context—device type mismatch detection and badge rendering |
| [test_permissions.py](../../netbox_librenms_plugin/tests/test_permissions.py) | Permission enforcement—mixin contracts, object-level permissions, and write guards |
| [test_integration_sync.py](../../netbox_librenms_plugin/tests/test_integration_sync.py) | Integration tests—API client against local mock HTTP server |
| [test_view_wiring.py](../../netbox_librenms_plugin/tests/test_view_wiring.py) | Smoke tests—view class MRO, mixin wiring, permission contracts, and template syntax |

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
pytest netbox_librenms_plugin/tests/test_librenms_api.py -v

# Import and validation tests
pytest netbox_librenms_plugin/tests/test_import_utils.py netbox_librenms_plugin/tests/test_import_validation_helpers.py netbox_librenms_plugin/tests/test_utils.py -v

# Background job tests
pytest netbox_librenms_plugin/tests/test_background_jobs.py -v

# Multi-server librenms_id tests
pytest netbox_librenms_plugin/tests/test_librenms_id.py -v

# Sync view tests (devices, interfaces)
pytest netbox_librenms_plugin/tests/test_sync_devices.py netbox_librenms_plugin/tests/test_sync_interfaces.py -v

# Integration tests (API client against mock HTTP server)
pytest netbox_librenms_plugin/tests/test_integration_sync.py -v

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
- **Fast execution**: The full suite runs in under 0.5 seconds.
- **Isolated**: Each test is independent with no shared state between tests.
- **No external network access**: Tests never call external services. Integration tests use a local loopback HTTP server (`mock_librenms_server.py`) to exercise the real API client against realistic HTTP responses without requiring a running LibreNMS instance.

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

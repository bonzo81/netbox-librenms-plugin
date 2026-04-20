---
applyTo: "tests/**"
description: Testing patterns and conventions for the NetBox LibreNMS plugin
---

# Testing Patterns

## General Test Conventions
- Use plain **pytest classes**, not Django `TestCase`. Avoid `from django.test import TestCase`.
- **Never use `@pytest.mark.django_db`** for unit tests—mock all database interactions with `MagicMock`.
- Use **inline imports** inside test methods to avoid Django initialization at module load time.
- Mock NetBox models (Device, Job, User) with `MagicMock()` instead of creating real instances.
- Use `assert x == y` syntax, not `self.assertEqual(x, y)` (no TestCase inheritance).
- See [docs/development/testing.md](../../docs/development/testing.md) for test file structure and running instructions.

## Background Job Tests
- Instantiate `JobRunner` subclasses using `object.__new__(JobClass)` to bypass `__init__`, then set `job.job = MagicMock()` and `job.logger = MagicMock()`. See `create_mock_job_runner()` helper in `tests/test_background_jobs.py`.
- Patch deferred/inline imports at their **source** module (e.g., `netbox_librenms_plugin.import_utils.process_device_filters`), not the consuming module.
- Patch `cache` where imported: `netbox_librenms_plugin.views.imports.list.cache`, not `django.core.cache.cache`.
- Test view decision logic by setting `view._filter_form_data = {...}` directly, not via HTTP requests.
- **Never use `RequestFactory`**—mock request objects directly or test method logic in isolation.
- Cache key tests must patch `get_validated_device_cache_key` from `import_utils.py`; never hardcode key formats like `job_123_device_1`.

## Test File Naming
- Follow the `test_{module_name}.py` convention for new test files.
- `test_netbox_librenms_plugin.py` is an empty placeholder — do not add tests there.

## Test Coverage by Module
- `librenms_api.py` → `test_librenms_api.py`, `test_librenms_api_helpers.py`
- `import_utils/` package (`filters.py`, `device_operations.py`, `vm_operations.py`, `cache.py`, `permissions.py`, `virtual_chassis.py`), `import_validation_helpers.py`, `utils.py` → `test_import_utils.py`, `test_import_validation_helpers.py`, `test_utils.py`
- `jobs.py`, `views/imports/list.py` → `test_background_jobs.py`
- Permission mixins, API permissions, constants → `test_permissions.py`
- VLAN API, mode detection, comparison, sync → `test_vlan_sync.py`
- `VlanAssignmentMixin`, VLAN enrichment → `test_interface_vlan_sync.py`
- Views (`views/sync/`, `views/object_sync/`, `views/imports/actions.py`) — no dedicated test files yet. Test business logic via the utility modules they call, not via HTTP requests.

## Permission Test Patterns
When testing permissions (see `test_permissions.py` for reference):
- Create a mock view instance with `object.__new__(ViewClass)`, set `request = MagicMock()` with `request.user.has_perm.side_effect = lambda p: p in allowed_perms`.
- For `NetBoxObjectPermissionMixin` tests, set `required_object_permissions` on the instance before calling `check_object_permissions()`.
- Test both the individual methods (`has_write_permission()`, `check_object_permissions()`) and the combined `require_all_permissions()` flow.
- For JSON variants (`require_all_permissions_json`), assert `isinstance(response, JsonResponse)` and check `response.status_code == 403`.

## Shared Fixtures (`conftest.py`)
Reuse fixtures from `tests/conftest.py` instead of creating ad-hoc mocks:
- **Configuration**: `mock_multi_server_config`, `mock_legacy_config`
- **API client**: `mock_librenms_api`
- **NetBox objects**: `mock_netbox_device`, `mock_netbox_vm`, `mock_netbox_site`, `mock_netbox_platform`, `mock_netbox_device_type`, `mock_netbox_device_role`, `mock_netbox_cluster`, `mock_netbox_rack`
- **HTTP responses**: `mock_response_factory`, `mock_success_response`, `mock_device_response`, `mock_error_response`, `mock_auth_error_response`
- **Import workflow**: `sample_librenms_device`, `sample_librenms_device_minimal`, `sample_validation_state`, `sample_validation_state_vm`

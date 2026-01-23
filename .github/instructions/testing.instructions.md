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

## Test Coverage by Module
- `librenms_api.py` → `test_librenms_api.py`
- `import_utils.py`, `import_validation_helpers.py`, `utils.py` → `test_import_utils.py`, `test_import_validation_helpers.py`, `test_utils.py`
- `jobs.py`, `views/imports/list.py` → `test_background_jobs.py`

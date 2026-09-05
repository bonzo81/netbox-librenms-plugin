---
applyTo: "tests/**"
description: Testing patterns and conventions for the NetBox LibreNMS plugin
---

# Testing Patterns

## What to test against

Rank a test by how much real behaviour it runs. Prefer, in this order:

1. **End to end** — a real request through the real view, the real permission gate, the real ORM,
   and the real response.
2. **Integration against real dependencies** — a real test database, real NetBox models, real forms
   and serializers.
3. **Narrow unit tests** — only for pure functions with no I/O.

Use a mock only for a boundary you cannot run locally:

- the LibreNMS HTTP API (prefer the in-repo stub server, see below),
- an error a local database cannot produce (an `IntegrityError` on save, a lock `DatabaseError`),
- a timing seam that injects one deterministic concurrent write into a real flow.

A `MagicMock` answers any attribute and any method. A test built on mocked models therefore stays
green while the real query path is broken, and it only re-asserts what the test author already
assumed. Do not mock NetBox models, managers, querysets, requests, users or permission checks.

## General Test Conventions

- Use plain **pytest classes**, not Django `TestCase`. Avoid `from django.test import TestCase`.
- Mark database tests with `@pytest.mark.django_db` on the class.
- Use **inline imports** inside test methods to avoid Django initialization at module load time.
- Use `assert x == y` syntax, not `self.assertEqual(x, y)` (no TestCase inheritance).
- See [docs/development/testing.md](../../docs/development/testing.md) for test file structure and running instructions.

## Real objects, real requests, real grants

`tests/conftest.py` builds real rows: `make_device`, `make_vm`, `make_interface`, `make_ip`,
`make_cluster`, `make_superuser`, `make_device_with_module_bays`.

`tests/view_test_helpers.py` builds the request and permission side:

- `make_request(method, data, user=..., **factory_kwargs)` — a real request with a session and
  working message storage. Pass `HTTP_HX_REQUEST="true"` for the HTMX path.
- `make_user_with_perms(username, [("change", Device)], plugin_write=False)` — a real non-superuser.
  NetBox enforces permissions only through `ObjectPermissionBackend`, so a grant must be a real
  `ObjectPermission` row; `grant()` creates one and returns the user with a fresh permission cache.
- `post(view, request, **kwargs)` / `get(...)` — call the view with the request bound the way
  `dispatch()` binds it.
- `message_texts(request, "error")` — read the messages the view actually queued.
- `assert_locked_before_update(captured, table)` — prove a `SELECT ... FOR UPDATE` precedes an update.

To test a permission denial, supply a user who genuinely lacks the permission. Do not assign a
canned response to `require_write_permission`: that asserts a contract the view does not implement
and cannot fail when the gate is deleted.

## The LibreNMS boundary

Drive the real `LibreNMSAPI` against the in-repo loopback HTTP server rather than mocking the client:

```python
with run_librenms_server() as server:
    configure_test_servers(settings, {server_key: {"librenms_url": server.url, ...}})
    server.device_info_response(device_id=42, hostname="r01", serial="SN1")
    view._librenms_api = LibreNMSAPI(server_key=server_key)
```

See `tests/mock_librenms_server.py` for the registrable responses (`device_info_response`,
`ports_response`, `inventory_response`, `vc_inventory_callable`, `auth_error_response`).

## Verifying a test earns its keep

Break the production line the test names and confirm the test turns red. A test that stays green
against broken production code covers a line without verifying it. Parametrized cases are the usual
offenders: check that each case can actually fail.

When a state turns out to be unreachable through the real producers, say so in the test file or
remove the guard, rather than fabricating the state with a mock.

## Background Job Tests

- Instantiate `JobRunner` subclasses using `object.__new__(JobClass)` to bypass `__init__`, then set
  `job.job` and `job.logger`. See `create_mock_job_runner()` in `tests/test_background_jobs.py`.
- Patch deferred/inline imports at their **source** module (e.g.
  `netbox_librenms_plugin.import_utils.process_device_filters`), not the consuming module.
- Patch `cache` where imported: `netbox_librenms_plugin.views.imports.list.cache`, not
  `django.core.cache.cache`.
- Cache key tests must derive the key from `get_validated_device_cache_key` in `import_utils.py`;
  never hardcode a key format like `job_123_device_1`.

## Test File Naming

- Follow the `test_{module_name}.py` convention for new test files.
- `test_netbox_librenms_plugin.py` is an empty placeholder — do not add tests there.

## Test Coverage by Module

The mapping below names each module's **primary** test file, not its only one. Split a focused suite
into its own `test_*.py` when the module's primary file already exists on a lower branch of the PR
stack: appending to that file's tail conflicts on every restack, so a new file is preferred over a
large tail addition. Give the new file a docstring pointing at the primary home.

- `librenms_api.py` → `test_librenms_api.py`, `test_librenms_api_helpers.py`
- `import_utils/` package (`filters.py`, `device_operations.py`, `vm_operations.py`, `cache.py`, `permissions.py`, `virtual_chassis.py`), `import_validation_helpers.py`, `utils.py` → `test_import_utils.py`, `test_import_validation_helpers.py`, `test_utils.py`
- `jobs.py`, `views/imports/list.py` → `test_background_jobs.py`
- `import_utils/bulk_import.py` → `test_coverage_bulk_import.py`
- Utility helpers (`utils.py` coverage tests) → `test_coverage_utils.py`
- Permission mixins, API permissions, constants → `test_permissions.py`
- VLAN API, mode detection, comparison, sync → `test_vlan_sync.py`
- `VlanAssignmentMixin`, VLAN enrichment → `test_interface_vlan_sync.py`
- `views/imports/actions.py` → `test_coverage_actions.py`
- Other views (`views/sync/`, `views/object_sync/`) → drive the view itself through
  `view_test_helpers.post()`; test the utility modules separately where the logic lives there.

## Permission Test Patterns

See `test_permissions.py` for the mixin contract tests. For a view, test the gate through a real
request:

- Build a user without the plugin change permission (`plugin_write=False`) and assert the response
  the gate actually returns: **302** to the script prefix for a normal POST, **200** with an
  `HX-Redirect` header for an HTMX POST (HTMX skips the swap on a non-2xx response). These views do
  not return 403.
- Assert the recorded message, and re-read the object to prove no write landed.
- Constrained grants (`constraints={"pk": device.pk}`) prove `restrict()` scoping: a device outside
  the grant must not be mutated by raw pk.
- `require_all_permissions_json` is the only variant that returns a `JsonResponse` with 403.

## Shared Fixtures (`conftest.py`)

- **Real objects**: `make_device`, `make_vm`, `make_interface`, `make_ip`, `make_cluster`,
  `make_superuser`, `make_serial_device`, `make_virtual_chassis_members`,
  `make_device_with_module_bays`
- **Configuration**: `mock_multi_server_config`, `mock_legacy_config`
- **HTTP boundary**: `mock_response_factory`, `mock_success_response`, `mock_device_response`,
  `mock_error_response`, `mock_auth_error_response`
- **Import workflow**: `sample_librenms_device`, `sample_librenms_device_minimal`,
  `sample_validation_state`, `sample_validation_state_vm`

The `mock_netbox_*` fixtures remain for the unconverted legacy tests. Do not use them in new tests.

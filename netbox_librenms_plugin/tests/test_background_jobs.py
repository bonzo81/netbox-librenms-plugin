"""Integration coverage for import decisions and NetBox background jobs."""

from copy import deepcopy
from uuid import uuid4

import pytest
from django.http import QueryDict

from netbox_librenms_plugin.tests.conftest import make_cluster, make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms


SERVER_KEY = "default"


def _configure_server(settings, server):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "display_name": "Background job test server",
            "librenms_url": server.url,
            "api_token": "test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(settings, monkeypatch):
    """Run a local HTTP server for real LibreNMS client requests."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        _configure_server(settings, server)
        yield server


def _device_payload(device_id, hostname=None, **overrides):
    name = hostname or f"background-{device_id}.example.test"
    payload = {
        "device_id": device_id,
        "hostname": name,
        "sysName": name,
        "hardware": "TestDT",
        "serial": "-",
        "os": "linux",
        "ip": f"198.18.40.{device_id % 250 + 1}",
        "version": "1.0",
        "location": "TestSite",
        "type": "network",
        "status": 1,
        "disabled": 0,
    }
    payload.update(overrides)
    return payload


def _job(user, tag):
    from core.models import Job

    return Job.objects.create(
        name=f"Background integration {tag}",
        user=user,
        job_id=uuid4(),
        data={},
    )


def _import_user(tag, *, devices=True, vms=True):
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    permissions = []
    if devices:
        permissions.extend([("add", Device), ("change", Device)])
    if vms:
        permissions.append(("add", VirtualMachine))
    return make_user_with_perms(f"background-import-{tag}", permissions)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("job_kind", "legacy_kwargs"),
    [
        pytest.param(
            "filter",
            {
                "filters": {},
                "vc_detection_enabled": False,
                "clear_cache": False,
                "show_disabled": False,
            },
            id="filter-devices",
        ),
        pytest.param(
            "import",
            {"device_ids": [], "vm_imports": {}},
            id="import-devices",
        ),
    ],
)
def test_legacy_job_payload_without_server_key_reaches_explicit_validation(job_kind, legacy_kwargs):
    """The real NetBox runner must record the missing server instead of a signature error."""
    from core.choices import JobStatusChoices

    from netbox_librenms_plugin.jobs import FilterDevicesJob, ImportDevicesJob

    job_class = FilterDevicesJob if job_kind == "filter" else ImportDevicesJob
    job = _job(make_superuser(f"background-legacy-{job_kind}-owner"), f"legacy-{job_kind}")

    job_class.handle(job=job, **legacy_kwargs)

    job.refresh_from_db()
    assert job.status == JobStatusChoices.STATUS_ERRORED
    assert "The job does not reference one configured LibreNMS server." in job.error


@pytest.mark.django_db
class TestShouldUseBackgroundJob:
    @staticmethod
    def _view(user, data=None, *, cleaned_data=None):
        from django.test import RequestFactory
        from netbox_librenms_plugin.forms import LibreNMSImportFilterForm
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        request = RequestFactory().get("/", data or {})
        request.user = user
        view = LibreNMSImportView()
        view.setup(request)
        if cleaned_data is None:
            form = LibreNMSImportFilterForm(request.GET, librenms_api=None)
            assert form.is_valid(), form.errors
            cleaned_data = form.cleaned_data
        view._filter_form_data = cleaned_data
        return view

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"use_background_job": "on"}, True),
            ({"use_background_job": ""}, False),
            ({}, True),
        ],
    )
    def test_superuser_decision_comes_from_the_real_bound_form(self, data, expected):
        view = self._view(make_superuser(f"background-choice-{expected}-{len(data)}"), data)

        assert view.should_use_background_job() is expected

    def test_missing_cleaned_field_uses_the_superuser_default(self):
        view = self._view(
            make_superuser("background-missing-cleaned-field"),
            cleaned_data={"some_other_field": "value"},
        )

        assert view.should_use_background_job() is True

    def test_non_superuser_cannot_select_background_mode(self, django_user_model):
        user = django_user_model.objects.create_user(username="background-non-superuser")
        view = self._view(user, {"use_background_job": "on"})

        assert view.should_use_background_job() is False

    def test_querydict_unchecked_checkbox_remains_false(self):
        data = QueryDict("use_background_job=")
        view = self._view(make_superuser("background-querydict-unchecked"), data)

        assert view.should_use_background_job() is False


@pytest.mark.django_db
class TestFilterDevicesJob:
    def test_real_filter_run_persists_only_enabled_unlinked_rows_and_options(self, librenms_server):
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        existing = make_device("background-existing", librenms_cf={SERVER_KEY: 6301})
        visible = _device_payload(
            6303,
            hostname="visible-host.example.test",
            sysName="visible-system.example.test",
            hardware=existing.device_type.model,
            location=existing.site.name,
        )
        librenms_server.register(
            "/api/v0/devices",
            {
                "status": "ok",
                "devices": [
                    _device_payload(
                        6301,
                        hostname=existing.name,
                        hardware=existing.device_type.model,
                        location=existing.site.name,
                    ),
                    _device_payload(
                        6302,
                        hostname="background-disabled",
                        disabled=1,
                        hardware=existing.device_type.model,
                        location=existing.site.name,
                    ),
                    visible,
                ],
            },
        )
        job = _job(make_superuser("background-filter-owner"), "filter-options")

        FilterDevicesJob(job).run(
            filters={"hostname": ""},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=False,
            exclude_existing=True,
            server_key=SERVER_KEY,
            use_sysname=False,
            strip_domain=True,
        )

        job.refresh_from_db()
        assert job.data["device_ids"] == [6303]
        assert job.data["total_processed"] == 1
        assert job.data["filters"] == {"hostname": ""}
        assert job.data["server_key"] == SERVER_KEY
        assert job.data["vc_detection_enabled"] is False
        assert job.data["use_sysname"] is False
        assert job.data["strip_domain"] is True
        assert job.data["cache_timeout"] == 300
        assert job.data["cached_at"].endswith("+00:00")
        assert job.data["completed"] is True

    def test_empty_live_result_persists_a_completed_empty_job(self, librenms_server):
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        librenms_server.register("/api/v0/devices", {"status": "ok", "devices": []})
        job = _job(make_superuser("background-empty-filter-owner"), "empty-filter")

        FilterDevicesJob(job).run(
            filters={},
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=True,
            server_key=SERVER_KEY,
        )

        job.refresh_from_db()
        assert job.data["device_ids"] == []
        assert job.data["total_processed"] == 0
        assert job.data["completed"] is True

    def test_job_rejects_a_server_key_removed_after_enqueue(self, settings, librenms_server):
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config
        job = _job(make_superuser("background-stale-filter-owner"), "stale-filter")

        with pytest.raises(ValueError, match="configured LibreNMS server"):
            FilterDevicesJob(job).run(
                filters={},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=False,
                server_key=SERVER_KEY,
            )

    def test_meta_name_is_stable(self):
        from netbox_librenms_plugin.jobs import FilterDevicesJob

        assert FilterDevicesJob.Meta.name == "LibreNMS Device Filter"


@pytest.mark.django_db
class TestImportDevicesJob:
    def test_mixed_device_and_vm_batch_imports_real_objects_and_persists_ids(self, librenms_server):
        from dcim.models import Device
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        infrastructure = make_device("background-import-infrastructure")
        cluster = make_cluster("background-import-cluster")
        user = _import_user("mixed")
        job = _job(user, "mixed-import")
        rows = {
            6401: _device_payload(
                6401,
                hostname="background-imported-device",
                hardware=infrastructure.device_type.model,
                location=infrastructure.site.name,
            ),
            6402: _device_payload(6402, hostname="background-imported-vm"),
        }

        ImportDevicesJob(job).run(
            device_ids=[6401],
            vm_imports={6402: {"cluster_id": cluster.pk}},
            server_key=SERVER_KEY,
            sync_options={"sync_interfaces": False, "sync_cables": False},
            manual_mappings_per_device={
                6401: {
                    "site_id": infrastructure.site_id,
                    "device_type_id": infrastructure.device_type_id,
                    "device_role_id": infrastructure.role_id,
                }
            },
            libre_devices_cache=rows,
        )

        job.refresh_from_db()
        imported_device = Device.objects.get(name="background-imported-device")
        imported_vm = VirtualMachine.objects.get(name="background-imported-vm")
        assert job.data["imported_device_pks"] == [imported_device.pk]
        assert job.data["imported_vm_pks"] == [imported_vm.pk]
        assert job.data["imported_libre_device_ids"] == [6401]
        assert job.data["imported_libre_vm_ids"] == [6402]
        assert job.data["server_key"] == SERVER_KEY
        assert job.data["total"] == 2
        assert job.data["success_count"] == 2
        assert job.data["failed_count"] == 0
        assert job.data["skipped_count"] == 0
        assert job.data["errors"] == []
        assert job.data["completed"] is True

    def test_unresolved_row_is_skipped_while_a_checked_row_imports(self, librenms_server):
        from dcim.models import Device
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        infrastructure = make_device("background-unresolved-infrastructure")
        user = _import_user("unresolved", vms=False)
        job = _job(user, "unresolved-import")
        librenms_server.register("/api/v0/devices/6403", {"status": "error"}, status=404)
        rows = {
            6404: _device_payload(
                6404,
                hostname="background-checked-device",
                hardware=infrastructure.device_type.model,
                location=infrastructure.site.name,
            )
        }
        mappings = {
            device_id: {
                "site_id": infrastructure.site_id,
                "device_type_id": infrastructure.device_type_id,
                "device_role_id": infrastructure.role_id,
            }
            for device_id in (6403, 6404)
        }

        ImportDevicesJob(job).run(
            device_ids=[6403, 6404],
            vm_imports={},
            server_key=SERVER_KEY,
            manual_mappings_per_device=mappings,
            libre_devices_cache=rows,
        )

        job.refresh_from_db()
        assert Device.objects.filter(name="background-checked-device").exists()
        assert job.data["success_count"] == 1
        assert job.data["failed_count"] == 1
        assert job.data["errors"][0]["device_id"] == 6403
        assert "couldn't be fetched to verify collisions" in job.data["errors"][0]["error"]

    def test_cross_mode_collision_blocks_the_whole_batch(self, librenms_server):
        from dcim.models import Device
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        target = make_device("background-collision-target")
        cluster = make_cluster("background-collision-cluster")
        job = _job(make_superuser("background-collision-owner"), "collision-import")
        rows = {
            6405: _device_payload(6405, hostname=target.name, sysName=target.name),
            6406: _device_payload(6406, hostname=target.name, sysName=target.name),
        }
        device_count = Device.objects.count()
        vm_count = VirtualMachine.objects.count()

        ImportDevicesJob(job).run(
            device_ids=[6405],
            vm_imports={6406: {"cluster_id": cluster.pk}},
            server_key=SERVER_KEY,
            libre_devices_cache=rows,
        )

        job.refresh_from_db()
        assert Device.objects.count() == device_count
        assert VirtualMachine.objects.count() == vm_count
        assert job.data["success_count"] == 0
        assert job.data["failed_count"] == 2
        assert {error["device_id"] for error in job.data["errors"]} == {6405, 6406}
        assert all("Bulk import blocked" in error["error"] for error in job.data["errors"])

    def test_revoked_permissions_block_before_librenms_or_job_data_changes(
        self,
        librenms_server,
        django_user_model,
    ):
        from django.core.exceptions import PermissionDenied
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        user = django_user_model.objects.create_user(username="background-revoked-user")
        job = _job(user, "revoked-import")

        with pytest.raises(PermissionDenied, match="dcim.add_device"):
            ImportDevicesJob(job).run(
                device_ids=[6407],
                vm_imports={},
                server_key=SERVER_KEY,
                libre_devices_cache={6407: _device_payload(6407)},
            )

        job.refresh_from_db()
        assert job.data == {}

    def test_vm_only_permission_is_sufficient_for_a_vm_only_batch(self, librenms_server):
        from virtualization.models import VirtualMachine
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        cluster = make_cluster("background-vm-only-cluster")
        user = _import_user("vm-only", devices=False)
        job = _job(user, "vm-only-import")

        ImportDevicesJob(job).run(
            device_ids=[],
            vm_imports={6408: {"cluster_id": cluster.pk}},
            server_key=SERVER_KEY,
            libre_devices_cache={6408: _device_payload(6408, hostname="background-vm-only")},
        )

        job.refresh_from_db()
        vm = VirtualMachine.objects.get(name="background-vm-only")
        assert job.data["imported_vm_pks"] == [vm.pk]
        assert job.data["success_count"] == 1

    def test_empty_batch_still_records_a_completed_result(self, librenms_server):
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        job = _job(make_superuser("background-empty-import-owner"), "empty-import")

        ImportDevicesJob(job).run(
            device_ids=[],
            vm_imports={},
            server_key=SERVER_KEY,
        )

        job.refresh_from_db()
        assert job.data == {
            "imported_device_pks": [],
            "imported_vm_pks": [],
            "imported_libre_device_ids": [],
            "imported_libre_vm_ids": [],
            "server_key": SERVER_KEY,
            "total": 0,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "virtual_chassis_created": 0,
            "errors": [],
            "completed": True,
        }

    def test_job_rejects_a_server_key_removed_after_enqueue(self, settings, librenms_server):
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {}
        settings.PLUGINS_CONFIG = plugin_config
        job = _job(make_superuser("background-stale-import-owner"), "stale-import")

        with pytest.raises(ValueError, match="configured LibreNMS server"):
            ImportDevicesJob(job).run(
                device_ids=[],
                vm_imports={},
                server_key=SERVER_KEY,
            )

    def test_meta_name_is_stable(self):
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        assert ImportDevicesJob.Meta.name == "LibreNMS Device Import"

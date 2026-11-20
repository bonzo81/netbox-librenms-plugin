"""Integration coverage for import decisions and NetBox background jobs."""

import pickle
from copy import deepcopy
from uuid import uuid4

import pytest
from django.http import QueryDict
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_cluster, make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import grant as grant_view_permission
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms, queued_request

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
    from virtualization.models import Cluster, VirtualMachine

    permissions = []
    if devices:
        permissions.extend([("add", Device), ("change", Device)])
    if vms:
        permissions.extend([("add", VirtualMachine), ("view", Cluster)])
    return make_user_with_perms(f"background-import-{tag}", permissions)


@pytest.mark.django_db
def test_legacy_filter_payload_without_server_key_reaches_explicit_validation():
    """The real NetBox runner must record the missing server instead of a signature error."""
    from core.choices import JobStatusChoices

    from netbox_librenms_plugin.jobs import FilterDevicesJob

    job = _job(make_superuser("background-legacy-filter-owner"), "legacy-filter")

    FilterDevicesJob.handle(
        job=job,
        filters={},
        vc_detection_enabled=False,
        clear_cache=False,
        show_disabled=False,
    )

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


FLUSHED_EVENTS = []


def record_events(events):
    """An ``EVENTS_PIPELINE`` entry that keeps each flushed event."""
    FLUSHED_EVENTS.extend((event["object_type"].model, event["object_id"], event["event_type"]) for event in events)


@pytest.fixture
def event_recorder(settings):
    FLUSHED_EVENTS.clear()
    settings.EVENTS_PIPELINE = [*settings.EVENTS_PIPELINE, f"{__name__}.record_events"]
    yield FLUSHED_EVENTS
    FLUSHED_EVENTS.clear()


class _RecordingQueue:
    """Stands in for the Redis-backed RQ queue: it pickles each call and keeps it for the test to run."""

    def __init__(self):
        self.calls = []

    def enqueue(self, func, *, job_id, **kwargs):
        self.calls.append(pickle.loads(pickle.dumps((func, job_id, kwargs))))


def _post_import(client, librenms_server, tag, device_id, *, background):
    from dcim.models import DeviceRole

    make_device(f"{tag}-infra")  # seeds the shared site, device type and role
    role = DeviceRole.objects.get(slug="test-role")
    name = f"{tag}.example.test"
    librenms_server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [_device_payload(device_id, hostname=name)]},
    )
    user = make_superuser(f"{tag}-importer")
    client.force_login(user)
    data = {"select": [str(device_id)], "server_key": SERVER_KEY, f"role_{device_id}": str(role.pk)}
    if background:
        data["use_background_job"] = "on"
    response = client.post(reverse("plugins:netbox_librenms_plugin:bulk_import_devices"), data)
    assert response.status_code == 302, response.content
    return name, user


def _logged_and_evented_change(name, user, events):
    from core.models import ObjectChange
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType

    device = Device.objects.get(name=name)
    changes = ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(Device), changed_object_id=device.pk
    )
    assert [(change.action, change.user_id) for change in changes] == [("create", user.pk)]
    assert ("device", device.pk, "object_created") in events, events
    return changes.get()


@pytest.mark.django_db
class TestQueuedImportRunsAsTheQueuedRequest:
    """
    An import writes the NetBox change log and event queue on both paths.

    The background path enqueues ``ImportDevicesJob`` through NetBox's real ``Job.enqueue``. A fake RQ
    queue pickles the call as RQ does. The test then runs it outside any request, as an RQ worker does.
    """

    @staticmethod
    def _queue_import(client, librenms_server, monkeypatch, capture_on_commit, tag, device_id):
        import django_rq
        from dcim.models import Device
        from netbox.context import current_request

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        queue, real_get_queue = _RecordingQueue(), django_rq.get_queue
        monkeypatch.setattr("utilities.rqworker.get_workers_for_queue", lambda name: 1)
        monkeypatch.setattr(django_rq, "get_queue", lambda *args, **kwargs: queue)
        with capture_on_commit(execute=True):
            name, user = _post_import(client, librenms_server, tag, device_id, background=True)
        monkeypatch.setattr(django_rq, "get_queue", real_get_queue)  # the job's cancellation check reads the real queue

        # Precondition: the view queued the job and did not import inline.
        assert not Device.objects.filter(name=name).exists()
        assert current_request.get() is None
        [kwargs] = [call[2] for call in queue.calls if call[0] == ImportDevicesJob.handle]
        return name, user, kwargs

    def test_a_synchronous_import_writes_the_change_log(self, client, librenms_server, event_recorder):
        name, user = _post_import(client, librenms_server, "changelog-sync", 6601, background=False)

        change = _logged_and_evented_change(name, user, event_recorder)
        assert change.request_id is not None

    def test_a_background_import_writes_the_change_log_of_the_queued_request(
        self, client, librenms_server, event_recorder, monkeypatch, django_capture_on_commit_callbacks
    ):
        from core.choices import JobStatusChoices
        from core.models import Job
        from netbox.context import current_request

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        name, user, kwargs = self._queue_import(
            client, librenms_server, monkeypatch, django_capture_on_commit_callbacks, "changelog-job", 6602
        )

        ImportDevicesJob.handle(**kwargs)

        job = Job.objects.get(pk=kwargs["job"].pk)
        assert job.status == JobStatusChoices.STATUS_COMPLETED, (job.status, job.error, job.log_entries)
        assert job.data["success_count"] == 1, job.data
        change = _logged_and_evented_change(name, user, event_recorder)
        assert change.user_id == job.user_id
        assert change.request_id == kwargs["request"].id
        assert current_request.get() is None

    @pytest.mark.parametrize(
        ("revoked_field", "error"),
        [
            pytest.param("is_superuser", "dcim.add_device", id="demoted"),
            pytest.param("is_active", "no longer exists or is inactive", id="deactivated"),
        ],
    )
    def test_a_background_import_checks_the_user_as_stored_when_it_runs(
        self, client, librenms_server, monkeypatch, django_capture_on_commit_callbacks, revoked_field, error
    ):
        """The queued payload holds a copy of the user from the enqueue time; the job must not trust it."""
        from core.choices import JobStatusChoices
        from core.models import Job
        from dcim.models import Device
        from netbox.context import current_request

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        name, user, kwargs = self._queue_import(
            client, librenms_server, monkeypatch, django_capture_on_commit_callbacks, f"revoked-{revoked_field}", 6603
        )
        setattr(user, revoked_field, False)
        user.save(update_fields=[revoked_field])

        ImportDevicesJob.handle(**kwargs)

        job = Job.objects.get(pk=kwargs["job"].pk)
        assert job.status == JobStatusChoices.STATUS_ERRORED
        assert "PermissionDenied" in job.error and error in job.error, job.error
        assert not Device.objects.filter(name=name).exists()
        assert current_request.get() is None


@pytest.mark.django_db
class TestImportDevicesJob:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"import_plans": [], "server_key": SERVER_KEY}, id="current-fields"),
            pytest.param({"device_ids": [6411], "vm_imports": {}, "server_key": SERVER_KEY}, id="legacy-fields"),
        ],
    )
    def test_a_payload_queued_before_the_upgrade_fails_without_importing(self, librenms_server, payload):
        """A job queued without the request has no change-log identity, so it must not import."""
        from core.choices import JobStatusChoices
        from dcim.models import Device

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        job = _job(make_superuser("background-no-request-owner"), "no-request-import")
        device_count = Device.objects.count()

        ImportDevicesJob.handle(job=job, **payload)

        job.refresh_from_db()
        assert job.status == JobStatusChoices.STATUS_ERRORED
        assert "Import job payload has no request. Submit the import again." in job.error
        assert job.data == {}
        assert Device.objects.count() == device_count
        assert librenms_server.requests == []

    def test_mixed_device_and_vm_batch_imports_real_objects_and_persists_ids(self, librenms_server):
        from dcim.models import Device, DeviceRole
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        infrastructure = make_device("background-import-infrastructure")
        cluster = make_cluster("background-import-cluster")
        user = _import_user("mixed")
        user = grant_view_permission(user, "view", DeviceRole, constraints={"pk": infrastructure.role_id})
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
        librenms_server.register("/api/v0/devices/6401", {"status": "ok", "devices": [rows[6401]]})

        ImportDevicesJob(job).run(
            request=queued_request(job.user),
            import_plans=[
                {
                    "source_device_id": 6401,
                    "object_type": "device",
                    "role_id": infrastructure.role_id,
                    "rack_id": None,
                },
                {
                    "source_device_id": 6402,
                    "object_type": "virtualmachine",
                    "placement": {"method": "cluster", "cluster_id": cluster.pk},
                    "role_id": None,
                },
            ],
            server_key=SERVER_KEY,
            sync_options={"sync_interfaces": False, "sync_cables": False},
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
        from dcim.models import Device, DeviceRole

        from netbox_librenms_plugin.jobs import ImportDevicesJob

        infrastructure = make_device("background-unresolved-infrastructure")
        user = _import_user("unresolved", vms=False)
        user = grant_view_permission(user, "view", DeviceRole, constraints={"pk": infrastructure.role_id})
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
        librenms_server.register("/api/v0/devices/6404", {"status": "ok", "devices": [rows[6404]]})
        ImportDevicesJob(job).run(
            request=queued_request(job.user),
            import_plans=[
                {
                    "source_device_id": device_id,
                    "object_type": "device",
                    "role_id": infrastructure.role_id,
                    "rack_id": None,
                }
                for device_id in (6403, 6404)
            ],
            server_key=SERVER_KEY,
            libre_devices_cache=rows,
        )

        job.refresh_from_db()
        assert Device.objects.filter(name="background-checked-device").exists()
        assert job.data["success_count"] == 1
        assert job.data["failed_count"] == 1
        assert job.data["errors"][0]["device_id"] == 6403
        assert "couldn't be read to verify them" in job.data["errors"][0]["error"]

    def test_a_lone_row_whose_stack_read_failed_is_skipped_not_imported(self, librenms_server):
        """One row runs the same pre-check as many: an unreadable stack must not import VC-less."""
        from dcim.models import Device, DeviceRole

        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
        from netbox_librenms_plugin.jobs import ImportDevicesJob
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        infrastructure = make_device("background-lone-stack-infrastructure")
        user = _import_user("lone-stack", vms=False)
        user = grant_view_permission(user, "view", DeviceRole, constraints={"pk": infrastructure.role_id})
        job = _job(user, "lone-stack-import")
        rows = {
            6421: _device_payload(
                6421,
                hostname="background-unreadable-stack",
                hardware=infrastructure.device_type.model,
                location=infrastructure.site.name,
            )
        }
        librenms_server.register("/api/v0/devices/6421", {"status": "ok", "devices": [rows[6421]]})
        # A 500 is a failed read, unlike the 404 that means "this device holds no inventory".
        librenms_server.register("/api/v0/inventory/6421", {"status": "error"}, status=500)

        # Precondition: the row really is an unreadable stack candidate. Without this a fixture
        # that served the inventory would import the device and look like the gate simply failing.
        detection = get_virtual_chassis_data(LibreNMSAPI(server_key=SERVER_KEY), 6421)
        assert detection["detection_failed"] is True
        assert detection["is_stack"] is False

        ImportDevicesJob(job).run(
            request=queued_request(job.user),
            import_plans=[
                {
                    "source_device_id": 6421,
                    "object_type": "device",
                    "role_id": infrastructure.role_id,
                    "rack_id": None,
                }
            ],
            server_key=SERVER_KEY,
            libre_devices_cache=rows,
        )

        job.refresh_from_db()
        assert not Device.objects.filter(name="background-unreadable-stack").exists()
        assert job.data["success_count"] == 0
        assert job.data["errors"][0]["device_id"] == 6421
        assert "couldn't be read to verify them" in job.data["errors"][0]["error"]

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
            request=queued_request(job.user),
            import_plans=[
                {
                    "source_device_id": 6405,
                    "object_type": "device",
                    "role_id": None,
                    "rack_id": None,
                },
                {
                    "source_device_id": 6406,
                    "object_type": "virtualmachine",
                    "placement": {"method": "cluster", "cluster_id": cluster.pk},
                    "role_id": None,
                },
            ],
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
                request=queued_request(job.user),
                import_plans=[
                    {
                        "source_device_id": 6407,
                        "object_type": "device",
                        "role_id": None,
                        "rack_id": None,
                    }
                ],
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
            request=queued_request(job.user),
            import_plans=[
                {
                    "source_device_id": 6408,
                    "object_type": "virtualmachine",
                    "placement": {"method": "cluster", "cluster_id": cluster.pk},
                    "role_id": None,
                }
            ],
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
            request=queued_request(job.user),
            import_plans=[],
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
                request=queued_request(job.user),
                import_plans=[],
                server_key=SERVER_KEY,
            )

    def test_meta_name_is_stable(self):
        from netbox_librenms_plugin.jobs import ImportDevicesJob

        assert ImportDevicesJob.Meta.name == "LibreNMS Device Import"

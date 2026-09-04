"""Cancellation, permission, and virtual-chassis behaviour of the bulk import paths.

Companion to ``test_coverage_bulk_import.py`` (the primary home for
``import_utils/bulk_import.py``), split out the way ``test_collisions.py`` and
``test_bulk_import_review_regressions.py`` already are.
"""

import logging
import operator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import delete_keeping_pk, make_device, make_superuser

pytestmark = pytest.mark.django_db


@pytest.fixture
def rq_job():
    """Create real RQ jobs in Redis and clean them up afterwards."""
    from django_rq import get_queue
    from rq.job import Job, JobStatus

    created = []

    def _create(status=JobStatus.QUEUED):
        queue = get_queue("default")
        job = Job.create(operator.add, args=(1, 2), connection=queue.connection, id=str(uuid4()))
        job.set_status(status)
        job.save()
        created.append(job)
        return job

    yield _create

    for job in created:
        job.delete()


class _JobContext:
    """A job context whose real RQ job is stopped after *stop_after* cancellation polls."""

    def __init__(self, rq_job, *, stop_after=0, logger=None):
        self._rq_job = rq_job
        self._stop_after = stop_after
        self._polls = 0
        self.logger = logger

    @property
    def job(self):
        from rq.job import JobStatus

        self._polls += 1
        if self._polls > self._stop_after:
            self._rq_job.set_status(JobStatus.STOPPED)
            self._rq_job.save()
        return SimpleNamespace(job_id=self._rq_job.id)


def _libre_device(device_id, hostname, *, hardware="TestDT", serial="", location="TestSite"):
    return {
        "device_id": device_id,
        "hostname": hostname,
        "sysName": hostname,
        "hardware": hardware,
        "serial": serial,
        "os": "linux",
        "version": "1",
        "features": "-",
        "location": location,
        "type": "network",
        "status": 1,
        "disabled": 0,
        "ip": f"198.18.{device_id % 200}.1",
    }


def _stack_root(index=1):
    """A Cisco StackWise style stack-class root inventory entry."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "stack",
        "entPhysicalSerialNum": "",
        "entPhysicalModelName": "",
        "entPhysicalName": "StackSub-0/0",
        "entPhysicalDescr": "Cisco StackWise",
        "entPhysicalContainedIn": 0,
    }


def _chassis(index, serial, *, model="WS-C3750X", position=1):
    """A chassis-class stack member inventory entry."""
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": "chassis",
        "entPhysicalSerialNum": serial,
        "entPhysicalModelName": model,
        "entPhysicalName": f"Chassis-{index}",
        "entPhysicalDescr": f"Chassis {index}",
        "entPhysicalParentRelPos": position,
    }


def _register_stack(live_librenms, device_id, row, members):
    live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]})
    live_librenms.server.vc_inventory_callable(device_id, [_stack_root(1)], {1: members})


def _released_port():
    """Bind a port, read it, release it: nothing is listening there when this returns."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _dead_cache_settings():
    """The real cache configuration pointed at a released port, so Redis calls raise for real."""
    from copy import deepcopy

    from django.conf import settings as django_settings

    caches = deepcopy(django_settings.CACHES)
    caches["default"]["LOCATION"] = f"redis://127.0.0.1:{_released_port()}/0"
    # Without explicit timeouts a stray listener would hang the suite instead of failing it.
    options = dict(caches["default"].get("OPTIONS") or {})
    options.update({"SOCKET_CONNECT_TIMEOUT": 1, "SOCKET_TIMEOUT": 1})
    caches["default"]["OPTIONS"] = options
    return caches


def _messages_from(caplog, logger_name):
    """Return only the records emitted by *logger_name*, so a re-routed log cannot pass."""
    return [record.getMessage() for record in caplog.records if record.name == logger_name]


_MODULE_LOGGER = "netbox_librenms_plugin.import_utils.bulk_import"


def _prerequisites(tag):
    """Return site/device-type/role ids that let an import succeed, without leaving a device behind."""
    seed = make_device(f"bulk-prereq-{tag}")
    result = {
        "site_id": seed.site_id,
        "device_type_id": seed.device_type_id,
        "device_role_id": seed.role_id,
        "hardware": seed.device_type.model,
        "location": seed.site.name,
    }
    delete_keeping_pk(seed)
    return result


class TestJobCancellationState:
    """Cancellation is read from real RQ state and never fails open."""

    @pytest.mark.parametrize(
        ("status_name", "expected"),
        [("STOPPED", True), ("FAILED", True), ("QUEUED", False), ("STARTED", False)],
    )
    def test_the_rq_status_decides(self, rq_job, status_name, expected):
        from rq.job import JobStatus

        from netbox_librenms_plugin.import_utils.bulk_import import _is_job_cancelled

        job = _JobContext(rq_job(getattr(JobStatus, status_name)), stop_after=99)

        assert _is_job_cancelled(job) is expected

    def test_a_job_context_without_an_rq_id_is_logged_and_not_cancelled(self, caplog):
        from netbox_librenms_plugin.import_utils.bulk_import import _is_job_cancelled

        with caplog.at_level("WARNING", logger=_MODULE_LOGGER):
            cancelled = _is_job_cancelled(SimpleNamespace(job=object()))

        assert cancelled is False
        assert any(
            "Unexpected error checking RQ job cancellation state" in message
            for message in _messages_from(caplog, _MODULE_LOGGER)
        )


class TestCollisionScanCancellation:
    """A cancelled pre-check stops scanning and reports the remainder as unchecked."""

    def test_an_already_stopped_job_scans_nothing(self, live_librenms, rq_job):
        from rq.job import JobStatus

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        job = _JobContext(rq_job(JobStatus.STOPPED), stop_after=99, logger=logging.getLogger("collision-scan"))

        collisions, unresolved = detect_collisions_for_device_ids(
            [201, 202, 203], live_librenms.api, libre_devices_cache={}, job=job
        )

        assert collisions == []
        assert unresolved == [201, 202, 203]
        assert live_librenms.server.requests == []

    def test_a_mid_scan_stop_marks_every_unscanned_id_unresolved(self, live_librenms, rq_job):
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        device_ids = list(range(211, 218))
        for device_id in device_ids:
            live_librenms.server.register(
                f"/api/v0/devices/{device_id}",
                {"status": "ok", "devices": [_libre_device(device_id, f"scan-{device_id}")]},
            )
        job = _JobContext(rq_job(), stop_after=1)

        collisions, unresolved = detect_collisions_for_device_ids(
            device_ids, live_librenms.api, libre_devices_cache={}, job=job
        )

        assert collisions == []
        assert unresolved == device_ids[4:]
        assert len(live_librenms.server.requests) == 4


class TestCollisionScanFetchFailure:
    """A LibreNMS lookup that cannot complete fails the row closed instead of aborting the batch."""

    def test_an_unreachable_server_marks_the_row_unresolved(self, settings):
        """A real client against a closed port: get_device_info reports the failure, it does not raise."""
        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

        configure_librenms_servers(
            settings,
            {
                "default": {
                    "librenms_url": f"http://127.0.0.1:{_released_port()}",
                    "api_token": "t",
                    "cache_timeout": 0,
                    "verify_ssl": False,
                }
            },
        )

        collisions, unresolved = detect_collisions_for_device_ids(
            [301], LibreNMSAPI(server_key="default"), libre_devices_cache={}
        )

        assert collisions == []
        assert unresolved == [301]

    def test_a_cache_backend_failure_mid_lookup_marks_the_row_unresolved(self, live_librenms, caplog):
        """get_device_info writes its result to Redis; a real Redis outage raises out of the client."""
        from django.test import override_settings

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        live_librenms.server.register(
            "/api/v0/devices/302", {"status": "ok", "devices": [_libre_device(302, "cache-down")]}
        )

        with caplog.at_level("WARNING", logger=_MODULE_LOGGER), override_settings(CACHES=_dead_cache_settings()):
            collisions, unresolved = detect_collisions_for_device_ids([302], live_librenms.api, libre_devices_cache={})

        assert collisions == []
        assert unresolved == [302]
        assert any(
            "Collision pre-check couldn't fetch device 302" in message
            for message in _messages_from(caplog, _MODULE_LOGGER)
        )

    def test_the_job_logger_receives_the_same_report(self, live_librenms, rq_job, caplog):
        from django.test import override_settings

        from netbox_librenms_plugin.import_utils.bulk_import import detect_collisions_for_device_ids

        live_librenms.server.register(
            "/api/v0/devices/303", {"status": "ok", "devices": [_libre_device(303, "cache-down-job")]}
        )
        job = _JobContext(rq_job(), stop_after=99, logger=logging.getLogger("collision-scan-job"))

        with caplog.at_level("WARNING", logger="collision-scan-job"), override_settings(CACHES=_dead_cache_settings()):
            _collisions, unresolved = detect_collisions_for_device_ids(
                [303], live_librenms.api, libre_devices_cache={}, job=job
            )

        assert unresolved == [303]
        assert any(
            "Collision pre-check couldn't fetch device 303" in message
            for message in _messages_from(caplog, "collision-scan-job")
        )
        assert not _messages_from(caplog, _MODULE_LOGGER)


class TestBulkImportCancellation:
    """A stopped import job reports cancellation and writes nothing."""

    def _run(self, job, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

        row = _libre_device(311, "cancelled-import")
        live_librenms.server.register("/api/v0/devices/311", {"status": "ok", "devices": [row]})
        return bulk_import_devices_shared(
            [311],
            server_key="default",
            libre_devices_cache={311: row},
            job=job,
            user=make_superuser("bulk-cancel-user"),
        )

    def test_the_module_logger_reports_a_job_without_its_own_logger(self, live_librenms, rq_job, caplog):
        from dcim.models import Device
        from rq.job import JobStatus

        job = _JobContext(rq_job(JobStatus.STOPPED), stop_after=99)

        with caplog.at_level("WARNING", logger=_MODULE_LOGGER):
            result = self._run(job, live_librenms)

        assert result["cancelled"] is True
        assert result["success"] == []
        assert "Import cancelled at device 1 of 1" in _messages_from(caplog, _MODULE_LOGGER)
        assert not Device.objects.filter(name="cancelled-import").exists()

    def test_a_job_logger_reports_the_stop_itself(self, live_librenms, rq_job, caplog):
        from rq.job import JobStatus

        job = _JobContext(rq_job(JobStatus.STOPPED), stop_after=99, logger=logging.getLogger("bulk-import-job"))

        with caplog.at_level("WARNING", logger="bulk-import-job"):
            result = self._run(job, live_librenms)

        assert result["cancelled"] is True
        assert "Import job stopped at device 1 of 1" in _messages_from(caplog, "bulk-import-job")
        assert not _messages_from(caplog, _MODULE_LOGGER)


class TestBulkImportIdentityAndMappings:
    """A live row that contradicts the requested id is neither imported nor cached."""

    def test_a_mismatched_live_row_fails_and_never_enters_the_shared_cache(self, live_librenms):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices

        live_librenms.server.register(
            "/api/v0/devices/321",
            {"status": "ok", "devices": [_libre_device(322, "wrong-identity")]},
        )
        shared_cache = {}

        result = bulk_import_devices(
            [321],
            server_key="default",
            libre_devices_cache=shared_cache,
            user=make_superuser("bulk-identity-user"),
        )

        assert result["success"] == []
        assert result["failed"] == [{"device_id": 321, "error": "Failed to retrieve device 321 from LibreNMS"}]
        assert shared_cache == {}
        assert not Device.objects.filter(name="wrong-identity").exists()

    def test_a_resolved_platform_is_applied_to_the_imported_device(self, live_librenms):
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices
        from netbox_librenms_plugin.models import PlatformMapping

        prerequisites = _prerequisites("platform")
        platform = Platform.objects.create(name="Linux Platform", slug="linux-platform")
        PlatformMapping.objects.create(librenms_os="linux", netbox_platform=platform)
        row = _libre_device(
            331,
            "platform-mapped",
            hardware=prerequisites["hardware"],
            location=prerequisites["location"],
        )
        live_librenms.server.register("/api/v0/devices/331", {"status": "ok", "devices": [row]})

        result = bulk_import_devices(
            [331],
            server_key="default",
            manual_mappings_per_device={
                331: {
                    "site_id": prerequisites["site_id"],
                    "device_type_id": prerequisites["device_type_id"],
                    "device_role_id": prerequisites["device_role_id"],
                }
            },
            libre_devices_cache={331: row},
            user=make_superuser("bulk-platform-user"),
        )

        assert result["failed"] == []
        assert Device.objects.get(name="platform-mapped").platform == platform


class TestProcessDeviceFiltersJobControl:
    """The filter job stops between phases and leaves the cache index usable."""

    def test_a_job_stopped_before_fetching_makes_no_librenms_call(self, live_librenms, rq_job):
        from rq.job import JobStatus

        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        job = _JobContext(rq_job(JobStatus.STOPPED), stop_after=99, logger=logging.getLogger("filter-job-early"))

        result = process_device_filters(
            live_librenms.api,
            {},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
            job=job,
            return_cache_status=True,
        )

        assert result == ([], False)
        assert live_librenms.server.requests == []

    def test_a_job_stopped_during_validation_returns_nothing(self, live_librenms, rq_job):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        live_librenms.server.register(
            "/api/v0/devices",
            {"status": "ok", "devices": [_libre_device(341, "filter-cancelled")]},
        )
        # Polls happen before the fetch, before VC pre-fetch, before validation, then per device.
        job = _JobContext(rq_job(), stop_after=3, logger=logging.getLogger("filter-job-loop"))

        result = process_device_filters(
            live_librenms.api,
            {},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
            job=job,
        )

        assert result == []

    def test_a_corrupt_cache_index_is_discarded_rather_than_crashing(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.import_utils.cache import get_cache_index_key

        index_key = get_cache_index_key("default")
        cache.set(index_key, "corrupt-not-a-list", 300)
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": []})

        rows = process_device_filters(
            live_librenms.api,
            {"hostname": "no-such-device"},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
        )

        assert rows == []
        assert cache.get(index_key) is None

    def test_a_cache_hit_re_checks_the_row_and_excludes_a_now_existing_device(self, live_librenms):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        live_librenms.api.cache_timeout = 300
        row = _libre_device(351, "cache-hit-excluded")
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})
        filters = {"hostname": "cache-hit-excluded"}

        first = process_device_filters(
            live_librenms.api,
            filters,
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=True,
        )
        assert [item["device_id"] for item in first] == [351]

        imported = make_device("cache-hit-excluded")
        imported.custom_field_data["librenms_id"] = {"default": 351}
        imported.save(update_fields=["custom_field_data"])

        second = process_device_filters(
            live_librenms.api,
            filters,
            vc_detection_enabled=False,
            clear_cache=False,
            show_disabled=True,
            exclude_existing=True,
        )

        assert second == []


class TestStackImportPermission:
    """A stack row is refused before import when the user cannot create a virtual chassis."""

    def _import_without_vc_permission(self, live_librenms, device_id, job=None):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        prerequisites = _prerequisites(f"stackperm-{device_id}")
        row = _libre_device(
            device_id,
            f"stack-noperm-{device_id}",
            hardware=prerequisites["hardware"],
            location=prerequisites["location"],
        )
        _register_stack(
            live_librenms,
            device_id,
            row,
            [_chassis(100, "SN-NP-A", position=1), _chassis(200, "SN-NP-B", position=2)],
        )
        user = make_user_with_perms(
            f"stack-noperm-user-{device_id}",
            [("add", Device), ("change", Device)],
            plugin_write=False,
        )

        return bulk_import_devices_shared(
            [device_id],
            server_key="default",
            manual_mappings_per_device={device_id: prerequisites},
            libre_devices_cache={device_id: row},
            job=job,
            user=user,
        )

    def test_the_row_fails_and_no_device_is_created(self, live_librenms, caplog):
        from dcim.models import Device

        with caplog.at_level("ERROR", logger=_MODULE_LOGGER):
            result = self._import_without_vc_permission(live_librenms, 401)

        assert result["success"] == []
        assert result["failed"] == [
            {
                "device_id": 401,
                "error": "Cannot import stack device 401: missing permission dcim.add_virtualchassis",
            }
        ]
        assert any(
            "missing permission dcim.add_virtualchassis" in message
            for message in _messages_from(caplog, _MODULE_LOGGER)
        )
        assert not Device.objects.filter(name="stack-noperm-401").exists()

    def test_a_job_logger_receives_the_refusal(self, live_librenms, rq_job, caplog):
        job = _JobContext(rq_job(), stop_after=99, logger=logging.getLogger("stack-perm-job"))

        with caplog.at_level("ERROR", logger="stack-perm-job"):
            result = self._import_without_vc_permission(live_librenms, 402, job=job)

        assert result["failed"][0]["device_id"] == 402
        assert any(
            "missing permission dcim.add_virtualchassis" in message
            for message in _messages_from(caplog, "stack-perm-job")
        )
        assert not _messages_from(caplog, _MODULE_LOGGER)


class TestStackImportCreatesTheVirtualChassis:
    """A stack row imports the master and materialises the chassis once per physical stack."""

    def _import(self, live_librenms, members_by_device, *, tag, job=None, user=None):
        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

        prerequisites = _prerequisites(tag)
        mappings = {}
        cache_rows = {}
        for device_id, members in members_by_device.items():
            row = _libre_device(
                device_id,
                f"{tag}-{device_id}",
                hardware=prerequisites["hardware"],
                location=prerequisites["location"],
            )
            _register_stack(live_librenms, device_id, row, members)
            mappings[device_id] = prerequisites
            cache_rows[device_id] = row
        return bulk_import_devices_shared(
            list(members_by_device),
            server_key="default",
            manual_mappings_per_device=mappings,
            libre_devices_cache=cache_rows,
            job=job,
            user=user or make_superuser(f"{tag}-user"),
        )

    def test_member_serials_key_the_chassis_and_the_members_are_created(self, live_librenms, caplog):
        from dcim.models import Device, VirtualChassis

        members = [_chassis(100, "SN-VC-A", position=1), _chassis(200, "SN-VC-B", position=2)]

        with caplog.at_level("INFO", logger=_MODULE_LOGGER):
            result = self._import(live_librenms, {411: members}, tag="stack-ok")

        assert result["failed"] == []
        assert result["virtual_chassis_created"] == 1
        assert any("Created VC" in message for message in _messages_from(caplog, _MODULE_LOGGER))
        chassis = VirtualChassis.objects.get(domain="librenms-default-411")
        members_in_netbox = Device.objects.filter(virtual_chassis=chassis)
        # The imported master keeps its blank serial; both detected members are created beside it.
        assert set(members_in_netbox.values_list("serial", flat=True)) == {"", "SN-VC-A", "SN-VC-B"}

    def test_one_serial_set_under_two_different_member_labels_creates_one_chassis(self, live_librenms):
        """The dedup key is the member SERIAL set, not the member names, models or positions."""
        from dcim.models import VirtualChassis

        first_labels = [_chassis(100, "SN-DEDUP-A", position=1), _chassis(200, "SN-DEDUP-B", position=2)]
        # Same physical stack seen through a second LibreNMS device: same serials, everything
        # else different. Keying on name/model/position would make this a second chassis.
        second_labels = [
            _chassis(300, "SN-DEDUP-B", model="WS-C3850", position=1),
            _chassis(400, "SN-DEDUP-A", model="WS-C3850", position=2),
        ]

        result = self._import(live_librenms, {421: first_labels, 422: second_labels}, tag="stack-dedup")

        assert len(result["success"]) == 2
        assert result["virtual_chassis_created"] == 1
        assert VirtualChassis.objects.filter(domain__startswith="librenms-default-42").count() == 1

    def test_members_without_serials_are_keyed_by_a_name_model_fingerprint(self, live_librenms):
        """With no serials the key falls back to a member name/model/position fingerprint."""
        from dcim.models import VirtualChassis

        members = [_chassis(100, "-", position=1), _chassis(200, "", position=2)]

        result = self._import(live_librenms, {431: members, 432: list(members)}, tag="stack-fingerprint")

        assert len(result["success"]) == 2
        assert result["virtual_chassis_created"] == 1
        assert VirtualChassis.objects.filter(domain__startswith="librenms-default-43").count() == 1

    def test_the_job_logger_reports_the_created_chassis(self, live_librenms, rq_job, caplog):
        job = _JobContext(rq_job(), stop_after=99, logger=logging.getLogger("stack-create-job"))
        members = [_chassis(100, "SN-JOB-A", position=1), _chassis(200, "SN-JOB-B", position=2)]

        with caplog.at_level("INFO", logger="stack-create-job"):
            result = self._import(live_librenms, {451: members}, tag="stack-joblog", job=job)

        assert result["virtual_chassis_created"] == 1
        assert any("Created VC" in message for message in _messages_from(caplog, "stack-create-job"))
        assert not any("Created VC" in message for message in _messages_from(caplog, _MODULE_LOGGER))


class TestVirtualChassisPrefetch:
    """Filtering with VC detection warms the chassis cache before any row is validated."""

    def _register(self, live_librenms, device_id):
        row = _libre_device(device_id, f"prefetch-{device_id}")
        live_librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})
        _register_stack(
            live_librenms,
            device_id,
            row,
            [_chassis(100, f"SN-PF-{device_id}-A", position=1), _chassis(200, f"SN-PF-{device_id}-B", position=2)],
        )
        return row

    def test_the_chassis_cache_is_warm_before_validation_starts(self, live_librenms, rq_job, caplog):
        """Stopping at the pre-validation poll leaves the cache as the only proof the prefetch ran."""
        from django.core.cache import cache

        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters
        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        # get_virtual_chassis_data bypasses the cache entirely when the timeout is 0.
        live_librenms.api.cache_timeout = 300
        row = self._register(live_librenms, 451)
        cache.delete(_vc_cache_key(live_librenms.api, 451))
        # Polls run before the fetch, before the prefetch, then before validation: stop on the third.
        job = _JobContext(rq_job(), stop_after=2, logger=logging.getLogger("vc-prefetch-job"))

        with caplog.at_level("INFO", logger="vc-prefetch-job"):
            rows = process_device_filters(
                live_librenms.api,
                {"hostname": row["hostname"]},
                vc_detection_enabled=True,
                clear_cache=True,
                show_disabled=True,
                job=job,
            )

        # Validation never ran, so nothing but the prefetch can have populated this key.
        assert rows == []
        cached = cache.get(_vc_cache_key(live_librenms.api, 451))
        assert cached is not None
        assert cached["is_stack"] is True
        assert cached["member_count"] == 2
        job_messages = _messages_from(caplog, "vc-prefetch-job")
        # Pin WHICH phase stopped: if the poll sequence changes, this fails loudly instead of
        # quietly cancelling somewhere else and asserting a cache the prefetch never wrote.
        assert "Job was already stopped before validation started" in job_messages
        assert any("Pre-fetching virtual chassis data for 1 devices" in m for m in job_messages)
        assert any("Virtual chassis data pre-fetch completed" in m for m in job_messages)

    def test_without_a_job_the_module_logger_announces_the_prefetch(self, live_librenms, caplog):
        """The no-job branch has only its log line; pin which logger emits it."""
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        live_librenms.api.cache_timeout = 300
        row = self._register(live_librenms, 452)

        with caplog.at_level("INFO", logger=_MODULE_LOGGER):
            rows = process_device_filters(
                live_librenms.api,
                {"hostname": row["hostname"]},
                vc_detection_enabled=True,
                clear_cache=True,
                show_disabled=True,
            )

        assert rows[0]["_validation"]["virtual_chassis"]["is_stack"] is True
        assert any(
            "Pre-fetching VC data for 1 devices" in message for message in _messages_from(caplog, _MODULE_LOGGER)
        )


class TestFailedImportReporting:
    """An import the validator cannot complete is reported as failed, not silently dropped."""

    def test_an_unresolvable_row_is_reported_to_the_job_logger(self, live_librenms, rq_job, caplog):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

        row = _libre_device(461, "unresolvable-import", hardware="NoSuchHardware", location="NoSuchSite")
        live_librenms.server.register("/api/v0/devices/461", {"status": "ok", "devices": [row]})
        live_librenms.server.inventory_response(461, [])
        job = _JobContext(rq_job(), stop_after=99, logger=logging.getLogger("failed-import-job"))

        with caplog.at_level("ERROR", logger="failed-import-job"):
            result = bulk_import_devices_shared(
                [461],
                server_key="default",
                libre_devices_cache={461: row},
                job=job,
                user=make_superuser("failed-import-user"),
            )

        assert result["success"] == []
        assert result["failed"][0]["device_id"] == 461
        assert any("Failed to import device 461" in message for message in _messages_from(caplog, "failed-import-job"))
        assert not _messages_from(caplog, _MODULE_LOGGER)
        assert not Device.objects.filter(name="unresolvable-import").exists()

"""A warm import search validates every row as a fresh search does; only LibreNMS data comes from the cache."""

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import (
    _shared_infra,
    ip_on,
    make_device,
    make_interface,
    make_ip,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.test_bulk_import_job_control import _chassis, _libre_device

pytestmark = pytest.mark.django_db


@pytest.fixture
def librenms(configure_librenms, monkeypatch):
    """A loopback LibreNMS whose answers the import search caches for 300 seconds."""
    from types import SimpleNamespace

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.mock_librenms_server import MockLibreNMSServer

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = MockLibreNMSServer().start()
    configure_librenms(
        {"default": {"librenms_url": server.url, "api_token": "test-token", "cache_timeout": 300, "verify_ssl": False}}
    )
    try:
        yield SimpleNamespace(server=server, api=LibreNMSAPI(server_key="default"))
    finally:
        server.stop()


def _row(device_id, name, **overrides):
    """One LibreNMS row on the shared Site and DeviceType, so a new row has every prerequisite but a role."""
    _shared_infra()
    return {**_libre_device(device_id, name), **overrides}


def _serve(librenms, *rows):
    """LibreNMS reports *rows*; none of them is a stack."""
    for row in rows:
        librenms.server.register(f"/api/v0/devices/{row['device_id']}", {"status": "ok", "devices": [row]})
        librenms.server.vc_inventory_callable(row["device_id"], [], {})
    librenms.server.register("/api/v0/devices", {"status": "ok", "devices": list(rows)})


def _librenms_now_reports(librenms, *rows):
    """LibreNMS changes its rows; the cached device list expires, and the per-device caches stay warm."""
    from netbox_librenms_plugin.import_utils.cache import get_import_search_cache_key

    _serve(librenms, *rows)
    assert cache.get(get_import_search_cache_key("default", {}, {})) is not None
    cache.delete(get_import_search_cache_key("default", {}, {}))


def _search(librenms, *, clear_cache=False, exclude_existing=False):
    """Run the import search; return ``{device_id: validation}`` of the rows it shows."""
    from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

    rows = process_device_filters(
        librenms.api,
        {},
        vc_detection_enabled=True,
        clear_cache=clear_cache,
        show_disabled=True,
        exclude_existing=exclude_existing,
    )
    return {row["device_id"]: row["_validation"] for row in rows}


def _fresh(librenms, row):
    """The validation of *row* by a search that reads everything again."""
    return _search(librenms, clear_cache=True)[row["device_id"]]


class TestNetBoxChanges:
    """A NetBox change between two searches shows in the second, as in a fresh search."""

    def test_a_renamed_device_loses_its_name_match_to_the_device_that_takes_the_name(self, librenms):
        from dcim.models import Device

        first = make_device("warm-renamed-match")
        first.oob_ip = make_ip("198.18.34.8/32", assigned_object=make_interface(first, "mgmt"))
        first.save()
        row = _row(5508, first.name, serial="-", ip="198.18.34.8")
        _serve(librenms, row)
        cold = _search(librenms, clear_cache=True)[5508]
        assert (cold["existing_device"], cold["serial_action"]) == (first, "oob_candidate")
        Device.objects.filter(pk=first.pk).update(name="warm-renamed-match-old")
        second = make_device("warm-renamed-match")

        warm = _search(librenms)[5508]

        assert warm["existing_device"] == second
        assert warm["oob_candidate"] is None or warm["oob_candidate"]["device"] != first

    def test_an_address_that_moves_to_another_device_moves_the_ip_match(self, librenms):
        first = make_device("warm-moved-ip-a")
        address = ip_on(first, "198.18.34.9/24", "idrac0")
        second = make_device("warm-moved-ip-b")
        row = _row(5509, "warm-moved-ip-bmc", os="idrac", serial="-", ip="198.18.34.9")
        _serve(librenms, row)
        cold = _search(librenms, clear_cache=True)[5509]
        assert (cold["existing_device"], cold["existing_match_type"]) == (first, "primary_ip")
        address.assigned_object = make_interface(second, "idrac0")
        address.save()

        warm = _search(librenms)[5509]

        assert warm["existing_device"] == second
        assert warm["oob_candidate"]["device"] == second

    def test_a_device_renamed_alone_leaves_a_new_row_that_exclude_existing_shows(self, librenms):
        from dcim.models import Device

        host = make_device("warm-sole-renamed")
        row = _row(5518, host.name, serial="-", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5518]["existing_device"] == host
        Device.objects.filter(pk=host.pk).update(name="warm-sole-renamed-old")

        warm = _search(librenms, exclude_existing=True)

        assert (warm[5518]["existing_device"], warm[5518]["existing_match_type"]) == (None, None)

    def test_a_vm_that_takes_the_device_name_leaves_the_row_unbound(self, librenms):
        host = make_device("warm-name-shared-with-vm")
        row = _row(5517, host.name, os="idrac", serial="-", ip="")
        _serve(librenms, row)
        cold = _search(librenms, clear_cache=True)[5517]
        assert (cold["existing_device"], cold["serial_action"]) == (host, "oob_candidate")
        make_vm(host.name)

        warm = _search(librenms)[5517]

        assert warm["existing_device"] is None
        assert warm["oob_candidate"] is None

    def test_a_nameless_row_matches_the_device_named_by_its_id(self, librenms):
        row = _row(5523, "", sysName="", serial="-", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5523]["existing_device"] is None
        device = make_device("device-5523")

        assert _search(librenms)[5523]["existing_device"] == device

    def test_a_new_name_match_wins_over_a_still_valid_ip_match(self, librenms):
        first = make_device("warm-ip-first")
        ip_on(first, "198.18.36.4/24", "eth1")
        row = _row(5524, "warm-ip-first-row", serial="-", ip="198.18.36.4")
        _serve(librenms, row)
        cold = _search(librenms, clear_cache=True)[5524]
        assert (cold["existing_device"], cold["existing_match_type"]) == (first, "primary_ip")
        named = make_device("warm-ip-first-row")

        warm = _search(librenms)[5524]

        assert (warm["existing_device"], warm["existing_match_type"]) == (named, "hostname")

    def test_a_serial_match_gains_the_role_offer_when_its_host_gains_a_link(self, librenms):
        host = make_device("warm-gains-link-host", serial="A210-WARM")
        row = _row(5510, "warm-gains-link-row", serial="A210-WARM", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5510]["oob_candidate"] is None
        seed_mapping(host, "default", own=9210)

        warm = _search(librenms)[5510]

        assert warm["oob_candidate"]["device"] == host

    def test_an_imported_device_shows_as_the_match_and_a_deleted_one_leaves_a_new_row(self, librenms):
        row = _row(5525, "warm-imported-row", serial="-", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5525]["existing_device"] is None
        imported = make_device("warm-imported-other-name", librenms_cf={"default": {"id": 5525}})
        matched = _search(librenms)[5525]
        assert (matched["existing_device"], matched["existing_match_type"]) == (imported, "librenms_id")
        imported.delete()

        new = _search(librenms)[5525]

        assert new["existing_device"] is None
        assert "Device role must be manually selected before import" in new["issues"]

    def test_a_row_that_a_database_error_stopped_is_validated_in_full_on_the_next_search(self, librenms):
        from django.db import OperationalError, connection

        from netbox_librenms_plugin.import_utils.device_operations import VALIDATION_ERROR_ISSUE_PREFIX

        row = _row(5526, "warm-timeout-row", serial="WARM-TIMEOUT-SN", ip="")
        _serve(librenms, row)

        def time_out_site_reads(execute, sql, params, many, context):
            if '"dcim_site"' in sql:
                raise OperationalError("canceling statement due to statement timeout")
            return execute(sql, params, many, context)

        # Error injection: the database times out during the first search only.
        with connection.execute_wrapper(time_out_site_reads):
            partial = _search(librenms, clear_cache=True)[5526]
        assert any(str(issue).startswith(VALIDATION_ERROR_ISSUE_PREFIX) for issue in partial["issues"])

        warm = _search(librenms)[5526]

        assert not any(str(issue).startswith(VALIDATION_ERROR_ISSUE_PREFIX) for issue in warm["issues"])
        assert warm["site"]["found"] is True
        assert warm["device_type"]["found"] is True


class TestLibreNMSChanges:
    """A LibreNMS change that the next device list reports shows like a fresh search."""

    def test_an_unmatched_row_renamed_in_librenms_does_not_bind_its_old_name(self, librenms):
        row = _row(5519, "warm-old-row-name", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5519]["existing_device"] is None
        make_device("warm-old-row-name")
        _librenms_now_reports(librenms, {**row, "hostname": "warm-new-row-name", "sysName": "warm-new-row-name"})

        warm = _search(librenms, exclude_existing=True)

        assert warm[5519]["existing_device"] is None

    def test_a_changed_sysname_follows_the_device_that_the_matcher_prefers(self, librenms):
        first = make_device("warm-preferred-a")
        second = make_device("warm-preferred-b")
        row = _row(5521, first.name, serial="-", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5521]["existing_device"] == first
        _librenms_now_reports(librenms, {**row, "sysName": second.name})

        assert _search(librenms)[5521]["existing_device"] == second

    def test_a_cleared_address_drops_the_ip_match(self, librenms):
        from dcim.models import Device

        host = make_device("warm-cleared-ip-host")
        Device.objects.filter(pk=host.pk).update(primary_ip4=ip_on(host, "198.18.35.1/24", "eth0"))
        ip_on(host, "198.18.35.22/24", "idrac0")
        row = _row(5522, "warm-cleared-ip-bmc", os="idrac", serial="-", ip="198.18.35.22")
        _serve(librenms, row)
        cold = _search(librenms, clear_cache=True)[5522]
        assert (cold["existing_device"], cold["existing_match_type"]) == (host, "primary_ip")
        _librenms_now_reports(librenms, {**row, "ip": ""})

        warm = _search(librenms, exclude_existing=True)

        assert warm[5522]["existing_device"] is None

    def test_a_matched_row_that_loses_both_names_matches_by_its_id_name(self, librenms):
        host = make_device("warm-lost-names-host")
        row = _row(5527, host.name, serial="-", ip="")
        _serve(librenms, row)
        assert _search(librenms, clear_cache=True)[5527]["existing_device"] == host
        by_id = make_device("device-5527")
        _librenms_now_reports(librenms, {**row, "hostname": "", "sysName": ""})

        assert _search(librenms)[5527]["existing_device"] == by_id

    @pytest.mark.parametrize(("before", "after"), [("WARM-GOOD-SN", "S" * 51), ("S" * 51, "WARM-FIXED-SN")])
    def test_the_serial_refusal_follows_the_serial_that_librenms_reports(self, librenms, before, after):
        from dcim.models import Device

        from netbox_librenms_plugin.serial_identity import serial_refusal

        refusal = serial_refusal("S" * 51, Device)
        row = _row(5528, "warm-serial-refusal", serial=before, ip="")
        _serve(librenms, row)
        assert (refusal in _search(librenms, clear_cache=True)[5528]["issues"]) is (before == "S" * 51)
        _librenms_now_reports(librenms, {**row, "serial": after})

        warm = _search(librenms)[5528]

        assert (refusal in warm["issues"]) is (after == "S" * 51)

    @pytest.mark.parametrize(
        ("standalone", "master"), [("SN-WARM-ROOT", "SN-WARM-MASTER"), (True, "True")], ids=["serial", "boolean"]
    )
    def test_a_stack_row_reads_the_member_serials_when_its_serial_starts_naming_the_master(
        self, librenms, standalone, master
    ):
        """JSON true is no serial, but the text "True" names the master, as a changed serial does."""
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.cache import get_import_search_cache_key
        from netbox_librenms_plugin.serial_identity import serial_refusal
        from netbox_librenms_plugin.tests.test_bulk_import_job_control import _register_stack

        members = [_chassis(100, str(master), position=1), _chassis(200, "M" * 51, position=2)]
        row = _row(5529, "warm-stack-serial", serial=standalone, ip="")
        _register_stack(librenms, 5529, row, members)
        librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [row]})
        refusal = serial_refusal("M" * 51, Device)
        # No member carries the device serial, so the row imports standalone and stores no member serial.
        assert not any(refusal in issue for issue in _search(librenms, clear_cache=True)[5529]["issues"])
        # LibreNMS now reports the master's serial, so the import creates the chassis and refuses the long member serial.
        served = {**row, "serial": master}
        librenms.server.register("/api/v0/devices/5529", {"status": "ok", "devices": [served]})
        librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [served]})
        cache.delete(get_import_search_cache_key("default", {}, {}))

        assert any(refusal in issue for issue in _search(librenms)[5529]["issues"])

    def test_a_warm_search_stores_the_current_raw_row_for_the_dialog(self, librenms):
        """The device list is fetched again while the per-device entries stay warm; the row was renamed."""
        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key

        make_device("warm-raw-row-a")
        renamed_target = make_device("warm-raw-row-b")
        row = _row(361, "warm-raw-row-a")
        _serve(librenms, row)
        _search(librenms, clear_cache=True)
        _librenms_now_reports(librenms, {**row, "hostname": "warm-raw-row-b", "sysName": "warm-raw-row-b"})

        warm = _search(librenms)

        assert warm[361]["existing_device"] == renamed_target
        assert cache.get(get_import_device_cache_key(361, "default"))["hostname"] == "warm-raw-row-b"


class TestCachedLibreNMSInputs:
    """A warm search reads the device list, the stack detection and the chassis inventory from the cache."""

    def _rows(self, librenms):
        from dcim.models import DeviceType

        from netbox_librenms_plugin.tests.test_bulk_import_job_control import _register_stack

        _site, manufacturer, _type, _role = _shared_infra()
        chassis_type = DeviceType.objects.create(manufacturer=manufacturer, model="WARM-CHASSIS", slug="warm-chassis")
        stack = _row(5530, "warm-cached-stack", serial="SN-CACHED-1", ip="")
        _register_stack(librenms, 5530, stack, [_chassis(100, "SN-CACHED-1"), _chassis(200, "SN-CACHED-2", position=2)])
        unmatched = _row(5531, "warm-cached-chassis", hardware="NO-SUCH-HARDWARE", ip="")
        librenms.server.register("/api/v0/devices/5531", {"status": "ok", "devices": [unmatched]})
        librenms.server.vc_inventory_callable(
            5531, [{"entPhysicalIndex": 1, "entPhysicalClass": "chassis", "entPhysicalName": "WARM-CHASSIS"}], {}
        )
        librenms.server.register("/api/v0/devices", {"status": "ok", "devices": [stack, unmatched]})
        return chassis_type

    def test_a_warm_search_sends_no_librenms_request(self, librenms):
        chassis_type = self._rows(librenms)
        cold = _search(librenms, clear_cache=True)
        assert cold[5530]["virtual_chassis"]["is_stack"] is True
        assert cold[5531]["device_type"]["device_type"] == chassis_type
        librenms.server.requests.clear()

        warm = _search(librenms)

        assert librenms.server.requests == []
        assert warm[5530]["virtual_chassis"]["is_stack"] is True
        assert (warm[5531]["device_type"]["device_type"], warm[5531]["device_type"]["match_type"]) == (
            chassis_type,
            "chassis",
        )

    def test_clear_cache_reads_the_chassis_inventory_again(self, librenms):
        self._rows(librenms)
        _search(librenms, clear_cache=True)
        librenms.server.requests.clear()

        _search(librenms, clear_cache=True)

        chassis_reads = [
            request
            for request in librenms.server.requests
            if request["path"] == "/api/v0/inventory/5531" and request["query"].get("entPhysicalClass") == ["chassis"]
        ]
        assert len(chassis_reads) == 1

"""Regression tests for the max-effort review findings on the OOB-sync PR.

Real-DB (django_db) coverage that exercises the actual ORM/model behaviour rather than mocks,
so a broken fix can't stay green by fabricating attributes.
"""

from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache as real_cache
from django.db import transaction
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device


def _superuser():
    # Must return an ACTUAL active superuser: User.objects.first() can hand back a pre-seeded
    # non-superuser (DB-ordering dependent), which would run permission-sensitive tests under the
    # wrong principal and cover the wrong branch. Filter explicitly, creating one if none exists.
    User = get_user_model()
    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if user:
        return user
    # NetBox's User model has no is_staff field — only is_superuser/is_active gate access here.
    return User.objects.create(username="review-su", is_superuser=True, is_active=True)


@pytest.mark.django_db
class TestAttachOobIpForeignKeyConflict:
    """_attach_oob_ip must not try to re-home an IP that is another device's primary/oob FK."""

    def test_conflict_when_ip_is_another_devices_oob_fk(self):
        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        donor = make_device("oob-donor")
        target = make_device("oob-target")
        target_iface = Interface.objects.create(device=target, name="mgmt0", type="1000base-t")

        # X is not assigned to ANY interface, yet it IS the donor's oob_ip — a state reachable
        # because the import path persists oob_ip via save(update_fields=[...]) (no full_clean()).
        ip = IPAddress.objects.create(address="10.10.0.5/32", status="active")
        donor.oob_ip = ip
        donor.save(update_fields=["oob_ip"])
        ip.refresh_from_db()
        assert ip.assigned_object is None

        request = RequestFactory().post("/")
        request.user = _superuser()

        # select_for_update needs an open transaction (the real caller provides one).
        with transaction.atomic():
            result_ip, reason = AddAsOOBView._attach_oob_ip(request, "10.10.0.5", target_iface)

        # Must surface a clean conflict, NOT re-home the IP into a doomed UNIQUE-constraint save.
        assert result_ip is None
        assert reason == "conflict"
        # The donor still owns it; nothing was silently re-homed.
        donor.refresh_from_db()
        ip.refresh_from_db()
        assert donor.oob_ip_id == ip.pk
        assert ip.assigned_object is None


@pytest.mark.django_db
class TestSaveDeviceValidatesPlatformDeviceTypeConsistency:
    """update_fields saves skip full_clean(), but a device_type/platform write must still honour the platform/manufacturer cross-field rule."""

    def test_update_fields_save_rejects_manufacturer_mismatch(self):
        from dcim.models import DeviceType, Manufacturer, Platform

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dt-consistency")  # device_type=TestDT, manufacturer=TestMfr
        mfr_a = Manufacturer.objects.get(slug="test-mfr")
        mfr_b, _ = Manufacturer.objects.get_or_create(name="OtherMfr", slug="other-mfr")
        # Platform limited to TestMfr — consistent with the device's current device_type.
        platform = Platform.objects.create(name="P-testmfr", slug="p-testmfr", manufacturer=mfr_a)
        device.platform = platform
        device.save(update_fields=["platform"])
        # A device_type from a DIFFERENT manufacturer than the platform allows.
        dt_other = DeviceType.objects.create(model="DT-other", slug="dt-other", manufacturer=mfr_b)

        device.device_type = dt_other
        resp = _save_device(device, update_fields=["device_type"])

        # Rejected with an error response, NOT silently persisted with a success toast.
        assert resp is not None
        device.refresh_from_db()
        assert device.device_type_id != dt_other.pk

    def test_update_fields_save_allows_consistent_device_type(self):
        from dcim.models import DeviceType, Manufacturer, Platform

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dt-consistent-ok")
        mfr_a = Manufacturer.objects.get(slug="test-mfr")
        platform = Platform.objects.create(name="P-ok", slug="p-ok", manufacturer=mfr_a)
        device.platform = platform
        device.save(update_fields=["platform"])
        # Same-manufacturer device_type — the consistent case must still save cleanly.
        dt_same = DeviceType.objects.create(model="DT-same", slug="dt-same", manufacturer=mfr_a)

        device.device_type = dt_same
        resp = _save_device(device, update_fields=["device_type"])

        assert resp is None
        device.refresh_from_db()
        assert device.device_type_id == dt_same.pk


@pytest.mark.django_db
class TestSerialMatchRoleIgnoresMissingDeviceId:
    """A missing/zero incoming device_id is unknown, not a 'linked elsewhere' mismatch."""

    def test_missing_device_id_does_not_offer_chassis_pair_toggle(self):
        from netbox_librenms_plugin.import_utils.device_operations import _detect_serial_match_role

        device = make_device("host1", serial="SN1")
        existing_link = {"host_id": 42, "oob_id": None}
        result = _detect_serial_match_role(
            existing_by_serial=device,
            existing_link=existing_link,
            hostname="host1",  # matches device.name
            serial="SN1",
            libre_device={"os": "ios", "hardware": "C9300"},  # no device_id key → normalizes to None
            server_key="default",
        )

        # Names match and there's no real incoming id to mismatch against, so this is a plain link
        # — NOT a host/OOB chassis-pair situation. The role-choice toggle must not be offered.
        assert result["serial_role_choice_available"] is False
        assert result["serial_action"] == "link"


@pytest.mark.django_db
class TestIpCachedSnapshotMgmtIpBackfill:
    """A pre-upgrade IP snapshot lacking the mgmt_ip key must resolve it on read, not silently skip auto-select."""

    def _view(self, mgmt_ip_resolves_to="10.0.0.9"):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        view = DeviceIPAddressTableView()
        api = MagicMock(server_key="default", cache_timeout=300)
        api.get_stored_librenms_id.return_value = 7
        api.get_device_info.return_value = (True, {"ip": mgmt_ip_resolves_to})
        view._librenms_api = api
        request = RequestFactory().get("/")
        request.user = _superuser()
        view.request = request
        return view, api, request

    def test_missing_mgmt_ip_key_resolves_on_cached_render(self):
        device = make_device("ip-preupgrade")
        view, api, request = self._view()
        key = view.get_cache_key(device, "ip_addresses", "default")
        # Pre-upgrade snapshot: NO "mgmt_ip" key.
        real_cache.set(key, {"ip_addresses": [], "ports_by_id": {"7": {}}}, timeout=300)
        try:
            view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
            # The missing key triggered a one-time live resolve of the management IP.
            api.get_device_info.assert_called_once_with(7)
        finally:
            real_cache.delete(key)

    def test_present_mgmt_ip_key_does_not_resolve(self):
        device = make_device("ip-postupgrade")
        view, api, request = self._view()
        key = view.get_cache_key(device, "ip_addresses", "default")
        # Complete snapshot: mgmt_ip already stored (even empty "" must be honoured, not re-resolved).
        real_cache.set(key, {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {"7": {}}}, timeout=300)
        try:
            view._prepare_context(request, device, "ifName", fetch_fresh=False, server_key="default")
            api.get_device_info.assert_not_called()
        finally:
            real_cache.delete(key)


@pytest.mark.django_db
class TestBuildIdServerInfoRejectsNonPositiveIds:
    """Per-server mapping rows must reject 0/negative/malformed host ids (LibreNMS ids start at 1)."""

    def test_zero_negative_and_malformed_host_ids_skipped(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = make_device("idsrv")
        device.custom_field_data["librenms_id"] = {
            "s_zero_int": 0,
            "s_zero_str": "0",
            "s_dict_zero": {"id": 0},
            "s_neg": -5,
            "s_bool": True,
            "s_good": 42,
            "s_good_dict": {"id": 7},
        }
        device.save()

        result = DeviceValidationDetailsView._build_id_server_info(device)

        # Only the genuinely-positive host ids survive — no bogus device_id 0 / -5 rows.
        server_keys = {r["server_key"]: r["device_id"] for r in (result or [])}
        assert server_keys == {"s_good": 42, "s_good_dict": 7}

    def test_oob_only_entry_is_surfaced_with_controller_id(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = make_device("idsrv-oob")
        device.custom_field_data["librenms_id"] = {
            "host_srv": {"id": 10},
            "oob_srv": {"oob": {"id": 99}},  # OOB-only link: no host "id"
        }
        device.save()

        result = DeviceValidationDetailsView._build_id_server_info(device)

        # The OOB-only link is still a real link — surface it (controller id), mirroring the
        # device-sync modal, rather than dropping it and risking a duplicate re-import.
        mapping = {r["server_key"]: r["device_id"] for r in (result or [])}
        assert mapping == {"host_srv": 10, "oob_srv": 99}


@pytest.mark.django_db
class TestSuggestOobInterfaceReusesMaterializedList:
    """_suggest_oob_interface must reuse a caller-materialized interface list, not re-query."""

    def test_no_query_when_interfaces_supplied(self):
        from dcim.models import Interface
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        device = make_device("oob-reuse")
        Interface.objects.create(device=device, name="idrac0", type="1000base-t")
        interfaces = list(device.interfaces.all())  # caller already materialized them

        with CaptureQueriesContext(connection) as ctx:
            iface_id, default_name = _suggest_oob_interface(device, {"type": "idrac"}, interfaces=interfaces)

        assert iface_id is not None  # matched idrac0
        assert default_name == "idrac0"
        # The supplied list is reused — no second device.interfaces.all() query.
        assert len(ctx.captured_queries) == 0


@pytest.mark.django_db
class TestValidateDedupsSerialDuplicateQuery:
    """The Stage-1 duplicate guard and Stage-2 merge detection share one serial[:2] lookup."""

    def test_serial_match_runs_serial_dup_query_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        # One NetBox device matched by serial; its name differs from the LibreNMS hostname so
        # validation takes the serial-match path (which runs both dup-detection stages).
        make_device("nb-name", serial="UNIQSER1")
        api = MagicMock(server_key="default", cache_timeout=300)
        api.get_device_info.return_value = (True, {"device_id": 1})
        libre_device = {
            "device_id": 5,
            "hostname": "libre-name",
            "sysName": "libre-name",
            "serial": "UNIQSER1",
            "hardware": "Model-X",
            "os": "ios",
        }

        with CaptureQueriesContext(connection) as ctx:
            validate_device_for_import(libre_device, api=api, include_vc_detection=False)

        # The duplicate-detection serial lookup (serial[:2], no .exclude) must run exactly once,
        # not once per stage. The .first() match query is LIMIT 1; the cross-side query has NOT.
        serial_dup_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if 'serial" =' in q["sql"].lower() and "limit 2" in q["sql"].lower() and "not" not in q["sql"].lower()
        ]
        assert len(serial_dup_queries) == 1


@pytest.mark.django_db
class TestAddDeviceTypeMappingSingleUpfrontQuery:
    """The upfront ambiguity check must use one [:2] fetch, not a separate count() + first()."""

    def test_no_count_query_on_mapping_upfront_check(self):
        from unittest.mock import patch

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.models import DeviceTypeMapping
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        dev = make_device("dtm-host")  # supplies a real DeviceType
        device_type = dev.device_type

        view = object.__new__(AddDeviceTypeMappingView)
        view._librenms_api = MagicMock(server_key="default")  # blank-key rebind returns "default"
        request = RequestFactory().post("/", {"device_type_id": str(device_type.pk), "server_key": ""})
        request.user = _superuser()
        view.request = request

        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"hardware": "WidgetX"},
            ),
            patch("netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView") as mock_detail,
            # Skip the post-save modal/row re-render (template URL reversal) — irrelevant to the
            # upfront query count, which has already run by then.
            patch.object(view, "get_validated_device_with_selections", return_value=(None, None, None)),
        ):
            mock_detail.return_value.get.return_value = MagicMock(content=b"<div></div>")
            with CaptureQueriesContext(connection) as ctx:
                view.post(request, device_id=1)

        # The fix collapses the upfront .count() + .first() into a single [:2] fetch (the locked
        # read already uses [:2]), so NO COUNT() query should touch the mapping table.
        count_qs = [
            q["sql"]
            for q in ctx.captured_queries
            if "count(" in q["sql"].lower() and "devicetypemapping" in q["sql"].lower()
        ]
        assert not count_qs, f"upfront ambiguity check must use [:2], not COUNT(): {count_qs}"
        # Sanity: the path ran to completion and created the mapping (normalized to lowercase).
        assert DeviceTypeMapping.objects.filter(librenms_hardware="widgetx").exists()

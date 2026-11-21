"""Integration coverage for the shared LibreNMS object sync page."""

import re
from copy import deepcopy

import pytest
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


SERVER_KEY = "default"
SECONDARY_KEY = "secondary"


def _configure_servers(settings, server, *keys):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: {
            "display_name": f"{key.title()} sync server",
            "librenms_url": server.url,
            "api_token": f"{key}-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
        for key in (keys or (SERVER_KEY, SECONDARY_KEY))
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(settings, monkeypatch):
    """Run a local HTTP server for real LibreNMS client requests."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        _configure_servers(settings, server)
        yield server


@pytest.fixture
def logged_in_client(client):
    client.force_login(make_superuser("sync-view-superuser"))
    return client


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": obj.pk})


def _register_device(server, device_id, name, *, inventory=None, **overrides):
    payload = {
        "device_id": device_id,
        "hostname": name,
        "sysName": name,
        "hardware": "TestDT",
        "serial": "SYNC-SERIAL",
        "os": "linux",
        "ip": "198.18.50.1",
        "version": "1.0",
        "features": "-",
        "location": "TestSite",
    }
    payload.update(overrides)
    server.register(
        f"/api/v0/devices/{device_id}",
        {"status": "ok", "devices": [payload]},
    )
    inventory = inventory or []
    server.register(
        f"/api/v0/inventory/{device_id}/all",
        {"status": "ok", "inventory": inventory},
    )
    server.register(
        f"/api/v0/inventory/{device_id}",
        {"status": "ok", "inventory": inventory},
    )
    server.register(f"/api/v0/devices/{device_id}/ports", {"status": "ok", "ports": []})
    server.register(f"/api/v0/devices/{device_id}/links", {"status": "ok", "links": []})
    server.register(f"/api/v0/devices/{device_id}/ip", {"status": "ok", "addresses": []})
    server.register(f"/api/v0/devices/{device_id}/transceivers", {"status": "ok", "transceivers": []})
    return payload


def _device_view(server_key=SERVER_KEY):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

    view = DeviceLibreNMSSyncView()
    view._librenms_api = LibreNMSAPI(server_key)
    # The VC Serials dialog preselects only members in the change scope of the request user.
    view.request = RequestFactory().get("/")
    view.request.user = make_superuser("sync-view-superuser")
    return view


@pytest.mark.django_db
class TestSyncPageRouting:
    def test_non_vc_device_uses_its_real_mapping_and_live_details(self, logged_in_client, librenms_server):
        device = make_device("sync-page-device", librenms_cf={SERVER_KEY: 6601})
        _register_device(
            librenms_server,
            6601,
            device.name,
            hardware=device.device_type.model,
            location=device.site.name,
            serial="PAGE-SERIAL",
        )

        response = logged_in_client.get(_sync_url(device))

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == device.pk
        assert response.context["librenms_device_id"] == 6601
        assert response.context["found_in_librenms"] is True
        assert response.context["librenms_device_serial"] == "PAGE-SERIAL"
        assert response.context["mismatched_device"] is False

    def test_requested_server_rebinds_header_tabs_and_refresh_links(self, logged_in_client, librenms_server):
        device = make_device(
            "sync-secondary-device",
            librenms_cf={SERVER_KEY: 6602, SECONDARY_KEY: 6603},
        )
        _register_device(librenms_server, 6603, device.name)

        response = logged_in_client.get(_sync_url(device), {"server_key": SECONDARY_KEY})

        assert response.status_code == 200
        assert response.context["server_key"] == SECONDARY_KEY
        assert response.context["librenms_server_info"]["server_key"] == SECONDARY_KEY
        assert response.context["librenms_device_id"] == 6603
        assert f"server_key={SECONDARY_KEY}" in response.content.decode()

    def test_stale_requested_server_renders_a_fail_closed_selection_shell(
        self,
        logged_in_client,
        librenms_server,
    ):
        _vc, members = make_virtual_chassis_members("sync-stale", count=2)
        viewed, mapped = members
        seed_mapping(mapped, SERVER_KEY, own=6604)

        response = logged_in_client.get(_sync_url(viewed), {"server_key": "retired"})

        assert response.status_code == 200
        assert response.context["server_selection_blocked"] is True
        assert response.context["server_key"] == "retired"
        assert response.context["lookup_device_pk"] == mapped.pk
        assert response.context["has_librenms_id"] is False
        assert [mapping.server_key for mapping in response.context["all_server_mappings"]] == [SERVER_KEY]

    def test_vc_member_without_mapping_delegates_to_the_linked_member(
        self,
        logged_in_client,
        librenms_server,
    ):
        _vc, members = make_virtual_chassis_members("sync-vc-delegate", count=2)
        linked, viewed = members
        seed_mapping(linked, SERVER_KEY, own=6605)
        _register_device(librenms_server, 6605, linked.name)

        response = logged_in_client.get(_sync_url(viewed))

        assert response.status_code == 200
        assert response.context["lookup_device_pk"] == linked.pk
        assert response.context["librenms_sync_device"] == linked
        assert response.context["sync_device_has_librenms_id"] is True

    @pytest.mark.parametrize(
        ("viewed_mapping", "delegation_shown"),
        [(None, True), (0, True), ({"_preferred_server": SERVER_KEY}, False), ({"retired": "abc"}, False)],
        ids=repr,
    )
    def test_the_delegation_notice_follows_the_viewed_members_own_mapping(
        self, logged_in_client, librenms_server, viewed_mapping, delegation_shown
    ):
        _vc, members = make_virtual_chassis_members(f"sync-vc-notice-{delegation_shown}", count=2)
        linked, viewed = members
        seed_mapping(linked, SERVER_KEY, own=6609)
        seed_stored_mapping(viewed, viewed_mapping, save=True)
        _register_device(librenms_server, 6609, linked.name)

        response = logged_in_client.get(_sync_url(viewed))

        assert response.status_code == 200
        assert response.context["librenms_sync_device"] == linked
        assert response.context["object_has_recorded_mapping"] is (not delegation_shown)
        notice = "LibreNMS sync for this virtual chassis is managed by"
        assert (notice in response.content.decode()) is delegation_shown

    def test_vc_member_with_own_mapping_remains_the_lookup_device(self, logged_in_client, librenms_server):
        _vc, members = make_virtual_chassis_members("sync-vc-own", count=2)
        viewed = members[1]
        seed_mapping(viewed, SERVER_KEY, own=6606)
        _register_device(librenms_server, 6606, viewed.name)

        response = logged_in_client.get(_sync_url(viewed))

        assert response.context["lookup_device_pk"] == viewed.pk
        assert response.context["librenms_device_id"] == 6606

    def test_vc_context_reports_the_linked_members_primary_ip(self, logged_in_client, librenms_server):
        _vc, members = make_virtual_chassis_members("sync-vc-ip", count=2)
        linked, viewed = members
        seed_mapping(linked, SERVER_KEY, own=6607, save=False)
        interface = make_interface(linked, "management")
        primary_ip = make_ip("198.18.50.7/32", assigned_object=interface)
        linked.primary_ip4 = primary_ip
        linked.save()
        _register_device(librenms_server, 6607, linked.name, ip="198.18.50.7")

        response = logged_in_client.get(_sync_url(viewed))

        assert response.context["is_vc_member"] is True
        assert response.context["sync_device_has_primary_ip"] is True

    def test_vm_page_uses_the_vm_mapping_and_vm_tabs(self, logged_in_client, librenms_server):
        vm = make_vm("sync-page-vm")
        seed_mapping(vm, SERVER_KEY, own=6608)
        _register_device(librenms_server, 6608, vm.name)

        response = logged_in_client.get(_sync_url(vm))

        assert response.status_code == 200
        assert response.context["object_model_name"] == "virtualmachine"
        assert response.context["lookup_device_model_name"] == "virtualmachine"
        assert response.context["librenms_device_id"] == 6608

    def test_all_applicable_tabs_and_active_tab_are_exposed(self, logged_in_client, librenms_server):
        device = make_device("sync-tab-device", librenms_cf={SERVER_KEY: 6609})
        _register_device(librenms_server, 6609, device.name)

        response = logged_in_client.get(_sync_url(device), {"tab": "cables"})

        assert response.context["active_sync_tab"] == "cables"
        assert response.context["interface_name_selector_visible"] is True
        assert {"interfaces", "cables", "ipaddresses", "vlans", "modules"}.issubset(response.context["sync_tab_urls"])
        assert response.context["module_sync"] is not None

    def test_invalid_tab_falls_back_to_interfaces(self, logged_in_client, librenms_server):
        device = make_device("sync-invalid-tab", librenms_cf={SERVER_KEY: 6610})
        _register_device(librenms_server, 6610, device.name)

        response = logged_in_client.get(_sync_url(device), {"tab": "not-a-tab"})

        assert response.context["active_sync_tab"] == "interfaces"

    def test_bare_integer_mapping_is_marked_as_legacy(self, logged_in_client, settings, librenms_server):
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device("sync-legacy", serial="LEGACY-SERIAL", librenms_cf=6611)
        _register_device(librenms_server, 6611, device.name, serial=device.serial)

        response = logged_in_client.get(_sync_url(device))

        assert response.context["librenms_id_is_legacy"] is True
        assert response.context["librenms_id_serial_confirmed"] is True

    @pytest.mark.parametrize("librenms_serial", [["LEGACY-SERIAL"], {"serial": "LEGACY-SERIAL"}, True])
    def test_a_malformed_librenms_serial_offers_no_legacy_conversion(
        self, logged_in_client, settings, librenms_server, librenms_serial
    ):
        """A list or an object is no serial: the page renders, and it offers no conversion and no serial sync."""
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device("sync-legacy-malformed", serial="LEGACY-SERIAL", librenms_cf=6616)
        _register_device(librenms_server, 6616, device.name, serial=librenms_serial)

        response = logged_in_client.get(_sync_url(device))

        assert response.status_code == 200
        assert response.context["librenms_id_is_legacy"] is True
        assert response.context["librenms_id_serial_confirmed"] is False
        assert response.context["serial_admitted"] is False

    @pytest.mark.parametrize(("librenms_serial", "matches"), [("S/N ABC-SYNC", True), ("XYZ-SYNC", False)])
    def test_the_serial_row_keys_both_serials_with_the_serial_rules(
        self, logged_in_client, settings, librenms_server, librenms_serial, matches
    ):
        """A stored "ABC" matches LibreNMS "S/N ABC" under the seeded rule, and "XYZ" does not."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device_id = 6612 if matches else 6613
        device = make_device(f"sync-keyed-serial-{device_id}", serial="ABC-SYNC", librenms_cf=device_id)
        _register_device(librenms_server, device_id, device.name, serial=librenms_serial)

        response = logged_in_client.get(_sync_url(device))

        assert response.context["serial_matches"] is matches
        assert response.context["librenms_id_serial_confirmed"] is matches
        # A match keeps the legacy stored form "ABC-SYNC", so the page offers to store the LibreNMS text.
        assert response.context["serial_stored_form_differs"] is matches
        sync_serial_url = reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": device.pk})
        assert sync_serial_url in response.content.decode()

    @pytest.mark.parametrize(("stored", "fits"), [("L" * 46, True), ("L" * 47, False)])
    def test_the_stored_form_sync_is_offered_only_for_a_serial_that_fits(
        self, logged_in_client, settings, librenms_server, stored, fits
    ):
        """The rule matches "S/N " and the stored serial, but the field holds 50 characters, so 51 cannot be stored."""
        from django.utils.html import escape

        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device_id = 6661 if fits else 6662
        device = make_device(f"sync-stored-form-fit-{device_id}", serial=stored, librenms_cf=device_id)
        _register_device(librenms_server, device_id, device.name, serial=f"S/N {stored}")

        response = logged_in_client.get(_sync_url(device))

        assert response.context["serial_matches"] is True
        assert response.context["serial_stored_form_differs"] is True
        content = response.content.decode()
        sync_serial_url = reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": device.pk})
        assert (sync_serial_url in content) is fits
        assert f"S/N {stored}" in content
        reason = "The LibreNMS serial has 51 characters; the field holds at most 50."
        assert (escape(reason) in content) is (not fits)

    @pytest.mark.parametrize(("stored", "reported", "matches"), [("100200", 100200, True), ("0", 0, False)])
    def test_a_numeric_serial_matches_its_stored_text_when_it_has_an_identity(
        self, logged_in_client, settings, librenms_server, stored, reported, matches
    ):
        """A key needs three letters or digits in a row, so a one-digit serial confirms nothing."""
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device(f"sync-numeric-serial-{stored}", serial=stored, librenms_cf=6614)
        _register_device(librenms_server, 6614, device.name, serial=reported)

        response = logged_in_client.get(_sync_url(device))

        assert response.context["serial_matches"] is matches
        assert response.context["librenms_id_serial_confirmed"] is matches

    def test_a_member_page_gates_the_legacy_id_on_the_mapping_owner_serial(
        self, logged_in_client, settings, librenms_server
    ):
        """The gate compares the owner's serial; the serial row compares the member's own serial."""
        _configure_servers(settings, librenms_server, SERVER_KEY)
        _vc, members = make_virtual_chassis_members("sync-vc-legacy-gate", count=2)
        owner, viewed = members
        owner.serial = "MASTER-GATE"
        owner.save()
        viewed.serial = "MEMBER-GATE"
        viewed.save()
        seed_stored_mapping(owner, 6615, save=True)
        _register_device(librenms_server, 6615, owner.name, serial="MASTER-GATE")

        response = logged_in_client.get(_sync_url(viewed))

        assert response.context["lookup_device_pk"] == owner.pk
        assert response.context["librenms_id_serial_confirmed"] is True
        assert response.context["serial_matches"] is False

    def test_a_serial_that_cannot_be_compared_shows_the_reason_and_no_sync(
        self, logged_in_client, settings, librenms_server
    ):
        from html import unescape

        from dcim.models import Device, VirtualChassis
        from django.utils.html import escape

        from netbox_librenms_plugin.serial_identity import UNAVAILABLE_MASTER_LINK

        _configure_servers(settings, librenms_server, SERVER_KEY)
        chassis, (viewed,) = make_virtual_chassis_members("sync-stray-master", count=1)
        viewed.serial = "STRAY-NETBOX"
        viewed.save()
        seed_stored_mapping(viewed, 6616, save=True)
        master = make_device("sync-stray-master-elsewhere")
        other = VirtualChassis.objects.create(name="sync-stray-other")
        # The master link names a device of another chassis, so no member serial can be compared.
        Device.objects.filter(pk=master.pk).update(virtual_chassis=other, vc_position=1)
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
        _register_device(librenms_server, 6616, viewed.name, serial="STRAY-LIBRENMS")

        response = logged_in_client.get(_sync_url(viewed))

        content = response.content.decode()
        assert "does not match NetBox serial" not in content
        sync_serial_url = reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": viewed.pk})
        assert sync_serial_url not in content
        assert escape(UNAVAILABLE_MASTER_LINK) in content
        # The reason is page text, not only a tooltip: a keyboard user cannot hover the icon.
        assert UNAVAILABLE_MASTER_LINK in unescape(re.sub(r"<[^>]*>", " ", content))

    @pytest.mark.parametrize("shared", [True, False])
    def test_a_device_that_shares_the_legacy_id_serial_gets_no_convert_offer(
        self, logged_in_client, settings, librenms_server, shared
    ):
        """The conversion needs the sole holder of the serial; the badge still compares the pair."""
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device(f"sync-legacy-shared-{shared}", serial="SHARED-LEGACY", librenms_cf=6617)
        if shared:
            make_device("sync-legacy-shared-peer", serial="SHARED-LEGACY")
        _register_device(librenms_server, 6617, device.name, serial="SHARED-LEGACY")

        response = logged_in_client.get(_sync_url(device))

        assert response.context["serial_matches"] is True
        assert response.context["librenms_id_is_legacy"] is True
        assert response.context["librenms_id_serial_confirmed"] is (not shared)
        action = reverse("plugins:netbox_librenms_plugin:convert_legacy_librenms_id", kwargs={"pk": device.pk})
        (form,) = re.findall(
            rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', response.content.decode(), re.S
        )
        buttons = [dict(re.findall(r'([\w-]+)(?:="([^"]*)")?', tag)) for tag in re.findall(r"<button\b([^>]*)>", form)]
        enabled_submits = [b for b in buttons if b.get("type", "submit") == "submit" and "disabled" not in b]
        # Only the sole holder gets an enabled Convert ID button.
        assert len(enabled_submits) == (0 if shared else 1)

    @pytest.mark.parametrize(("reported", "offered"), [("12", False), ("NEW-SERIAL-12", True)])
    def test_a_librenms_serial_without_an_identity_offers_no_serial_sync(
        self, logged_in_client, settings, librenms_server, reported, offered
    ):
        """The writer refuses a serial without an identity, so the page shows the text and offers no sync."""
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device(f"sync-no-identity-{offered}", serial="STORED-SERIAL", librenms_cf={SERVER_KEY: 6618})
        _register_device(librenms_server, 6618, device.name, serial=reported)

        response = logged_in_client.get(_sync_url(device))

        content = response.content.decode()
        assert response.context["librenms_device_serial"] == reported
        sync_serial_url = reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": device.pk})
        assert (sync_serial_url in content) is offered

    @pytest.mark.parametrize(
        ("stored", "reported", "mismatch"), [("STORED-12", "12", False), ("STORED-12", "OTHER-12", True)]
    )
    def test_only_an_admitted_librenms_serial_is_marked_as_a_mismatch(
        self, logged_in_client, settings, librenms_server, stored, reported, mismatch
    ):
        """A serial without an identity matches nothing, so the page must not mark a different text as a mismatch."""
        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device(f"sync-mismatch-{mismatch}", serial=stored, librenms_cf={SERVER_KEY: 6620})
        _register_device(librenms_server, 6620, device.name, serial=reported)

        response = logged_in_client.get(_sync_url(device))

        assert response.context["serial_matches"] is False
        assert ("data-serial-mismatch" in response.content.decode()) is mismatch

    def test_a_broken_serial_rule_of_the_device_keeps_the_sync_page(self, logged_in_client, settings, librenms_server):
        """The serial decisions are unavailable and name the rule; the other tabs still render."""
        from html import unescape

        from netbox_librenms_plugin.models import NormalizationRule, RuleConfigurationError

        _configure_servers(settings, librenms_server, SERVER_KEY)
        device = make_device("sync-broken-serial-rule", serial="BROKEN-RULE", librenms_cf={SERVER_KEY: 6619})
        rule = NormalizationRule.objects.create(
            scope="serial",
            match_pattern=r"^S/N\s+(.+)$",
            replacement=r"\1",
            manufacturer=device.device_type.manufacturer,
        )
        # A stored legacy rule that no longer meets the extraction format.
        NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"invalid-\1")
        rule.refresh_from_db()
        _register_device(librenms_server, 6619, device.name, serial="BROKEN-RULE-LIBRENMS")

        response = logged_in_client.get(_sync_url(device))

        assert response.status_code == 200
        assert response.context["interface_sync"] is not None
        assert response.context["serial_matches"] is False
        reason = RuleConfigurationError(rule, "replacement").public_message
        assert response.context["serial_unavailable"] == reason
        assert reason in unescape(re.sub(r"<[^>]*>", " ", response.content.decode()))
        sync_serial_url = reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": device.pk})
        assert sync_serial_url not in response.content.decode()


@pytest.mark.django_db
class TestServerMappingContext:
    def _mappings(self, obj, active=SERVER_KEY):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        return BaseLibreNMSSyncView._build_all_server_mappings(obj, active)

    @pytest.mark.parametrize("value", [None, 42, "42", [], True, {}])
    def test_non_scoped_or_empty_values_have_no_mapping_list(self, librenms_server, value):
        device = make_device(f"mapping-none-{type(value).__name__}")
        seed_stored_mapping(device, value, save=True)

        assert self._mappings(device) is None

    def test_scoped_entries_include_urls_display_names_and_active_state(self, librenms_server):
        device = make_device(
            "mapping-context",
            librenms_cf={SERVER_KEY: "6621", SECONDARY_KEY: {"id": 6622}},
        )

        mappings = self._mappings(device, SECONDARY_KEY)

        assert [mapping["server_key"] for mapping in mappings] == [SECONDARY_KEY, SERVER_KEY]
        assert [mapping["device_id"] for mapping in mappings] == [6622, 6621]
        assert mappings[0]["display_name"] == "Secondary sync server"
        assert mappings[0]["device_url"].endswith("/device/device=6622/")
        assert mappings[0]["is_active"] is True
        assert mappings[1]["is_active"] is False
        assert all(mapping["is_configured"] for mapping in mappings)

    def test_oob_only_mapping_is_surfaced_without_a_host_id(self, librenms_server):
        device = make_device(
            "mapping-oob-only",
            librenms_cf={SERVER_KEY: {"id": "invalid", "oob": {"id": "6623", "type": "idrac"}}},
        )

        mappings = self._mappings(device)

        assert mappings[0]["device_id"] == 6623
        assert mappings[0]["is_oob_only"] is True

    def test_invalid_mapping_entries_are_omitted(self, librenms_server):
        device = make_device(
            "mapping-invalid-entries",
            librenms_cf={SERVER_KEY: True, SECONDARY_KEY: "not-an-id", "retired": None},
        )

        assert self._mappings(device) is None

    def test_unconfigured_mapping_remains_visible_but_not_selectable(self, librenms_server):
        device = make_device("mapping-retired", librenms_cf={"retired": 6624})

        mappings = self._mappings(device, "retired")

        assert mappings[0]["server_key"] == "retired"
        assert mappings[0]["is_configured"] is False
        assert mappings[0]["is_selectable"] is False
        assert mappings[0]["device_url"] is None


@pytest.mark.django_db
class TestLibreNMSDeviceInfo:
    def test_absent_id_returns_the_default_not_found_contract(self, librenms_server):
        device = make_device("info-without-id")
        view = _device_view()
        view.librenms_id = None

        result = view.get_librenms_device_info(device)

        assert result["found_in_librenms"] is False
        assert result["device_info_unavailable"] is False
        assert result["mismatched_device"] is False
        assert result["librenms_device_details"]["librenms_device_hardware"] == "-"

    def test_real_live_info_populates_details_and_matches_short_hostname(self, librenms_server):
        device = make_device("info-matched")
        _register_device(
            librenms_server,
            6631,
            "info-matched.example.test",
            hardware=device.device_type.model,
            serial="INFO-SERIAL",
            os="info-os",
            version="2.0",
            location=device.site.name,
        )
        view = _device_view()
        view.librenms_id = 6631

        result = view.get_librenms_device_info(device, RequestFactory().get("/"))

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False
        details = result["librenms_device_details"]
        assert details["librenms_device_hardware"] == device.device_type.model
        assert details["librenms_device_serial"] == "INFO-SERIAL"
        assert details["librenms_device_os"] == "info-os"
        assert details["librenms_device_version"] == "2.0"

    def test_unrelated_names_and_addresses_are_mismatched(self, librenms_server):
        device = make_device("netbox-unrelated-name")
        _register_device(
            librenms_server,
            6632,
            "librenms-unrelated-name",
            ip="198.18.50.32",
        )
        view = _device_view()
        view.librenms_id = 6632

        result = view.get_librenms_device_info(device)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    def test_primary_ip_dns_name_can_establish_identity(self, librenms_server):
        device = make_device("different-netbox-name")
        interface = make_interface(device, "management")
        primary_ip = make_ip("198.18.50.33/32", assigned_object=interface)
        primary_ip.dns_name = "dns-identity.example.test"
        primary_ip.save()
        device.primary_ip4 = primary_ip
        device.save()
        device.refresh_from_db()
        _register_device(librenms_server, 6633, "dns-identity.example.test", ip="198.18.50.99")
        view = _device_view()
        view.librenms_id = 6633

        result = view.get_librenms_device_info(device)

        assert result["mismatched_device"] is False
        assert result["librenms_device_details"]["netbox_dns_name"] == "dns-identity.example.test"


def _stack_inventory(*members, extra=()):
    """Return a stack root, its chassis members and *extra* rows as one inventory."""
    root = {"entPhysicalIndex": 1, "entPhysicalClass": "stack", "entPhysicalContainedIn": 0}
    rows = [
        {"entPhysicalIndex": 100 + offset, "entPhysicalClass": "chassis", "entPhysicalContainedIn": 1, **member}
        for offset, member in enumerate(members)
    ]
    return [root, *rows, *extra]


# The reporter's EX4400 VC rows that the serials modal reads (serials masked by the reporter).
_JUNOS_VC_INVENTORY = [
    {
        "entPhysicalIndex": 1,
        "entPhysicalClass": "chassis",
        "entPhysicalDescr": "Juniper Virtual Chassis Switch",
        "entPhysicalSerialNum": "12345",
        "entPhysicalContainedIn": 0,
    },
    {
        "entPhysicalIndex": 4,
        "entPhysicalClass": "powerSupply",
        "entPhysicalDescr": "FPC 1 Power Supply 0",
        "entPhysicalSerialNum": "12348",
        "entPhysicalContainedIn": 1,
    },
    *(
        {
            "entPhysicalIndex": 120 + member,
            "entPhysicalClass": "container",
            "entPhysicalDescr": f"FPC: EX4400-24X @ {member}/*/*",
            "entPhysicalName": "EX4400-24X-S",
            "entPhysicalModelName": "650-151094",
            "entPhysicalSerialNum": serial,
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": member,
        }
        for member, serial in ((0, "12345"), (1, "12350"))
    ),
    {
        "entPhysicalIndex": 271,
        "entPhysicalClass": "other",
        "entPhysicalDescr": "Routing Engine 1",
        "entPhysicalName": "EX4400-24X-S",
        "entPhysicalModelName": "BUILTIN",
        "entPhysicalSerialNum": "12351",
        "entPhysicalContainedIn": 1,
        "entPhysicalParentRelPos": 1,
    },
]


@pytest.mark.django_db
class TestVirtualChassisInventory:
    def test_junos_fpc_members_are_both_offered_in_the_serials_modal(self, logged_in_client, librenms_server):
        """A Junos VC lists every FPC member serial, not only the root chassis serial."""
        _vc, members = make_virtual_chassis_members("inventory-junos", count=2)
        members[0].serial = "12345"
        members[0].save()
        seed_mapping(members[0], SERVER_KEY, own=6647)
        _register_device(librenms_server, 6647, members[0].name, inventory=_JUNOS_VC_INVENTORY)

        response = logged_in_client.get(_sync_url(members[0]))

        html = response.content.decode()
        start = html.index('id="vc-serials-form"')
        form = html[start : html.index("</form>", start)]
        assert "VC Serials (2)" in html
        assert "12345" in form
        assert "12350" in form
        assert "EX4400-24X-S" in form
        assert "12348" not in form
        assert "12351" not in form

    @pytest.mark.parametrize("stored", [("VC-DUP", "S/N VC-DUP"), ("S/N VC-DUP", "VC-DUP")])
    def test_a_serial_that_two_members_share_assigns_neither(self, librenms_server, stored):
        """Two members normalize to one serial key, so the modal names no assigned member."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        _vc, members = make_virtual_chassis_members("inventory-dup-key", count=2)
        for member, serial in zip(members, stored, strict=True):
            member.serial = serial
            member.save()
        inventory = _stack_inventory({"entPhysicalSerialNum": "VC-DUP"}, {"entPhysicalSerialNum": "VC-OTHER"})
        _register_device(librenms_server, 6648, members[0].name, inventory=inventory)
        view = _device_view()
        view.librenms_id = 6648

        result = view._get_vc_inventory_serials(members[0])

        assert [(item["serial"], item["assigned_member"]) for item in result] == [("VC-DUP", None), ("VC-OTHER", None)]

    def test_real_inventory_links_chassis_serials_to_members(self, librenms_server):
        _vc, members = make_virtual_chassis_members("inventory-members", count=2)
        members[0].serial = "VC-SERIAL-A"
        members[0].save()
        inventory = _stack_inventory(
            {
                "entPhysicalDescr": "First member",
                "entPhysicalSerialNum": " VC-SERIAL-A ",
                "entPhysicalModelName": "Member model A",
            },
            {
                "entPhysicalDescr": "Unassigned member",
                "entPhysicalSerialNum": "VC-SERIAL-B",
                "entPhysicalModelName": "Member model B",
            },
            {"entPhysicalSerialNum": "-"},
            extra=[{"entPhysicalIndex": 200, "entPhysicalClass": "module", "entPhysicalSerialNum": "IGNORED-MODULE"}],
        )
        _register_device(librenms_server, 6641, members[0].name, inventory=inventory)
        view = _device_view()
        view.librenms_id = 6641

        result = view._get_vc_inventory_serials(members[0])

        assert len(result) == 2
        assert result[0]["assigned_member"] == members[0]
        assert result[0]["model"] == "Member model A"
        assert result[1]["assigned_member"] is None

    def test_a_vendor_marker_does_not_hide_the_assigned_member(self, librenms_server):
        """
        Juniper reports the chassis serial as "S/N BCFB9793"; NetBox stores it without.

        Compared raw the two never match, so an already-assigned member is offered for
        assignment again. The modal shows the LibreNMS text.
        """
        _vc, members = make_virtual_chassis_members("inventory-marker", count=1)
        members[0].serial = "BCFB9793"
        members[0].save()
        inventory = _stack_inventory(
            {"entPhysicalDescr": "Member 1", "entPhysicalSerialNum": "S/N BCFB9793", "entPhysicalModelName": "EX4300"},
            {"entPhysicalDescr": "Member 2", "entPhysicalSerialNum": "S/N BCFB9794", "entPhysicalModelName": "EX4300"},
        )
        _register_device(librenms_server, 6644, members[0].name, inventory=inventory)
        view = _device_view()
        view.librenms_id = 6644

        result = view._get_vc_inventory_serials(members[0])

        assert len(result) == 2
        assert result[0]["serial"] == "S/N BCFB9793"
        assert result[0]["assigned_member"] == members[0]

    @pytest.mark.parametrize("has_master", [True, False])
    def test_member_inventory_uses_the_master_serial_rules(self, librenms_server, has_master):
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule

        vc, members = make_virtual_chassis_members("inventory-vendor", count=2)
        master, member = members
        other = Manufacturer.objects.create(name="Other inventory vendor", slug="other-inventory-vendor")
        member.device_type = DeviceType.objects.create(manufacturer=other, model="Other member", slug="other-member")
        member.serial = "MEMBER-SERIAL"
        member.save()
        vc.master = master if has_master else None
        vc.save()
        NormalizationRule.objects.create(
            scope="serial",
            manufacturer=master.device_type.manufacturer if has_master else other,
            match_pattern=r"^VENDOR:(.+)$",
            replacement=r"\1",
        )
        _register_device(
            librenms_server,
            6646,
            master.name,
            inventory=_stack_inventory(
                {"entPhysicalSerialNum": "VENDOR:MEMBER-SERIAL"}, {"entPhysicalSerialNum": "VENDOR:OTHER-SERIAL"}
            ),
        )
        view = _device_view()
        view.librenms_id = 6646

        result = view._get_vc_inventory_serials(member)

        assert result[0]["serial"] == "VENDOR:MEMBER-SERIAL"
        assert result[0]["assigned_member"] == member

    def test_a_masterless_member_serial_is_compared_under_its_own_rules(self, librenms_server):
        """Only the page maker strips "TAG ", so "TAG ABC" assigns no member of another maker that stores "ABC"."""
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule

        chassis, (page, member) = make_virtual_chassis_members("inventory-masterless", count=2)
        chassis.master = None
        chassis.save()
        NormalizationRule.objects.create(
            scope="serial", manufacturer=page.device_type.manufacturer, match_pattern=r"^TAG (.+)$", replacement=r"\1"
        )
        other = Manufacturer.objects.create(name="Masterless dialog vendor", slug="masterless-dialog-vendor")
        member.device_type = DeviceType.objects.create(
            manufacturer=other, model="Masterless dialog", slug="masterless-dialog"
        )
        member.serial = "ABC-DIALOG"
        member.save()
        _register_device(
            librenms_server,
            6650,
            page.name,
            inventory=_stack_inventory(
                {"entPhysicalSerialNum": "TAG ABC-DIALOG"}, {"entPhysicalSerialNum": "VC-OTHER-2"}
            ),
        )
        view = _device_view()
        view.librenms_id = 6650

        result = view._get_vc_inventory_serials(page)

        assert result[0]["serial"] == "TAG ABC-DIALOG"
        assert result[0]["assigned_member"] is None

    def test_a_masterless_dialog_keeps_a_row_that_only_a_member_rule_admits(self, librenms_server):
        """The page maker strips "TAG-" to a short value, but the member that stores "TAG-AB" keeps it whole."""
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule

        chassis, (page, member) = make_virtual_chassis_members("inventory-admits", count=2)
        chassis.master = None
        chassis.save()
        NormalizationRule.objects.create(
            scope="serial", manufacturer=page.device_type.manufacturer, match_pattern=r"^TAG-(.+)$", replacement=r"\1"
        )
        other = Manufacturer.objects.create(name="Admitting member vendor", slug="admitting-member-vendor")
        member.device_type = DeviceType.objects.create(
            manufacturer=other, model="Admitting member", slug="admitting-member"
        )
        member.serial = "TAG-AB"
        member.save()
        _register_device(
            librenms_server,
            6651,
            page.name,
            inventory=_stack_inventory({"entPhysicalSerialNum": "TAG-AB"}, {"entPhysicalSerialNum": "VC-OTHER-3"}),
        )
        view = _device_view()
        view.librenms_id = 6651

        result = view._get_vc_inventory_serials(page)

        assert [(item["serial"], item["assigned_member"]) for item in result][0] == ("TAG-AB", member)

    def test_a_masterless_junos_stack_keeps_an_fpc_that_only_a_member_rule_admits(self, librenms_server):
        """The page maker strips "TAG-" to a short value, so only the member's rules give the second FPC a serial."""
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule

        chassis, (page, member) = make_virtual_chassis_members("inventory-junos-admits", count=2)
        chassis.master = None
        chassis.save()
        NormalizationRule.objects.create(
            scope="serial", manufacturer=page.device_type.manufacturer, match_pattern=r"^TAG-(.+)$", replacement=r"\1"
        )
        other = Manufacturer.objects.create(name="Junos admitting vendor", slug="junos-admitting-vendor")
        member.device_type = DeviceType.objects.create(
            manufacturer=other, model="Junos admitting member", slug="junos-admitting-member"
        )
        member.serial = "TAG-AB"
        member.save()
        inventory = [
            {**row, "entPhysicalSerialNum": {"12345": "ROOT123", "12350": "TAG-AB"}[row["entPhysicalSerialNum"]]}
            for row in _JUNOS_VC_INVENTORY
            if row["entPhysicalSerialNum"] in ("12345", "12350")
        ]
        _register_device(librenms_server, 6652, page.name, inventory=inventory)
        view = _device_view()
        view.librenms_id = 6652

        result = view._get_vc_inventory_serials(page)

        assert [(item["serial"], item["assigned_member"]) for item in result] == [("ROOT123", None), ("TAG-AB", member)]

    def test_the_serial_rules_load_once_for_the_whole_chassis_loop(self, librenms_server):
        """The view reads its serial rules once, so the chassis loop pays no query per chassis component."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        # Two members of a masterless chassis: each member reads the context of its own type.
        chassis, members = make_virtual_chassis_members("inventory-preload", count=2)
        chassis.master = None
        chassis.save()
        inventory = _stack_inventory(
            *(
                {
                    "entPhysicalDescr": f"Member {index}",
                    "entPhysicalSerialNum": f"VC-PRELOAD-{index}",
                    "entPhysicalModelName": "Member model",
                }
                for index in range(5)
            )
        )
        _register_device(librenms_server, 6645, members[0].name, inventory=inventory)
        view = _device_view()
        view.librenms_id = 6645

        with CaptureQueriesContext(connection) as captured:
            result = view._get_vc_inventory_serials(members[0])

        assert len(result) == 5
        rule_queries = [q for q in captured.captured_queries if "normalizationrule" in q["sql"].lower()]
        # One read of the serial rules; per-call lookups would scale with the five components instead.
        assert len(rule_queries) == 1, f"expected one rule read for the loop, saw {len(rule_queries)} queries"
        # The members come with their types and chassis, so no member reads its own row of either table.
        per_member = [
            q["sql"]
            for q in captured.captured_queries
            if re.search(r'FROM "dcim_(devicetype|virtualchassis)"', q["sql"])
        ]
        assert per_member == []

    def test_failed_inventory_lookup_returns_an_empty_list(self, librenms_server):
        _vc, members = make_virtual_chassis_members("inventory-failure", count=1)
        librenms_server.register("/api/v0/inventory/6642/all", {"status": "error"}, status=404)
        view = _device_view()
        view.librenms_id = 6642

        assert view._get_vc_inventory_serials(members[0]) == []

    def test_the_dialog_stores_the_raw_serial_and_shows_the_member_as_assigned(self, logged_in_client, librenms_server):
        """The dialog posts and shows the LibreNMS serial, and finds the member on the next render."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        _vc, members = make_virtual_chassis_members("inventory-raw-dialog", count=2)
        owner, member = members
        owner.serial = "OWNER-RAW"
        owner.save()
        seed_mapping(owner, SERVER_KEY, own=6649)
        inventory = _stack_inventory(
            {"entPhysicalSerialNum": "S/N OWNER-RAW"}, {"entPhysicalSerialNum": "S/N MEMBER-RAW"}
        )
        _register_device(librenms_server, 6649, owner.name, inventory=inventory)

        html = logged_in_client.get(_sync_url(owner)).content.decode()
        start = html.index('id="vc-serials-form"')
        form = html[start : html.index("</form>", start)]
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))
        rows = {name.rsplit("_", 1)[1]: value for name, value in fields.items() if name.startswith("librenms_serial_")}
        (row,) = (index for index, value in rows.items() if value == "S/N MEMBER-RAW")
        logged_in_client.post(
            reverse("plugins:netbox_librenms_plugin:assign_vc_serial", kwargs={"pk": owner.pk}),
            {**fields, **{f"member_id_{index}": "" for index in rows}, f"member_id_{row}": str(member.pk)},
        )

        member.refresh_from_db()
        assert member.serial == "S/N MEMBER-RAW"
        response = logged_in_client.get(_sync_url(owner))
        shown = {item["serial"]: item["assigned_member"] for item in response.context["vc_inventory_serials"]}
        assert shown == {"S/N OWNER-RAW": owner, "S/N MEMBER-RAW": member}

    @staticmethod
    def _post_vc_serial_form(client, html, page, serial, member):
        """Post the rendered VC Serials form as a browser does, with *member* selected on the row of *serial*."""
        from netbox_librenms_plugin.tests.view_test_helpers import module_tab_forms

        (form,) = [form for form in module_tab_forms(html).forms if "assign-vc-serial" in form["action"]]
        # A browser does not submit a disabled select.
        selects = form["selects"]
        fields = {name: value for name, value in form["fields"].items() if selects.get(name, {}).get("enabled", True)}
        (row,) = (
            name.rsplit("_", 1)[1]
            for name, value in fields.items()
            if name.startswith("librenms_serial_") and value == serial
        )
        # A user can select only an enabled option of an enabled selector.
        assert form["selects"][f"member_id_{row}"]["enabled"] is True
        assert form["selects"][f"member_id_{row}"]["options"].get(str(member.pk)) is True
        fields[f"member_id_{row}"] = str(member.pk)
        assert form["action"] == reverse("plugins:netbox_librenms_plugin:assign_vc_serial", kwargs={"pk": page.pk})
        client.post(form["action"], fields)
        return form["fields"]

    def test_the_dialog_preselects_only_a_member_that_the_user_may_change(self, client, librenms_server):
        """The user may view both members but change only B, so the row of A shows A and selects nobody."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        _vc, (holder, page) = make_virtual_chassis_members("inventory-change-scope", count=2)
        holder.serial = "SCOPE-A"
        holder.save()
        seed_mapping(page, SERVER_KEY, own=6653)
        inventory = _stack_inventory({"entPhysicalSerialNum": "SCOPE-A"}, {"entPhysicalSerialNum": "SCOPE-B"})
        _register_device(librenms_server, 6653, page.name, inventory=inventory)
        user = make_user_with_perms("vc-serial-change-b", [("view", Device)])
        client.force_login(grant(user, "change", Device, constraints={"pk": page.pk}))

        response = client.get(_sync_url(page))

        shown = [(item["serial"], item["assigned_member"]) for item in response.context["vc_inventory_serials"]]
        assert shown == [("SCOPE-A", holder), ("SCOPE-B", None)]
        rendered = self._post_vc_serial_form(client, response.content.decode(), page, "SCOPE-B", page)
        page.refresh_from_db()
        holder.refresh_from_db()
        assert (holder.serial, page.serial) == ("SCOPE-A", "SCOPE-B")
        assert rendered["member_id_1"] == ""

    def test_two_rows_that_match_one_holder_select_no_member(self, logged_in_client, librenms_server):
        """Two rows report the serial of A, so neither row selects A and the post of a third row succeeds."""
        _vc, (holder, other) = make_virtual_chassis_members("inventory-shared-holder", count=2)
        holder.serial = "SHARED-A"
        holder.save()
        seed_mapping(holder, SERVER_KEY, own=6654)
        inventory = _stack_inventory(
            {"entPhysicalSerialNum": "SHARED-A"},
            {"entPhysicalSerialNum": "SHARED-A"},
            {"entPhysicalSerialNum": "SHARED-B"},
        )
        _register_device(librenms_server, 6654, holder.name, inventory=inventory)

        response = logged_in_client.get(_sync_url(holder))

        items = response.context["vc_inventory_serials"]
        assert [(item["serial"], item["assigned_member"]) for item in items] == [
            ("SHARED-A", holder),
            ("SHARED-A", holder),
            ("SHARED-B", None),
        ]
        rendered = self._post_vc_serial_form(logged_in_client, response.content.decode(), holder, "SHARED-B", other)
        other.refresh_from_db()
        assert other.serial == "SHARED-B"
        assert (rendered["member_id_1"], rendered["member_id_2"]) == ("", "")
        assert [bool(item.get("shared_holder")) for item in items] == [True, True, False]

    def test_a_holder_row_with_an_overlength_librenms_serial_selects_no_member(self, logged_in_client, librenms_server):
        """The rules match the 52-character text to the stored serial of A, but the field holds 50 characters."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        _vc, (holder, other) = make_virtual_chassis_members("inventory-overlength-holder", count=2)
        holder.serial = "L" * 48
        holder.save()
        seed_mapping(holder, SERVER_KEY, own=6656)
        inventory = _stack_inventory({"entPhysicalSerialNum": f"S/N {'L' * 48}"}, {"entPhysicalSerialNum": "LONG-B"})
        _register_device(librenms_server, 6656, holder.name, inventory=inventory)

        response = logged_in_client.get(_sync_url(holder))

        items = response.context["vc_inventory_serials"]
        assert [(item["serial"], item["assigned_member"]) for item in items] == [
            (f"S/N {'L' * 48}", holder),
            ("LONG-B", None),
        ]
        rendered = self._post_vc_serial_form(logged_in_client, response.content.decode(), holder, "LONG-B", other)
        other.refresh_from_db()
        holder.refresh_from_db()
        assert (holder.serial, other.serial) == ("L" * 48, "LONG-B")
        assert rendered["member_id_1"] == ""

    @pytest.mark.parametrize(("serial", "fits"), [("N" * 50, True), ("N" * 51, False)])
    def test_a_row_whose_serial_does_not_fit_shows_the_refusal_and_no_member_selector(
        self, logged_in_client, librenms_server, serial, fits
    ):
        """The field holds 50 characters; a selected member for a longer serial rolls back the other rows."""
        from django.utils.html import escape

        from netbox_librenms_plugin.tests.view_test_helpers import module_tab_forms

        _vc, (page, other) = make_virtual_chassis_members(f"inventory-fit-{fits}", count=2)
        device_id = 6663 if fits else 6664
        seed_mapping(page, SERVER_KEY, own=device_id)
        inventory = _stack_inventory({"entPhysicalSerialNum": serial}, {"entPhysicalSerialNum": "FIT-B"})
        _register_device(librenms_server, device_id, page.name, inventory=inventory)

        response = logged_in_client.get(_sync_url(page))

        html = response.content.decode()
        assert f"<code>{serial}</code>" in html
        (form,) = [form for form in module_tab_forms(html).forms if "assign-vc-serial" in form["action"]]
        if fits:
            assert form["selects"]["member_id_1"]["enabled"] is True
            assert form["selects"]["member_id_1"]["options"][str(page.pk)] is True
        else:
            assert "member_id_1" not in form["selects"]
        reason = "The LibreNMS serial has 51 characters; the field holds at most 50."
        assert (escape(reason) in html) is (not fits)
        rendered = self._post_vc_serial_form(logged_in_client, html, page, "FIT-B", other)
        other.refresh_from_db()
        assert (rendered["member_id_1"], other.serial) == ("", "FIT-B")

    def test_a_holder_row_whose_post_rewrites_the_stored_text_selects_no_member(
        self, logged_in_client, librenms_server
    ):
        """The rules match "S/N KEEP-A" to the stored "KEEP-A", so a preselected row would rewrite the serial of A."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        _vc, (holder, other) = make_virtual_chassis_members("inventory-rewrite-holder", count=2)
        holder.serial = "KEEP-A"
        holder.save()
        seed_mapping(holder, SERVER_KEY, own=6657)
        inventory = _stack_inventory({"entPhysicalSerialNum": "S/N KEEP-A"}, {"entPhysicalSerialNum": "KEEP-B"})
        _register_device(librenms_server, 6657, holder.name, inventory=inventory)

        response = logged_in_client.get(_sync_url(holder))

        items = response.context["vc_inventory_serials"]
        assert [(item["serial"], item["assigned_member"]) for item in items] == [
            ("S/N KEEP-A", holder),
            ("KEEP-B", None),
        ]
        rendered = self._post_vc_serial_form(logged_in_client, response.content.decode(), holder, "KEEP-B", other)
        other.refresh_from_db()
        holder.refresh_from_db()
        assert (holder.serial, other.serial) == ("KEEP-A", "KEEP-B")
        assert rendered["member_id_1"] == ""

    def test_a_holder_row_that_posts_no_change_selects_the_holder(self, logged_in_client, librenms_server):
        """A holder that stores the LibreNMS text already is preselected, and posting the form keeps it."""
        _vc, (holder, other) = make_virtual_chassis_members("inventory-noop-holder", count=2)
        holder.serial = "NOOP-A"
        holder.save()
        seed_mapping(holder, SERVER_KEY, own=6658)
        inventory = _stack_inventory({"entPhysicalSerialNum": "NOOP-A"}, {"entPhysicalSerialNum": "NOOP-B"})
        _register_device(librenms_server, 6658, holder.name, inventory=inventory)

        response = logged_in_client.get(_sync_url(holder))

        rendered = self._post_vc_serial_form(logged_in_client, response.content.decode(), holder, "NOOP-B", other)
        other.refresh_from_db()
        holder.refresh_from_db()
        assert (holder.serial, other.serial) == ("NOOP-A", "NOOP-B")
        assert rendered["member_id_1"] == str(holder.pk)

    def test_the_serials_form_submits_the_active_server_and_tab(self, logged_in_client, librenms_server):
        """AssignVCSerialView can only preserve the context the rendered form posts."""
        _vc, members = make_virtual_chassis_members("inventory-form-context", count=2)
        seed_mapping(members[0], SERVER_KEY, own=6643)
        _register_device(
            librenms_server,
            6643,
            members[0].name,
            inventory=_stack_inventory(
                {"entPhysicalDescr": "Unassigned member", "entPhysicalSerialNum": "VC-FORM-SERIAL"},
                {"entPhysicalDescr": "Second member", "entPhysicalSerialNum": "VC-FORM-SERIAL-2"},
            ),
        )

        response = logged_in_client.get(_sync_url(members[0]), {"tab": "cables"})

        html = response.content.decode()
        form = html[html.index('id="vc-serials-form"') : html.index("</form>", html.index('id="vc-serials-form"'))]
        assert f'name="server_key" value="{SERVER_KEY}"' in form
        assert 'name="tab" value="cables"' in form


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("-{serial}", None),
        ("-M{position}-{serial[0]}", None),
        ("-M{position}-{position:{serial[0]}>3}", None),
        ("-M{position}", "stack-M2"),
        ("-M{position}-{{serial}}", "stack-M2-{serial}"),
    ],
)
def test_vc_name_sync_uses_the_serial_rule_only_for_serial_suffixes(
    logged_in_client, librenms_server, pattern, expected
):
    from netbox_librenms_plugin.models import LibreNMSSettings, NormalizationRule

    chassis, (master, member) = make_virtual_chassis_members("rename-rule")
    chassis.master = master
    chassis.save(update_fields=["master"])
    member.name = "stack-ABC123"
    member.serial = "ABC123"
    member.save(update_fields=["name", "serial"])
    seed_mapping(member, SERVER_KEY, own=6650)
    settings_row = LibreNMSSettings.objects.first() or LibreNMSSettings()
    settings_row.vc_member_name_pattern = pattern
    settings_row.save()
    rule = NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=master.device_type.manufacturer
    )
    NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern="(")
    _register_device(librenms_server, 6650, "stack")
    action_url = reverse("plugins:netbox_librenms_plugin:update_device_name", args=[member.pk])

    response = logged_in_client.get(_sync_url(member), {"server_key": SERVER_KEY})
    assert response.status_code == 200
    assert (f'action="{action_url}"' in response.content.decode()) is (expected is not None)
    assert response.context["resolved_name"] == expected

    response = logged_in_client.post(action_url, {"server_key": SERVER_KEY})
    assert response.status_code == 302
    member.refresh_from_db()
    assert member.name == (expected or "stack-ABC123")


@pytest.mark.django_db
@pytest.mark.parametrize(("serial", "expected"), [("12", None), ("ABC123", "stack-ABC123")], ids=["short", "identity"])
def test_import_details_show_why_a_linked_member_gets_no_name(logged_in_client, librenms_server, serial, expected):
    """The import details of a linked member with serial 12 show the refusal that the Sync page shows."""
    from netbox_librenms_plugin.models import LibreNMSSettings

    chassis, (master, member) = make_virtual_chassis_members(f"import-no-token-{serial}")
    chassis.master = master
    chassis.save(update_fields=["master"])
    member.name = "member-<old>-name"
    member.serial = serial
    member.save(update_fields=["name", "serial"])
    seed_mapping(member, SERVER_KEY, own=6653)
    settings_row = LibreNMSSettings.objects.first() or LibreNMSSettings()
    settings_row.vc_member_name_pattern = "-{serial}"
    settings_row.save()
    _register_device(librenms_server, 6653, "stack")
    librenms_server.register("/api/v0/resources/locations", {"status": "ok", "locations": []})

    response = logged_in_client.get(
        reverse("plugins:netbox_librenms_plugin:device_validation_details", kwargs={"device_id": 6653}),
        {"server_key": SERVER_KEY},
    )

    assert response.status_code == 200
    content = response.content.decode()
    refusals = re.findall(r"<span[^>]*data-member-name-refusal[^>]*>(.*?)</span>", content, flags=re.S)
    assert len(refusals) == int(expected is None)
    assert ('value="sync_name"' in content) is (expected is not None)
    for refusal in refusals:
        assert f"&#x27;{serial}&#x27;" in refusal


@pytest.mark.django_db
@pytest.mark.parametrize(("serial", "expected"), [("12", None), ("ABC123", "stack-ABC123")], ids=["short", "identity"])
def test_vc_name_sync_offers_no_name_for_a_member_serial_without_an_identity(
    logged_in_client, librenms_server, serial, expected
):
    """A ``{serial}`` pattern gives a member with serial 12 no name, so the page shows why and the POST renames nothing."""
    from django.contrib.messages import get_messages

    from netbox_librenms_plugin.models import LibreNMSSettings
    from netbox_librenms_plugin.views.sync.device_fields import _MEMBER_NAME_RULES_UNAVAILABLE

    chassis, (master, member) = make_virtual_chassis_members(f"rename-no-token-{serial}")
    chassis.master = master
    chassis.save(update_fields=["master"])
    member.name = "member-old-name"
    member.serial = serial
    member.save(update_fields=["name", "serial"])
    seed_mapping(member, SERVER_KEY, own=6652)
    settings_row = LibreNMSSettings.objects.first() or LibreNMSSettings()
    settings_row.vc_member_name_pattern = "-{serial}"
    settings_row.save()
    _register_device(librenms_server, 6652, "stack")
    action_url = reverse("plugins:netbox_librenms_plugin:update_device_name", args=[member.pk])

    response = logged_in_client.get(_sync_url(member), {"server_key": SERVER_KEY})
    assert response.status_code == 200
    content = response.content.decode()
    assert (f'action="{action_url}"' in content) is (expected is not None)
    assert ("data-member-name-refusal" in content) is (expected is None)
    assert response.context["resolved_name"] == expected

    response = logged_in_client.post(action_url, {"server_key": SERVER_KEY})
    assert response.status_code == 302
    member.refresh_from_db()
    assert member.name == (expected or "member-old-name")
    warnings = [str(message) for message in get_messages(response.wsgi_request) if message.level_tag == "warning"]
    assert bool(warnings) is (expected is None)
    assert _MEMBER_NAME_RULES_UNAVAILABLE not in warnings


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("pattern", "member_name", "expected"),
    [("-{serial}", "stack-ABC123", None), ("-{serial}", None, None), ("-M{position}", "stack-ABC123", "stack-M2")],
)
def test_import_name_sync_uses_the_same_vc_name_as_the_dialog(
    logged_in_client, librenms_server, pattern, member_name, expected
):
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.models import LibreNMSSettings

    chassis, (_first, member) = make_virtual_chassis_members("import-rename-rule")
    outside_master = make_device("import-rename-outside-master")
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=outside_master)
    member.name = member_name
    member.serial = "ABC123"
    member.save(update_fields=["name", "serial"])
    seed_mapping(member, SERVER_KEY, own=6651)
    settings_row = LibreNMSSettings.objects.first() or LibreNMSSettings()
    settings_row.vc_member_name_pattern = pattern
    settings_row.save()
    _register_device(librenms_server, 6651, "stack")
    validation_url = reverse("plugins:netbox_librenms_plugin:device_validation_details", args=[6651])
    action_url = reverse("plugins:netbox_librenms_plugin:device_conflict_action", args=[6651])

    response = logged_in_client.get(validation_url, {"server_key": SERVER_KEY})
    assert response.status_code == 200
    assert response.context["validation"]["existing_device"] == member
    assert response.context["validation"]["name_matches"] is False
    assert (b'name="action" value="sync_name"' in response.content) is (expected is not None)
    assert response.context["validation"]["suggested_name"] == expected

    logged_in_client.post(
        action_url, {"server_key": SERVER_KEY, "action": "sync_name", "existing_device_id": member.pk}
    )
    member.refresh_from_db()
    assert member.name == (expected or member_name)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("stored", "reported", "offered"),
    [("ABC123", "S/N ABC123", True), ("S/N ABC123", "S/N ABC123", False), ("OLD123", "12", False)],
    ids=["other-stored-form", "exact-stored-form", "no-identity"],
)
def test_the_import_dialog_offers_a_serial_sync_for_another_stored_form(
    logged_in_client, librenms_server, stored, reported, offered
):
    """A stored serial that names the LibreNMS hardware in another form is healed by the sync, as on the sync page."""
    from netbox_librenms_plugin.models import NormalizationRule

    device = make_device(f"import-stored-form-{offered}-{len(stored)}", serial=stored)
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1", manufacturer=device.device_type.manufacturer
    )
    seed_mapping(device, SERVER_KEY, own=6653)
    _register_device(librenms_server, 6653, device.name, serial=reported)
    params = {"server_key": SERVER_KEY}

    response = logged_in_client.get(
        reverse("plugins:netbox_librenms_plugin:device_validation_details", args=[6653]), params
    )

    assert response.status_code == 200
    assert response.context["validation"]["existing_device"] == device
    assert (b'name="action" value="sync_serial"' in response.content) is offered
    if offered:
        logged_in_client.post(
            reverse("plugins:netbox_librenms_plugin:device_conflict_action", args=[6653]),
            {**params, "action": "sync_serial", "existing_device_id": device.pk},
        )
        device.refresh_from_db()
        assert device.serial == reported


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("pattern", "expected"), [("-{serial}", "stack-OLD123"), ("-M{position}-{position:{serial[0]}>3}", "stack-M2-OO2")]
)
def test_vc_name_sync_keeps_both_pages_in_agreement_when_the_reported_serial_changes(
    logged_in_client, librenms_server, pattern, expected
):
    from netbox_librenms_plugin.models import LibreNMSSettings

    chassis, (master, member) = make_virtual_chassis_members("rename-serial-drift")
    chassis.master = master
    chassis.save(update_fields=["master"])
    member.serial = "OLD123"
    member.save(update_fields=["serial"])
    seed_mapping(member, SERVER_KEY, own=6652)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"vc_member_name_pattern": pattern})
    _register_device(librenms_server, 6652, "stack", serial="NEW456")
    validation_url = reverse("plugins:netbox_librenms_plugin:device_validation_details", args=[6652])
    import_action_url = reverse("plugins:netbox_librenms_plugin:device_conflict_action", args=[6652])
    sync_action_url = reverse("plugins:netbox_librenms_plugin:update_device_name", args=[member.pk])
    params = {"server_key": SERVER_KEY}

    dialog = logged_in_client.get(validation_url, params)
    sync_page = logged_in_client.get(_sync_url(member), params)
    assert dialog.status_code == sync_page.status_code == 200
    assert dialog.context["validation"]["suggested_name"] == sync_page.context["resolved_name"] == expected

    logged_in_client.post(import_action_url, {**params, "action": "sync_name", "existing_device_id": member.pk})
    member.refresh_from_db()
    assert (member.name, member.serial) == (expected, "OLD123")
    sync_page = logged_in_client.get(_sync_url(member), params)
    assert f'action="{sync_action_url}"' not in sync_page.content.decode()
    assert logged_in_client.post(sync_action_url, params).status_code == 302
    member.refresh_from_db()
    assert (member.name, member.serial) == (expected, "OLD123")


@pytest.mark.django_db
class TestPlatformAndPatternHelpers:
    def test_platform_info_uses_an_exact_real_platform(self, librenms_server):
        from dcim.models import Platform

        device = make_device("platform-info")
        platform = Platform.objects.create(name="SyncOS", slug="syncos")
        details = {
            "librenms_device_details": {
                "librenms_device_os": "SyncOS",
                "librenms_device_version": "9.1",
            }
        }

        result = _device_view()._get_platform_info(details, device, None)

        assert result["assessment"].target == platform
        assert result["assessment"].operation == "assign"
        assert result["librenms_os"] == "SyncOS"
        assert result["librenms_version"] == "9.1"

    def test_missing_platform_name_has_no_match(self, librenms_server):
        device = make_device("platform-info-missing")
        details = {
            "librenms_device_details": {
                "librenms_device_os": "-",
                "librenms_device_version": "-",
            }
        }

        result = _device_view()._get_platform_info(details, device, None)

        assert result["assessment"].reason == "no_os"
        assert result["assessment"].operation is None

    def test_vc_pattern_strips_configured_suffixes(self, librenms_server):
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        settings_row = LibreNMSSettings.objects.order_by("pk").first() or LibreNMSSettings()
        settings_row.vc_member_name_pattern = "-STACK{position}"
        settings_row.save()
        assert BaseLibreNMSSyncView._strip_vc_pattern("switch01-stack2") == "switch01"

        settings_row.vc_member_name_pattern = "-{serial}"
        settings_row.save()
        assert BaseLibreNMSSyncView._strip_vc_pattern("switch-SERIAL-12") == "switch"
        assert BaseLibreNMSSyncView._strip_vc_pattern("switch") is None

    def test_base_context_hooks_are_explicitly_empty(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        view = BaseLibreNMSSyncView()
        request = RequestFactory().get("/")
        device = object()

        assert view.get_interface_context(request, device) is None
        assert view.get_cable_context(request, device) is None
        assert view.get_ip_context(request, device) is None
        assert view.get_vlan_context(request, device) is None
        assert view.get_module_context(request, device) is None

"""Behavior tests for LibreNMS device and interface tables."""

import re
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_cluster,
    make_device,
    make_interface,
    make_virtual_chassis,
    make_vm,
)


def _import_record(device_id=4101, **validation):
    """Return one exact LibreNMS import-table record."""
    return {
        "device_id": device_id,
        "hostname": f"edge-{device_id}.example.test",
        "sysName": f"edge-{device_id}",
        "location": "Test Lab",
        "hardware": "Test Router",
        "_validation": validation,
    }


def _port(port_id=42, **overrides):
    """Return one complete LibreNMS interface record."""
    record = {
        "port_id": port_id,
        "ifName": "Ethernet1",
        "ifDescr": "Ethernet1",
        "ifAlias": "Uplink",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifPhysAddress": "AA:BB:CC:DD:EE:FF",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "untagged_vlan": None,
        "tagged_vlans": [],
        "missing_vlans": [],
        "vlan_group_map": {},
    }
    record.update(overrides)
    return record


def _interface_table(device=None, *, data=None, server_key="default", interface_name_field="ifName"):
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    return LibreNMSInterfaceTable(
        data=data or [],
        device=device,
        interface_name_field=interface_name_field,
        server_key=server_key,
    )


@pytest.mark.django_db
class TestDeviceStatusTable:
    @pytest.mark.parametrize(
        ("value", "css_class", "label"),
        [
            (True, "text-success", "Found"),
            (False, "text-danger", "Not Found"),
            (None, "text-secondary", "Unknown"),
        ],
    )
    def test_status_links_to_the_real_sync_route(self, value, css_class, label):
        from netbox_librenms_plugin.tables.device_status import DeviceStatusTable

        device = make_device(f"status-{label.lower().replace(' ', '-')}")
        table = DeviceStatusTable(type(device).objects.none())

        html = str(table.render_librenms_status(value, device))

        assert (
            reverse(
                "plugins:netbox_librenms_plugin:device_librenms_sync",
                kwargs={"pk": device.pk},
            )
            in html
        )
        assert css_class in html
        assert label in html

    def test_virtual_chassis_member_links_to_the_real_sync_member(self):
        from netbox_librenms_plugin.tables.device_status import DeviceStatusTable
        from netbox_librenms_plugin.utils import set_librenms_device_id

        viewed = make_device("status-vc-viewed")
        sync_member = make_device("status-vc-sync")
        make_virtual_chassis("status-vc", viewed, sync_member)
        set_librenms_device_id(sync_member, 4102, "default")
        sync_member.save()
        table = DeviceStatusTable(type(viewed).objects.none())

        html = str(table.render_librenms_status(True, viewed))

        assert f"See {sync_member.name}" in html
        assert (
            reverse(
                "plugins:netbox_librenms_plugin:device_librenms_sync",
                kwargs={"pk": sync_member.pk},
            )
            in html
        )


@pytest.mark.django_db
class TestDeviceImportTable:
    def _table(self, data=None, **kwargs):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        return DeviceImportTable(data=data or [], **kwargs)

    @pytest.mark.parametrize(
        ("order_by", "field"),
        [
            ("hostname", "hostname"),
            ("sysname", "sysName"),
            ("location", "location"),
            ("hardware", "hardware"),
        ],
    )
    def test_supported_ordering_is_case_insensitive(self, order_by, field):
        first = _import_record(1)
        second = _import_record(2)
        first[field] = "zulu"
        second[field] = "Alpha"

        table = self._table([first, second], order_by=order_by)

        assert [row["device_id"] for row in table.data] == [2, 1]

    def test_descending_order_and_none_values_are_stable(self):
        records = [_import_record(1), _import_record(2), _import_record(3)]
        records[0]["hostname"] = None
        records[1]["hostname"] = "alpha"
        records[2]["hostname"] = "Zulu"

        table = self._table(records, order_by="-hostname")

        assert [row["device_id"] for row in table.data] == [3, 2, 1]

    def test_unknown_order_does_not_mutate_input(self):
        records = [_import_record(2), _import_record(1)]

        table = self._table(records, order_by="device_id")

        assert [row["device_id"] for row in table.data] == [2, 1]

    def test_selection_uses_the_netbox_toggle_column(self):
        from netbox.tables.columns import ToggleColumn

        table = self._table([_import_record(can_import=True)])

        assert isinstance(table.columns["selection"].column, ToggleColumn)

    @pytest.mark.parametrize(
        ("hostname", "sysname", "device_id", "accessible_name"),
        [
            ("edge.example.test", "edge", 4101, "edge.example.test"),
            ("", "edge", 4102, "edge"),
            ("", "", 4103, "4103"),
        ],
    )
    def test_selection_has_an_accessible_name(self, hostname, sysname, device_id, accessible_name):
        record = _import_record(device_id, can_import=True)
        record["hostname"] = hostname
        record["sysName"] = sysname
        table = self._table([record])

        html = str(table.rows[0].get_cell("selection"))

        assert f'aria-label="Select {accessible_name}"' in html

    def test_selection_and_hostname_escape_librenms_values(self):
        record = _import_record(5, can_import=True)
        record["hostname"] = '"><script>alert(1)</script>'
        record["sysName"] = '" onfocus="alert(2)'
        table = self._table([record])

        selection = str(table.rows[0].get_cell("selection"))
        hostname = str(table.render_hostname(record["hostname"], record))

        assert "<script>" not in selection
        assert "&lt;script&gt;" in selection
        assert 'onfocus="alert(2)' not in selection
        assert "<script>" not in hostname
        assert "&lt;script&gt;" in hostname

    def test_disabled_selection_keeps_its_static_name(self):
        table = self._table([_import_record(can_import=False)])

        html = str(table.rows[0].get_cell("selection"))

        assert "disabled" in html
        assert 'name="select"' in html

    def test_cluster_cells_use_real_vm_device_and_cluster_rows(self):
        cluster = make_cluster("Import table cluster")
        vm = make_vm("import-table-vm", cluster)
        device = make_device("import-table-device")
        table = self._table()

        vm_html = str(table.render_netbox_cluster(None, _import_record(existing_device=vm)))
        device_html = str(table.render_netbox_cluster(None, _import_record(existing_device=device)))
        select_html = str(
            table.render_netbox_cluster(
                None,
                _import_record(cluster={"found": True, "cluster": cluster}),
            )
        )

        assert cluster.name in vm_html
        assert "Device (not VM)" in device_html
        assert f'value="{cluster.pk}" selected' in select_html
        assert (
            reverse(
                "plugins:netbox_librenms_plugin:device_cluster_update",
                kwargs={"device_id": 4101},
            )
            in select_html
        )

    def test_clusterless_vm_is_rendered_without_dereferencing_a_cluster(self):
        vm = make_vm("import-table-standalone-vm")
        vm.cluster = None
        vm.save()

        html = str(self._table().render_netbox_cluster(None, _import_record(existing_device=vm)))

        assert "VM (no cluster)" in html

    def test_role_dropdown_uses_real_roles_and_scoped_server_key(self):
        from dcim.models import DeviceRole

        selected = DeviceRole.objects.create(name="Import selected role", slug="import-selected-role", color="336699")
        DeviceRole.objects.create(name="Import other role", slug="import-other-role")
        table = self._table(server_key="secondary")

        html = str(
            table.render_netbox_role(
                None,
                _import_record(
                    device_role={"found": True, "role": selected},
                    _vc_detection_enabled=True,
                ),
            )
        )

        assert f'value="{selected.pk}" selected' in html
        assert "?enable_vc_detection=true" in html
        assert "hx-vals='{&quot;server_key&quot;: &quot;secondary&quot;}'" in html

    def test_existing_role_uses_the_real_role_color(self):
        from dcim.models import DeviceRole

        role = DeviceRole.objects.create(name="Rendered role", slug="rendered-role", color="123456")
        device = make_device("role-render-device")
        device.role = role
        device.save()

        html = str(self._table().render_netbox_role(None, _import_record(existing_device=device)))

        assert role.name in html
        assert "#123456" in html

    def test_vm_role_placeholder_is_optional(self):
        html = str(self._table().render_netbox_role(None, _import_record(import_as_vm=True)))

        assert "Select Role (Optional)" in html

    def test_rack_dropdown_uses_real_location_and_rack_once(self):
        from dcim.models import Location, Rack

        device = make_device("rack-table-device")
        location = Location.objects.create(
            name="Import floor",
            slug="import-floor",
            site=device.site,
            status="active",
        )
        rack = Rack.objects.create(
            name="Import rack",
            site=device.site,
            location=location,
            status="active",
        )
        table = self._table(server_key="secondary")

        html = str(
            table.render_netbox_rack(
                None,
                _import_record(
                    site={"found": True},
                    rack={"rack": rack, "available_racks": [rack]},
                    _vc_detection_enabled=True,
                ),
            )
        )

        assert html.count(f'<option value="{rack.pk}"') == 1
        assert f'value="{rack.pk}" selected' in html
        assert f"{location.name} - {rack.name}" in html
        assert "?enable_vc_detection=true" in html
        assert "secondary" in html

    def test_rack_cells_cover_vm_missing_site_and_existing_device(self):
        from dcim.models import Rack

        existing = make_device("existing-rack-device")
        rack = Rack.objects.create(name="Existing rack", site=existing.site, status="active")
        existing.rack = rack
        existing.save()
        table = self._table()

        assert "N/A (VM)" in str(table.render_netbox_rack(None, _import_record(import_as_vm=True)))
        assert ">--<" in str(table.render_netbox_rack(None, _import_record(site={"found": False})))
        assert rack.name in str(table.render_netbox_rack(None, _import_record(existing_device=existing)))

    @pytest.mark.parametrize(
        ("vc_data", "expected"),
        [
            ({}, "—"),
            ({"is_stack": True, "member_count": 1}, "—"),
            ({"is_stack": True, "member_count": 2, "detection_error": "timeout"}, "Error"),
            ({"is_stack": True, "member_count": 3}, "3 members"),
        ],
    )
    def test_virtual_chassis_states_use_the_real_details_route(self, vc_data, expected):
        table = self._table(server_key="server with space")

        html = str(table.render_virtual_chassis(None, _import_record(7, virtual_chassis=vc_data)))

        assert expected in html
        if expected != "—":
            assert (
                reverse(
                    "plugins:netbox_librenms_plugin:device_vc_details",
                    kwargs={"device_id": 7},
                )
                in html
            )
            assert "server_key=server+with+space" in html

    def test_validation_url_prefers_cluster_and_preserves_server_and_vc_state(self):
        cluster = make_cluster("Validation URL cluster")
        table = self._table(server_key="secondary server")

        url = table._build_validation_details_url(
            9,
            {
                "cluster": {"found": True, "cluster": cluster},
                "_vc_detection_enabled": True,
            },
        )
        parsed = urlparse(url)

        assert parsed.path == reverse(
            "plugins:netbox_librenms_plugin:device_validation_details",
            kwargs={"device_id": 9},
        )
        assert parse_qs(parsed.query) == {
            "cluster_id": [str(cluster.pk)],
            "enable_vc_detection": ["true"],
            "server_key": ["secondary server"],
        }

    def test_validation_url_uses_role_when_no_cluster_is_selected(self):
        from dcim.models import DeviceRole

        role = DeviceRole.objects.create(name="Validation URL role", slug="validation-url-role")
        url = self._table()._build_validation_details_url(
            10,
            {"device_role": {"found": True, "role": role}},
        )

        assert parse_qs(urlparse(url).query) == {"role_id": [str(role.pk)]}

    def test_actions_render_real_object_routes(self):
        device = make_device("actions-existing-device")
        vm = make_vm("actions-existing-vm")
        table = self._table(server_key="secondary")

        device_html = str(table.render_actions(None, _import_record(existing_device=device)))
        vm_html = str(table.render_actions(None, _import_record(existing_device=vm)))

        assert reverse("dcim:device", kwargs={"pk": device.pk}) in device_html
        assert "View Device in NetBox" in device_html
        assert reverse("virtualization:virtualmachine", kwargs={"pk": vm.pk}) in vm_html
        assert "View VM in NetBox" in vm_html
        assert "server_key=secondary" in device_html

    @pytest.mark.parametrize(
        ("validation", "marker"),
        [
            ({"device_type_mismatch": True}, "Conflict"),
            ({"existing_match_type": "hostname"}, "Conflict"),
            ({"existing_match_type": "librenms_id", "name_sync_available": True}, "Details"),
            (
                {"existing_match_type": "librenms_id", "librenms_id_needs_migration": True},
                "Legacy ID",
            ),
            ({"existing_match_type": "serial", "serial_action": "oob_candidate"}, " OOB"),
            (
                {
                    "existing_match_type": "librenms_oob",
                    "existing_librenms_link": {"host_id": 88},
                },
                "paired host: LibreNMS #88",
            ),
            (
                {
                    "existing_match_type": "librenms_id",
                    "existing_librenms_link": {"host_id": 41, "oob_id": 42, "oob_type": "BMC"},
                },
                " Host",
            ),
            ({"existing_match_type": "librenms_id"}, "btn-outline-success"),
        ],
    )
    def test_existing_device_action_states(self, validation, marker):
        existing = make_device(f"action-state-{abs(hash(marker))}")
        validation["existing_device"] = existing

        html = str(self._table().render_actions(None, _import_record(**validation)))

        assert marker in html

    def test_malformed_pair_metadata_never_leaks_raw_values(self):
        existing = make_device("malformed-pair-device")
        html = str(
            self._table().render_actions(
                None,
                _import_record(
                    existing_device=existing,
                    existing_match_type="librenms_id",
                    existing_librenms_link={"host_id": True, "oob_id": "bad"},
                ),
            )
        )

        assert "LibreNMS #bad" not in html
        assert "LibreNMS #1" not in html

    @pytest.mark.parametrize(
        ("validation", "marker"),
        [
            ({"is_ready": True, "can_import": True}, "device-ready"),
            ({"is_ready": False, "can_import": True}, "Review"),
            ({"is_ready": False, "can_import": False}, "disabled"),
        ],
    )
    def test_new_device_action_states(self, validation, marker):
        html = str(self._table().render_actions(None, _import_record(**validation)))

        assert marker in html

    def test_ready_stack_action_contains_an_escaped_complete_payload(self):
        validation = {
            "is_ready": True,
            "can_import": True,
            "virtual_chassis": {
                "is_stack": True,
                "member_count": 2,
                "members": [
                    {"position": 1, "serial": "A&1", "suggested_name": 'edge"1'},
                    {"position": 2, "serial": "B2", "suggested_name": "edge2"},
                ],
            },
        }
        record = _import_record(**validation)
        record["hostname"] = 'master"><script>'

        html = str(self._table().render_actions(None, record))

        assert 'data-vc-is-stack="true"' in html
        assert 'data-vc-member-count="2"' in html
        assert "&quot;members&quot;" in html
        assert "<script>" not in html

    def test_full_table_render_has_stable_row_identity_and_real_action_urls(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        record = _import_record(can_import=True, is_ready=True)
        table = DeviceImportTable([record], server_key="secondary")
        html = table.as_html(RequestFactory().get("/"))

        assert 'id="device-row-4101"' in html
        assert 'value="4101"' in html
        assert (
            reverse(
                "plugins:netbox_librenms_plugin:device_validation_details",
                kwargs={"device_id": 4101},
            )
            in html
        )

    def test_full_table_render_keeps_row_metadata_off_the_header_checkbox(self):
        record = _import_record(can_import=True, is_ready=True)
        table = self._table([record], server_key="secondary")

        html = table.as_html(RequestFactory().get("/"))
        header = html[html.index("<thead") : html.index("</thead>")]
        row_tag = re.search(r'<tr[^>]*id="device-row-4101"[^>]*>', html)

        assert row_tag is not None
        assert 'data-device-id="4101"' in row_tag.group()
        assert 'data-hostname="edge-4101.example.test"' in row_tag.group()
        assert 'data-sysname="edge-4101"' in row_tag.group()
        assert "data-device-id" not in header
        assert "data-hostname" not in header
        assert "data-sysname" not in header


@pytest.mark.django_db
class TestInterfaceTableFields:
    def test_complete_record_renders_against_a_real_interface(self):
        from dcim.models import MACAddress
        from netbox_librenms_plugin.models import InterfaceTypeMapping
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("interface-fields")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        interface.speed = 1_000_000
        interface.mtu = 1500
        interface.enabled = True
        interface.description = "Uplink"
        set_librenms_device_id(interface, 42, "default")
        interface.save()
        mac = MACAddress.objects.create(mac_address="AA:BB:CC:DD:EE:FF")
        interface.mac_addresses.add(mac)
        InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd",
            librenms_speed=1_000_000,
            netbox_type="1000base-t",
        )
        record = _port(exists_in_netbox=True, netbox_interface=interface)
        table = _interface_table(device)

        assert "text-success" in str(table.render_name(record["ifName"], record))
        assert "text-success" in str(table.render_type(record["ifType"], record))
        assert "text-success" in str(table.render_speed(record["ifSpeed"], record))
        assert "text-success" in str(table.render_mac_address(record["ifPhysAddress"], record))
        assert "text-success" in str(table.render_mtu(record["ifMtu"], record))
        assert "text-success" in str(table.render_enabled(record["ifAdminStatus"], record))
        assert "text-success" in str(table.render_description(record["ifAlias"], record))
        assert "text-success" in str(table.render_librenms_id(record["port_id"], record))

    def test_mismatched_real_interface_fields_render_as_warnings(self):
        device = make_device("interface-mismatches")
        interface = make_interface(device, "OtherName", iface_type="virtual")
        interface.speed = 1000
        interface.mtu = 9000
        interface.enabled = False
        interface.description = "Other description"
        interface.save()
        record = _port(exists_in_netbox=True, netbox_interface=interface)
        table = _interface_table(device)

        for rendered in (
            table.render_name(record["ifName"], record),
            table.render_speed(record["ifSpeed"], record),
            table.render_mac_address(record["ifPhysAddress"], record),
            table.render_mtu(record["ifMtu"], record),
            table.render_enabled(record["ifAdminStatus"], record),
            table.render_description(record["ifAlias"], record),
        ):
            assert "text-warning" in str(rendered)

    @pytest.mark.parametrize("value", ["up", "UP", True])
    def test_enabled_values_normalize_to_enabled(self, value):
        html = str(_interface_table().render_enabled(value, {"exists_in_netbox": False}))

        assert "Enabled" in html
        assert "text-danger" in html

    @pytest.mark.parametrize("value", ["down", False, None])
    def test_disabled_values_normalize_to_disabled(self, value):
        html = str(_interface_table().render_enabled(value, {"exists_in_netbox": False}))

        assert "Disabled" in html

    def test_untrusted_interface_values_are_escaped_at_each_html_sink(self):
        xss = '<img src=x onerror="alert(1)">'
        table = _interface_table()
        record = _port(
            exists_in_netbox=False,
            ifName=xss,
            untagged_vlan=xss,
            missing_vlans=[xss],
        )

        rendered = "".join(
            (
                str(table.render_name(xss, record)),
                str(table.render_librenms_id(xss, record)),
                str(table.render_vlans(None, record)),
            )
        )

        assert "<img" not in rendered
        assert "&lt;img" in rendered

    def test_oob_and_shared_lom_badges_are_rendered_after_the_name(self):
        table = _interface_table()
        html = str(
            table.render_name(
                "mgmt0",
                _port(ifName="mgmt0", exists_in_netbox=False, _source="oob", _dedup_conflict=True),
            )
        )

        assert "mgmt0" in html
        assert "From OOB controller" in html
        assert "Shared LOM" in html

    def test_real_librenms_id_states(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("interface-id-states")
        interface = make_interface(device, "Ethernet1")
        table = _interface_table(device)

        missing = str(table.render_librenms_id(42, {"exists_in_netbox": True, "netbox_interface": interface}))
        set_librenms_device_id(interface, 99, "default")
        interface.save()
        interface = type(interface).objects.get(pk=interface.pk)
        mismatch = str(table.render_librenms_id(42, {"exists_in_netbox": True, "netbox_interface": interface}))
        interface.custom_field_data["librenms_id"] = {"default": 42}
        interface.save()
        interface = type(interface).objects.get(pk=interface.pk)
        matched = str(table.render_librenms_id(42, {"exists_in_netbox": True, "netbox_interface": interface}))

        assert "No librenms_id" in missing
        assert "Existing LibreNMS ID: 99" in mismatch
        assert "text-success" in matched


@pytest.mark.django_db
class TestInterfaceTypeMappings:
    def test_exact_mapping_wins_over_type_only_fallback(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        fallback = InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd",
            librenms_speed=None,
            netbox_type="virtual",
        )
        exact = InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd",
            librenms_speed=1_000_000,
            netbox_type="1000base-t",
        )
        table = _interface_table()

        assert table.get_interface_mapping("ethernetCsmacd", 1_000_000) == exact
        assert table.get_interface_mapping("ethernetCsmacd", 10_000) == fallback
        assert table.get_interface_mapping("other", 1_000_000) is None

    def test_mapping_rows_are_snapshotted_once(self, django_assert_num_queries):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd",
            librenms_speed=None,
            netbox_type="virtual",
        )
        table = _interface_table()

        with django_assert_num_queries(1):
            for speed in range(5):
                table.get_interface_mapping("ethernetCsmacd", speed)

    def test_mapping_tooltips_use_real_mapping_data(self):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        mapping = InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd",
            librenms_speed=None,
            netbox_type="1000base-t",
        )
        table = _interface_table()

        display, linked_icon = table.render_mapping_tooltip("ethernetCsmacd", 1000, mapping)
        raw_display, unlinked_icon = table.render_mapping_tooltip("other", 1000, None)

        assert display == "1000base-t"
        assert "mdi-link-variant" in str(linked_icon)
        assert raw_display == "other"
        assert "mdi-link-variant-off" in str(unlinked_icon)


@pytest.mark.django_db
class TestInterfaceVlans:
    def test_real_vlan_assignments_drive_match_colors_and_hidden_inputs(self):
        from ipam.models import VLAN, VLANGroup

        device = make_device("interface-vlans")
        interface = make_interface(device, "Ethernet1")
        group = VLANGroup.objects.create(name="Interface VLAN group", slug="interface-vlan-group")
        untagged = VLAN.objects.create(vid=100, name="Untagged 100", group=group, status="active")
        tagged = VLAN.objects.create(vid=200, name="Tagged 200", group=group, status="active")
        interface.untagged_vlan = untagged
        interface.save()
        interface.tagged_vlans.add(tagged)
        table = _interface_table(device)
        record = _port(
            port_id="010",
            exists_in_netbox=True,
            netbox_interface=interface,
            untagged_vlan=100,
            tagged_vlans=[200],
            vlan_group_map={
                100: {"group_id": str(group.pk), "group_name": group.name},
                200: {"group_id": str(group.pk), "group_name": group.name},
            },
            vlan_groups=[group],
        )

        html = str(table.render_vlans(None, record))

        assert "100(U)" in html
        assert "200(T)" in html
        assert html.count("text-success") >= 2
        assert 'name="vlan_group_10_100"' in html
        assert 'name="vlan_group_10_200"' in html
        assert group.name in html

    def test_empty_and_long_vlan_sets_render_compactly(self):
        table = _interface_table()

        assert str(table.render_vlans(None, _port())) == "—"
        html = str(
            table.render_vlans(
                None,
                _port(
                    exists_in_netbox=False,
                    untagged_vlan=100,
                    tagged_vlans=[200, 300, 400],
                ),
            )
        )
        assert "+1 more" in html

    def test_missing_vlan_warning_and_unresolvable_scope(self):
        table = _interface_table()
        record = _port(
            exists_in_netbox=False,
            untagged_vlan=100,
            missing_vlans=[100],
            sync_target_resolvable=False,
        )

        html = str(table.render_vlans(None, record))

        assert "100(U)" in html
        assert "vlan-edit-btn" not in html
        assert "vlan-group-hidden" not in html


@pytest.mark.django_db
class TestInterfaceRelationships:
    def test_device_table_renders_lag_and_parent_buttons_with_real_routes(self):
        device = make_device("relationship-device")
        interface = make_interface(device, "Ethernet1")
        record = _port(
            netbox_interface=interface,
            lag_sync_status="missing_nb",
            librenms_lag_name="Port-Channel1",
            librenms_lag_port_id=81,
            parent_sync_status="mismatch",
            librenms_parent_name="Ethernet1.100",
            librenms_parent_port_id=82,
        )
        table = _interface_table(device)

        html = str(table.render_parent(None, record))

        assert "LAG Port-Channel1" in html
        assert "Parent Ethernet1.100" in html
        assert "lag-sync-btn" in html
        assert "parent-sync-btn" in html
        assert f'data-object-id="{device.pk}"' in html
        assert (
            reverse(
                "plugins:netbox_librenms_plugin:sync_interface_lag",
                kwargs={"object_type": "device", "object_id": device.pk},
            )
            in html
        )

    def test_migrated_mode_and_unresolvable_targets_keep_status_without_actions(self):
        device = make_device("relationship-donor")
        interface = make_interface(device, "Ethernet1")
        record = _port(
            netbox_interface=interface,
            lag_sync_status="mismatch",
            librenms_lag_name="Port-Channel1",
            librenms_lag_port_id=81,
            parent_sync_status="missing_nb",
            librenms_parent_name="Ethernet1.100",
            librenms_parent_port_id=82,
            parent_target_resolvable=False,
        )
        table = _interface_table(device)
        table.migrated_to_marker = True

        html = str(table.render_parent(None, record))

        assert "LAG Port-Channel1" in html
        assert "Parent Ethernet1.100" in html
        assert "sync-btn" not in html

    def test_vm_table_hides_lag_but_keeps_parent_sync(self):
        from virtualization.models import VMInterface
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        vm = make_vm("relationship-vm")
        interface = VMInterface.objects.create(virtual_machine=vm, name="eth0.100")
        table = LibreNMSVMInterfaceTable(data=[], device=vm, interface_name_field="ifName")
        record = _port(
            netbox_interface=interface,
            lag_sync_status="missing_nb",
            librenms_lag_name="bond0",
            librenms_lag_port_id=80,
            parent_sync_status="missing_nb",
            librenms_parent_name="eth0",
            librenms_parent_port_id=81,
        )

        html = str(table.render_parent(None, record))

        assert "lag-sync-btn" not in html
        assert "parent-sync-btn" in html
        assert 'data-object-type="virtualmachine"' in html
        assert f'data-object-id="{vm.pk}"' in html


@pytest.mark.django_db
class TestInterfaceFormatting:
    def test_main_row_can_bind_by_name_but_oob_row_cannot(self):
        device = make_device("format-interface")
        interface = make_interface(device, "Ethernet1")
        table = _interface_table(device)
        main = _port(name_fallback_allowed=True, _source="main")
        oob = _port(port_id=43, name_fallback_allowed=True, _source="oob")

        main_result = table.format_interface_data(main, device)
        table.format_interface_data(oob, device)

        assert main["netbox_interface"] == interface
        assert main["exists_in_netbox"] is True
        assert oob["netbox_interface"] is None
        assert oob["exists_in_netbox"] is False
        assert set(main_result) == {
            "name",
            "type",
            "speed",
            "mac_address",
            "mtu",
            "enabled",
            "description",
            "vlans",
            "librenms_id",
            "parent",
        }

    def test_matching_alias_is_cleared_in_the_real_formatted_result(self):
        device = make_device("format-alias")
        record = _port(ifAlias="Ethernet1")

        result = _interface_table(device).format_interface_data(record, device)

        assert record["ifAlias"] == ""
        assert ">Ethernet1<" not in str(result["description"])

    def test_constructor_state_is_per_instance(self):
        device = make_device("table-instance-state")
        first = _interface_table(device, interface_name_field="ifDescr", server_key="primary")
        second = _interface_table(device, interface_name_field="ifName")

        assert str(first.columns["name"].accessor) == "ifDescr"
        assert str(second.columns["name"].accessor) == "ifName"
        assert first.server_key == "primary"
        assert second.server_key == "default"
        assert first.row_attrs is not second.row_attrs

    def test_configure_uses_the_real_request_and_paginator(self):
        table = _interface_table(data=[_port()])
        request = RequestFactory().get("/", {"interfaces_per_page": "1"})

        table.configure(request)

        assert table.page.paginator.per_page == 1


@pytest.mark.django_db
class TestVirtualChassisInterfaceTable:
    def _members(self, tag):
        first = make_device(f"{tag}-1")
        second = make_device(f"{tag}-2")
        make_virtual_chassis(f"{tag}-vc", first, second)
        return first, second

    def test_physical_rows_select_the_named_member_and_logical_rows_use_the_viewed_member(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        first, second = self._members("vc-selection")
        table = VCInterfaceTable(data=[], device=first, interface_name_field="ifName")

        physical = str(table.render_device_selection(None, _port(ifName="Ethernet2", ifDescr="Ethernet2")))
        logical = str(table.render_device_selection(None, _port(ifName="Vlan2", ifType="l3ipvlan")))

        assert f'value="{second.pk}" selected' in physical
        assert f'value="{first.pk}" selected' in logical
        assert f'value="{second.pk}" selected' not in logical

    def test_real_matched_interface_keeps_button_and_dropdown_on_the_same_owner(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        first, second = self._members("vc-owner")
        interface = make_interface(second, "Vlan100", iface_type="virtual")
        table = VCInterfaceTable(data=[], device=first, interface_name_field="ifName")
        record = _port(
            ifName="Vlan100",
            ifType="l3ipvlan",
            netbox_interface=interface,
            parent_sync_status="mismatch",
            librenms_parent_name="Bdi1",
            librenms_parent_port_id=60,
        )

        dropdown = str(table.render_device_selection(None, record))
        relationship = str(table.render_parent(None, record))

        assert f'value="{second.pk}" selected' in dropdown
        assert f'data-object-id="{second.pk}"' in relationship

    def test_member_query_is_reused_across_rows(self, django_assert_num_queries):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        first, second = self._members("vc-query")
        table = VCInterfaceTable(data=[], device=first, interface_name_field="ifName")

        with django_assert_num_queries(1):
            owners = [
                table._resolve_row_member_id(_port(ifName=f"Ethernet{position}", ifDescr=f"Ethernet{position}"))
                for position in (1, 2, 1, 2)
            ]
            table.render_device_selection(None, _port(ifName="Ethernet2"))

        assert owners == [first.pk, second.pk, first.pk, second.pk]

    def test_duplicate_display_names_still_use_port_ids_as_form_keys(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        first, _second = self._members("vc-keys")
        records = [
            _port(10, ifName="Ethernet1", ifDescr="Ethernet"),
            _port(11, ifName="Ethernet2", ifDescr="Ethernet"),
        ]
        table = VCInterfaceTable(data=records, device=first, interface_name_field="ifDescr")

        selections = [str(row.get_cell("selection")) for row in table.rows]
        dropdowns = [str(table.render_device_selection(None, record)) for record in records]

        assert 'name="select" value="10"' in selections[0]
        assert 'name="select" value="11"' in selections[1]
        assert 'name="device_selection_10"' in dropdowns[0]
        assert 'name="device_selection_11"' in dropdowns[1]

    def test_member_names_are_escaped_in_dropdown_options(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        first = make_device("vc-escape-first")
        hostile = make_device('<script>alert("xss")</script>')
        make_virtual_chassis("vc-escape", first, hostile)
        table = VCInterfaceTable(data=[], device=first, interface_name_field="ifName")

        html = str(table.render_device_selection(None, _port(ifType="l3ipvlan")))

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_formatting_adds_the_real_member_selector(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        first, _second = self._members("vc-format")
        table = VCInterfaceTable(data=[], device=first, interface_name_field="ifName")

        result = table.format_interface_data(_port(), first)

        assert "vc-member-select" in str(result["device_selection"])


@pytest.mark.django_db
class TestVMInterfaceTable:
    def test_vm_columns_and_routes_match_vm_capabilities(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        vm = make_vm("vm-table-columns")
        table = LibreNMSVMInterfaceTable(data=[], device=vm, interface_name_field="ifName")

        assert "type" not in table.columns
        assert "speed" not in table.columns
        assert "parent" in table.columns
        assert table.sync_object_type == "virtualmachine"


class TestSharedOobBadges:
    def test_interface_module_and_cable_rows_share_oob_badge_semantics(self):
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        interface_html = str(
            _interface_table().render_name(
                "mgmt0",
                _port(ifName="mgmt0", exists_in_netbox=False, _source="oob"),
            )
        )
        module_html = str(object.__new__(LibreNMSModuleTable).render_name("PSU 1", {"_source": "oob", "depth": 0}))
        cable_html = str(object.__new__(LibreNMSCableTable).render_local_port("Gi0/1", {"_source": "oob"}))

        for html in (interface_html, module_html, cable_html):
            assert "From OOB controller" in html

    def test_plain_rows_do_not_receive_oob_badges(self):
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        interface_html = str(
            _interface_table().render_name(
                "Ethernet1",
                _port(exists_in_netbox=False, _source="main"),
            )
        )
        module_html = str(object.__new__(LibreNMSModuleTable).render_name("PSU 1", {"depth": 0}))
        cable_html = str(object.__new__(LibreNMSCableTable).render_local_port("Gi0/1", {"_source": "main"}))

        for html in (interface_html, module_html, cable_html):
            assert "From OOB controller" not in html

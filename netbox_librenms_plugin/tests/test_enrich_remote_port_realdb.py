"""Real-DB coverage for BaseCableTableView.enrich_remote_port librenms_id lookup.

Issue #113 (CodeRabbit): the mock-based enrich_remote_port tests in test_coverage_base_views2.py
use a reported remote_port equal to the interface name, so they still pass if the librenms_id
branch is broken and the method falls back to ``name=remote_port`` — the mocked queryset returns
the same interface for *every* filter call.

These tests exercise the real ORM with the reported port name deliberately DIFFERENT from the
interface name, so a match can only come from the librenms_id custom-field lookup
(``_librenms_id_q``). If the id branch regresses, the name fallback finds nothing and the
assertions fail — a genuine red→green guard.
"""

from unittest.mock import MagicMock

import pytest


def _make_view():
    from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

    view = object.__new__(BaseCableTableView)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    return view


def _make_device(name, slug_suffix):
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="ACME-113", slug="acme-113")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-113", slug="dt-113")
    role, _ = DeviceRole.objects.get_or_create(name="Role-113", slug="role-113")
    site, _ = Site.objects.get_or_create(name="Site-113", slug="site-113")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")


@pytest.mark.django_db
class TestEnrichRemotePortLibrenmsIdRealDB:
    def test_non_vc_finds_by_librenms_id_when_name_differs(self):
        """Non-VC: interface name differs from the reported remote_port, so only the librenms_id lookup can match."""
        from dcim.models import Interface

        device = _make_device("switch-a", "a")
        iface = Interface.objects.create(
            device=device,
            name="ge-0/0/77",
            type="1000base-t",
            custom_field_data={"librenms_id": {"default": 20}},
        )

        view = _make_view()
        # Reported port name deliberately != iface.name; match must come from remote_port_id.
        link = {"remote_port": "reported-different-name", "remote_port_id": 20}

        result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == iface.pk
        assert result["remote_port_name"] == "ge-0/0/77"

    def test_non_vc_returns_no_interface_when_id_misses_and_name_differs(self):
        """When the librenms_id misses and the name differs, nothing matches — confirms the positive test above is genuinely driven by the id lookup, not an unconditional return."""
        from dcim.models import Interface

        device = _make_device("switch-a", "a")
        Interface.objects.create(
            device=device,
            name="ge-0/0/77",
            type="1000base-t",
            custom_field_data={"librenms_id": {"default": 20}},
        )

        view = _make_view()
        link = {"remote_port": "reported-different-name", "remote_port_id": 999}

        result = view.enrich_remote_port(link, device)

        assert "netbox_remote_interface_id" not in result

    def test_vc_member_finds_by_librenms_id_when_name_differs(self):
        """VC: the reported port keeps the slot prefix (Gi1/...) so member selection resolves vc_position=1, but the interface name differs — so only the librenms_id lookup can match."""
        from dcim.models import Interface, VirtualChassis

        member = _make_device("vc-member", "vc")
        vc = VirtualChassis.objects.create(name="vc-113")
        member.virtual_chassis = vc
        member.vc_position = 1
        member.save()

        iface = Interface.objects.create(
            device=member,
            name="xe-1/0/5",
            type="10gbase-x-sfpp",
            custom_field_data={"librenms_id": {"default": 21}},
        )

        view = _make_view()
        # "Gi1/0/99" → slot 1 (member_pos), but != iface.name "xe-1/0/5".
        link = {"remote_port": "Gi1/0/99", "remote_port_id": 21}

        result = view.enrich_remote_port(link, member)

        assert result["netbox_remote_interface_id"] == iface.pk
        assert result["remote_port_name"] == "xe-1/0/5"


@pytest.mark.django_db
class TestVCCableTableMemberQueryCount:
    """VCCableTable must fetch the VC member set once (in __init__), not per row: render_device_selection previously ran members.all() plus a members.get (via get_virtual_chassis_member) for every cable row."""

    def test_render_device_selection_does_not_query_members_per_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from dcim.models import VirtualChassis
        from netbox_librenms_plugin.tables.cables import VCCableTable
        from netbox_librenms_plugin.utils import assign_cable_row_ids

        m1 = _make_device("vc-cable-m1", "c1")
        vc = VirtualChassis.objects.create(name="vc-cables")
        m1.virtual_chassis = vc
        m1.vc_position = 1
        m1.save()
        m2 = _make_device("vc-cable-m2", "c2")
        m2.virtual_chassis = vc
        m2.vc_position = 2
        m2.save()

        rows = [
            {"local_port": f"Gi1/0/{i}", "local_port_id": i, "remote_port": "", "remote_device": "", "cable_status": ""}
            for i in range(1, 6)  # 5 rows
        ]
        # Build the table OUTSIDE the capture so the one-time member fetch isn't counted.
        # The view assigns row identities before the table renders; do the same here.
        rows = assign_cable_row_ids(rows)
        table = VCCableTable(rows, device=m1)

        with CaptureQueriesContext(connection) as ctx:
            for row in rows:
                table.render_device_selection(row["row_id"], row)

        member_queries = [q for q in ctx.captured_queries if "dcim_device" in q["sql"].lower()]
        # Every row renders from the cached member set → no per-row member queries.
        assert member_queries == [], f"render_device_selection queried members per row: {len(member_queries)} queries"


@pytest.mark.django_db
class TestModuleTableInstalledModulePrefetch:
    """LibreNMSModuleTable must batch-load installed modules (with interface templates) in __init__, so render_actions' VC 'Report VC issue' diagnostic adds no per-row Module/interfacetemplate query."""

    def test_render_actions_does_not_query_installed_modules_per_row(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from dcim.models import Module, ModuleBay, ModuleType, VirtualChassis
        from netbox_librenms_plugin.tables.modules import VCModuleTable

        member = _make_device("vc-mod-m1", "modf4")
        vc = VirtualChassis.objects.create(name="vc-mod-f4")
        member.virtual_chassis = vc
        member.vc_position = 1
        member.save()
        mt = ModuleType.objects.create(manufacturer=member.device_type.manufacturer, model="LC-F4-48x10G")

        rows = []
        for i in range(1, 4):  # 3 installed modules → 3 rows
            bay = ModuleBay.objects.create(device=member, name=f"Slot{i}")
            module = Module.objects.create(device=member, module_bay=bay, module_type=mt)
            rows.append({"installed_module_id": module.pk, "status": "Matched", "name": f"Slot{i}"})

        # Build OUTSIDE the capture so the one-time batch prefetch isn't counted.
        table = VCModuleTable(rows, device=member, has_write_permission=True, can_add_module=True)

        with CaptureQueriesContext(connection) as ctx:
            for row in rows:
                table.render_actions(None, row)

        per_row_queries = [
            q for q in ctx.captured_queries if '"dcim_module"' in q["sql"] or '"dcim_interfacetemplate"' in q["sql"]
        ]
        assert per_row_queries == [], (
            f"render_actions queried installed modules/templates per row: {len(per_row_queries)} queries"
        )

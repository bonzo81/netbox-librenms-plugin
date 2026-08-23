"""
Comprehensive coverage tests for tables/device_status.py and tables/interfaces.py.

Targets ≥95% coverage of both modules.

Conventions:
- Plain pytest classes (no Django TestCase)
- No @pytest.mark.django_db — all DB interactions mocked
- Inline imports inside test methods
- object.__new__(TableClass) for render method tests where __init__ is complex
- MagicMock for all external dependencies
- assert x == y style assertions

Exception: TestRenderLibreNMSId runs against real Interface rows (@pytest.mark.django_db) — the
LibreNMS-id cell's colour depends on the real get_librenms_device_id custom-field read, which a
MagicMock interface would let drift silently.
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device_import_table(data=None, order_by=None):
    """
    Instantiate DeviceImportTable with patched DB querysets.
    Returns the table instance.
    """
    from netbox_librenms_plugin.tables.device_status import DeviceImportTable

    mock_cluster_qs = MagicMock()
    mock_cluster_qs.__iter__ = lambda self: iter([])
    mock_cluster_qs.__bool__ = lambda self: False

    mock_role_qs = MagicMock()
    mock_role_qs.__iter__ = lambda self: iter([])
    mock_role_qs.__bool__ = lambda self: False

    with (
        patch("netbox_librenms_plugin.tables.device_status.VirtualMachine") as _mock_vm_cls,
        patch("dcim.models.DeviceRole") as mock_role_model,
        patch("virtualization.models.Cluster") as mock_cluster_model,
        patch("django.urls.reverse", return_value="/fake/url/"),
    ):
        mock_cluster_model.objects.all.return_value.order_by.return_value = list(mock_cluster_qs)
        mock_role_model.objects.all.return_value.order_by.return_value = list(mock_role_qs)

        kwargs = {}
        if order_by is not None:
            kwargs["order_by"] = order_by

        table = DeviceImportTable(data=data or [], **kwargs)
        table._cached_clusters = []
        table._cached_roles = []
        return table


def _make_interface_table(device=None, interface_name_field="ifName", vlan_groups=None, server_key="default"):
    """
    Instantiate LibreNMSInterfaceTable with patched dependencies.
    Returns the table instance.
    """
    from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

    mock_device = device or MagicMock()

    with patch(
        "netbox_librenms_plugin.tables.interfaces.get_interface_name_field",
        return_value=interface_name_field,
    ):
        table = LibreNMSInterfaceTable(
            data=[],
            device=mock_device,
            interface_name_field=interface_name_field,
            vlan_groups=vlan_groups or [],
            server_key=server_key,
        )
    return table


# ===========================================================================
# DeviceStatusTable tests
# ===========================================================================


class TestDeviceStatusTableRenderLibreNMSStatus:
    """Tests for DeviceStatusTable.render_librenms_status()."""

    def _make_table(self):
        from netbox_librenms_plugin.tables.device_status import DeviceStatusTable

        table = object.__new__(DeviceStatusTable)
        return table

    def test_value_true_renders_found(self):
        table = self._make_table()

        record = MagicMock()
        record.pk = 1
        record.virtual_chassis = None

        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/sync/1/"):
            result = str(table.render_librenms_status(value=True, record=record))

        assert "text-success" in result
        assert "Found" in result
        assert "mdi-check-circle" in result
        assert "/sync/1/" in result

    def test_value_false_renders_not_found(self):
        table = self._make_table()

        record = MagicMock()
        record.pk = 2
        record.virtual_chassis = None

        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/sync/2/"):
            result = str(table.render_librenms_status(value=False, record=record))

        assert "text-danger" in result
        assert "Not Found" in result
        assert "mdi-close-circle" in result

    def test_value_none_renders_unknown(self):
        table = self._make_table()

        record = MagicMock()
        record.pk = 3
        record.virtual_chassis = None

        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/sync/3/"):
            result = str(table.render_librenms_status(value=None, record=record))

        assert "text-secondary" in result
        assert "Unknown" in result
        assert "mdi-help-circle" in result

    def test_vc_member_redirects_to_sync_device(self):
        table = self._make_table()

        sync_device = MagicMock()
        sync_device.pk = 99
        sync_device.name = "vc-master"

        record = MagicMock()
        record.pk = 10
        record.virtual_chassis = MagicMock()

        def fake_reverse(name, kwargs=None):
            if kwargs and kwargs.get("pk") == 99:
                return "/sync/99/"
            return f"/sync/{kwargs['pk']}/" if kwargs else "/sync/"

        with (
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=fake_reverse),
            patch(
                "netbox_librenms_plugin.tables.device_status.get_librenms_sync_device",
                return_value=sync_device,
            ),
        ):
            result = str(table.render_librenms_status(value=True, record=record))

        assert "text-info" in result
        assert "vc-master" in result
        assert "/sync/99/" in result
        assert "mdi-server-network" in result

    def test_vc_member_same_pk_falls_through(self):
        """When VC master is the same device, show normal status."""
        table = self._make_table()

        record = MagicMock()
        record.pk = 10
        record.virtual_chassis = MagicMock()

        with (
            patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/sync/10/"),
            patch(
                "netbox_librenms_plugin.tables.device_status.get_librenms_sync_device",
                return_value=record,  # sync_device.pk == record.pk
            ),
        ):
            result = str(table.render_librenms_status(value=True, record=record))

        assert "text-success" in result
        assert "Found" in result

    def test_vc_sync_device_none_falls_through(self):
        """When get_librenms_sync_device returns None, show normal status."""
        table = self._make_table()

        record = MagicMock()
        record.pk = 10
        record.virtual_chassis = MagicMock()

        with (
            patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/sync/10/"),
            patch(
                "netbox_librenms_plugin.tables.device_status.get_librenms_sync_device",
                return_value=None,
            ),
        ):
            result = str(table.render_librenms_status(value=False, record=record))

        assert "text-danger" in result


# ===========================================================================
# DeviceImportTable._sort_data tests
# ===========================================================================


class TestDeviceImportTableSortData:
    """Tests for DeviceImportTable._sort_data()."""

    def _make_table_with_data(self, data, order_by=None):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        table = object.__new__(DeviceImportTable)
        # Simulate what __init__ sets
        inner = MagicMock()
        inner.data = list(data)

        class FakeTableData:
            def __init__(self, items):
                self.data = list(items)

        table.data = FakeTableData(data)
        table._order_by = order_by or []
        return table

    def test_empty_data_returns_early(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        table = object.__new__(DeviceImportTable)
        table.data = None
        table._order_by = ["hostname"]
        # Should not raise
        table._sort_data()

    @pytest.mark.django_db
    def test_construction_time_order_by_sorts_case_insensitively(self):
        """order_by passed to the constructor must apply _sort_data's case-insensitive sort — the import list view wires request.GET["sort"] straight into DeviceImportTable(...). django_tables2's native ordering also sorts on construction but case-SENSITIVELY ("Zulu" < "alpha"), so this catches the __init__ sort block going dead (the direct _sort_data tests above bypass __init__ and cannot)."""
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        data = [
            {"device_id": 1, "hostname": "Zulu"},
            {"device_id": 2, "hostname": "alpha"},
            {"device_id": 3, "hostname": "Mango"},
        ]
        table = DeviceImportTable(data=data, order_by="hostname")
        assert [row["hostname"] for row in table.data.data] == ["alpha", "Mango", "Zulu"]

    def test_sort_by_hostname_ascending(self):
        data = [
            {"hostname": "zebra", "sysName": "z"},
            {"hostname": "apple", "sysName": "a"},
            {"hostname": "mango", "sysName": "m"},
        ]
        table = self._make_table_with_data(data, order_by=["hostname"])
        table._sort_data()
        assert table.data.data[0]["hostname"] == "apple"
        assert table.data.data[-1]["hostname"] == "zebra"

    def test_sort_by_hostname_descending(self):
        data = [
            {"hostname": "apple"},
            {"hostname": "zebra"},
            {"hostname": "mango"},
        ]
        table = self._make_table_with_data(data, order_by=["-hostname"])
        table._sort_data()
        assert table.data.data[0]["hostname"] == "zebra"
        assert table.data.data[-1]["hostname"] == "apple"

    def test_sort_by_sysname(self):
        data = [
            {"sysName": "zz"},
            {"sysName": "aa"},
        ]
        table = self._make_table_with_data(data, order_by=["sysname"])
        table._sort_data()
        assert table.data.data[0]["sysName"] == "aa"

    def test_sort_by_location(self):
        data = [
            {"location": "DC2"},
            {"location": "DC1"},
        ]
        table = self._make_table_with_data(data, order_by=["location"])
        table._sort_data()
        assert table.data.data[0]["location"] == "DC1"

    def test_sort_by_hardware(self):
        data = [
            {"hardware": "Z-Switch"},
            {"hardware": "A-Router"},
        ]
        table = self._make_table_with_data(data, order_by=["hardware"])
        table._sort_data()
        assert table.data.data[0]["hardware"] == "A-Router"

    def test_unknown_field_skips_sort(self):
        data = [{"hostname": "b"}, {"hostname": "a"}]
        table = self._make_table_with_data(data, order_by=["unknown_field"])
        # Should not raise; data unchanged
        table._sort_data()
        assert table.data.data[0]["hostname"] == "b"

    def test_sort_handles_none_values(self):
        data = [
            {"hostname": None},
            {"hostname": "apple"},
        ]
        table = self._make_table_with_data(data, order_by=["hostname"])
        table._sort_data()
        # None values are treated as "" → they sort first
        assert table.data.data[0]["hostname"] is None

    def test_sort_falls_back_to_plain_list(self):
        """When data.data.sort raises AttributeError, fall back to sorting plain list."""
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        table = object.__new__(DeviceImportTable)
        # Make data a plain list (no .data attribute on the inner)
        table.data = [{"hostname": "z"}, {"hostname": "a"}]
        table._order_by = ["hostname"]
        table._sort_data()
        assert table.data[0]["hostname"] == "a"

    def test_order_by_is_string_not_list(self):
        """Handles order_by as a single string (not a list)."""
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        table = object.__new__(DeviceImportTable)

        class FakeData:
            def __init__(self):
                self.data = [{"hostname": "z"}, {"hostname": "a"}]

        table.data = FakeData()
        table._order_by = "hostname"  # string, not list
        table._sort_data()
        assert table.data.data[0]["hostname"] == "a"


# ===========================================================================
# DeviceImportTable render_selection tests
# ===========================================================================


class TestDeviceImportTableRenderSelection:
    """Tests for DeviceImportTable.render_selection()."""

    def _table(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        return t

    def test_can_import_true_renders_enabled_checkbox(self):
        table = self._table()
        record = {
            "device_id": 42,
            "hostname": "myhost",
            "sysName": "mysys",
            "_validation": {"can_import": True},
        }
        result = str(table.render_selection(value=42, record=record))
        assert 'type="checkbox"' in result
        assert "device-select" in result
        assert 'value="42"' in result
        assert "disabled" not in result
        assert 'data-hostname="myhost"' in result
        assert 'data-sysname="mysys"' in result

    def test_can_import_false_renders_disabled_checkbox(self):
        table = self._table()
        record = {
            "device_id": 7,
            "hostname": "x",
            "sysName": "y",
            "_validation": {"can_import": False},
        }
        result = str(table.render_selection(value=7, record=record))
        assert "disabled" in result
        assert "device-select" not in result
        assert "Cannot import" in result

    def test_missing_validation_defaults_to_disabled(self):
        table = self._table()
        record = {"device_id": 1}
        result = str(table.render_selection(value=1, record=record))
        assert "disabled" in result


# ===========================================================================
# DeviceImportTable render_hostname tests
# ===========================================================================


class TestDeviceImportTableRenderHostname:
    def test_wraps_in_strong(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        result = str(t.render_hostname(value="myhost", record={}))
        assert "<strong>myhost</strong>" in result


# ===========================================================================
# DeviceImportTable render_netbox_cluster tests
# ===========================================================================


class TestDeviceImportTableRenderNetboxCluster:
    """Tests for DeviceImportTable.render_netbox_cluster()."""

    def _table(self, clusters=None):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        t._cached_clusters = clusters or []
        return t

    def test_existing_vm_shows_cluster_badge(self):
        from virtualization.models import VirtualMachine

        table = self._table()

        cluster = MagicMock()
        cluster.name = "VMware-01"
        existing = MagicMock(spec=VirtualMachine)
        existing.__class__ = VirtualMachine
        existing.cluster = cluster

        record = {
            "device_id": 1,
            "_validation": {"existing_device": existing},
        }

        with patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine):
            result = str(table.render_netbox_cluster(value=1, record=record))

        assert "VMware-01" in result
        assert "badge" in result

    def test_existing_device_shows_not_vm(self):
        from virtualization.models import VirtualMachine

        table = self._table()

        # Plain MagicMock (not a VirtualMachine instance) triggers the Device branch
        existing = MagicMock()

        record = {
            "device_id": 1,
            "_validation": {"existing_device": existing},
        }

        with patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine):
            result = str(table.render_netbox_cluster(value=1, record=record))

        assert "Device (not VM)" in result

    def test_no_existing_shows_dropdown(self):

        cluster1 = MagicMock()
        cluster1.pk = 10
        cluster1.name = "Cluster-A"

        table = self._table(clusters=[cluster1])
        record = {
            "device_id": 5,
            "_validation": {
                "existing_device": None,
                "cluster": {"found": False, "cluster": None},
            },
        }

        with patch("django.urls.reverse", return_value="/cluster-update/5/"):
            result = str(table.render_netbox_cluster(value=5, record=record))

        assert "Cluster-A" in result
        assert "cluster-select" in result
        assert "/cluster-update/5/" in result

    def test_selected_cluster_has_selected_attribute(self):

        cluster1 = MagicMock()
        cluster1.pk = 10
        cluster1.name = "Cluster-A"

        table = self._table(clusters=[cluster1])

        selected_cluster = MagicMock()
        selected_cluster.pk = 10

        record = {
            "device_id": 5,
            "_validation": {
                "existing_device": None,
                "cluster": {"found": True, "cluster": selected_cluster},
            },
        }

        with patch("django.urls.reverse", return_value="/cluster-update/5/"):
            result = str(table.render_netbox_cluster(value=5, record=record))

        assert "selected" in result

    def test_vc_detection_flag_in_url(self):

        table = self._table()
        record = {
            "device_id": 5,
            "_validation": {
                "existing_device": None,
                "cluster": {"found": False, "cluster": None},
                "_vc_detection_enabled": True,
            },
        }

        with patch("django.urls.reverse", return_value="/cluster-update/5/"):
            result = str(table.render_netbox_cluster(value=5, record=record))

        assert "enable_vc_detection=true" in result


class TestDeviceImportTableRowSelectsServerKey:
    """The role/cluster/rack row selects must post the import page's server_key.

    Their hx-posts reach DeviceRole/Cluster/RackUpdateView, which rebind to the POSTed
    server_key; without an hx-vals carrying it the rebind falls back to the GLOBAL
    selected server and re-validates/caches the wrong server's device for the row.
    """

    def _table(self, server_key="secondary"):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        t._cached_clusters = []
        t._cached_roles = []
        t.server_key = server_key
        return t

    def _record(self):
        rack = MagicMock()
        rack.pk = 3
        rack.name = "R1"
        rack.location = None
        return {
            "device_id": 5,
            "_validation": {
                "existing_device": None,
                "import_as_vm": False,
                "cluster": {"found": False, "cluster": None},
                "device_role": {"found": False, "role": None},
                "site": {"found": True},
                "rack": {"rack": None, "available_racks": [rack]},
            },
        }

    @pytest.mark.parametrize("renderer", ["render_netbox_cluster", "render_netbox_role", "render_netbox_rack"])
    def test_select_carries_server_key_hx_vals(self, renderer):
        table = self._table()
        with patch("django.urls.reverse", return_value="/row-update/5/"):
            result = str(getattr(table, renderer)(value=5, record=self._record()))
        assert "hx-vals=" in result and "server_key" in result and "secondary" in result, result

    @pytest.mark.parametrize("renderer", ["render_netbox_cluster", "render_netbox_role", "render_netbox_rack"])
    def test_no_server_key_renders_no_hx_vals(self, renderer):
        """Single-server pages (no server_key threaded) keep the select unchanged."""
        table = self._table(server_key=None)
        with patch("django.urls.reverse", return_value="/row-update/5/"):
            result = str(getattr(table, renderer)(value=5, record=self._record()))
        assert "hx-vals" not in result


# ===========================================================================
# DeviceImportTable render_netbox_role tests
# ===========================================================================


class TestDeviceImportTableRenderNetboxRole:
    """Tests for DeviceImportTable.render_netbox_role()."""

    def _table(self, roles=None):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        t._cached_roles = roles or []
        return t

    def test_existing_with_role_shows_badge(self):
        table = self._table()

        role = MagicMock()
        role.name = "Access Switch"
        role.color = "0d6efd"
        existing = MagicMock()
        existing.role = role

        record = {
            "device_id": 1,
            "_validation": {
                "existing_device": existing,
                "import_as_vm": False,
            },
        }

        result = str(table.render_netbox_role(value=1, record=record))

        assert "Access Switch" in result
        assert "0d6efd" in result
        assert "badge" in result

    def test_existing_with_role_fallback_color(self):
        """When role has no color attribute, uses fallback."""
        table = self._table()

        role = MagicMock(spec=[])  # No 'color' attribute in spec
        role.name = "Switch"
        existing = MagicMock()
        existing.role = role

        record = {
            "device_id": 1,
            "_validation": {
                "existing_device": existing,
                "import_as_vm": False,
            },
        }

        result = str(table.render_netbox_role(value=1, record=record))
        assert "Switch" in result

    def test_existing_no_role_shows_dropdown(self):
        table = self._table()
        existing = MagicMock()
        existing.role = None
        record = {
            "device_id": 1,
            "_validation": {
                "existing_device": existing,
                "import_as_vm": False,
                "device_role": {"found": False, "role": None},
            },
        }
        with patch("django.urls.reverse", return_value="/role-update/1/"):
            result = str(table.render_netbox_role(value=1, record=record))
        assert "device-role-select" in result

    def test_no_existing_device_shows_dropdown(self):
        role1 = MagicMock()
        role1.pk = 3
        role1.name = "Switch"
        table = self._table(roles=[role1])
        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": None,
                "import_as_vm": False,
                "device_role": {"found": False, "role": None},
            },
        }
        with patch("django.urls.reverse", return_value="/role-update/2/"):
            result = str(table.render_netbox_role(value=2, record=record))
        assert "Switch" in result
        assert "Select Role" in result
        assert "device-role-select" in result

    def test_vm_import_shows_optional_placeholder(self):
        table = self._table()
        record = {
            "device_id": 3,
            "_validation": {
                "existing_device": None,
                "import_as_vm": True,
                "device_role": {"found": False, "role": None},
            },
        }
        with patch("django.urls.reverse", return_value="/role-update/3/"):
            result = str(table.render_netbox_role(value=3, record=record))
        assert "Optional" in result

    def test_selected_role_has_selected_attribute(self):
        role1 = MagicMock()
        role1.pk = 3
        role1.name = "Switch"
        table = self._table(roles=[role1])

        selected_role = MagicMock()
        selected_role.pk = 3

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": None,
                "import_as_vm": False,
                "device_role": {"found": True, "role": selected_role},
            },
        }
        with patch("django.urls.reverse", return_value="/role-update/2/"):
            result = str(table.render_netbox_role(value=2, record=record))
        assert "selected" in result

    def test_vc_detection_flag(self):
        table = self._table()
        record = {
            "device_id": 4,
            "_validation": {
                "existing_device": None,
                "import_as_vm": False,
                "device_role": {"found": False, "role": None},
                "_vc_detection_enabled": True,
            },
        }
        with patch("django.urls.reverse", return_value="/role-update/4/"):
            result = str(table.render_netbox_role(value=4, record=record))
        assert "enable_vc_detection=true" in result


# ===========================================================================
# DeviceImportTable render_netbox_rack tests
# ===========================================================================


class TestDeviceImportTableRenderNetboxRack:
    """Tests for DeviceImportTable.render_netbox_rack()."""

    def _table(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        return t

    def test_is_vm_shows_na(self):
        table = self._table()
        record = {
            "device_id": 1,
            "_validation": {"import_as_vm": True, "existing_device": None},
        }
        result = str(table.render_netbox_rack(value=1, record=record))
        assert "N/A" in result
        assert "VM" in result

    def test_existing_with_rack_shows_badge(self):
        table = self._table()
        rack = MagicMock()
        rack.name = "Rack-A"
        rack.location = MagicMock()
        rack.location.name = "Row-1"
        existing = MagicMock()
        existing.rack = rack
        record = {
            "device_id": 1,
            "_validation": {
                "import_as_vm": False,
                "existing_device": existing,
            },
        }
        result = str(table.render_netbox_rack(value=1, record=record))
        assert "Row-1" in result
        assert "Rack-A" in result
        assert "badge" in result

    def test_existing_with_rack_no_location(self):
        table = self._table()
        rack = MagicMock()
        rack.name = "Rack-B"
        rack.location = None
        existing = MagicMock()
        existing.rack = rack
        record = {
            "device_id": 1,
            "_validation": {
                "import_as_vm": False,
                "existing_device": existing,
            },
        }
        result = str(table.render_netbox_rack(value=1, record=record))
        assert "No Location" in result
        assert "Rack-B" in result

    def test_existing_without_rack_shows_no_rack(self):
        table = self._table()
        existing = MagicMock()
        existing.rack = None
        record = {
            "device_id": 1,
            "_validation": {
                "import_as_vm": False,
                "existing_device": existing,
            },
        }
        result = str(table.render_netbox_rack(value=1, record=record))
        assert "No rack" in result

    def test_no_site_found_shows_dash(self):
        table = self._table()
        record = {
            "device_id": 1,
            "_validation": {
                "import_as_vm": False,
                "existing_device": None,
                "site": {"found": False},
            },
        }
        result = str(table.render_netbox_rack(value=1, record=record))
        assert "--" in result

    def test_site_found_shows_dropdown(self):
        table = self._table()
        rack1 = MagicMock()
        rack1.pk = 5
        rack1.name = "Rack-A"
        rack1.location = MagicMock()
        rack1.location.name = "Row-1"
        record = {
            "device_id": 2,
            "_validation": {
                "import_as_vm": False,
                "existing_device": None,
                "site": {"found": True},
                "rack": {"available_racks": [rack1], "rack": None},
            },
        }
        with patch("django.urls.reverse", return_value="/rack-update/2/"):
            result = str(table.render_netbox_rack(value=2, record=record))
        assert "Row-1 - Rack-A" in result
        assert "rack-select" in result

    def test_selected_rack_has_selected_attribute(self):
        table = self._table()
        rack1 = MagicMock()
        rack1.pk = 5
        rack1.name = "Rack-A"
        rack1.location = MagicMock()
        rack1.location.name = "Row-1"
        selected_rack = MagicMock()
        selected_rack.pk = 5
        record = {
            "device_id": 2,
            "_validation": {
                "import_as_vm": False,
                "existing_device": None,
                "site": {"found": True},
                "rack": {"available_racks": [rack1], "rack": selected_rack},
            },
        }
        with patch("django.urls.reverse", return_value="/rack-update/2/"):
            result = str(table.render_netbox_rack(value=2, record=record))
        assert "selected" in result

    def test_vc_detection_flag(self):
        table = self._table()
        record = {
            "device_id": 3,
            "_validation": {
                "import_as_vm": False,
                "existing_device": None,
                "site": {"found": True},
                "rack": {"available_racks": [], "rack": None},
                "_vc_detection_enabled": True,
            },
        }
        with patch("django.urls.reverse", return_value="/rack-update/3/"):
            result = str(table.render_netbox_rack(value=3, record=record))
        assert "enable_vc_detection=true" in result


# ===========================================================================
# DeviceImportTable render_virtual_chassis tests
# ===========================================================================


class TestDeviceImportTableRenderVirtualChassis:
    """Tests for DeviceImportTable.render_virtual_chassis()."""

    def _table(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        return t

    def test_not_a_stack_shows_dash(self):
        table = self._table()
        record = {
            "device_id": 1,
            "_validation": {"virtual_chassis": {"is_stack": False, "member_count": 0}},
        }
        result = str(table.render_virtual_chassis(value=1, record=record))
        assert "—" in result or "&mdash;" in result or "text-muted" in result

    def test_single_member_shows_dash(self):
        table = self._table()
        record = {
            "device_id": 1,
            "_validation": {"virtual_chassis": {"is_stack": True, "member_count": 1}},
        }
        result = str(table.render_virtual_chassis(value=1, record=record))
        assert "text-muted" in result

    def test_detection_error_shows_error_button(self):
        table = self._table()
        record = {
            "device_id": 5,
            "_validation": {
                "virtual_chassis": {
                    "is_stack": True,
                    "member_count": 2,
                    "detection_error": "timeout",
                }
            },
        }
        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/vc-details/5/"):
            result = str(table.render_virtual_chassis(value=5, record=record))
        assert "Error" in result
        assert "btn-outline-warning" in result

    def test_multi_member_shows_count_button(self):
        table = self._table()
        table.server_key = "secondary"
        record = {
            "device_id": 6,
            "_validation": {
                "virtual_chassis": {
                    "is_stack": True,
                    "member_count": 3,
                    "detection_error": None,
                }
            },
        }
        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/vc-details/6/"):
            result = str(table.render_virtual_chassis(value=6, record=record))
        assert "3 members" in result
        assert "btn-outline-info" in result
        assert 'hx-get="/vc-details/6/?server_key=secondary"' in result

    def test_no_vc_data_shows_dash(self):
        """When virtual_chassis key is missing from validation, shows dash."""
        table = self._table()
        record = {
            "device_id": 7,
            "_validation": {},
        }
        result = str(table.render_virtual_chassis(value=7, record=record))
        assert "text-muted" in result


# ===========================================================================
# DeviceImportTable render_actions tests
# ===========================================================================


class TestDeviceImportTableRenderActions:
    """Tests for DeviceImportTable.render_actions()."""

    def _table(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        t = object.__new__(DeviceImportTable)
        return t

    def _fake_reverse(self, name, kwargs=None):
        pk = (kwargs or {}).get("pk", "")
        device_id = (kwargs or {}).get("device_id", "")
        return f"/fake/{name}/{pk or device_id}/"

    def test_existing_vm_shows_view_vm_button(self):
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=VirtualMachine)
        existing.__class__ = VirtualMachine
        existing.pk = 99

        record = {
            "device_id": 1,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=1, record=record))

        assert "View VM in NetBox" in result
        assert "mdi-open-in-new" in result

    def test_existing_device_shows_view_device_button(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "View Device in NetBox" in result

    def test_validation_details_url_carries_server_key(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()
        table.server_key = "secondary"  # the server the import page was rendered against

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        # The validation-details modal hx-get must carry the rendered server_key so the
        # modal-open GET fetches from the import's server, not the global selected_server.
        assert "server_key=secondary" in result

    def test_existing_device_type_mismatch_shows_conflict_danger(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "hostname",
                "serial_action": None,
                "device_type_mismatch": True,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-danger" in result
        assert "Conflict" in result

    def test_existing_hostname_match_type_shows_conflict_warning(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "hostname",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-warning" in result

    def test_existing_serial_match_with_action_shows_conflict_warning(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "serial",
                "serial_action": "update_serial",
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-warning" in result
        assert "Conflict" in result

    def test_existing_serial_match_oob_already_linked_is_informational_not_conflict(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "serial",
                "serial_action": "oob_already_linked",
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        # Informational state (re-import updates the existing OOB entry) — not a warning "Conflict".
        assert "btn-outline-warning" not in result
        assert "btn-outline-success" in result

    def test_existing_name_sync_shows_details_warning(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": True,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-warning" in result
        assert "Details" in result

    def test_existing_librenms_id_needs_migration_shows_legacy(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": True,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-warning" in result
        assert "Legacy ID" in result

    def test_existing_clean_match_shows_success(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-success" in result

    def test_is_ready_shows_import_button(self):

        table = self._table()

        record = {
            "device_id": 10,
            "hostname": "myhost",
            "sysName": "mysys",
            "_validation": {
                "existing_device": None,
                "is_ready": True,
                "can_import": True,
                "virtual_chassis": None,
            },
        }

        with patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse):
            result = str(table.render_actions(value=10, record=record))

        assert "device-import-btn" in result
        assert "device-ready" in result
        assert "Import" in result

    def test_can_import_with_warnings_shows_review(self):
        table = self._table()

        record = {
            "device_id": 11,
            "hostname": "h",
            "sysName": "s",
            "_validation": {
                "existing_device": None,
                "is_ready": False,
                "can_import": True,
                "virtual_chassis": None,
            },
        }

        with patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse):
            result = str(table.render_actions(value=11, record=record))

        assert "Review" in result
        assert "btn-warning" in result

    def test_cannot_import_shows_disabled_button(self):
        table = self._table()

        record = {
            "device_id": 12,
            "hostname": "h",
            "sysName": "s",
            "_validation": {
                "existing_device": None,
                "is_ready": False,
                "can_import": False,
                "virtual_chassis": None,
            },
        }

        with patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse):
            result = str(table.render_actions(value=12, record=record))

        assert "disabled" in result
        assert "btn-outline-danger" in result
        assert "Details" in result

    def test_existing_librenms_id_sync_needed(self):
        """librenms_id match with serial_action in (update_serial, conflict) shows Details."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": "conflict",
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-warning" in result
        assert "Details" in result

    def test_existing_oob_candidate_shows_add_as_oob_button(self):
        """serial_action == 'oob_candidate' renders the purple "Add as OOB controller" button."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "serial",
                "serial_action": "oob_candidate",
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-purple" in result
        assert "mdi-chip" in result
        assert "Add as OOB controller" in result

    def test_existing_oob_linked_shows_linked_oob_button_with_paired_host(self):
        """existing_match_type == 'librenms_oob' renders the info "Linked as OOB controller" button and surfaces the paired host id in the title."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_oob",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "existing_librenms_link": {"host_id": 42},
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-info" in result
        assert "mdi-chip" in result
        assert "Linked as OOB controller (paired host: LibreNMS #42)" in result

    def test_existing_oob_linked_malformed_paired_host_id_omitted(self):
        """A malformed paired host_id (bool/float) must use the strict coercion the host-half branch uses, not int(): a boolean True must NOT render a bogus 'LibreNMS #1' — the title falls back to the plain 'Linked as OOB controller'."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_oob",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "existing_librenms_link": {"host_id": True},  # malformed (bool) — int() would give 1
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "Linked as OOB controller" in result
        # The malformed id must NOT surface as a paired host number.
        assert "paired host: LibreNMS #" not in result

    def test_existing_librenms_link_non_dict_does_not_crash_render(self):
        """A malformed ``existing_librenms_link`` that isn't a dict (e.g. a legacy bare int) must not crash the actions render."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_oob",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "existing_librenms_link": "garbage-not-a-dict",  # malformed payload
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "Linked as OOB controller" in result
        assert "paired host: LibreNMS #" not in result

    def test_existing_paired_host_shows_host_button(self):
        """A librenms_id match whose link carries an oob_id distinct from the host id renders the info "Host" button (the host half of a host/OOB pair), escaping the oob type."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 55

        record = {
            "device_id": 2,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                # oob_type comes from a user-editable custom field — use a value that REQUIRES
                # escaping so the assertion below actually proves render_actions() escapes it.
                "existing_librenms_link": {"host_id": 42, "oob_id": 99, "oob_type": "<idrac>"},
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=2, record=record))

        assert "btn-outline-info" in result
        assert "mdi-server-network" in result
        # The oob_type must be HTML-escaped in the title; the raw value must not leak through.
        assert "Linked as host (paired OOB: LibreNMS #99, &lt;idrac&gt;)" in result
        assert "<idrac>" not in result

    def test_malformed_paired_oob_id_does_not_render_host_state(self):
        """A malformed paired oob_id coerces to None and must NOT render the paired-host state with a bogus 'LibreNMS #bad' title — it should fall through to the generic details button."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 56

        record = {
            "device_id": 3,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "existing_librenms_link": {"host_id": 42, "oob_id": "bad", "oob_type": "idrac"},
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=3, record=record))

        assert "Linked as host" not in result
        assert "#bad" not in result
        # Positive assertion: it actually fell through to the generic details button (the
        # btn-outline-success "View details" fallback), not an unintended/empty state that
        # would also pass the negative checks above.
        assert "btn-outline-success" in result
        assert 'title="View details"' in result

    def test_oob_id_without_host_id_does_not_render_host_state(self):
        """A librenms_id link carrying an oob_id but NO readable host_id (corrupt/partial CF) must not render the 'Linked as host' badge — there's no host id, so oob_id != None must not satisfy the host-pair branch; it falls through to the generic details button."""
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        table = self._table()

        existing = MagicMock(spec=Device)
        existing.__class__ = Device
        existing.pk = 57

        record = {
            "device_id": 4,
            "_validation": {
                "existing_device": existing,
                "is_ready": False,
                "can_import": False,
                "existing_match_type": "librenms_id",
                "serial_action": None,
                "device_type_mismatch": False,
                "name_sync_available": False,
                "librenms_id_needs_migration": False,
                "existing_librenms_link": {"oob_id": 99, "oob_type": "idrac"},  # no host_id
                "virtual_chassis": None,
            },
        }

        with (
            patch("netbox_librenms_plugin.tables.device_status.VirtualMachine", VirtualMachine),
            patch("netbox_librenms_plugin.tables.device_status.reverse", side_effect=self._fake_reverse),
        ):
            result = str(table.render_actions(value=4, record=record))

        assert "Linked as host" not in result
        assert "btn-outline-success" in result
        assert 'title="View details"' in result


# ===========================================================================
# DeviceImportTable._build_validation_details_url tests
# ===========================================================================


class TestBuildValidationDetailsUrl:
    """Tests for DeviceImportTable._build_validation_details_url()."""

    def _call(self, device_id, validation, server_key=None):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        table = object.__new__(DeviceImportTable)
        table.server_key = server_key
        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/validation/"):
            return table._build_validation_details_url(device_id, validation)

    def test_no_params_returns_plain_url(self):
        url = self._call(1, {})
        assert url == "/validation/"
        assert "?" not in url

    def test_cluster_found_adds_cluster_id(self):
        cluster = MagicMock()
        cluster.id = 10
        validation = {
            "cluster": {"found": True, "cluster": cluster},
        }
        url = self._call(1, validation)
        assert "cluster_id=10" in url

    def test_role_found_adds_role_id(self):
        role = MagicMock()
        role.id = 5
        validation = {
            "cluster": {"found": False, "cluster": None},
            "device_role": {"found": True, "role": role},
        }
        url = self._call(1, validation)
        assert "role_id=5" in url

    def test_vc_detection_enabled_adds_flag(self):
        validation = {"_vc_detection_enabled": True}
        url = self._call(1, validation)
        assert "enable_vc_detection=true" in url

    def test_multiple_params(self):
        role = MagicMock()
        role.id = 7
        validation = {
            "cluster": {"found": False, "cluster": None},
            "device_role": {"found": True, "role": role},
            "_vc_detection_enabled": True,
        }
        url = self._call(1, validation)
        assert "role_id=7" in url
        assert "enable_vc_detection=true" in url


# ===========================================================================
# DeviceImportTable._build_vc_attributes tests
# ===========================================================================


class TestBuildVcAttributes:
    """Tests for DeviceImportTable._build_vc_attributes()."""

    def test_not_a_stack_returns_false_attr(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        result = DeviceImportTable._build_vc_attributes(
            validation={"virtual_chassis": {"is_stack": False}},
            record={"hostname": "host"},
        )
        assert 'data-vc-is-stack="false"' in result

    def test_no_vc_data_returns_false_attr(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        result = DeviceImportTable._build_vc_attributes(validation={}, record={})
        assert 'data-vc-is-stack="false"' in result

    def test_is_stack_returns_full_payload(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        vc_data = {
            "is_stack": True,
            "member_count": 2,
            "members": [
                {"position": 1, "serial": "SN001", "suggested_name": "sw01"},
                {"position": 2, "serial": "SN002", "suggested_name": "sw02"},
            ],
            "detection_error": None,
        }
        result = DeviceImportTable._build_vc_attributes(
            validation={"virtual_chassis": vc_data},
            record={"hostname": "sw01", "sysName": "sw01"},
        )
        assert 'data-vc-is-stack="true"' in result
        assert 'data-vc-member-count="2"' in result
        assert "data-vc-info=" in result
        assert "data-vc-master=" in result

    def test_stack_uses_sysname_when_no_hostname(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        vc_data = {
            "is_stack": True,
            "member_count": 1,
            "members": [],
            "detection_error": None,
        }
        result = DeviceImportTable._build_vc_attributes(
            validation={"virtual_chassis": vc_data},
            record={"hostname": None, "sysName": "mysys"},
        )
        assert "mysys" in result


# ===========================================================================
# LibreNMSInterfaceTable tests
# ===========================================================================


class TestInterfaceTableLibreNMSIdColumnAndBadgeContrast:
    """Regression guards for two UI fixes on the interface sync table."""

    # The relationship column's status keys; an unknown key exercises the .get() fallback badge.
    _STATUSES = ["match", "mismatch", "missing_nb", "missing_lnms", "definitely-unknown-status"]

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            return LibreNMSInterfaceTable(data=[], device=mock_device, server_key="default")

    def _status_badge_classes(self, table, status):
        """Render a status's badge in isolation and return its CSS class tokens."""
        import re

        html = str(
            table._render_relationship_column(
                lnms_name="",
                lnms_port_id=None,
                sync_status=status,
                record={},
                btn_class="lag-sync-btn",
                data_related_key="data-lag-port-id",
                # render_parent always passes a label; without it the guard inspects markup
                # and title text the application never renders.
                type_label="LAG",
            )
        )
        m = re.search(r'class="badge ([^"]*)"', html)
        assert m, f"no status badge rendered for {status!r}: {html!r}"
        return m.group(1).split()

    def test_librenms_id_column_present(self):
        table = self._table()
        column_names = [c.name for c in table.columns]
        assert "librenms_id" in column_names, "LibreNMS ID column was dropped from the interface table"
        assert table.columns["librenms_id"].verbose_name == "LibreNMS ID"

    def test_every_status_badge_pairs_background_with_text_colour(self):
        """A solid ``bg-*`` colour fill with no companion text colour is the grey-on-grey / grey-on-green readability bug."""
        table = self._table()
        for status in self._STATUSES:
            tokens = self._status_badge_classes(table, status)
            # A *-lt token is a known-readable pairing; it isn't a bare solid fill.
            solid_bg = [t for t in tokens if t.startswith("bg-") and not t.endswith("-lt")]
            has_text_colour = any(t.startswith("text-") for t in tokens)
            assert has_text_colour or not solid_bg, (
                f"status {status!r} badge {tokens} sets a solid background with no contrasting "
                "text colour (use text-bg-*, bg-*-lt, or an explicit text-* class)"
            )

    def test_missing_lnms_badge_label_renders(self):
        table = self._table()
        tokens = self._status_badge_classes(table, "missing_lnms")
        # The relationship pill uses Tabler's light variant (ships its own readable text colour in
        # both themes; exempt from the bare-bg badge guard) and a status icon.
        assert "bg-secondary-lt" in tokens


class TestRelationshipBadgeCompactLayout:
    """The Parent/LAG column must render ONE compact pill per relationship line: the relationship type + LibreNMS name in a single light badge, with the status conveyed by colour + an mdi icon + a title tooltip."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(
                data=[], device=MagicMock(pk=3, virtual_chassis=None), interface_name_field="ifName"
            )
        table.migrated_to_marker = False
        return table

    def test_named_relationship_renders_single_pill_with_type_name_and_title(self):
        table = self._table()
        html = str(
            table._render_relationship_column(
                type_label="LAG",
                lnms_name="ae36",
                lnms_port_id=None,
                sync_status="missing_nb",
                record={},
                btn_class="lag-sync-btn",
                data_related_key="data-lag-port-id",
            )
        )
        # Exactly one badge — the old design rendered two (status badge + name badge).
        assert html.count('class="badge') == 1
        # Type + name inline in that single pill.
        assert "LAG" in html
        assert "ae36" in html
        # Status conveyed by an icon + the full text in the title (not a long inline word).
        assert "mdi-plus-circle" in html
        assert 'title="LAG: Not in NetBox"' in html

    def test_match_pill_uses_light_variant_and_check_icon(self):
        table = self._table()
        html = str(
            table._render_relationship_column(
                type_label="Parent",
                lnms_name="lo0",
                lnms_port_id=None,
                sync_status="match",
                record={},
                btn_class="parent-sync-btn",
                data_related_key="data-parent-port-id",
            )
        )
        assert "bg-success-lt" in html
        assert "mdi-check-circle" in html
        assert 'title="Parent: Match"' in html

    def test_render_parent_stacks_lag_and_parent_as_compact_pills(self):
        table = self._table()
        record = {
            "port_id": 5,
            "ifName": "eth0",
            "netbox_interface": None,
            "lag_sync_status": "missing_nb",
            "librenms_lag_name": "ae0",
            "librenms_lag_port_id": 111,
            "parent_sync_status": "match",
            "librenms_parent_name": "et-0/0/1",
            "librenms_parent_port_id": 222,
        }
        html = str(table.render_parent(None, record))
        # One pill per line — two relationships → two badges total (not four).
        assert html.count('class="badge') == 2
        assert "LAG" in html
        assert "Parent" in html
        # Type label lives inside the pill now, not in a separate muted prefix span.
        assert "text-muted small" not in html

    @pytest.mark.django_db
    def test_mismatch_renders_reconcile_sync_button(self):
        """A 'mismatch' (LibreNMS aggregate/parent differs from NetBox) must expose the inline sync button so the row can be reconciled to LibreNMS — not just an amber pill the user can't act on. The LibreNMS port_id is set for a mismatch, so the control has a target."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        table = self._table()
        source = make_interface(make_device("relationship-mismatch"), "eth0")
        html = str(
            table._render_relationship_column(
                type_label="LAG",
                lnms_name="ae9",
                lnms_port_id=42,
                sync_status="mismatch",
                record={"port_id": 7, "netbox_interface": source},
                btn_class="lag-sync-btn",
                data_related_key="data-lag-port-id",
            )
        )
        # The reconcile button is present and wired to the LibreNMS aggregate port_id.
        assert "lag-sync-btn" in html
        assert "mdi-sync" in html
        assert 'data-lag-port-id="42"' in html
        # The tooltip spells out that the click overwrites NetBox with the LibreNMS value.
        assert 'title="Update LAG to match LibreNMS"' in html
        # Icon-only button also carries an accessible name (title alone is not a reliable one).
        assert 'aria-label="Update LAG to match LibreNMS"' in html

    @pytest.mark.django_db
    def test_name_field_does_not_leak_into_a_later_table(self):
        """A non-default name field must not retarget the columns of the next table built.

        ``base_columns`` and ``_meta`` are class attributes, and Table.__init__ deep-copies
        base_columns only after ``__init__`` runs, so assigning to either before ``super()``
        rewrote the accessor for every later table in the worker process, across requests.
        """
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            LibreNMSInterfaceTable(data=[], device=None, interface_name_field="ifDescr", server_key="default")
            later = LibreNMSInterfaceTable(data=[], device=None, server_key="default")

        assert later.interface_name_field == "ifName"
        assert later.columns["name"].column.accessor == "ifName"
        # The class itself must be untouched, so a fresh worker sees the declared default.
        assert LibreNMSInterfaceTable.base_columns["name"].accessor is None

    def test_row_attrs_are_per_instance_not_shared_on_the_class(self):
        """The row-attribute map must be bound to the instance, not written onto Meta."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=None, interface_name_field="ifDescr", server_key="default")

        assert "data-interface" in table.row_attrs
        assert not LibreNMSInterfaceTable._meta.row_attrs

    @pytest.mark.django_db
    def test_unresolvable_owner_renders_badge_only_instead_of_failing_the_render(self):
        """An unresolved owner must degrade this cell, not raise NoReverseMatch for the table.

        ``_resolve_row_member_id`` returns "" when the table has no device context and the row's
        interface carries no device id. ``reverse()`` with an empty object_id raises, which would
        take down the whole table render instead of dropping one button.
        """
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=None, server_key="default")
        source = make_interface(make_device("relationship-no-owner"), "eth0")
        source.device_id = None  # in-memory only: models the row whose owner cannot be resolved

        html = str(
            table._render_relationship_column(
                type_label="LAG",
                lnms_name="ae9",
                lnms_port_id=42,
                sync_status="mismatch",
                record={"port_id": 7, "netbox_interface": source},
                btn_class="lag-sync-btn",
                data_related_key="data-lag-port-id",
            )
        )

        assert "lag-sync-btn" not in html
        assert "mdi-sync" not in html
        assert 'class="badge' in html

    def test_missing_lnms_renders_badge_only_no_button(self):
        """missing_lnms (NetBox has the relationship, LibreNMS doesn't) has no LibreNMS port_id to sync TO, so the lnms_port_id guard keeps the button off — only the status pill renders."""
        table = self._table()
        html = str(
            table._render_relationship_column(
                type_label="LAG",
                lnms_name="",
                lnms_port_id=None,
                sync_status="missing_lnms",
                record={"port_id": 7},
                btn_class="lag-sync-btn",
                data_related_key="data-lag-port-id",
            )
        )
        assert "mdi-sync" not in html
        assert "lag-sync-btn" not in html

    @pytest.mark.django_db
    def test_mismatch_button_suppressed_on_migrated_donor(self):
        """Even a mismatch must NOT render the inline control on a migrated donor page (the bulk form is hidden; a direct POST would mutate donor state)."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        table = self._table()
        table.migrated_to_marker = True
        source = make_interface(make_device("migrated-relationship-mismatch"), "eth0")
        html = str(
            table._render_relationship_column(
                type_label="LAG",
                lnms_name="ae9",
                lnms_port_id=42,
                sync_status="mismatch",
                record={"port_id": 7, "netbox_interface": source},
                btn_class="lag-sync-btn",
                data_related_key="data-lag-port-id",
            )
        )
        assert "mdi-sync" not in html
        assert "lag-sync-btn" not in html


class TestLibreNMSInterfaceTableInit:
    """Tests for LibreNMSInterfaceTable.__init__()."""

    def test_default_interface_name_field_is_used(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch(
            "netbox_librenms_plugin.tables.interfaces.get_interface_name_field",
            return_value="ifName",
        ):
            table = LibreNMSInterfaceTable(data=[], device=mock_device)

        assert table.interface_name_field == "ifName"

    def test_explicit_interface_name_field(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=mock_device, interface_name_field="ifDescr")

        assert table.interface_name_field == "ifDescr"

    def test_vlan_groups_defaults_to_empty_list(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=mock_device)

        assert table.vlan_groups == []

    def test_tab_and_prefix_set(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=mock_device)

        assert table.tab == "interfaces"
        assert table.prefix == "interfaces_"

    def test_ipaddress_table_sets_tab_and_prefix(self):
        """The IP table must set tab='ipaddresses' so the paginator links (?tab={{ table.tab }}) keep the user on the IP Addresses tab, and prefix='ipaddresses_' so its per-page param is namespaced (configure() passes self.prefix to get_table_paginate_count) rather than shared with the generic one."""
        from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable

        table = IPAddressTable([])
        assert table.tab == "ipaddresses"
        assert table.prefix == "ipaddresses_"

    def test_server_key_stored(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=mock_device, server_key="prod")

        assert table.server_key == "prod"

    def test_server_key_defaults_when_none(self):
        """A None server_key must default to "default" — render_librenms_id passes self.server_key into get_librenms_device_id, and a None key would miss {"default": 42} custom-field values."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=mock_device, server_key=None)

        assert table.server_key == "default"


class TestRelationshipSyncObjectType:
    """The missing_nb relationship-sync button must carry the right object type, driven by the table subclass — not a runtime self.device.cluster probe that misclassifies a cluster-less VM as a device."""

    def _render(self, table_cls, device, netbox_interface):
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = table_cls(data=[], device=device, interface_name_field="ifName")
        record = {"port_id": 5, "ifName": "eth0", "netbox_interface": netbox_interface}
        return str(
            table._render_relationship_column(
                lnms_name="eth0",
                lnms_port_id=10,
                sync_status="missing_nb",
                record=record,
                btn_class="parent-sync-btn",
                data_related_key="data-parent-port-id",
            )
        )

    @pytest.mark.django_db
    def test_device_table_emits_device_object_type(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("relationship-object-type")
        html = self._render(LibreNMSInterfaceTable, device, make_interface(device, "eth0"))
        assert 'data-object-type="device"' in html

    @pytest.mark.django_db
    def test_vm_table_emits_virtualmachine_even_without_cluster(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("relationship-object-type-vm")
        html = self._render(LibreNMSVMInterfaceTable, vm, VMInterface.objects.create(virtual_machine=vm, name="eth0"))
        assert 'data-object-type="virtualmachine"' in html


class TestParentColumnContainsLagButton:
    """Regression guard for the 'refresh the LAG cell after VC-member verification' review note."""

    def _table(self, device):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = LibreNMSInterfaceTable(data=[], device=device, interface_name_field="ifName")
        table.migrated_to_marker = False  # inline sync buttons active
        return table

    @pytest.mark.django_db
    def test_render_parent_emits_lag_button_with_port_id_in_same_cell(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("relationship-buttons")
        interface = make_interface(device, "eth0")
        table = self._table(device)
        record = {
            "port_id": 5,
            "ifName": "eth0",
            "netbox_interface": interface,
            "selected_object_id": 3,
            "selected_object_type": "device",
            # Both relationship halves present and unsynced → both buttons render.
            "lag_sync_status": "missing_nb",
            "librenms_lag_name": "ae0",
            "librenms_lag_port_id": 111,
            "parent_sync_status": "missing_nb",
            "librenms_parent_name": "eth-parent",
            "librenms_parent_port_id": 222,
        }
        html = str(table.render_parent(None, record))
        # Both buttons live in the single combined column render_parent produces.
        assert "lag-sync-btn" in html
        assert 'data-lag-port-id="111"' in html
        assert "parent-sync-btn" in html
        assert 'data-parent-port-id="222"' in html

    @pytest.mark.django_db
    def test_relationship_button_uses_reversed_prefixed_url(self):
        from django.urls import get_script_prefix, set_script_prefix

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("prefixed-relationship-button")
        interface = make_interface(device, "eth0")
        table = self._table(device)
        record = {
            "port_id": 5,
            "ifName": "eth0",
            "netbox_interface": interface,
            "selected_object_id": device.pk,
            "selected_object_type": "device",
            "parent_sync_status": "missing_nb",
            "librenms_parent_name": "eth-parent",
            "librenms_parent_port_id": 222,
        }
        previous_prefix = get_script_prefix()
        set_script_prefix("/netbox/")
        try:
            html = str(table.render_parent(None, record))
        finally:
            set_script_prefix(previous_prefix)

        assert f'data-sync-url="/netbox/plugins/librenms_plugin/device/{device.pk}/sync-interface-parent/"' in html

    @pytest.mark.django_db
    def test_unsynced_source_does_not_offer_relationship_action(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        table = self._table(make_device("unsynced-relationship-source"))
        record = {
            "port_id": 5,
            "ifName": "eth0",
            "netbox_interface": None,
            "lag_sync_status": "missing_nb",
            "librenms_lag_name": "ae0",
            "librenms_lag_port_id": 111,
            "parent_sync_status": "missing_nb",
            "librenms_parent_name": "eth-parent",
            "librenms_parent_port_id": 222,
        }

        html = str(table.render_parent(None, record))

        assert "Not in NetBox" in html
        assert "lag-sync-btn" not in html
        assert "parent-sync-btn" not in html

    @pytest.mark.django_db
    def test_no_standalone_lag_column_exists(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        table = self._table(make_device("relationship-columns"))
        col_names = [c.name for c in table.columns]
        assert "parent" in col_names  # the combined Parent/LAG column
        assert "lag" not in col_names  # there is no separate LAG cell for JS to target


class TestMigratedModeSuppressesRelationshipButton:
    """On a migrated donor page the per-row LAG/parent sync button must be suppressed: it POSTs directly via librenms_sync.js, so a live button would let a migrated donor mutate relationship state even though the bulk sync form is hidden."""

    def _render(self, migrated, device, netbox_interface):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=device, interface_name_field="ifName")
        table.migrated_to_marker = migrated
        record = {"port_id": 5, "ifName": "eth0", "netbox_interface": netbox_interface}
        return str(
            table._render_relationship_column(
                lnms_name="eth0",
                lnms_port_id=10,
                sync_status="missing_nb",
                record=record,
                btn_class="parent-sync-btn",
                data_related_key="data-parent-port-id",
            )
        )

    @pytest.mark.django_db
    def test_button_present_in_normal_mode(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("normal-relationship-button")
        html = self._render(False, device, make_interface(device, "eth0"))
        assert "parent-sync-btn" in html

    @pytest.mark.django_db
    def test_button_suppressed_in_migrated_mode(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("migrated-relationship-button")
        html = self._render(True, device, make_interface(device, "eth0"))
        assert "parent-sync-btn" not in html
        # The status badge must still render so the relationship state stays visible.
        assert "Not in NetBox" in html


class TestRelationshipColumnNameEscaping:
    """Contract: the related interface name in the Parent/LAG badge is escaped exactly once for a name containing & < >."""

    def _render(self, lnms_name):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(
                data=[], device=MagicMock(pk=3, virtual_chassis=None), interface_name_field="ifName"
            )
        record = {"port_id": 5, "ifName": "eth0", "netbox_interface": None}
        return str(
            table._render_relationship_column(
                lnms_name=lnms_name,
                lnms_port_id=10,
                sync_status="match",  # renders the name badge, no sync button
                record=record,
                btn_class="parent-sync-btn",
                data_related_key="data-parent-port-id",
            )
        )

    def test_special_chars_escaped_once(self):
        html = self._render("a&b<c>")
        assert "a&amp;b&lt;c&gt;" in html  # escaped exactly once
        assert "&amp;amp;" not in html  # not double-encoded
        assert "&amp;lt;" not in html


# ===========================================================================
# LibreNMSInterfaceTable._parse_group_id tests
# ===========================================================================


class TestParseGroupId:
    """Tests for LibreNMSInterfaceTable._parse_group_id()."""

    def test_empty_string_returns_none(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        assert LibreNMSInterfaceTable._parse_group_id("") is None

    def test_none_returns_none(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        assert LibreNMSInterfaceTable._parse_group_id(None) is None

    def test_valid_string_returns_int(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        assert LibreNMSInterfaceTable._parse_group_id("42") == 42

    def test_zero_string_returns_zero(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        assert LibreNMSInterfaceTable._parse_group_id("0") == 0


# ===========================================================================
# LibreNMSInterfaceTable render_vlans tests
# ===========================================================================


class TestLibreNMSInterfaceTableRenderVlans:
    """Tests for LibreNMSInterfaceTable.render_vlans()."""

    def _table(self):
        table = object.__new__(
            __import__(
                "netbox_librenms_plugin.tables.interfaces", fromlist=["LibreNMSInterfaceTable"]
            ).LibreNMSInterfaceTable
        )
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []
        return table

    def test_no_vlans_returns_dash(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        record = {"ifName": "eth0", "untagged_vlan": None, "tagged_vlans": [], "missing_vlans": []}

        result = str(table.render_vlans(value=None, record=record))
        assert "—" in result

    def test_vlan_keys_use_the_canonical_port_id(self):
        """The rendered key must match the one _sync_interface_vlans() reads back."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable
        from netbox_librenms_plugin.utils import normalize_librenms_port_id

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        # A leading-zero port id is the shape the view normalises but the table used raw.
        record = {
            "ifName": "eth0",
            "port_id": "010",
            "untagged_vlan": 100,
            "tagged_vlans": [],
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_untagged_vlan_css_class", return_value="text-danger"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        canonical = normalize_librenms_port_id("010")
        assert canonical == 10
        assert f'name="vlan_group_{canonical}_100"' in result
        assert 'name="vlan_group_010_100"' not in result
        assert f'data-row-key="{canonical}"' in result

    def test_untagged_vlan_rendered(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        record = {
            "ifName": "eth0",
            "untagged_vlan": 100,
            "tagged_vlans": [],
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_untagged_vlan_css_class", return_value="text-danger"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "100" in result
        assert "(U)" in result

    def test_tagged_vlans_rendered(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        record = {
            "ifName": "eth0",
            "untagged_vlan": None,
            "tagged_vlans": [200, 300],
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_tagged_vlan_css_class", return_value="text-success"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "200" in result
        assert "300" in result
        assert "(T)" in result

    def test_more_than_max_inline_shows_summary(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        record = {
            "ifName": "eth0",
            "untagged_vlan": None,
            "tagged_vlans": [10, 20, 30, 40, 50],  # 5 tagged → 2 "more"
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_tagged_vlan_css_class", return_value="text-success"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "more" in result
        assert "+2" in result

    def test_missing_vlans_shows_warning_in_tooltip(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        record = {
            "ifName": "eth0",
            "untagged_vlan": 100,
            "tagged_vlans": [],
            "missing_vlans": [100],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=False),
            patch("netbox_librenms_plugin.tables.interfaces.get_untagged_vlan_css_class", return_value="text-danger"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value="⚠"),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "Not in NetBox" in result

    def test_vlan_group_map_used_in_tooltip(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        record = {
            "ifName": "eth0",
            "untagged_vlan": 100,
            "tagged_vlans": [],
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {100: {"group_id": "5", "group_name": "SiteVLANs"}},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_untagged_vlan_css_class", return_value="text-success"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "SiteVLANs" in result

    def test_vlan_groups_option_list_built(self):
        """With vlan_groups set, renders group options in JSON."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        group = MagicMock()
        group.pk = 10
        group.name = "MyGroup"
        group.scope = "site"

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = [group]

        record = {
            "ifName": "eth0",
            "untagged_vlan": 50,
            "tagged_vlans": [],
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_untagged_vlan_css_class", return_value="text-success"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "MyGroup" in result

    def test_netbox_interface_untagged_vlan_extracted(self):
        """When netbox_interface has untagged_vlan, its vid and group_id are read."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        nb_iface = MagicMock()
        nb_iface.untagged_vlan = MagicMock()
        nb_iface.untagged_vlan.vid = 100
        nb_iface.untagged_vlan.group_id = 5
        nb_iface.tagged_vlans.all.return_value = []

        record = {
            "ifName": "eth0",
            "untagged_vlan": 100,
            "tagged_vlans": [],
            "missing_vlans": [],
            "exists_in_netbox": True,
            "netbox_interface": nb_iface,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_untagged_vlan_css_class", return_value="text-success"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "100" in result


# ===========================================================================
# LibreNMSInterfaceTable._get_interface_status_display tests
# ===========================================================================


class TestGetInterfaceStatusDisplay:
    """Tests for LibreNMSInterfaceTable._get_interface_status_display()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        t = object.__new__(LibreNMSInterfaceTable)
        return t

    def test_not_in_netbox_returns_danger(self):
        table = self._table()
        display, css = table._get_interface_status_display(True, {"exists_in_netbox": False})
        assert display == "Enabled"
        assert css == "text-danger"

    def test_in_netbox_enabled_matches_returns_success(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.enabled = True
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface}
        display, css = table._get_interface_status_display(True, record)
        assert css == "text-success"

    def test_in_netbox_enabled_mismatches_returns_warning(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.enabled = False
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface}
        display, css = table._get_interface_status_display(True, record)
        assert css == "text-warning"

    def test_in_netbox_no_interface_returns_danger(self):
        table = self._table()
        record = {"exists_in_netbox": True, "netbox_interface": None}
        display, css = table._get_interface_status_display(True, record)
        assert css == "text-danger"

    def test_disabled_interface_display_value(self):
        table = self._table()
        display, css = table._get_interface_status_display(False, {"exists_in_netbox": False})
        assert display == "Disabled"


# ===========================================================================
# LibreNMSInterfaceTable._parse_enabled_status tests
# ===========================================================================


class TestParseEnabledStatus:
    """Tests for LibreNMSInterfaceTable._parse_enabled_status()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def test_string_up_returns_true(self):
        assert self._table()._parse_enabled_status("up") is True

    def test_string_down_returns_false(self):
        assert self._table()._parse_enabled_status("down") is False

    def test_string_up_case_insensitive(self):
        # "UP".lower() == "up" → True; "Up".lower() == "up" → True
        assert self._table()._parse_enabled_status("UP") is True
        assert self._table()._parse_enabled_status("Up") is True

    def test_bool_true_returns_true(self):
        assert self._table()._parse_enabled_status(True) is True

    def test_bool_false_returns_false(self):
        assert self._table()._parse_enabled_status(False) is False

    def test_none_returns_false(self):
        assert self._table()._parse_enabled_status(None) is False


# ===========================================================================
# LibreNMSInterfaceTable render_enabled tests
# ===========================================================================


class TestRenderEnabled:
    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def test_enabled_up_not_in_netbox(self):
        table = self._table()
        record = {"exists_in_netbox": False, "netbox_interface": None}
        result = str(table.render_enabled(value="up", record=record))
        assert "Enabled" in result
        assert "text-danger" in result

    def test_disabled_up_matching_in_netbox(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.enabled = False
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface}
        result = str(table.render_enabled(value="down", record=record))
        assert "Disabled" in result
        assert "text-success" in result


# ===========================================================================
# LibreNMSInterfaceTable._compare_mac_addresses tests
# ===========================================================================


class TestCompareMacAddresses:
    """Tests for LibreNMSInterfaceTable._compare_mac_addresses()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def test_no_netbox_interface_returns_false(self):
        table = self._table()
        assert table._compare_mac_addresses("AA:BB:CC:DD:EE:FF", None) is False

    def test_mac_found_in_interface_macs(self):
        table = self._table()
        mac_obj = MagicMock()
        mac_obj.mac_address = "AA:BB:CC:DD:EE:FF"
        nb_iface = MagicMock()
        nb_iface.mac_addresses.all.return_value = [mac_obj]
        assert table._compare_mac_addresses("AA:BB:CC:DD:EE:FF", nb_iface) is True

    def test_mac_not_found_in_interface_macs(self):
        table = self._table()
        mac_obj = MagicMock()
        mac_obj.mac_address = "11:22:33:44:55:66"
        nb_iface = MagicMock()
        nb_iface.mac_addresses.all.return_value = [mac_obj]
        assert table._compare_mac_addresses("AA:BB:CC:DD:EE:FF", nb_iface) is False


# ===========================================================================
# LibreNMSInterfaceTable._render_field tests
# ===========================================================================


class TestRenderField:
    """Tests for LibreNMSInterfaceTable._render_field()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def test_not_in_netbox_returns_danger(self):
        table = self._table()
        result = str(table._render_field("myval", {"exists_in_netbox": False}, "ifAlias", "description"))
        assert "text-danger" in result
        assert "myval" in result

    def test_no_netbox_interface_returns_danger(self):
        table = self._table()
        record = {"exists_in_netbox": True, "netbox_interface": None}
        result = str(table._render_field("myval", record, "ifAlias", "description"))
        assert "text-danger" in result

    def test_mac_address_match_returns_success(self):
        table = self._table()
        mac_obj = MagicMock()
        mac_obj.mac_address = "AA:BB:CC:DD:EE:FF"
        nb_iface = MagicMock()
        nb_iface.mac_addresses.all.return_value = [mac_obj]
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifPhysAddress": "AA:BB:CC:DD:EE:FF"}
        result = str(table._render_field("AA:BB:CC:DD:EE:FF", record, "ifPhysAddress", "mac_address"))
        assert "text-success" in result

    def test_mac_address_mismatch_returns_warning(self):
        table = self._table()
        mac_obj = MagicMock()
        mac_obj.mac_address = "11:22:33:44:55:66"
        nb_iface = MagicMock()
        nb_iface.mac_addresses.all.return_value = [mac_obj]
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifPhysAddress": "AA:BB:CC:DD:EE:FF"}
        result = str(table._render_field("AA:BB:CC:DD:EE:FF", record, "ifPhysAddress", "mac_address"))
        assert "text-warning" in result

    def test_field_matches_returns_success(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.description = "my desc"
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifAlias": "my desc"}
        result = str(table._render_field("my desc", record, "ifAlias", "description"))
        assert "text-success" in result

    def test_field_mismatches_returns_warning(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.description = "other desc"
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifAlias": "my desc"}
        result = str(table._render_field("my desc", record, "ifAlias", "description"))
        assert "text-warning" in result

    def test_speed_comparison_uses_kbps_conversion(self):
        """ifSpeed comparison converts value to kbps before comparing."""
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.speed = 1000000  # 1Gbps in kbps
        record = {
            "exists_in_netbox": True,
            "netbox_interface": nb_iface,
            "ifSpeed": 1000000000,  # 1Gbps in bps
        }

        with patch("netbox_librenms_plugin.tables.interfaces.convert_speed_to_kbps", return_value=1000000):
            result = str(table._render_field("1 Gbps", record, "ifSpeed", "speed"))

        assert "text-success" in result

    def test_speed_mismatch_returns_warning(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.speed = 100000  # 100Mbps
        record = {
            "exists_in_netbox": True,
            "netbox_interface": nb_iface,
            "ifSpeed": 1000000000,
        }

        with patch("netbox_librenms_plugin.tables.interfaces.convert_speed_to_kbps", return_value=1000000):
            result = str(table._render_field("1 Gbps", record, "ifSpeed", "speed"))

        assert "text-warning" in result


# ===========================================================================
# LibreNMSInterfaceTable render_speed, render_name, render_description tests
# ===========================================================================


class TestRenderSpeedNameDescription:
    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        t = object.__new__(LibreNMSInterfaceTable)
        t.interface_name_field = "ifName"
        return t

    def test_render_speed_delegates_to_render_field(self):
        table = self._table()
        record = {"exists_in_netbox": False}

        with patch("netbox_librenms_plugin.tables.interfaces.convert_speed_to_kbps", return_value=1000):
            with patch("netbox_librenms_plugin.tables.interfaces.humanize_speed", return_value="1 Mbps"):
                result = str(table.render_speed(value=1000000, record=record))

        assert "text-danger" in result

    def test_render_name_delegates_to_render_field(self):
        table = self._table()
        record = {"exists_in_netbox": False, "ifName": "eth0"}
        result = str(table.render_name(value="eth0", record=record))
        assert "text-danger" in result

    def test_render_description_delegates_to_render_field(self):
        table = self._table()
        record = {"exists_in_netbox": False, "ifAlias": "uplink"}
        result = str(table.render_description(value="uplink", record=record))
        assert "text-danger" in result

    def test_render_mtu_delegates_to_render_field(self):
        table = self._table()
        record = {"exists_in_netbox": False, "ifMtu": 1500}
        result = str(table.render_mtu(value=1500, record=record))
        assert "text-danger" in result

    def test_render_mac_address_delegates_to_render_field(self):
        table = self._table()
        record = {"exists_in_netbox": False, "ifPhysAddress": "aabbccddeeff"}
        with patch("netbox_librenms_plugin.tables.interfaces.format_mac_address", return_value="AA:BB:CC:DD:EE:FF"):
            result = str(table.render_mac_address(value="aabbccddeeff", record=record))
        assert "text-danger" in result


# ===========================================================================
# LibreNMSInterfaceTable render_type tests
# ===========================================================================


class TestRenderType:
    """Tests for LibreNMSInterfaceTable.render_type()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        t = object.__new__(LibreNMSInterfaceTable)
        t.interface_name_field = "ifName"
        return t

    def test_not_in_netbox_returns_danger(self):
        table = self._table()
        record = {"exists_in_netbox": False, "ifSpeed": 1000000000}

        with patch.object(table, "get_interface_mapping", return_value=None):
            with patch.object(table, "render_mapping_tooltip", return_value=("ethernet", MagicMock())):
                result = str(table.render_type(value="ethernetCsmacd", record=record))

        assert "text-danger" in result

    def test_in_netbox_type_matches_returns_success(self):
        table = self._table()
        mapping = MagicMock()
        mapping.netbox_type = "1000base-t"
        nb_iface = MagicMock()
        nb_iface.type = "1000base-t"
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifSpeed": 1000000000}

        with patch.object(table, "get_interface_mapping", return_value=mapping):
            with patch.object(table, "render_mapping_tooltip", return_value=("1000base-t", MagicMock())):
                result = str(table.render_type(value="ethernetCsmacd", record=record))

        assert "text-success" in result

    def test_in_netbox_type_mismatches_returns_warning(self):
        table = self._table()
        mapping = MagicMock()
        mapping.netbox_type = "1000base-t"
        nb_iface = MagicMock()
        nb_iface.type = "10gbase-t"
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifSpeed": 1000000000}

        with patch.object(table, "get_interface_mapping", return_value=mapping):
            with patch.object(table, "render_mapping_tooltip", return_value=("1000base-t", MagicMock())):
                result = str(table.render_type(value="ethernetCsmacd", record=record))

        assert "text-warning" in result

    def test_in_netbox_no_mapping_returns_danger(self):
        table = self._table()
        nb_iface = MagicMock()
        nb_iface.type = "1000base-t"
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface, "ifSpeed": 1000000000}

        with patch.object(table, "get_interface_mapping", return_value=None):
            with patch.object(table, "render_mapping_tooltip", return_value=("ethernetCsmacd", MagicMock())):
                result = str(table.render_type(value="ethernetCsmacd", record=record))

        assert "text-danger" in result


# ===========================================================================
# LibreNMSInterfaceTable get_interface_mapping tests
# ===========================================================================


@pytest.mark.django_db
class TestGetInterfaceMapping:
    """Tests for LibreNMSInterfaceTable.get_interface_mapping()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def _mapping(self, librenms_type, librenms_speed, netbox_type="1000base-t"):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        return InterfaceTypeMapping.objects.create(
            librenms_type=librenms_type, librenms_speed=librenms_speed, netbox_type=netbox_type
        )

    def test_exact_match_returned(self):
        table = self._table()
        mapping = self._mapping("ethernetCsmacd", 1000000)
        self._mapping("ethernetCsmacd", None, netbox_type="virtual")  # type-only fallback

        assert table.get_interface_mapping("ethernetCsmacd", 1000000) == mapping

    def test_fallback_type_only_match(self):
        table = self._table()
        # Only a type-only (speed is None) mapping exists; the exact (type, speed) lookup misses.
        fallback = self._mapping("ethernetCsmacd", None, netbox_type="virtual")

        assert table.get_interface_mapping("ethernetCsmacd", 1000000) == fallback

    def test_no_match_returns_none(self):
        table = self._table()
        self._mapping("ethernetCsmacd", 1000000)  # a mapping exists, but not for this type

        assert table.get_interface_mapping("unknown_type", 0) is None

    def test_mappings_snapshotted_once_for_repeated_lookups(self):
        """The mapping table is read once, not per lookup, so a multi-row render doesn't re-query the static table."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        table = self._table()
        self._mapping("ethernetCsmacd", 1000000)
        self._mapping("ethernetCsmacd", None, netbox_type="virtual")

        with CaptureQueriesContext(connection) as queries:
            for _ in range(5):
                table.get_interface_mapping("ethernetCsmacd", 1000000)

        mapping_queries = [q for q in queries.captured_queries if "interfacetypemapping" in q["sql"].lower()]
        assert len(mapping_queries) == 1


# ===========================================================================
# LibreNMSInterfaceTable render_mapping_tooltip tests
# ===========================================================================


class TestRenderMappingTooltip:
    """Tests for LibreNMSInterfaceTable.render_mapping_tooltip()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def test_with_mapping_returns_netbox_type_and_link_icon(self):
        table = self._table()
        mapping = MagicMock()
        mapping.netbox_type = "1000base-t"
        display, icon = table.render_mapping_tooltip("ethernetCsmacd", 1000000, mapping)
        assert display == "1000base-t"
        assert "mdi-link-variant" in str(icon)

    def test_without_mapping_returns_raw_value_and_off_icon(self):
        table = self._table()
        display, icon = table.render_mapping_tooltip("ethernetCsmacd", 1000000, None)
        assert display == "ethernetCsmacd"
        assert "mdi-link-variant-off" in str(icon)


# ===========================================================================
# LibreNMSInterfaceTable format_interface_data tests
# ===========================================================================


class TestFormatInterfaceData:
    """Tests for LibreNMSInterfaceTable.format_interface_data()."""

    def _table(self, interface_name_field="ifName"):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        t = object.__new__(LibreNMSInterfaceTable)
        t.interface_name_field = interface_name_field
        t.server_key = "default"
        t.vlan_groups = []
        t.device = MagicMock()
        t.device.pk = 1
        return t

    def test_format_returns_dict_with_expected_keys(self):
        table = self._table()
        device = MagicMock()
        nb_iface = MagicMock()
        device.interfaces.filter.return_value.first.return_value = nb_iface

        port_data = {
            "ifName": "eth0",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifPhysAddress": "aabbccddeeff",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "ifAlias": "uplink",
            "ifDescr": "eth0",
        }

        with (
            patch.object(table, "render_name", return_value="<span>eth0</span>"),
            patch.object(table, "render_type", return_value="<span>1g</span>"),
            patch.object(table, "render_speed", return_value="<span>1G</span>"),
            patch.object(table, "render_mac_address", return_value="<span>mac</span>"),
            patch.object(table, "render_mtu", return_value="<span>1500</span>"),
            patch.object(table, "render_enabled", return_value="<span>Enabled</span>"),
            patch.object(table, "render_description", return_value="<span>uplink</span>"),
        ):
            result = table.format_interface_data(port_data, device)

        assert "name" in result
        assert "type" in result
        assert "speed" in result
        assert "mac_address" in result
        assert "mtu" in result
        assert "enabled" in result
        assert "description" in result

    def test_clears_alias_when_same_as_name(self):
        """When ifAlias == ifName, it is cleared before rendering."""
        table = self._table()
        device = MagicMock()
        device.interfaces.filter.return_value.first.return_value = None

        port_data = {
            "ifName": "eth0",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 0,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "ifAlias": "eth0",  # same as ifName → should be cleared
            "ifDescr": "eth0",
        }

        with (
            patch.object(table, "render_name", return_value=""),
            patch.object(table, "render_type", return_value=""),
            patch.object(table, "render_speed", return_value=""),
            patch.object(table, "render_mac_address", return_value=""),
            patch.object(table, "render_mtu", return_value=""),
            patch.object(table, "render_enabled", return_value=""),
            patch.object(table, "render_description", return_value="") as mock_desc,
        ):
            table.format_interface_data(port_data, device)
            # render_description is called with "" (cleared alias)
            mock_desc.assert_called_once_with("", port_data)

    def test_oob_row_never_binds_to_host_interface_by_name(self, db):
        """An OOB-controller row (shared-LOM 'eth0') must stay unmatched on row re-render, mirroring the interfaces-tab guard — otherwise the VC-dropdown re-render flips it to green 'matched' against an unrelated host interface."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        device = make_device("format-interface-oob")
        make_interface(device, "eth0")
        table = LibreNMSInterfaceTable(data=[], device=device, interface_name_field="ifName")

        port_data = {
            "_source": "oob",
            "port_id": 10,
            "ifName": "eth0",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 0,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "ifAlias": "",
            "ifDescr": "eth0",
            "untagged_vlan": None,
            "tagged_vlans": [],
        }

        table.format_interface_data(port_data, device)

        assert port_data["netbox_interface"] is None
        assert port_data["exists_in_netbox"] is False

    def test_main_row_still_binds_by_name(self, db):
        """The guard is OOB-specific: a main-source row keeps the name binding."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        device = make_device("format-interface-main")
        host_iface = make_interface(device, "eth0")
        table = LibreNMSInterfaceTable(data=[], device=device, interface_name_field="ifName")

        port_data = {
            "_source": "main",
            "port_id": 10,
            "ifName": "eth0",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 0,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "ifAlias": "",
            "ifDescr": "eth0",
            "untagged_vlan": None,
            "tagged_vlans": [],
            "name_fallback_allowed": True,
        }

        table.format_interface_data(port_data, device)

        assert port_data["netbox_interface"].pk == host_iface.pk
        assert port_data["exists_in_netbox"] is True


# ===========================================================================
# LibreNMSInterfaceTable configure tests
# ===========================================================================


class TestConfigure:
    def test_configure_calls_request_config(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.prefix = "interfaces_"

        request = MagicMock()

        with (
            patch("netbox_librenms_plugin.tables.interfaces.get_table_paginate_count", return_value=25),
            patch("netbox_librenms_plugin.tables.interfaces.tables.RequestConfig") as mock_rc,
        ):
            mock_rc_instance = MagicMock()
            mock_rc.return_value = mock_rc_instance
            table.configure(request)

        mock_rc.assert_called_once()
        mock_rc_instance.configure.assert_called_once_with(table)


# ===========================================================================
# VCInterfaceTable tests
# ===========================================================================


class TestVCInterfaceTable:
    """Tests for VCInterfaceTable."""

    def _table(self, device=None, interface_name_field="ifName"):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        mock_device = device or MagicMock()
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = []

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = VCInterfaceTable(
                data=[],
                device=mock_device,
                interface_name_field=interface_name_field,
            )
        return table

    def test_device_selection_column_visible_for_vc(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()  # Has VC

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = VCInterfaceTable(data=[], device=mock_device, interface_name_field="ifName")

        # device_selection column should be shown for VC devices
        assert "device_selection" in table.columns

    @pytest.mark.django_db
    def test_duplicate_display_names_have_distinct_stable_form_keys(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        member1 = make_device("stable-form-member-1")
        member2 = make_device("stable-form-member-2")
        make_virtual_chassis("stable-form-vc", member1, member2)
        records = [
            {"port_id": 10, "ifName": "Ethernet1", "ifDescr": "Ethernet", "ifType": "ethernetCsmacd"},
            {"port_id": 11, "ifName": "Ethernet2", "ifDescr": "Ethernet", "ifType": "ethernetCsmacd"},
        ]
        table = VCInterfaceTable(data=records, device=member1, interface_name_field="ifDescr")

        selection_cells = [str(row.get_cell("selection")) for row in table.rows]
        member_selects = [str(table.render_device_selection(None, record)) for record in records]

        assert 'name="select" value="10"' in selection_cells[0]
        assert 'name="select" value="11"' in selection_cells[1]
        assert 'name="device_selection_10"' in member_selects[0]
        assert 'name="device_selection_11"' in member_selects[1]

    @pytest.mark.django_db
    def test_render_device_selection_reuses_single_member_query(self, django_assert_num_queries):
        """The member dropdown loads the real chassis member queryset once per table."""
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        member1 = make_device("vc-query-member-1")
        member2 = make_device("vc-query-member-2")
        make_virtual_chassis("vc-query-members", member1, member2)
        table = VCInterfaceTable(data=[], device=member1, interface_name_field="ifName")

        with django_assert_num_queries(1):
            table.render_device_selection(None, {"ifName": "Gi1/0", "ifType": "ethernetCsmacd"})
            table.render_device_selection(None, {"ifName": "Gi2/0", "ifType": "ethernetCsmacd"})
            _ = table._vc_members_by_position

    @pytest.mark.django_db
    def test_render_device_selection_ethernet_uses_vc_member(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        member1 = make_device("vc-physical-member-1")
        member2 = make_device("vc-physical-member-2")
        make_virtual_chassis("vc-physical-members", member1, member2)
        table = VCInterfaceTable(data=[], device=member1, interface_name_field="ifName")

        result = str(
            table.render_device_selection(
                value=None,
                record={"ifName": "Gi2/0/1", "ifType": "ethernetCsmacd"},
            )
        )

        assert member1.name in result
        assert member2.name in result
        assert f'value="{member2.pk}" selected' in result
        assert "vc-member-select" in result

    @pytest.mark.django_db
    def test_render_device_selection_logical_iface_defaults_to_viewed_member(self):
        """
        A not-yet-synced LOGICAL interface (Vlan2) on a real VC must default to the VIEWED member,
        NOT the member whose vc_position happens to equal the name's trailing digit.

        Real-DB: the digit in ``Vlan2`` is a VLAN id, not a member index — the name-position
        heuristic must not run for logical types. Against the unfixed code (no ethernet/dotted
        guard) this resolves to the position-2 member (wrong); the guard defaults it to the viewed
        member. Uses real Devices in a real VirtualChassis so the vc_position map is genuinely
        queried — a MagicMock member with a MagicMock vc_position hid this by never matching.
        """
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        sw1 = make_device("sw-①-1")
        sw2 = make_device("sw-①-2")
        make_virtual_chassis("vc-①-logical", sw1, sw2)  # sw1 -> vc_position 1, sw2 -> 2

        table = VCInterfaceTable(data=[], device=sw1, interface_name_field="ifName")

        record = {"ifName": "Vlan2", "ifType": "l3ipvlan"}  # digit 2 == sw2's vc_position

        assert table._resolve_row_member_id(record) == sw1.pk  # viewed member, not sw2
        dropdown = str(table.render_device_selection(value=None, record=record))
        assert f'value="{sw1.pk}" selected' in dropdown
        assert f'value="{sw2.pk}" selected' not in dropdown

    @pytest.mark.django_db
    def test_render_device_selection_uses_name_heuristic_only_for_physical_rows(self):
        """
        The name-position heuristic applies to physical Ethernet rows. A dot does not prove that
        a logical child belongs to the member encoded in its name.
        """
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        sw1 = make_device("sw-①b-1")
        sw2 = make_device("sw-①b-2")
        make_virtual_chassis("vc-①-physical", sw1, sw2)  # sw1 -> 1, sw2 -> 2

        table = VCInterfaceTable(data=[], device=sw1, interface_name_field="ifName")

        # Physical ethernet port on member 2 (ethernetCsmacd) -> heuristic resolves sw2.
        assert table._resolve_row_member_id({"ifName": "Ethernet2", "ifType": "ethernetCsmacd"}) == sw2.pk
        # The logical child has no stable owner binding, so it stays on the viewed member.
        assert table._resolve_row_member_id({"ifName": "Ethernet2.100", "ifType": "l3ipvlan"}) == sw1.pk

    @pytest.mark.django_db
    def test_render_device_selection_no_member_uses_device_id(self):
        """A physical name with no matching position falls back to the viewed member."""
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        member = make_device("vc-fallback-member")
        make_virtual_chassis("vc-fallback", member)
        table = VCInterfaceTable(data=[], device=member, interface_name_field="ifName")
        result = str(
            table.render_device_selection(
                value=None,
                record={"ifName": "Gi2/0/1", "ifType": "ethernetCsmacd"},
            )
        )

        assert f'value="{member.pk}" selected' in result

    def test_format_interface_data_includes_device_selection(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        mock_member = MagicMock()
        mock_member.id = 1
        mock_member.name = "switch-1"

        mock_device = MagicMock()
        mock_device.id = 1
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [mock_member]

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = VCInterfaceTable(data=[], device=mock_device, interface_name_field="ifName")

        device = MagicMock()
        device.interfaces.filter.return_value.first.return_value = None

        port_data = {
            "ifName": "eth0",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 0,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "ifAlias": "uplink",
            "ifDescr": "eth0",
        }

        with patch.object(table, "render_device_selection", return_value="<select></select>"):
            with (
                patch.object(table, "render_name", return_value=""),
                patch.object(table, "render_type", return_value=""),
                patch.object(table, "render_speed", return_value=""),
                patch.object(table, "render_mac_address", return_value=""),
                patch.object(table, "render_mtu", return_value=""),
                patch.object(table, "render_enabled", return_value=""),
                patch.object(table, "render_description", return_value=""),
            ):
                result = table.format_interface_data(port_data, device)

        assert "device_selection" in result
        assert result["device_selection"] == "<select></select>"


# ===========================================================================
# LibreNMSVMInterfaceTable tests
# ===========================================================================


class TestLibreNMSVMInterfaceTable:
    """Tests for LibreNMSVMInterfaceTable (type and speed removed)."""

    def test_type_column_is_none(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        assert LibreNMSVMInterfaceTable.type is None

    def test_speed_column_is_none(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        assert LibreNMSVMInterfaceTable.speed is None

    def test_instantiation(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSVMInterfaceTable(data=[], device=mock_device)

        assert table.tab == "interfaces"

    def test_parent_column_is_in_sequence(self):
        """The Parent/LAG column must be exposed on VM pages — VMInterface supports sub-interface parents and the relationship sync path resolves VMInterface targets, so omitting it from the sequence would make the feature unreachable for VMs."""
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable

        assert "parent" in LibreNMSVMInterfaceTable.Meta.sequence


# ===========================================================================
# DeviceImportTable.__init__ integration tests
# ===========================================================================


class TestDeviceImportTableInit:
    """Tests for DeviceImportTable.__init__ with proper DB mocking."""

    def test_init_caches_querysets(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        cluster1 = MagicMock()
        cluster1.name = "Cluster-A"
        role1 = MagicMock()
        role1.name = "Switch"

        with (
            patch("virtualization.models.Cluster") as mock_cluster,
            patch("dcim.models.DeviceRole") as mock_role,
            patch("django.urls.reverse", return_value="/fake/"),
        ):
            mock_cluster.objects.all.return_value.order_by.return_value = [cluster1]
            mock_role.objects.all.return_value.order_by.return_value = [role1]

            table = DeviceImportTable(data=[])

        assert cluster1 in table._cached_clusters
        assert role1 in table._cached_roles

    def test_init_with_order_by_triggers_sort(self):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        data = [
            {"hostname": "z-host", "device_id": 2},
            {"hostname": "a-host", "device_id": 1},
        ]

        with (
            patch("virtualization.models.Cluster") as mock_cluster,
            patch("dcim.models.DeviceRole") as mock_role,
            patch("django.urls.reverse", return_value="/fake/"),
        ):
            mock_cluster.objects.all.return_value.order_by.return_value = []
            mock_role.objects.all.return_value.order_by.return_value = []

            table = DeviceImportTable(data=data, order_by=["hostname"])

        # After sorting, a-host should be first
        rows = list(table.rows)
        assert "a-host" in str(rows[0].get_cell("hostname"))


# ===========================================================================
# LibreNMSInterfaceTable.render_vlans — tagged_vlans.all() body (lines 144-145)
# ===========================================================================


class TestRenderVlansTaggedVlansIteration:
    """Cover lines 144-145: netbox_interface tagged_vlans.all() loop body."""

    def test_tagged_vlans_from_netbox_interface_stored(self):
        """When netbox_interface has tagged VLANs, their vids/group_ids are collected."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        table.device = MagicMock()
        table.device.pk = 1
        table.vlan_groups = []

        tagged_v1 = MagicMock()
        tagged_v1.vid = 200
        tagged_v1.group_id = 7
        tagged_v2 = MagicMock()
        tagged_v2.vid = 300
        tagged_v2.group_id = 8

        nb_iface = MagicMock()
        nb_iface.untagged_vlan = None
        nb_iface.tagged_vlans.all.return_value = [tagged_v1, tagged_v2]

        record = {
            "ifName": "eth0",
            "untagged_vlan": None,
            "tagged_vlans": [200, 300],
            "missing_vlans": [],
            "exists_in_netbox": True,
            "netbox_interface": nb_iface,
            "vlan_group_map": {},
        }

        with (
            patch("netbox_librenms_plugin.tables.interfaces.check_vlan_group_matches", return_value=True),
            patch("netbox_librenms_plugin.tables.interfaces.get_tagged_vlan_css_class", return_value="text-success"),
            patch("netbox_librenms_plugin.tables.interfaces.get_missing_vlan_warning", return_value=""),
        ):
            result = str(table.render_vlans(value=None, record=record))

        assert "200" in result
        assert "300" in result


class TestInterfaceTableXSSEscaping:
    """Issue #105: _render_field / render_librenms_id must escape untrusted LibreNMS values (ifName, description, MAC, …) instead of rendering them as live HTML (stored XSS)."""

    XSS = "<img src=x onerror=alert(1)>"

    def test_render_field_escapes_value_when_not_in_netbox(self):
        table = _make_interface_table()
        rendered = str(table._render_field(self.XSS, {"exists_in_netbox": False}, "ifName", "name"))
        assert "<img" not in rendered
        assert "&lt;img" in rendered

    def test_render_field_escapes_value_on_mismatch(self):
        table = _make_interface_table()
        nb = MagicMock()
        nb.name = "eth0"
        record = {"exists_in_netbox": True, "netbox_interface": nb, "ifName": self.XSS}
        rendered = str(table._render_field(self.XSS, record, "ifName", "name"))
        assert "<img" not in rendered
        assert "&lt;img" in rendered

    def test_render_librenms_id_escapes_value(self):
        table = _make_interface_table()
        rendered = str(table.render_librenms_id(self.XSS, {"exists_in_netbox": False}))
        assert "<img" not in rendered
        assert "&lt;img" in rendered

    def test_render_vlans_escapes_malicious_vid(self):
        """Sibling sink to #105: a malicious VLAN id from LibreNMS must be escaped in both the inline summary and the tooltip rather than rendered as live HTML."""
        table = _make_interface_table()
        record = {
            "untagged_vlan": self.XSS,
            "tagged_vlans": [],
            "missing_vlans": [],
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
            "ifName": "eth0",
        }
        rendered = str(table.render_vlans(value=None, record=record))
        assert "<img" not in rendered
        assert "&lt;img" in rendered

    def test_render_vlans_escapes_malicious_vid_when_missing(self):
        """Same #105 sink but with the VLAN flagged missing, so the mark_safe(warning) missing-branch is exercised."""
        table = _make_interface_table()
        record = {
            "untagged_vlan": self.XSS,
            "tagged_vlans": [],
            "missing_vlans": [self.XSS],  # vid in missing_vlans → missing branch (summary icon + tooltip)
            "exists_in_netbox": False,
            "netbox_interface": None,
            "vlan_group_map": {},
            "ifName": "eth0",
        }
        rendered = str(table.render_vlans(value=None, record=record))
        assert "<img" not in rendered
        assert "&lt;img" in rendered


# ---------------------------------------------------------------------------
# render_device_selection — XSS escape
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestRenderDeviceSelectionEscape:
    """VCCableTable.render_device_selection must HTML-escape a virtual-chassis member's name."""

    def test_member_name_is_escaped(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site, VirtualChassis

        from netbox_librenms_plugin.tables.cables import VCCableTable

        mfr, _ = Manufacturer.objects.get_or_create(name="XssMfr", slug="xssmfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="XssDT", slug="xssdt")
        role, _ = DeviceRole.objects.get_or_create(name="XssRole", slug="xssrole")
        site, _ = Site.objects.get_or_create(name="XssSite", slug="xsssite")
        vc = VirtualChassis.objects.create(name="XssVC")
        member = Device.objects.create(
            name='<script>alert("xss")</script>',
            device_type=dt,
            role=role,
            site=site,
            status="active",
            virtual_chassis=vc,
            vc_position=1,
        )

        # The device IS a VC member, so device.virtual_chassis.members.all() includes it and its
        # (malicious) name flows into the dropdown options cached in __init__.
        table = VCCableTable([], device=member)
        html = str(table.render_device_selection(None, {"local_port": "eth0", "local_port_id": "42"}))

        # The raw <script> tag must NOT appear — it should be escaped.
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_interface_table_member_name_is_escaped(self):
        """The VC interface table dropdown must escape member names just like the cable table."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device("vc-if-master-xss")
        # create() bypasses full_clean(), so the hostile name persists verbatim; the render is what
        # must neutralise it. The real ORM member list is iterated by render_device_selection().
        evil = make_device('<script>alert("xss")</script>')
        vc = VirtualChassis.objects.create(name="vc-if-xss")
        for pos, dev in enumerate((master, evil), start=1):
            dev.virtual_chassis = vc
            dev.vc_position = pos
            dev.save()

        table = VCInterfaceTable(data=[], device=master, interface_name_field="ifName")
        table.device = master
        # Non-ethernet row → selected member is the device itself (no get_virtual_chassis_member
        # lookup), so the assertion isolates the option-label escaping.
        record = {"ifName": "Vlan100", "ifType": "l3ipvlan"}
        html = str(table.render_device_selection(None, record))

        # The raw <script> tag must NOT appear — it must be escaped, matching the cable table.
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ===========================================================================
# OOB badge wiring — the shared utils.oob_badge_html helper in each table
# ===========================================================================


class TestOobBadgeWiring:
    """Each table render that shows the OOB badge must go through utils.oob_badge_html."""

    def _interface_table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        table.interface_name_field = "ifName"
        return table

    def test_interface_render_name_badges_an_oob_row(self):
        table = self._interface_table()
        # exists_in_netbox falsy → _render_field short-circuits; the render is real end-to-end.
        html = str(table.render_name("mgmt0", {"ifName": "mgmt0", "_source": "oob"}))
        assert 'title="From OOB controller"' in html
        assert "mgmt0" in html

    def test_interface_render_name_plain_row_has_no_badge(self):
        table = self._interface_table()
        html = str(table.render_name("eth0", {"ifName": "eth0", "_source": "main"}))
        assert "From OOB controller" not in html

    def test_module_render_name_badges_an_oob_row(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        html = str(table.render_name("PSU 1", {"_source": "oob", "depth": 0}))
        assert 'title="From OOB controller"' in html
        assert "PSU 1" in html

    def test_module_render_name_plain_row_has_no_badge(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        html = str(table.render_name("PSU 1", {"depth": 0}))
        assert "From OOB controller" not in html

    def test_cable_render_local_port_badges_an_oob_row_after_the_name(self):
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable

        table = object.__new__(LibreNMSCableTable)
        html = str(table.render_local_port("Gi0/1", {"_source": "oob"}))
        # Leading space: the badge follows the port name.
        assert html.startswith("Gi0/1 <span")
        assert 'title="From OOB controller"' in html

    def test_cable_render_local_port_plain_row_has_no_badge(self):
        from netbox_librenms_plugin.tables.cables import LibreNMSCableTable

        table = object.__new__(LibreNMSCableTable)
        html = str(table.render_local_port("Gi0/1", {"_source": "main"}))
        assert "From OOB controller" not in html


# ---------------------------------------------------------------------------
# Relationship owner resolution (findings S2/S3) + VM LAG button (finding #1)
# Real-DB: real Device/VC/Interface objects so button + dropdown resolution is
# exercised end-to-end rather than re-asserting mock call wiring.
# ---------------------------------------------------------------------------
class TestRelationshipOwnerResolutionConsistency:
    """The relationship sync button (data-object-id) and the VC member dropdown must resolve the same owner; otherwise the JS posts the dropdown value and the sync 404s."""

    @staticmethod
    def _vc():
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        m1 = make_device("vc-owner-a")
        m2 = make_device("vc-owner-b")
        make_virtual_chassis("VC-OWNER", m1, m2)
        return m1, m2

    @staticmethod
    def _vc_table(device):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        return VCInterfaceTable(data=[], device=device, interface_name_field="ifName")

    def test_non_ethernet_subiface_button_and_dropdown_agree_on_owner(self, db):
        """A non-ethernet sub-interface owned by another VC member: both the sync button and the member dropdown point at that member, not the viewed one."""
        from netbox_librenms_plugin.tests.conftest import make_interface

        m1, m2 = self._vc()
        sub = make_interface(m2, "Vlan100", iface_type="virtual")
        table = self._vc_table(m1)  # viewing member1
        record = {
            "ifName": "Vlan100",
            "ifType": "l3ipvlan",
            "port_id": 5,
            "netbox_interface": sub,
            "lag_sync_status": None,
            "parent_sync_status": "mismatch",
            "librenms_parent_name": "Bdi1",
            "librenms_parent_port_id": 6,
        }
        dropdown = str(table.render_device_selection(None, record))
        button = str(table.render_parent(None, record))
        assert f'value="{m2.id}" selected' in dropdown  # dropdown defaults to the true owner
        assert f'value="{m1.id}" selected' not in dropdown
        assert f'data-object-id="{m2.id}"' in button  # button agrees
        assert f'data-object-id="{m1.id}"' not in button  # not the viewed member

    def test_vc_member_resolution_is_prefetched_not_per_row(self, db):
        """The name-based VC owner resolution must prefetch members once, not query per row."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        m1, m2 = self._vc()  # positions 1 and 2
        table = self._vc_table(m1)  # viewing member1
        # Rows that fall through to the name-based VC heuristic (no netbox_interface /
        # selected_object_id), alternating between the two members' positions.
        records = [{"ifName": f"Ethernet{pos}", "ifType": "ethernetCsmacd"} for pos in (1, 2, 1, 2, 1, 2)]

        with CaptureQueriesContext(connection) as ctx:
            owners = [table._resolve_row_member_id(r) for r in records]

        # Behaviour preserved: each row resolves to the member at its name's vc_position.
        assert owners == [m1.pk, m2.pk, m1.pk, m2.pk, m1.pk, m2.pk]
        # The member set is prefetched once through the cached property.
        assert len(ctx.captured_queries) == 1


class TestVMTableHidesLagSyncButton:
    """LAG sync is device-only (VMInterface has no lag field); a VM table must not render a LAG sync button that would 404. Parent sync stays available for VMs."""

    @staticmethod
    def _vm_table():
        from netbox_librenms_plugin.tables.interfaces import LibreNMSVMInterfaceTable
        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("vm-lag")
        return LibreNMSVMInterfaceTable(data=[], device=vm, interface_name_field="ifName"), vm

    def test_vm_table_omits_lag_button(self, db):
        from virtualization.models import VMInterface

        table, vm = self._vm_table()
        record = {
            "ifName": "eth0",
            "ifType": "ethernetCsmacd",
            "port_id": 3,
            "netbox_interface": VMInterface.objects.create(virtual_machine=vm, name="eth0"),
            "lag_sync_status": "missing_nb",
            "librenms_lag_name": "Po1",
            "librenms_lag_port_id": 9,
            "parent_sync_status": None,
        }
        html = str(table.render_parent(None, record))
        assert "lag-sync-btn" not in html  # no LAG control on a VM (it could only 404)

    def test_vm_table_keeps_parent_button(self, db):
        from virtualization.models import VMInterface

        table, vm = self._vm_table()
        record = {
            "ifName": "eth0.100",
            "ifType": "l3ipvlan",
            "port_id": 4,
            "netbox_interface": VMInterface.objects.create(virtual_machine=vm, name="eth0.100"),
            "lag_sync_status": None,
            "parent_sync_status": "missing_nb",
            "librenms_parent_name": "eth0",
            "librenms_parent_port_id": 10,
        }
        html = str(table.render_parent(None, record))
        assert "parent-sync-btn" in html  # parent sync IS supported for VMs
        assert f'data-object-id="{vm.id}"' in html


@pytest.mark.django_db
class TestRenderLibreNMSId:
    """render_librenms_id colours the LibreNMS-id cell by how the stored port_id compares to NetBox.

    Restores real coverage (deleted on the parent/LAG UI work) using real Interface custom-field
    reads — a mock netbox_interface would let the get_librenms_device_id contract drift silently.
    """

    def test_no_exists_in_netbox_renders_red(self):
        table = _make_interface_table(server_key="default")
        html = table.render_librenms_id("42", {"exists_in_netbox": False})
        assert "text-danger" in html and ">42<" in html

    def test_exists_but_no_netbox_interface_renders_red(self):
        table = _make_interface_table(server_key="default")
        html = table.render_librenms_id("42", {"exists_in_netbox": True, "netbox_interface": None})
        assert "text-danger" in html

    def test_interface_without_librenms_cf_renders_red_with_title(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        iface = make_interface(make_device("rid-nocf"), "Gi0/1")  # no librenms_id CF
        table = _make_interface_table(server_key="default")
        html = table.render_librenms_id("42", {"exists_in_netbox": True, "netbox_interface": iface})
        assert "text-danger" in html and "No librenms_id" in html

    def test_mismatch_renders_orange_with_existing_id(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(make_device("rid-mismatch"), "Gi0/1")
        set_librenms_device_id(iface, 99, "default")
        iface.save()
        table = _make_interface_table(server_key="default")
        html = table.render_librenms_id("42", {"exists_in_netbox": True, "netbox_interface": iface})
        assert "text-warning" in html and "Existing LibreNMS ID: 99" in html and ">42<" in html

    def test_match_renders_green(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(make_device("rid-match"), "Gi0/1")
        set_librenms_device_id(iface, 42, "default")
        iface.save()
        table = _make_interface_table(server_key="default")
        html = table.render_librenms_id("42", {"exists_in_netbox": True, "netbox_interface": iface})
        assert "text-success" in html and ">42<" in html

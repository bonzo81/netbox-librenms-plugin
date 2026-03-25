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
"""

from unittest.mock import MagicMock, patch


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


# ===========================================================================
# DeviceImportTable._build_validation_details_url tests
# ===========================================================================


class TestBuildValidationDetailsUrl:
    """Tests for DeviceImportTable._build_validation_details_url()."""

    def _call(self, device_id, validation):
        from netbox_librenms_plugin.tables.device_status import DeviceImportTable

        with patch("netbox_librenms_plugin.tables.device_status.reverse", return_value="/validation/"):
            return DeviceImportTable._build_validation_details_url(device_id, validation)

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

    def test_server_key_stored(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = LibreNMSInterfaceTable(data=[], device=mock_device, server_key="prod")

        assert table.server_key == "prod"


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
# LibreNMSInterfaceTable render_librenms_id tests
# ===========================================================================


class TestRenderLibreNMSId:
    """Tests for LibreNMSInterfaceTable.render_librenms_id()."""

    def _table(self, server_key="default"):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        t = object.__new__(LibreNMSInterfaceTable)
        t.server_key = server_key
        return t

    def test_not_in_netbox_returns_danger(self):
        table = self._table()
        record = {"exists_in_netbox": False, "netbox_interface": None}
        result = str(table.render_librenms_id(value=123, record=record))
        assert "text-danger" in result
        assert "123" in result

    def test_no_netbox_interface_returns_danger(self):
        table = self._table()
        record = {"exists_in_netbox": True, "netbox_interface": None}
        result = str(table.render_librenms_id(value=123, record=record))
        assert "text-danger" in result

    def test_netbox_librenms_id_is_none_returns_danger(self):
        table = self._table()
        nb_iface = MagicMock()
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface}

        with patch(
            "netbox_librenms_plugin.tables.interfaces.get_librenms_device_id",
            return_value=None,
        ):
            result = str(table.render_librenms_id(value=456, record=record))

        assert "text-danger" in result
        assert "No librenms_id" in result

    def test_ids_match_returns_success(self):
        table = self._table()
        nb_iface = MagicMock()
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface}

        with patch(
            "netbox_librenms_plugin.tables.interfaces.get_librenms_device_id",
            return_value=42,
        ):
            result = str(table.render_librenms_id(value=42, record=record))

        assert "text-success" in result

    def test_ids_mismatch_returns_warning(self):
        table = self._table()
        nb_iface = MagicMock()
        record = {"exists_in_netbox": True, "netbox_interface": nb_iface}

        with patch(
            "netbox_librenms_plugin.tables.interfaces.get_librenms_device_id",
            return_value=99,
        ):
            result = str(table.render_librenms_id(value=42, record=record))

        assert "text-warning" in result
        assert "Existing LibreNMS ID: 99" in result


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


class TestGetInterfaceMapping:
    """Tests for LibreNMSInterfaceTable.get_interface_mapping()."""

    def _table(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        return object.__new__(LibreNMSInterfaceTable)

    def test_exact_match_returned(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        mapping = MagicMock()

        with patch("netbox_librenms_plugin.tables.interfaces.InterfaceTypeMapping") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = mapping
            result = table.get_interface_mapping("ethernetCsmacd", 1000000)

        assert result is mapping

    def test_fallback_type_only_match(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)
        fallback_mapping = MagicMock()

        def mock_filter_side_effect(**kwargs):
            mock_qs = MagicMock()
            if "librenms_speed" in kwargs and kwargs["librenms_speed"] is not None:
                # Exact match with speed → returns None
                mock_qs.first.return_value = None
            else:
                # Type-only match
                mock_qs.first.return_value = fallback_mapping
            return mock_qs

        with patch("netbox_librenms_plugin.tables.interfaces.InterfaceTypeMapping") as mock_model:
            mock_model.objects.filter.side_effect = mock_filter_side_effect
            result = table.get_interface_mapping("ethernetCsmacd", 1000000)

        assert result is fallback_mapping

    def test_no_match_returns_none(self):
        from netbox_librenms_plugin.tables.interfaces import LibreNMSInterfaceTable

        table = object.__new__(LibreNMSInterfaceTable)

        with patch("netbox_librenms_plugin.tables.interfaces.InterfaceTypeMapping") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            result = table.get_interface_mapping("unknown_type", 0)

        assert result is None


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

    def test_render_device_selection_ethernet_uses_vc_member(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        mock_member1 = MagicMock()
        mock_member1.id = 1
        mock_member1.name = "switch-1"

        mock_member2 = MagicMock()
        mock_member2.id = 2
        mock_member2.name = "switch-2"

        mock_device = MagicMock()
        mock_device.id = 1
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [mock_member1, mock_member2]

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = VCInterfaceTable(data=[], device=mock_device, interface_name_field="ifName")

        table.device = mock_device

        record = {
            "ifName": "Gi1/0/1",
            "ifType": "ethernetCsmacd",
        }

        with patch(
            "netbox_librenms_plugin.tables.interfaces.get_virtual_chassis_member",
            return_value=mock_member1,
        ):
            result = str(table.render_device_selection(value=None, record=record))

        assert "switch-1" in result
        assert "switch-2" in result
        assert "vc-member-select" in result

    def test_render_device_selection_non_ethernet_uses_device_id(self):
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        mock_member1 = MagicMock()
        mock_member1.id = 1
        mock_member1.name = "switch-1"

        mock_device = MagicMock()
        mock_device.id = 1
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [mock_member1]

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = VCInterfaceTable(data=[], device=mock_device, interface_name_field="ifName")

        table.device = mock_device
        record = {
            "ifName": "Vlan100",
            "ifType": "l3ipvlan",  # non-ethernet
        }

        result = str(table.render_device_selection(value=None, record=record))

        assert "switch-1" in result
        assert 'value="1"' in result

    def test_render_device_selection_no_member_uses_device_id(self):
        """When get_virtual_chassis_member returns None, fall back to device.id."""
        from netbox_librenms_plugin.tables.interfaces import VCInterfaceTable

        mock_member1 = MagicMock()
        mock_member1.id = 1
        mock_member1.name = "switch-1"

        mock_device = MagicMock()
        mock_device.id = 1
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [mock_member1]

        with patch("netbox_librenms_plugin.tables.interfaces.get_interface_name_field", return_value="ifName"):
            table = VCInterfaceTable(data=[], device=mock_device, interface_name_field="ifName")

        table.device = mock_device
        record = {
            "ifName": "Gi2/0/1",
            "ifType": "ethernetcsmacd",
        }

        with patch(
            "netbox_librenms_plugin.tables.interfaces.get_virtual_chassis_member",
            return_value=None,
        ):
            result = str(table.render_device_selection(value=None, record=record))

        # When chassis_member is None, selected_member_id = self.device.id
        assert "switch-1" in result

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

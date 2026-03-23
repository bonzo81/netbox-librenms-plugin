"""Unit tests for SyncInterfacesView: update_interface_attributes and handle_mac_address."""

from unittest.mock import MagicMock, patch

import pytest


class TestUpdateInterfaceAttributes:
    """update_interface_attributes() must set fields respecting exclude_columns."""

    @pytest.fixture
    def view(self, mock_librenms_api):
        """Return a SyncInterfacesView wired to the shared mock API fixture."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        v = object.__new__(SyncInterfacesView)
        v._librenms_api = mock_librenms_api
        v.request = MagicMock()
        v._lookup_maps = {}
        return v

    def _make_device_interface(self, **extra):
        """Return a MagicMock mimicking a dcim.Interface."""
        from dcim.models import Interface  # noqa: F401

        iface = MagicMock(
            spec=[
                "name",
                "type",
                "speed",
                "description",
                "mtu",
                "enabled",
                "save",
                "cf",
                "custom_field_data",
                "mac_addresses",
                "primary_mac_address",
            ]
        )
        iface.cf = {"librenms_id": {"default": 1}}
        iface.__class__ = Interface
        for k, v in extra.items():
            setattr(iface, k, v)
        return iface

    def test_sets_speed_via_convert(self, view):
        iface = self._make_device_interface()
        librenms_data = {"ifName": "eth0", "ifSpeed": 1_000_000_000}

        with patch(
            "netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1_000_000
        ) as mock_convert:
            with patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id"):
                view.update_interface_attributes(iface, librenms_data, "1000base-t", set(), "ifName")

        mock_convert.assert_called_once_with(1_000_000_000)
        assert iface.speed == 1_000_000

    def test_skips_excluded_columns(self, view):
        speed_sentinel = object()
        iface = self._make_device_interface(speed=speed_sentinel)
        librenms_data = {"ifName": "eth0", "ifSpeed": 1_000_000_000, "ifAlias": "uplink"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1_000_000):
            with patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id"):
                view.update_interface_attributes(iface, librenms_data, "1000base-t", {"speed"}, "ifName")

        # speed should NOT have been mutated (excluded)
        assert iface.speed is speed_sentinel

    def test_sets_type_for_device_interface(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0", "ifType": "ethernetCsmacd"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, "1000base-t", set(), "ifName")

        assert iface.type == "1000base-t"

    def test_does_not_set_type_for_vm_interface(self, view):
        from virtualization.models import VMInterface

        iface = MagicMock()
        iface.__class__ = VMInterface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        original_type = "some_type"
        iface.type = original_type
        librenms_data = {"ifName": "eth0", "ifType": "ethernetCsmacd"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, "1000base-t", set(), "ifName")

        # type is NOT in the mapping for non-device interfaces (type set only if is_device_interface)
        assert iface.type == original_type

    def test_sets_description_only_when_alias_differs_from_name(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        desc_sentinel = object()
        iface.description = desc_sentinel

        # ifAlias == interface name field value → description should NOT be set
        librenms_data = {"ifName": "eth0", "ifAlias": "eth0"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        assert iface.description is desc_sentinel  # untouched: alias == name, no update

    def test_sets_description_when_alias_differs(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()

        librenms_data = {"ifName": "eth0", "ifAlias": "uplink-port"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        assert iface.description == "uplink-port"

    def test_sets_librenms_id_when_port_id_present(self, view):
        """
        set_librenms_device_id() is called unconditionally when port_id is not None.

        Previously the call was guarded by ``"librenms_id" in interface.cf``, which
        prevented the mapping from being created for brand-new interfaces. The fix
        (this PR) drops that guard so first-time writes are handled correctly.
        """
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}  # empty — first-time write, no existing mapping
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0", "port_id": 77}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id") as mock_set:
                view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        mock_set.assert_called_once_with(iface, 77, view._librenms_api.server_key)

    def test_does_not_set_librenms_id_when_port_id_none(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {"librenms_id": {"default": 1}}
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0", "port_id": None}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id") as mock_set:
                view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        mock_set.assert_not_called()

    def test_sets_enabled_true_when_admin_status_none(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0", "ifAdminStatus": None}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        assert iface.enabled is True

    def test_sets_enabled_based_on_admin_status_string(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0", "ifAdminStatus": "down"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        assert iface.enabled is False

    def test_calls_save_at_end(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(iface, librenms_data, None, {"type", "speed", "mtu"}, "ifName")

        iface.save.assert_called_once()

    def test_excludes_mac_address_when_in_excluded(self, view):
        from dcim.models import Interface

        iface = MagicMock()
        iface.__class__ = Interface
        iface.cf = {}
        iface.mac_addresses = MagicMock()
        librenms_data = {"ifName": "eth0", "ifPhysAddress": "aa:bb:cc:dd:ee:ff"}

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            with patch.object(view, "handle_mac_address") as mock_mac:
                view.update_interface_attributes(iface, librenms_data, None, {"mac_address"}, "ifName")

        mock_mac.assert_not_called()


class TestHandleMacAddress:
    """
    handle_mac_address() must work for both Interface (has primary_mac_address)
    and VMInterface (does not have primary_mac_address)."""

    @pytest.fixture
    def view(self, mock_librenms_api):
        """Return a SyncInterfacesView wired to the shared mock API fixture."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        v = object.__new__(SyncInterfacesView)
        v._librenms_api = mock_librenms_api
        v.request = MagicMock()
        v._lookup_maps = {}
        return v

    def test_creates_new_mac_and_adds_to_interface(self, view):
        iface = MagicMock()
        iface.mac_addresses = MagicMock()
        iface.mac_addresses.filter.return_value.first.return_value = None
        new_mac = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_cls:
            mock_cls.objects.create.return_value = new_mac
            view.handle_mac_address(iface, "aa:bb:cc:dd:ee:ff")

        mock_cls.objects.create.assert_called_once_with(mac_address="aa:bb:cc:dd:ee:ff")
        iface.mac_addresses.add.assert_called_once_with(new_mac)

    def test_reuses_existing_mac(self, view):
        existing_mac = MagicMock()
        iface = MagicMock()
        iface.mac_addresses = MagicMock()
        iface.mac_addresses.filter.return_value.first.return_value = existing_mac

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_cls:
            view.handle_mac_address(iface, "aa:bb:cc:dd:ee:ff")

        mock_cls.objects.create.assert_not_called()
        iface.mac_addresses.add.assert_called_once_with(existing_mac)

    def test_sets_primary_mac_when_attribute_present(self, view):
        mac_obj = MagicMock()
        iface = MagicMock(spec=["mac_addresses", "primary_mac_address"])
        iface.mac_addresses = MagicMock()
        iface.mac_addresses.filter.return_value.first.return_value = None

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_cls:
            mock_cls.objects.create.return_value = mac_obj
            view.handle_mac_address(iface, "aa:bb:cc:dd:ee:ff")

        assert iface.primary_mac_address is mac_obj

    def test_no_error_when_primary_mac_attribute_absent(self, view):
        """VMInterface does not have primary_mac_address — handle_mac_address must not raise."""
        mac_obj = MagicMock()
        iface = MagicMock(spec=["mac_addresses"])  # no primary_mac_address attr
        iface.mac_addresses = MagicMock()
        iface.mac_addresses.filter.return_value.first.return_value = None

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_cls:
            mock_cls.objects.create.return_value = mac_obj
            # Must not raise AttributeError
            view.handle_mac_address(iface, "aa:bb:cc:dd:ee:ff")

    def test_noop_when_mac_address_is_falsy(self, view):
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_cls:
            view.handle_mac_address(iface, "")
            view.handle_mac_address(iface, None)

        mock_cls.objects.create.assert_not_called()

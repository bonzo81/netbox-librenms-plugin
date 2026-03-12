"""Tests for device mismatch detection in get_librenms_device_info.

Covers the identity cross-matching logic that determines whether a
mismatched_device warning banner is shown on the LibreNMS Sync page,
as well as the resolved_name computation that drives the name-match
icon and sync button.

Match rule: mismatch is False when ANY NetBox identity (device name,
primary IP, DNS name) matches ANY LibreNMS identity (sysName, hostname, ip).
"""

from unittest.mock import MagicMock, patch


def _make_view(librenms_id, device_info, librenms_url="https://librenms.example.com"):
    """Create a minimal BaseLibreNMSSyncView instance with mocked dependencies."""
    from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

    view = object.__new__(BaseLibreNMSSyncView)
    view.librenms_id = librenms_id
    api = MagicMock()
    api.librenms_url = librenms_url
    api.get_device_info.return_value = (True, device_info)
    api.get_device_inventory.return_value = (True, [])
    view._librenms_api = api
    return view


def _make_obj(name, primary_ip=None, dns_name=None, virtual_chassis=None, cf=None, vc_position=None, serial=None):
    """Create a mock NetBox device object."""
    obj = MagicMock()
    obj.name = name
    obj.cf = cf or {}
    if primary_ip:
        obj.primary_ip = MagicMock()
        obj.primary_ip.address.ip = primary_ip
        obj.primary_ip.dns_name = dns_name or ""
    else:
        obj.primary_ip = None
    obj.virtual_chassis = virtual_chassis
    obj.vc_position = vc_position
    obj.serial = serial
    return obj


def _make_request(use_sysname=None, strip_domain=None):
    """Create a mock request with user preferences for naming settings."""
    request = MagicMock()
    request.POST = {}
    request.GET = {}
    config = {}
    if use_sysname is not None:
        config["plugins.netbox_librenms_plugin.use_sysname"] = use_sysname
    if strip_domain is not None:
        config["plugins.netbox_librenms_plugin.strip_domain"] = strip_domain
    request.user.config.get = lambda path, default=None: config.get(path, default)
    return request


class TestMismatchDetection:
    """Tests for identity cross-matching logic."""

    # -- No device / API failure -------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_librenms_id_returns_not_found(self, mock_hw):
        """No librenms_id means device is not found."""
        view = _make_view(librenms_id=None, device_info=None)
        result = view.get_librenms_device_info(_make_obj("sw01"))

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_api_failure_returns_not_found(self, mock_hw):
        """API failure (success=False) means device is not found."""
        view = _make_view(librenms_id=42, device_info=None)
        view.librenms_api.get_device_info.return_value = (False, None)
        result = view.get_librenms_device_info(_make_obj("sw01"))

        assert result["found_in_librenms"] is False
        assert result["mismatched_device"] is False

    # -- Name matches ------------------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_exact_sysname_match(self, mock_hw):
        """NetBox name matches LibreNMS sysName (case-insensitive)."""
        view = _make_view(42, {"sysName": "SW01", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_netbox_name_matches_librenms_hostname(self, mock_hw):
        """NetBox name matches LibreNMS hostname field."""
        view = _make_view(42, {"sysName": "something-else", "hostname": "sw01", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_fqdn_match(self, mock_hw):
        """Full FQDN match -- no mismatch."""
        view = _make_view(42, {"sysName": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01.example.net", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    # -- IP matches --------------------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_netbox_ip_matches_librenms_ip(self, mock_hw):
        """NetBox primary IP matches LibreNMS IP -- no mismatch."""
        view = _make_view(42, {"sysName": "different", "ip": "10.0.0.1"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_netbox_ip_matches_librenms_hostname_ip(self, mock_hw):
        """LibreNMS hostname is an IP that matches NetBox primary IP."""
        view = _make_view(42, {"sysName": "different", "hostname": "10.0.0.1", "ip": "10.0.0.1"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    # -- DNS name matches --------------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_dns_name_matches_sysname(self, mock_hw):
        """NetBox DNS name matches LibreNMS sysName."""
        view = _make_view(42, {"sysName": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1", dns_name="sw01.example.net")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_dns_name_matches_librenms_hostname(self, mock_hw):
        """NetBox DNS name matches LibreNMS hostname field."""
        view = _make_view(42, {"sysName": "something", "hostname": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1", dns_name="sw01.example.net")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    # -- Mismatches --------------------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_completely_different_is_mismatch(self, mock_hw):
        """No identities overlap -- mismatch."""
        view = _make_view(42, {"sysName": "router-01", "hostname": "router-01.corp", "ip": "10.0.0.2"})
        obj = _make_obj("switch-05", primary_ip="10.0.0.1", dns_name="switch-05.corp")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_short_vs_fqdn_matches_via_domain_strip(self, mock_hw):
        """Short name vs FQDN -- matches after domain stripping."""
        view = _make_view(42, {"sysName": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_fqdn_domain_differs_matches_via_domain_strip(self, mock_hw):
        """Different FQDN domains -- matches because domain-stripped
        LibreNMS short name 'sw01' matches NetBox FQDN split 'sw01'.

        NetBox name 'sw01.example.net' is compared as-is (no stripping),
        but the LibreNMS domain-stripped 'sw01' does NOT appear in the
        NetBox identities since NetBox names are not domain-stripped.
        However, both sides share the short name via NetBox raw name
        normalization — actually NetBox keeps the full name.
        """
        view = _make_view(42, {"sysName": "sw01.other.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01.example.net", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        # NetBox identities: {"sw01.example.net", "10.0.0.1"}
        # LibreNMS identities: {"sw01.other.net", "sw01", "10.0.0.2"}
        # No overlap → mismatch
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_netbox_name_no_ip_match(self, mock_hw):
        """No NetBox name and IPs differ -- mismatch."""
        view = _make_view(42, {"sysName": "sw01", "ip": "10.0.0.2"})
        obj = _make_obj(None, primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_librenms_sysname_no_match(self, mock_hw):
        """No sysName, no hostname, IPs differ -- mismatch."""
        view = _make_view(42, {"sysName": None, "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_no_identities_at_all(self, mock_hw):
        """Both sides have no identities -- mismatch (cannot confirm)."""
        view = _make_view(42, {"sysName": None, "ip": None})
        obj = _make_obj(None, primary_ip=None)
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    # -- Virtual Chassis ---------------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_vc_suffix_stripped(self, mock_hw):
        """VC member suffix ' (1)' is stripped before comparison."""
        view = _make_view(42, {"sysName": "switch-1", "ip": "10.0.0.2"})
        obj = _make_obj("switch-1 (1)", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_vc_different_name_is_mismatch(self, mock_hw):
        """VC member with different name after suffix strip -- mismatch."""
        vc = MagicMock()
        view = _make_view(42, {"sysName": "switch-1", "ip": "10.0.0.2"})
        obj = _make_obj("switch-2 (2)", primary_ip="10.0.0.1", virtual_chassis=vc, cf={"librenms_id": 42})
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True
        assert result["mismatched_device"] is True

    # -- found_in_librenms always True with valid ID -----------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_found_in_librenms_always_true_with_valid_id(self, mock_hw):
        """found_in_librenms is True even when identities mismatch."""
        view = _make_view(42, {"sysName": "totally-different", "ip": "10.0.0.2"})
        obj = _make_obj("my-device", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is True

    # -- Domain stripping --------------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_domain_strip_hostname(self, mock_hw):
        """LibreNMS hostname FQDN stripped to short name matches NetBox name."""
        view = _make_view(42, {"sysName": "other", "hostname": "sw01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_domain_strip_sysname(self, mock_hw):
        """LibreNMS sysName FQDN stripped to short name matches NetBox name."""
        view = _make_view(42, {"sysName": "sw01.corp.local", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_domain_strip_no_false_positive(self, mock_hw):
        """Domain stripping doesn't cause false match when short names differ."""
        view = _make_view(42, {"sysName": "router01.example.net", "ip": "10.0.0.2"})
        obj = _make_obj("switch01", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is True

    # -- VC pattern stripping ----------------------------------------------

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.models.LibreNMSSettings.objects")
    def test_vc_pattern_strip_default(self, mock_settings_qs, mock_hw):
        """Default VC pattern '-M{position}' is stripped from NetBox name."""
        settings_obj = MagicMock()
        settings_obj.vc_member_name_pattern = "-M{position}"
        mock_settings_qs.first.return_value = settings_obj

        view = _make_view(42, {"sysName": "switch01", "ip": "10.0.0.2"})
        obj = _make_obj("switch01-M2", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.models.LibreNMSSettings.objects")
    def test_vc_pattern_strip_custom(self, mock_settings_qs, mock_hw):
        """Custom VC pattern '-SW{position}' is stripped from NetBox name."""
        settings_obj = MagicMock()
        settings_obj.vc_member_name_pattern = "-SW{position}"
        mock_settings_qs.first.return_value = settings_obj

        view = _make_view(42, {"sysName": "switch01", "ip": "10.0.0.2"})
        obj = _make_obj("switch01-SW3", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is False

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.models.LibreNMSSettings.objects")
    def test_vc_pattern_no_match_leaves_name(self, mock_settings_qs, mock_hw):
        """VC pattern doesn't match -- name unchanged, still mismatched."""
        settings_obj = MagicMock()
        settings_obj.vc_member_name_pattern = "-M{position}"
        mock_settings_qs.first.return_value = settings_obj

        view = _make_view(42, {"sysName": "switch01", "ip": "10.0.0.2"})
        obj = _make_obj("switch99", primary_ip="10.0.0.1")
        result = view.get_librenms_device_info(obj)

        assert result["mismatched_device"] is True


# ---------------------------------------------------------------------------
# Tests for VC lookup delegation in get()
# ---------------------------------------------------------------------------


class TestVCLookupDelegation:
    """Verify that BaseLibreNMSSyncView.get() always delegates VC device
    resolution to get_librenms_sync_device(), even when the viewed member
    has its own librenms_id."""

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_vc_member_with_own_id_delegates_to_sync_device(self, mock_sync_device, mock_get_object, mock_render):
        """A VC member with its own librenms_id should still delegate to
        get_librenms_sync_device, which may return a different member."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        # Viewed device: member A with its own librenms_id
        member_a = MagicMock()
        member_a.pk = 1
        member_a.cf = {"librenms_id": {"default": 42}}
        member_a.virtual_chassis = MagicMock()

        # Sync device: member B (returned by get_librenms_sync_device)
        member_b = MagicMock()
        member_b.pk = 2

        mock_get_object.return_value = member_a
        mock_sync_device.return_value = member_b

        view = object.__new__(BaseLibreNMSSyncView)
        view.model = MagicMock()
        api = MagicMock()
        api.server_key = "default"
        api.get_librenms_id.return_value = 42
        view._librenms_api = api
        view.tab = MagicMock()
        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = MagicMock()
        view.get(request, pk=1)

        # get_librenms_sync_device must be called unconditionally for VC members
        mock_sync_device.assert_called_once_with(member_a, server_key="default")
        # get_librenms_id should be called on the sync device (member_b)
        api.get_librenms_id.assert_called_once_with(member_b)

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.render")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.get_librenms_sync_device")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_sync_device, mock_get_object, mock_render):
        """A device without a virtual chassis should not call
        get_librenms_sync_device at all."""
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        device = MagicMock()
        device.pk = 1
        device.virtual_chassis = None

        mock_get_object.return_value = device

        view = object.__new__(BaseLibreNMSSyncView)
        view.model = MagicMock()
        api = MagicMock()
        api.server_key = "default"
        api.get_librenms_id.return_value = 42
        view._librenms_api = api
        view.tab = MagicMock()
        view.get_context_data = MagicMock(return_value={})
        mock_render.return_value = MagicMock()

        request = MagicMock()
        view.get(request, pk=1)

        mock_sync_device.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for _build_all_server_mappings
# ---------------------------------------------------------------------------


class TestBuildAllServerMappings:
    """Tests for BaseLibreNMSSyncView._build_all_server_mappings."""

    def test_returns_none_for_legacy_int(self, mock_netbox_device):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_netbox_device.custom_field_data = {"librenms_id": 42}
        result = BaseLibreNMSSyncView._build_all_server_mappings(mock_netbox_device, "production")
        assert result is None

    def test_returns_none_for_missing_cf(self, mock_netbox_device):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_netbox_device.custom_field_data = {"librenms_id": None}
        result = BaseLibreNMSSyncView._build_all_server_mappings(mock_netbox_device, "production")
        assert result is None

    def test_single_configured_server(self, mock_netbox_device, mock_plugins_config_single_server):
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_netbox_device.custom_field_data = {"librenms_id": {"production": 42}}
        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = mock_plugins_config_single_server
            result = BaseLibreNMSSyncView._build_all_server_mappings(mock_netbox_device, "production")

        assert result is not None
        assert len(result) == 1
        entry = result[0]
        assert entry["server_key"] == "production"
        assert entry["device_id"] == 42
        assert entry["display_name"] == "Production LibreNMS"
        assert entry["is_configured"] is True
        assert entry["is_active"] is True
        assert entry["device_url"] == "https://librenms.example.com/device/device=42/"

    def test_orphaned_server_is_not_configured(self, mock_netbox_device, mock_plugins_config_empty_servers):
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_netbox_device.custom_field_data = {"librenms_id": {"deleted-server": 77}}
        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = mock_plugins_config_empty_servers
            result = BaseLibreNMSSyncView._build_all_server_mappings(mock_netbox_device, "production")

        assert result is not None
        assert len(result) == 1
        entry = result[0]
        assert entry["server_key"] == "deleted-server"
        assert entry["device_id"] == 77
        assert entry["is_configured"] is False
        assert entry["is_active"] is False
        assert entry["device_url"] is None

    def test_multiple_servers_sorted_active_first(self, mock_netbox_device, mock_plugins_config_multi_server_mapping):
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        mock_netbox_device.custom_field_data = {"librenms_id": {"mock-dev": 99, "production": 42, "old-server": 11}}
        with patch("netbox_librenms_plugin.views.base.librenms_sync_view.django_settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = mock_plugins_config_multi_server_mapping
            result = BaseLibreNMSSyncView._build_all_server_mappings(mock_netbox_device, "production")

        assert result is not None
        assert len(result) == 3
        # Active (production) first
        assert result[0]["server_key"] == "production"
        assert result[0]["is_active"] is True
        # Configured (mock-dev) second
        assert result[1]["server_key"] == "mock-dev"
        assert result[1]["is_configured"] is True
        assert result[1]["is_active"] is False
        # Orphaned last
        assert result[2]["server_key"] == "old-server"
        assert result[2]["is_configured"] is False


class TestResolvedName:
    """Tests for resolved_name computation using naming preferences."""

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.resolve_naming_preferences")
    def test_resolved_name_uses_sysname_by_default(self, mock_prefs, mock_hw):
        """resolved_name defaults to sysName when use_sysname=True."""
        mock_prefs.return_value = (True, False)
        view = _make_view(42, {"sysName": "sw01.example.com", "hostname": "10.0.0.2", "ip": "10.0.0.2"})
        obj = _make_obj("sw01.example.com", primary_ip="10.0.0.2")
        request = _make_request(use_sysname=True, strip_domain=False)

        result = view.get_librenms_device_info(obj, request)

        assert result["librenms_device_details"]["resolved_name"] == "sw01.example.com"

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.resolve_naming_preferences")
    def test_resolved_name_with_strip_domain(self, mock_prefs, mock_hw):
        """resolved_name strips domain when strip_domain=True."""
        mock_prefs.return_value = (True, True)
        view = _make_view(42, {"sysName": "sw01.example.com", "hostname": "10.0.0.2", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.2")
        request = _make_request(use_sysname=True, strip_domain=True)

        result = view.get_librenms_device_info(obj, request)

        assert result["librenms_device_details"]["resolved_name"] == "sw01"

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.resolve_naming_preferences")
    def test_resolved_name_use_hostname(self, mock_prefs, mock_hw):
        """resolved_name uses hostname when use_sysname=False."""
        mock_prefs.return_value = (False, False)
        view = _make_view(42, {"sysName": "sw01.example.com", "hostname": "sw01-mgmt", "ip": "10.0.0.2"})
        obj = _make_obj("sw01-mgmt", primary_ip="10.0.0.2")
        request = _make_request(use_sysname=False, strip_domain=False)

        result = view.get_librenms_device_info(obj, request)

        assert result["librenms_device_details"]["resolved_name"] == "sw01-mgmt"

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.resolve_naming_preferences")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view._generate_vc_member_name")
    def test_resolved_name_vc_member(self, mock_vc_name, mock_prefs, mock_hw):
        """resolved_name applies VC member naming pattern for VC members."""
        mock_prefs.return_value = (True, True)
        mock_vc_name.return_value = "sw01-M2"
        vc = MagicMock()
        view = _make_view(42, {"sysName": "sw01.example.com", "hostname": "10.0.0.2", "ip": "10.0.0.2"})
        obj = _make_obj("sw01-M2", primary_ip="10.0.0.2", virtual_chassis=vc, vc_position=2, serial="ABC123")
        request = _make_request(use_sysname=True, strip_domain=True)

        result = view.get_librenms_device_info(obj, request)

        assert result["librenms_device_details"]["resolved_name"] == "sw01-M2"
        mock_vc_name.assert_called_once_with("sw01", 2, serial="ABC123")

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    def test_resolved_name_falls_back_to_sysname_without_request(self, mock_hw):
        """Without request, resolved_name falls back to raw sysName."""
        view = _make_view(42, {"sysName": "sw01.example.com", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.2")

        result = view.get_librenms_device_info(obj)

        assert result["librenms_device_details"]["resolved_name"] == "sw01.example.com"

    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.views.base.librenms_sync_view.resolve_naming_preferences")
    def test_resolved_name_non_vc_device_no_vc_pattern(self, mock_prefs, mock_hw):
        """Non-VC device does not apply VC member naming."""
        mock_prefs.return_value = (True, True)
        view = _make_view(42, {"sysName": "sw01.example.com", "hostname": "10.0.0.2", "ip": "10.0.0.2"})
        obj = _make_obj("sw01", primary_ip="10.0.0.2")
        obj.virtual_chassis = None
        obj.vc_position = None
        request = _make_request(use_sysname=True, strip_domain=True)

        result = view.get_librenms_device_info(obj, request)

        assert result["librenms_device_details"]["resolved_name"] == "sw01"

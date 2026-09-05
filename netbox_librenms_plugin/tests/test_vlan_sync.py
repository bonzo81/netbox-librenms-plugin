"""
Tests for VLAN sync feature.

Tests cover:
- LibreNMS VLAN API methods
- VLAN mode detection logic
- VLAN comparison logic
- Port VLAN data parsing
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests import test_librenms_api_helpers

# Bind the helper's autouse fixture into this module so it patches the config here only.
# `pytest_plugins` would register it session-wide and shadow PLUGINS_CONFIG for later tests.
mock_librenms_config = test_librenms_api_helpers.mock_librenms_config


# ============================================
# TEST DATA FIXTURES
# ============================================

# Sample LibreNMS VLAN response (from /resources/vlans endpoint)
# Note: This endpoint includes vlan_id and device_id, unlike /devices/{id}/vlans
MOCK_DEVICE_VLANS = {
    "status": "ok",
    "vlans": [
        {
            "vlan_id": 101,
            "device_id": 123,
            "vlan_vlan": 1,
            "vlan_name": "default",
            "vlan_type": "ethernet",
            "vlan_state": 1,
            "vlan_domain": 1,
        },
        {
            "vlan_id": 102,
            "device_id": 123,
            "vlan_vlan": 50,
            "vlan_name": "ORG_DATA",
            "vlan_type": "ethernet",
            "vlan_state": 1,
            "vlan_domain": 1,
        },
        {
            "vlan_id": 103,
            "device_id": 123,
            "vlan_vlan": 60,
            "vlan_name": "ORG_VOICE",
            "vlan_type": "ethernet",
            "vlan_state": 1,
            "vlan_domain": 1,
        },
    ],
    "count": 3,
}

# Sample port VLAN info response (bulk call)
MOCK_PORT_VLAN_INFO = {
    "status": "ok",
    "ports": [
        {"port_id": 114184, "ifName": "Gi1/0/40", "ifVlan": "50", "ifTrunk": None},
        {"port_id": 114326, "ifName": "Gi3/0/48", "ifVlan": "1", "ifTrunk": "dot1Q"},
        {"port_id": 114327, "ifName": "Gi3/1/1", "ifVlan": "1", "ifTrunk": None},
        {"port_id": 114145, "ifName": "Gi1/0/1", "ifVlan": "", "ifTrunk": None},  # No VLAN
    ],
}

# Sample port with vlans detail response (for trunk port)
MOCK_PORT_VLAN_DETAILS_TRUNK = {
    "status": "ok",
    "port": [
        {
            "port_id": 227011,
            "ifName": "Te1/1/1",
            "ifVlan": "90",
            "ifTrunk": "dot1Q",
            "vlans": [
                {"vlan": 90, "untagged": 1, "state": "unknown", "port_vlan_id": 195164},
                {"vlan": 50, "untagged": 0, "state": "forwarding", "port_vlan_id": 2165422},
            ],
        }
    ],
}

# Sample port with vlans detail response (for access port)
MOCK_PORT_VLAN_DETAILS_ACCESS = {
    "status": "ok",
    "port": [
        {
            "port_id": 729403,
            "ifName": "Gi0/2",
            "ifVlan": "50",
            "ifTrunk": None,
            "vlans": [
                {"vlan": 50, "untagged": 1, "state": "forwarding", "port_vlan_id": 3234550},
            ],
        }
    ],
}


def create_mock_device():
    """Create a mock NetBox device."""
    device = MagicMock()
    device.pk = 123
    device.name = "test-switch"
    device._meta.model_name = "device"
    device.site = MagicMock()
    device.site.pk = 1
    device.site.name = "Test Site"
    return device


def create_mock_interface(name, mode=None, untagged_vlan=None, tagged_vlans=None):
    """Create a mock NetBox interface."""
    interface = MagicMock()
    interface.pk = hash(name)
    interface.name = name
    interface.mode = mode
    interface.untagged_vlan = untagged_vlan
    interface.tagged_vlans = MagicMock()
    interface.tagged_vlans.all.return_value = tagged_vlans or []
    return interface


def create_mock_vlan(vid, name, group=None):
    """Create a mock NetBox VLAN."""
    vlan = MagicMock()
    vlan.pk = vid * 100
    vlan.vid = vid
    vlan.name = name
    vlan.group = group
    return vlan


# ============================================
# API METHOD TESTS
# ============================================


class TestVLANAPIClient:
    """Tests for LibreNMS VLAN API methods."""

    @patch("requests.get")
    def test_get_device_vlans_success(self, mock_get, mock_librenms_config):
        """Test successful VLAN fetch from /resources/vlans endpoint."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = MOCK_DEVICE_VLANS

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        success, data = api.get_device_vlans(123)

        assert success is True
        assert len(data) == 3
        assert data[1]["vlan_vlan"] == 50
        assert data[1]["vlan_name"] == "ORG_DATA"
        # Verify vlan_id is present from /resources/vlans endpoint
        assert data[1]["vlan_id"] == 102

    @patch("requests.get")
    def test_get_device_vlans_filters_by_device_id(self, mock_get, mock_librenms_config):
        """Test that VLANs are filtered by device_id."""
        # Response includes VLANs from multiple devices
        mock_response_data = {
            "status": "ok",
            "vlans": [
                {"vlan_id": 101, "device_id": 123, "vlan_vlan": 1, "vlan_name": "default"},
                {"vlan_id": 201, "device_id": 456, "vlan_vlan": 1, "vlan_name": "default"},  # Different device
                {"vlan_id": 102, "device_id": 123, "vlan_vlan": 50, "vlan_name": "DATA"},
            ],
        }
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_response_data

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, data = api.get_device_vlans(123)

        assert success is True
        assert len(data) == 2  # Only device 123's VLANs
        assert all(str(v["device_id"]) == "123" for v in data)

    @patch("requests.get")
    def test_get_device_vlans_error(self, mock_get, mock_librenms_config):
        """Test VLAN fetch with error."""
        from requests.exceptions import HTTPError

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = HTTPError(response=mock_response)
        mock_get.return_value = mock_response

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        success, data = api.get_device_vlans(999)

        assert success is False
        assert "not found" in data.lower()

    @patch("requests.get")
    def test_get_port_vlan_details_trunk(self, mock_get, mock_librenms_config):
        """Test fetching trunk port VLAN details."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = MOCK_PORT_VLAN_DETAILS_TRUNK

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        success, data = api.get_port_vlan_details(227011)

        assert success is True
        assert data["ifTrunk"] == "dot1Q"
        assert len(data["vlans"]) == 2

    @patch("requests.get")
    def test_get_port_vlan_details_not_found(self, mock_get, mock_librenms_config):
        """Test fetching port details when port not found."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "port": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        success, data = api.get_port_vlan_details(999999)

        assert success is False
        assert "not found" in data.lower()


# ============================================
# MODE DETECTION TESTS
# ============================================


class TestVLANModeDetection:
    """Tests for 802.1Q mode detection logic."""

    def test_parse_port_vlan_data_access_port(self, mock_librenms_config):
        """Access port: ifVlan set, ifTrunk null."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {"port_id": 1, "ifName": "Gi1/0/1", "ifVlan": "50", "ifTrunk": None}
        result = api.parse_port_vlan_data(port_data)

        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 50
        assert result["tagged_vlans"] == []

    def test_parse_port_vlan_data_trunk_port(self, mock_librenms_config):
        """Trunk port: ifTrunk = dot1Q."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "port_id": 2,
            "ifName": "Te1/1/1",
            "ifVlan": "90",
            "ifTrunk": "dot1Q",
            "vlans": [
                {"vlan": 90, "untagged": 1},
                {"vlan": 50, "untagged": 0},
                {"vlan": 60, "untagged": 0},
            ],
        }
        result = api.parse_port_vlan_data(port_data)

        assert result["mode"] == "tagged"
        assert result["untagged_vlan"] == 90
        assert result["tagged_vlans"] == [50, 60]

    def test_parse_port_vlan_data_no_vlan(self, mock_librenms_config):
        """No VLAN: ifVlan empty."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {"port_id": 3, "ifName": "Gi1/0/48", "ifVlan": "", "ifTrunk": None}
        result = api.parse_port_vlan_data(port_data)

        assert result["mode"] is None
        assert result["untagged_vlan"] is None
        assert result["tagged_vlans"] == []


# ============================================
# VLAN COMPARISON TESTS
# ============================================


class TestVLANComparison:
    """Tests for VLAN comparison logic."""

    def test_compare_vlans_exists_in_netbox(self):
        """Test VLAN exists in NetBox VLAN group."""
        netbox_vlans = {50: create_mock_vlan(50, "ORG_DATA")}
        librenms_vlan = {"vlan_vlan": 50, "vlan_name": "ORG_DATA"}

        exists = librenms_vlan["vlan_vlan"] in netbox_vlans
        assert exists is True

    def test_compare_vlans_missing_from_netbox(self):
        """Test VLAN missing from NetBox."""
        netbox_vlans = {50: create_mock_vlan(50, "ORG_DATA")}
        librenms_vlan = {"vlan_vlan": 60, "vlan_name": "ORG_VOICE"}

        exists = librenms_vlan["vlan_vlan"] in netbox_vlans
        assert exists is False

    def test_compare_vlans_name_matches(self):
        """Test VLAN name comparison when matching."""
        netbox_vlan = create_mock_vlan(50, "ORG_DATA")
        librenms_name = "ORG_DATA"

        name_matches = netbox_vlan.name == librenms_name
        assert name_matches is True

    def test_compare_vlans_name_differs(self):
        """Test VLAN name comparison when different."""
        netbox_vlan = create_mock_vlan(50, "DATA_VLAN")
        librenms_name = "ORG_DATA"

        name_matches = netbox_vlan.name == librenms_name
        assert name_matches is False


# ============================================
# PORT VLAN PARSING TESTS
# ============================================


class TestPortVLANParsing:
    """Tests for parsing port VLAN data."""

    def test_parse_trunk_port_vlans(self):
        """Parse trunk port into untagged and tagged lists."""
        vlans_data = MOCK_PORT_VLAN_DETAILS_TRUNK["port"][0]["vlans"]

        untagged = [v["vlan"] for v in vlans_data if v["untagged"] == 1]
        tagged = [v["vlan"] for v in vlans_data if v["untagged"] == 0]

        assert untagged == [90]
        assert tagged == [50]

    def test_parse_access_port_vlans(self):
        """Parse access port - single untagged VLAN."""
        vlans_data = MOCK_PORT_VLAN_DETAILS_ACCESS["port"][0]["vlans"]

        untagged = [v["vlan"] for v in vlans_data if v["untagged"] == 1]
        tagged = [v["vlan"] for v in vlans_data if v["untagged"] == 0]

        assert untagged == [50]
        assert tagged == []

    def test_parse_port_with_multiple_tagged(self):
        """Parse trunk port with multiple tagged VLANs."""
        vlans_data = [
            {"vlan": 1, "untagged": 1},
            {"vlan": 10, "untagged": 0},
            {"vlan": 20, "untagged": 0},
            {"vlan": 30, "untagged": 0},
        ]

        untagged = [v["vlan"] for v in vlans_data if v["untagged"] == 1]
        tagged = [v["vlan"] for v in vlans_data if v["untagged"] == 0]

        assert untagged == [1]
        assert len(tagged) == 3
        assert set(tagged) == {10, 20, 30}


# ============================================
# SYNC ACTION TESTS
# ============================================


class TestSyncVLANActions:
    """Tests for VLAN sync action logic."""

    def test_mode_mapping_access(self):
        """Test mapping LibreNMS access mode to NetBox."""
        librenms_mode = "access"
        expected_netbox_mode = "access"

        mode_map = {"access": "access", "tagged": "tagged"}
        result = mode_map.get(librenms_mode)

        assert result == expected_netbox_mode

    def test_mode_mapping_tagged(self):
        """Test mapping LibreNMS tagged mode to NetBox."""
        librenms_mode = "tagged"
        expected_netbox_mode = "tagged"

        mode_map = {"access": "access", "tagged": "tagged"}
        result = mode_map.get(librenms_mode)

        assert result == expected_netbox_mode

    def test_vlan_state_mapping_active(self):
        """Test mapping active VLAN state."""
        vlan_state = 1

        status = "active" if vlan_state == 1 else "reserved"
        assert status == "active"

    def test_vlan_state_mapping_inactive(self):
        """Test mapping inactive VLAN state."""
        vlan_state = 0

        status = "active" if vlan_state == 1 else "reserved"
        assert status == "reserved"


# ============================================
# VLAN SYNC CSS CLASS UTILITY
# ============================================


class TestGetVlanSyncCssClass:
    """Tests for the shared get_vlan_sync_css_class utility."""

    def test_not_in_netbox(self):
        """VLAN not in NetBox should return text-danger."""
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=False) == "text-danger"

    def test_not_in_netbox_name_match_irrelevant(self):
        """Name match flag should be irrelevant when VLAN doesn't exist."""
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=False, name_matches=True) == "text-danger"

    def test_exists_name_matches(self):
        """VLAN exists with matching name should return text-success."""
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=True, name_matches=True) == "text-success"

    def test_exists_name_mismatch(self):
        """VLAN exists but name differs should return text-warning."""
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=True, name_matches=False) == "text-warning"

    def test_default_name_matches_is_true(self):
        """Default name_matches should be True (success when exists)."""
        from netbox_librenms_plugin.utils import get_vlan_sync_css_class

        assert get_vlan_sync_css_class(exists_in_netbox=True) == "text-success"


class TestVlanEntryDictGuardInSync:
    """Verify isinstance(vlan_entry, dict) guard works in parse_port_vlan_data."""

    def test_mixed_vlans_data_only_dicts_parsed(self, mock_librenms_config):
        """vlans array with non-dict entries: only dict entries produce VIDs."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        port_data = {
            "port_id": 1,
            "ifName": "GigabitEthernet0/0",
            "ifDescr": "GigabitEthernet0/0",
            "ifTrunk": "dot1Q",
            "ifVlan": None,
            "vlans": [{"vlan": 10, "untagged": 1}, "bad_entry", {"vlan": 20}],
        }
        result = api.parse_port_vlan_data(port_data)
        assert result["untagged_vlan"] == 10
        assert result["tagged_vlans"] == [20]


class TestVLANPostServerKeyScoping:
    """The refresh POST must scope migrated context (and cache) to the POSTed server_key, not the session-active server, in multi-server setups."""

    def test_post_uses_post_server_key_for_migrated_context(self):
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        obj = MagicMock(pk=1)
        view.get_object = MagicMock(return_value=obj)
        view._get_error_context = MagicMock(return_value={})
        # Seed the session client on _librenms_api and use the REAL librenms_api property so
        # rebind_api_for_server() actually swaps the client post() uses — a property override
        # would pin post() to the session client and mask whether the rebind took effect.
        mock_api = MagicMock(server_key="default")
        view._librenms_api = mock_api
        rebound_api = MagicMock(server_key="prod")
        rebound_api.get_librenms_id.return_value = None  # short-circuit before fetch/cache

        req = MagicMock()
        req.POST.get.side_effect = lambda k, d=None: {"server_key": "prod"}.get(k, d)

        with (
            # The POST rebinds the API to the posted server before anything else.
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=rebound_api),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as mock_bmc,
            patch("netbox_librenms_plugin.views.base.vlan_table_view.cache"),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render"),
        ):
            view.post(req, pk=1)

        mock_bmc.assert_called_once_with(obj, "prod")
        # The id lookup ran on the REBOUND client, never the session one.
        rebound_api.get_librenms_id.assert_called_once_with(obj)
        mock_api.get_librenms_id.assert_not_called()

    def test_post_scopes_cache_keys_to_post_server_key(self):
        """Drive the request past the short-circuit into cache-key construction: a regression that namespaced the cache under the session server (not the POSTed one) must fail here."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        obj = MagicMock(pk=1)
        view.get_object = MagicMock(return_value=obj)
        view.get_vlan_context = MagicMock(return_value={})
        view.get_cache_key = MagicMock(return_value="ck")
        view.get_last_fetched_key = MagicMock(return_value="lfk")
        # Seed the session client and use the REAL librenms_api property so the rebind swaps
        # the client; the fetch returns live on the REBOUND client (what post() should query).
        mock_api = MagicMock(server_key="default", cache_timeout=30)
        view._librenms_api = mock_api
        rebound_api = MagicMock(server_key="prod", cache_timeout=30)
        rebound_api.get_librenms_id.return_value = 10  # truthy → reach fetch + cache
        rebound_api.get_device_vlans.return_value = (True, [{"vlan_vlan": 10}])

        req = MagicMock()
        req.POST.get.side_effect = lambda k, d=None: {"server_key": "prod"}.get(k, d)

        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=rebound_api),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.cache"),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render"),
        ):
            view.post(req, pk=1)

        # The VLAN cache must be namespaced to the POSTed server, not the session one —
        # and must NOT also be built under "default" (the regression this guards against).
        from unittest.mock import call

        view.get_cache_key.assert_any_call(obj, "vlans", "prod")
        assert call(obj, "vlans", "default") not in view.get_cache_key.call_args_list
        # The last-fetched metadata key must be scoped to the POSTed server too, or
        # multi-server cache timestamps bleed across servers under "default".
        view.get_last_fetched_key.assert_any_call(obj, "vlans", "prod")
        assert call(obj, "vlans", "default") not in view.get_last_fetched_key.call_args_list
        view.get_vlan_context.assert_called_once_with(req, obj, "prod")
        # The VLAN fetch ran on the REBOUND client, never the session one.
        rebound_api.get_device_vlans.assert_called_once()
        mock_api.get_device_vlans.assert_not_called()


class TestVlanRefreshFailureClearsCache:
    """A failed VLAN refresh must evict the server-scoped snapshot."""

    def _view(self):
        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        view.get_object = MagicMock(return_value=MagicMock(pk=1))
        view._get_error_context = MagicMock(return_value={})
        # Encode server_key into the mock keys so the delete-assertions actually prove the
        # POSTed "prod" scope was used — a constant return ("ck"/"lfk") would pass even if the
        # view evicted the wrong server's ("default") key.
        view.get_cache_key = MagicMock(side_effect=lambda _obj, kind, server_key: f"{server_key}:{kind}")
        view.get_last_fetched_key = MagicMock(side_effect=lambda _obj, kind, server_key: f"{server_key}:{kind}:last")
        view._librenms_api = MagicMock(server_key="default", cache_timeout=30)
        return view

    def _run(self, *, librenms_id, vlans_result):
        from unittest.mock import patch

        view = self._view()
        rebound_api = MagicMock(server_key="prod", cache_timeout=30)
        rebound_api.get_librenms_id.return_value = librenms_id
        rebound_api.get_device_vlans.return_value = vlans_result

        req = MagicMock()
        req.POST.get.side_effect = lambda k, d=None: {"server_key": "prod"}.get(k, d)

        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=rebound_api),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.vlan_table_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render"),
        ):
            view.post(req, pk=1)
        return view, mock_cache

    def test_missing_librenms_id_evicts_scoped_cache(self):
        view, mock_cache = self._run(librenms_id=None, vlans_result=(True, []))
        view.get_cache_key.assert_any_call(view.get_object.return_value, "vlans", "prod")
        view.get_last_fetched_key.assert_any_call(view.get_object.return_value, "vlans", "prod")
        mock_cache.delete.assert_any_call("prod:vlans")
        mock_cache.delete.assert_any_call("prod:vlans:last")

    def test_fetch_failure_evicts_scoped_cache(self):
        view, mock_cache = self._run(librenms_id=10, vlans_result=(False, "boom"))
        view.get_cache_key.assert_any_call(view.get_object.return_value, "vlans", "prod")
        view.get_last_fetched_key.assert_any_call(view.get_object.return_value, "vlans", "prod")
        mock_cache.delete.assert_any_call("prod:vlans")
        mock_cache.delete.assert_any_call("prod:vlans:last")


class TestVLANErrorContextServerKey:
    """_get_error_context must preserve an explicit server_key=None (stale-server branch) rather than falling back to the session server — otherwise the fragment re-renders on a different, still-configured server and a retry syncs against the wrong instance."""

    def _view(self):
        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        view._librenms_api = MagicMock(server_key="default")
        view.get_vlan_groups_for_device = MagicMock(return_value=[])
        return view

    def test_explicit_none_is_preserved(self):
        view = self._view()
        ctx = view._get_error_context(MagicMock(), "err", server_key=None)
        assert ctx["server_key"] is None

    def test_omitted_falls_back_to_session(self):
        view = self._view()
        ctx = view._get_error_context(MagicMock(), "err")
        assert ctx["server_key"] == "default"

    def test_explicit_key_is_used(self):
        view = self._view()
        ctx = view._get_error_context(MagicMock(), "err", server_key="prod")
        assert ctx["server_key"] == "prod"


@pytest.mark.django_db
class TestVlanSyncContentTemplateMigratedMode:
    """Render the real _vlan_sync_content.html template both ways."""

    def _render(self, *, migrated, server_key="default"):
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
        from netbox_librenms_plugin.tests.conftest import make_device

        from django.contrib.auth.models import AnonymousUser

        device = make_device("vlan-tmpl-dev")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # NetBox context processors read request.user
        # One real row so {% if not vlan_sync.vlan_table.rows %} is False and the form branch renders.
        table = LibreNMSVLANTable([{"vlan_id": 10, "name": "v10", "type": "tagged", "state": "active"}])
        RequestConfig(request).configure(table)
        vlan_sync = {
            "object": device,
            "vlan_table": table,
            "server_key": server_key,
            "error_message": None,
            "cache_expiry": None,
        }
        return render_to_string(
            "netbox_librenms_plugin/_vlan_sync_content.html",
            {"vlan_sync": vlan_sync, "migrated_to_marker": migrated},
            request=request,
        )

    def test_migrated_mode_drops_form_but_keeps_csrf_and_server_key(self):
        # Non-default server so the assertion proves the actual value is emitted.
        html = self._render(migrated={"server_key": "prod", "device_id": 1, "at": "now"}, server_key="prod")
        # The live POST form is gone (a donor must not POST a sync)...
        assert "<form" not in html
        # ...but CSRF + server_key remain so the JS verify-vlan-group fetch targets the right
        # server with a usable token.
        assert "csrfmiddlewaretoken" in html
        assert 'name="server_key"' in html
        assert 'value="prod"' in html
        # The form-submit action input is form-only and must NOT render in migrated mode.
        assert 'name="action"' not in html

    def test_normal_mode_emits_form_with_csrf_and_hidden_inputs(self):
        html = self._render(migrated=False)
        assert "<form" in html
        assert "csrfmiddlewaretoken" in html
        assert 'name="action"' in html
        assert 'name="server_key"' in html

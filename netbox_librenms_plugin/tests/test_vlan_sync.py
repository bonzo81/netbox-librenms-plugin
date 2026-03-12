"""
Tests for VLAN sync feature.

Tests cover:
- LibreNMS VLAN API methods
- VLAN mode detection logic
- VLAN comparison logic
- Port VLAN data parsing
"""

from unittest.mock import MagicMock, patch

# Import the autouse fixture from helpers
pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]


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

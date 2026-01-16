import logging
import urllib.parse

import requests
from django.core.cache import cache
from netbox.plugins import get_plugin_config

# HTTP request timeout constants (in seconds)
DEFAULT_API_TIMEOUT = 10
EXTENDED_API_TIMEOUT = 20  # For endpoints that may take longer (e.g., device listing)

logger = logging.getLogger(__name__)


class LibreNMSAPI:
    """
    Client to interact with the LibreNMS API and retrieve interface data for devices.
    """

    def __init__(self, server_key=None):
        """
        Initialize LibreNMS API client with support for multiple servers.

        Args:
            server_key: Key for specific server configuration. If None, uses selected server or default.
        """
        # If no server_key is provided, try to get the selected server from settings
        if not server_key:
            try:
                from netbox_librenms_plugin.models import LibreNMSSettings

                settings = LibreNMSSettings.objects.first()
                if settings:
                    server_key = settings.selected_server
            except (ImportError, AttributeError):
                pass

        # Default to 'default' if still no server_key
        server_key = server_key or "default"
        self.server_key = server_key

        # Get server configuration
        servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

        if (
            servers_config
            and isinstance(servers_config, dict)
            and server_key in servers_config
        ):
            # Multi-server configuration
            config = servers_config[server_key]
            self.librenms_url = config["librenms_url"]
            self.api_token = config["api_token"]
            self.cache_timeout = config.get("cache_timeout", 300)
            self.verify_ssl = config.get("verify_ssl", True)
        else:
            # Fallback to legacy single-server configuration
            self.librenms_url = get_plugin_config(
                "netbox_librenms_plugin", "librenms_url"
            )
            self.api_token = get_plugin_config("netbox_librenms_plugin", "api_token")
            self.cache_timeout = get_plugin_config(
                "netbox_librenms_plugin", "cache_timeout", 300
            )
            self.verify_ssl = get_plugin_config(
                "netbox_librenms_plugin", "verify_ssl", True
            )

        if not self.librenms_url or not self.api_token:
            raise ValueError(
                f"LibreNMS URL or API token is not configured for server '{server_key}'."
            )

        self.headers = {"X-Auth-Token": self.api_token}

    def test_connection(self):
        """
        Test connection to LibreNMS server by calling the /system endpoint.

        Returns:
            dict: System information if successful, error dict if failed
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/system",
                headers=self.headers,
                verify=self.verify_ssl,
                timeout=DEFAULT_API_TIMEOUT,
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok" and data.get("system"):
                    return data["system"][0] if data["system"] else None

            # Handle different HTTP status codes with user-friendly messages
            if response.status_code == 401:
                return {
                    "error": True,
                    "message": "Authentication failed - check API token",
                }
            elif response.status_code == 403:
                return {
                    "error": True,
                    "message": "Access forbidden - check API token permissions",
                }
            elif response.status_code == 404:
                return {
                    "error": True,
                    "message": "API endpoint not found - check LibreNMS URL",
                }
            elif response.status_code >= 500:
                return {
                    "error": True,
                    "message": "LibreNMS server error - check server status",
                }
            else:
                return {
                    "error": True,
                    "message": f"HTTP {response.status_code} - unexpected server response",
                }

        except requests.exceptions.SSLError:
            return {
                "error": True,
                "message": "SSL certificate verification failed - try setting verify_ssl to false",
            }
        except requests.exceptions.ConnectionError:
            return {
                "error": True,
                "message": "Connection failed - check server URL and network connectivity",
            }
        except requests.exceptions.Timeout:
            return {
                "error": True,
                "message": "Connection timeout - server may be slow or unreachable",
            }
        except Exception as e:
            return {"error": True, "message": f"Unexpected error: {str(e)}"}

    @classmethod
    def get_available_servers(cls):
        """
        Get list of available server configurations.

        Returns:
            dict: Dictionary of server keys and their display names
        """
        servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

        if servers_config and isinstance(servers_config, dict):
            # Multi-server configuration
            result = {}
            for key, config in servers_config.items():
                display_name = config.get("display_name", key)
                result[key] = display_name
            return result
        else:
            # Legacy single-server configuration
            legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
            if legacy_url:
                return {"default": f"Default Server ({legacy_url})"}
            return {"default": "Default Server"}

    def get_librenms_id(self, obj):
        """
        Args:
            obj: NetBox device or VM object

        Returns:
            int: LibreNMS device ID if found, None otherwise

        Notes:
            Lookup order:
            1. Custom field 'librenms_id' on object
            2. Cached librenms_id value
            3. API lookup using:
                a. primary_ip
                b. primary IP's DNS name
                c. hostname if FQDN

            If found via API, stores ID in custom field if available,
            otherwise caches the value.
        """
        librenms_id = obj.cf.get("librenms_id")
        if librenms_id:
            return librenms_id

        # Check cache
        cache_key = self._get_cache_key(obj)
        librenms_id = cache.get(cache_key)
        if librenms_id:
            return librenms_id

        # Determine dynamically from API
        ip_address = obj.primary_ip.address.ip if obj.primary_ip else None
        dns_name = obj.primary_ip.dns_name if obj.primary_ip else None
        hostname = obj.name if obj.name else None

        # Try IP address
        if ip_address:
            librenms_id = self.get_device_id_by_ip(ip_address)
            if librenms_id:
                self._store_librenms_id(obj, librenms_id)
                return librenms_id

        # Try primary IP's DNS name
        if dns_name:
            librenms_id = self.get_device_id_by_hostname(dns_name)
            if librenms_id:
                self._store_librenms_id(obj, librenms_id)
                return librenms_id

        # Try hostname if FQDN
        if hostname:
            librenms_id = self.get_device_id_by_hostname(hostname)
            if librenms_id:
                self._store_librenms_id(obj, librenms_id)
                return librenms_id

        return None

    def _get_cache_key(self, obj):
        """
        Generate a unique cache key for an object.

        Args:
            obj: NetBox device or VM object

        Returns:
            str: Cache key
        """
        object_type = obj._meta.model_name
        server_key = getattr(self, "server_key", "default")
        return f"librenms_device_id_{object_type}_{obj.pk}_{server_key}"

    def _store_librenms_id(self, obj, librenms_id):
        """
        Store in custom field if available

        Args:
            obj: NetBox device or VM object
            librenms_id: LibreNMS device ID

        Returns:
            None
        """
        if "librenms_id" in obj.cf:
            obj.custom_field_data["librenms_id"] = librenms_id
            obj.save()
        else:
            # Use cache as fallback
            cache_key = self._get_cache_key(obj)
            cache.set(cache_key, librenms_id, timeout=self.cache_timeout)

    def get_device_id_by_ip(self, ip_address):
        """
        Retrieve the device ID using the device's IP address.

        Args:
            ip_address: Device IP address

        Retruns:
            int: LibreNMS device ID if found, None otherwise
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{ip_address}",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            device_data = response.json()["devices"][0]
            return device_data["device_id"]
        except (requests.exceptions.RequestException, IndexError, KeyError):
            return None

    def get_device_id_by_hostname(self, hostname):
        """
        Retrieve the device ID using the device's hostname.

        Args:
            hostname: Device hostname

        Returns:
            int: LibreNMS device ID if found, None otherwise
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{hostname}",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            device_data = response.json()["devices"][0]
            return device_data["device_id"]
        except (requests.exceptions.RequestException, IndexError, KeyError):
            return None

    def get_device_info(self, device_id):
        """
        Fetch device information from LibreNMS using its primary IP.

        Args:
            device_id: LibreNMS device ID

        Returns:
            tuple: (success: bool, data: dict)
        """

        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            if response.status_code == 200:
                device_data = response.json()["devices"][0]
                return True, device_data
            return False, None
        except requests.exceptions.RequestException:
            return False, None

    def get_ports(self, device_id):
        """
        Fetch ports data from LibreNMS for a device using its primary IP.

        Args:
            device_id: LibreNMS device ID

        Returns:
            tuple: (success: bool, data: dict)
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/ports",
                headers=self.headers,
                params={
                    "columns": "port_id,ifName,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias,ifPhysAddress,ifMtu"
                },
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            data = response.json()
            return True, data
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return False, "Device not found in LibreNMS"
            return False, f"HTTP error: {str(e)}"
        except requests.exceptions.RequestException as e:
            return False, f"Error connecting to LibreNMS: {str(e)}"

    def add_device(self, data):
        """
        Add a device to LibreNMS.

        Args:
            Dictionary containing device data including:
                - hostname: Device hostname or IP
                - snmp_version: SNMP version (v2c or v3)
                - force_add: Skip checks for duplicate device and SNMP reachability (optional, default False)
                - port: SNMP port (optional, defaults to config value)
                - transport: SNMP transport protocol (optional: udp, tcp, udp6, tcp6)
                - port_association_mode: Port identification method (optional: ifIndex, ifName, ifDescr, ifAlias)
                - poller_group: Poller group ID (optional, defaults to 0)
                - community: SNMP community string (for v2c)
                - authlevel, authname, authpass, authalgo, cryptopass, cryptoalgo: SNMP v3 parameters

        Returns:
            tuple: (success: bool, message: str)
        """
        payload = {
            "hostname": data["hostname"],
            "snmpver": data["snmp_version"],
            "force_add": data.get("force_add", False),
        }

        # Add optional common fields if provided
        if data.get("port"):
            payload["port"] = data["port"]
        if data.get("transport"):
            payload["transport"] = data["transport"]
        if data.get("port_association_mode"):
            payload["port_association_mode"] = data["port_association_mode"]
        if data.get("poller_group") is not None:
            payload["poller_group"] = data["poller_group"]

        if data["snmp_version"] == "v2c":
            payload["community"] = data["community"]
        elif data["snmp_version"] == "v3":
            payload.update(
                {
                    "authlevel": data["authlevel"],
                    "authname": data["authname"],
                    "authpass": data["authpass"],
                    "authalgo": data["authalgo"],
                    "cryptopass": data["cryptopass"],
                    "cryptoalgo": data["cryptoalgo"],
                }
            )

        try:
            response = requests.post(
                f"{self.librenms_url}/api/v0/devices",
                headers=self.headers,
                json=payload,
                timeout=EXTENDED_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "ok":
                return True, "Device added successfully."
            else:
                return False, result.get("message", "Unknown error.")
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def update_device_field(self, device_id, field_data):
        """
        Update a specific field for a device in LibreNMS.

        Args:
            device_id: LibreNMS device ID
            field_data: Dictionary containing field name and value

            e.g {
                    "field": ["location", "override_sysLocation"],
                    "data": [device.site.name, "1"]

        Returns:
            tuple (success: bool, message: str)
        """
        try:
            response = requests.patch(
                f"{self.librenms_url}/api/v0/devices/{device_id}",
                headers=self.headers,
                json=field_data,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()

            if result.get("status") == "ok":
                return True, "Device fields updated successfully"
            else:
                return False, result.get("message", "Unknown error occurred")
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, "json"):
                error_details = e.response.json()
                error_message = error_details.get("message", error_message)
            return False, error_message

    def get_locations(self):
        """
        Fetch locations data from LibreNMS.

        Args:
            None

        Returns:
            tuple: (success: bool, data: dict)
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/resources/locations",
                headers=self.headers,
                timeout=EXTENDED_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()

            if "locations" in result:
                return True, result["locations"]
            else:
                return False, "No locations found or unexpected response format"
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            return False, error_message

    def add_location(self, location_data):
        """
        Add a location to LibreNMS.

        Args:
            location_data: Dictionary containing location data

            e.g location_data = {
                    "location": site.name,
                    "lat": str(site.latitude),
                    "lng": str(site.longitude)
                }

        Return:
            tuple: (success: bool, message: str)
        """
        try:
            response = requests.post(
                f"{self.librenms_url}/api/v0/locations",
                headers=self.headers,
                json=location_data,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()
            if result.get("status") == "ok":
                location_id = result["message"].split("#")[-1]
                return True, {"id": location_id, "message": result["message"]}
            else:
                return False, result.get("message", "Unexpected response format")
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, "json"):
                error_details = e.response.json()
                error_message = error_details.get("message", error_message)
            return False, error_message

    def update_location(self, location_name, location_data):
        """
        Update a location in LibreNMS.

        Args:
            location_name: LibreNMS Location name
            location_data: Dictionary containing location data

            e.g location_data = {
                    "lat": str(site.latitude),
                    "lng": str(site.longitude)
                }

        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            encoded_location_name = urllib.parse.quote(location_name)
            response = requests.patch(
                f"{self.librenms_url}/api/v0/locations/{encoded_location_name}",
                headers=self.headers,
                json=location_data,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "ok":
                return True, result["message"]
            else:
                return False, result.get("message", "Unexpected response format")
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, "json"):
                error_details = e.response.json()
                error_message = error_details.get("message", error_message)
            return False, error_message

    def get_device_links(self, device_id):
        """
        Get links for a specific device from LibreNMS.

        Args:
            hostname: LibreNMS Device ID

        Returns:
            tuple: (success: bool, data: dict)
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/links",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            return True, response.json()
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def get_device_ips(self, device_id):
        """
        Fetch IP address data for a specific device from LibreNMS.

        Args:
            device_id: LibreNMS Device ID

        Returns:
            tuple: (success: bool, data: dict)
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/ip",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            if response.status_code == 200:
                ip_data = response.json()["addresses"]
                return True, ip_data
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def get_port_by_id(self, port_id):
        """
        Fetch specific port data from LibreNMS using port ID.

        Args:
            port_id: LibreNMS Port ID

        Returns:
            tuple: (success: bool, data: dict)
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/ports/{port_id}",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            return True, response.json()
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def get_device_inventory(self, device_id):
        """
        Fetch complete inventory for a device from LibreNMS.
        Useful for getting component details like chassis serial numbers for Virtual Chassis.

        Route: /api/v0/inventory/{device_id}/all

        Args:
            device_id: LibreNMS device ID

        Returns:
            tuple: (success: bool, data: list)

        Example inventory item:
            {
                "entPhysicalDescr": "Chassis Component",
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "ABC123456",
                "entPhysicalModelName": "EX4300-48P",
                ...
            }
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/inventory/{device_id}/all",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            if response.status_code == 200:
                inventory_data = response.json()
                return True, inventory_data.get("inventory", [])
            return False, []
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def get_poller_groups(self):
        """
        Fetch all poller groups from LibreNMS.

        Route: /api/v0/poller_group

        Returns:
            tuple: (success: bool, data: list)

        Example poller group:
            {
                "id": 1,
                "group_name": "test",
                "descr": "test group"
            }
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/poller_group",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "ok":
                    return True, result.get("get_poller_group", [])
            return False, []
        except requests.exceptions.RequestException as e:
            return False, str(e)

    def get_inventory_filtered(
        self, device_id, ent_physical_class=None, ent_physical_contained_in=None
    ):
        """
        Fetch filtered inventory from LibreNMS with optional filtering.
        Uses query parameters if supported, falls back to client-side filtering.

        Route: /api/v0/inventory/{device_id}

        Args:
            device_id: LibreNMS device ID
            ent_physical_class: Filter by entPhysicalClass (e.g., 'chassis', 'stack')
            ent_physical_contained_in: Filter by entPhysicalContainedIn (0=root, 1=first level, etc.)

        Returns:
            tuple: (success: bool, inventory: list)

        Example:
            >>> api.get_inventory_filtered(22, ent_physical_class='chassis', ent_physical_contained_in=1)
            (True, [{'entPhysicalClass': 'chassis', ...}, ...])
        """
        logger.debug(
            f"get_inventory_filtered: device={device_id}, "
            f"class={ent_physical_class}, contained_in={ent_physical_contained_in}"
        )
        try:
            # Build query parameters for API filtering
            params = {}
            if ent_physical_class is not None:
                params["entPhysicalClass"] = ent_physical_class
            if ent_physical_contained_in is not None:
                params["entPhysicalContainedIn"] = str(ent_physical_contained_in)

            # Try the filtered endpoint first (non-/all)
            response = requests.get(
                f"{self.librenms_url}/api/v0/inventory/{device_id}",
                headers=self.headers,
                params=params,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok":
                    inventory = data.get("inventory", [])
                    logger.debug(f"API returned {len(inventory)} items")

                    # If we got results or didn't specify filters, return
                    if inventory or not params:
                        return True, inventory

            # If filtered endpoint returned empty but we have filters,
            # try /all endpoint and filter client-side
            if params:
                logger.debug(
                    "Filtered inventory API returned no results, falling back to client-side filtering"
                )
                success, all_inventory = self.get_device_inventory(device_id)

                if not success:
                    return False, []

                # Apply client-side filters
                filtered = all_inventory
                if ent_physical_class:
                    filtered = [
                        item
                        for item in filtered
                        if item.get("entPhysicalClass") == ent_physical_class
                    ]
                if ent_physical_contained_in is not None:
                    filtered = [
                        item
                        for item in filtered
                        if str(item.get("entPhysicalContainedIn"))
                        == str(ent_physical_contained_in)
                    ]

                return True, filtered

            return False, []

        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to fetch filtered inventory: {e}")
            return False, []

    def list_devices(self, filters=None):
        """
        List all devices from LibreNMS with optional filtering.

        Route: /api/v0/devices

        Args:
            filters (dict, optional): Filter parameters:
                - type: Device type filter (e.g., 'network', 'server', 'storage')
                - location_id: Location ID filter (numeric ID from LibreNMS)
                - hostname: Hostname filter (partial match)
                - os: Operating system filter
                - version: OS version filter
                - hardware: Hardware model filter
                - features: Features filter
                - device_id: Specific device ID
                - query: Search query (searches across multiple fields)

        Returns:
            tuple: (success: bool, data: list)

        Example device:
            {
                "device_id": 1,
                "hostname": "router01.example.com",
                "sysName": "router01",
                "ip": "192.168.1.1",
                "hardware": "Cisco C9300-48P",
                "version": "IOS 16.9.4",
                "location": "Datacenter 1",
                "status": 1,
                "status_reason": "",
                "ignore": 0,
                "disabled": 0,
                "uptime": 3153600,
                "os": "ios",
                "type": "network",
                "serial": "ABC123456789",
                "icon": "cisco.svg",
                ...
            }
        """
        try:
            params = {}
            if filters:
                # Build query parameters from filters
                for key, value in filters.items():
                    if value is not None and value != "":
                        params[key] = value

            response = requests.get(
                f"{self.librenms_url}/api/v0/devices",
                headers=self.headers,
                params=params,
                timeout=EXTENDED_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "ok":
                    return True, result.get("devices", [])

            return False, []
        except requests.exceptions.RequestException as e:
            return False, str(e)

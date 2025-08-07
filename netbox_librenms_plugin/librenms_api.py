import urllib.parse

import requests
from django.core.cache import cache
from netbox.plugins import get_plugin_config


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
                timeout=10,
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
                timeout=10,
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
                timeout=10,
                verify=self.verify_ssl,
            )
            if response.status_code == 200:
                device_data = response.json()["devices"][0]
                return True, device_data
            return False, None
        except requests.exceptions.RequestException:
            return False, None

    def get_ports(self, device_id):
        # TODO: id 2 - Fix return to use tuple (success, data)
        """
        Fetch ports data from LibreNMS for a device using its primary IP.

        Args:
            device_id: LibreNMS device ID

        Returns:
            dict: Ports data
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/ports",
                headers=self.headers,
                params={
                    "columns": "port_id,ifName,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias,ifPhysAddress,ifMtu"
                },
                timeout=10,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            data = response.json()

            return data
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return {"error": "Device not found in LibreNMS"}
            raise
        except requests.exceptions.RequestException as e:
            return {"error": f"Error connecting to LibreNMS: {str(e)}"}

    def add_device(self, data):
        # TODO: id 1 - Fix return to use tuple (success, message)
        """
        Add a device to LibreNMS.

        Args:
            Dictionary containing device data

        Returns:
            Dictionary with 'success' and 'message' keys
        """
        payload = {
            "hostname": data["hostname"],
            "snmpver": data["snmp_version"],
            "force_add": False,
        }

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
                timeout=20,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "ok":
                return {"success": True, "message": "Device added successfully."}
            else:
                return {
                    "success": False,
                    "message": result.get("message", "Unknown error."),
                }
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": str(e)}

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
                timeout=10,
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
                f"{self.librenms_url}/api/v0/resources/locations/",
                headers=self.headers,
                timeout=10,
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
                timeout=10,
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
                timeout=10,
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
                timeout=10,
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
                timeout=10,
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
                timeout=10,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            return True, response.json()
        except requests.exceptions.RequestException as e:
            return False, str(e)

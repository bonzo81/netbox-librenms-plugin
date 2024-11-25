import urllib.parse

import requests
from django.core.cache import cache
from netbox.plugins import get_plugin_config


class LibreNMSAPI:
    """
    Client to interact with the LibreNMS API and retrieve interface data for devices.
    """

    def __init__(self):
        self.librenms_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
        self.api_token = get_plugin_config("netbox_librenms_plugin", "api_token")
        self.cache_timeout = get_plugin_config(
            "netbox_librenms_plugin", "cache_timeout", 300
        )
        self.verify_ssl = get_plugin_config("netbox_librenms_plugin", "verify_ssl")

        if not self.librenms_url or not self.api_token:
            raise ValueError("LibrenMS URL or API token is not configured.")

        self.headers = {"X-Auth-Token": self.api_token}

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
        librenms_id = obj.custom_field_data.get("librenms_id")
        if librenms_id:
            return librenms_id

        # Check cache
        cache_key = f"librenms_device_id_{obj.id}"
        librenms_id = cache.get(cache_key)
        if librenms_id:
            return librenms_id

        # Determine dynamically from API
        ip_address = obj.primary_ip.address.ip if obj.primary_ip else None
        dns_name = obj.primary_ip.dns_name if obj.primary_ip else None
        hostname = obj.name if '.' in obj.name else None  # Consider as FQDN if it contains a dot

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

    def _store_librenms_id(self, obj, librenms_id):
        # Store in custom field if available
        if hasattr(obj, "custom_field_data"):
            obj.custom_field_data["librenms_id"] = librenms_id
            obj.save()
        else:
            # Otherwise use cache
            cache_key = f"librenms_device_id_{obj.id}"
            cache.set(cache_key, librenms_id, timeout=3600)

    def get_device_info(self, device_id):
        """
        Fetch device information from LibreNMS using its primary IP.
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
        """
        Fetch ports data from LibreNMS for a device using its primary IP.
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

    def add_device(self, hostname, community, version):
        """
        Add a device to LibreNMS.

        :param hostname: Device hostname as ip address
        :param community: SNMP community
        :param version: SNMP version (1, 2c, 3)

        :return: True if successful, False otherwise
        :return: Message indicating the result
        """
        data = {
            "hostname": hostname,
            "community": community,
            "version": version,
            "force_add": False,
        }

        try:
            response = requests.post(
                f"{self.librenms_url}/api/v0/devices",
                headers=self.headers,
                json=data,
                timeout=10,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()
            if result.get("status") == "ok":
                return True, "Device added successfully"
            else:
                return False, result.get("message", "Unknown error occurred")
        except requests.exceptions.RequestException as e:
            response_json = response.json()
            error_message = response_json.get(
                "message", f"Unknown error occurred {str(e)}"
            )
            return False, error_message

    def update_device_field(self, device_id, field_data):
        """
        Update a specific field for a device in LibreNMS.

        :param hostname: Device hostname as ip address
        :param field_data: List of dictionaries containing field name and value
        e.g field_data = {
                "field": ["location", "override_sysLocation"],
                "data": [device.site.name, "1"]
            }

        :return: True if successful, False otherwise
        :return: Message indicating the result
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

        :param location_data: Dictionary containing location data
        e.g location_data = {
                "location": site.name,
                "lat": str(site.latitude),
                "lng": str(site.longitude)
            }

        :return: True if successful, False otherwise
        :return: Message indicating the result
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

        :param location_name: Location name
        :param location_data: Dictionary containing location data
        e.g location_data = {
                "lat": str(site.latitude),
                "lng": str(site.longitude)
            }

        :return: True if successful, False otherwise
        :return: Message indicating the result
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
            hostname: Device hostname or ID

        Returns:
            tuple: (success, data)
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

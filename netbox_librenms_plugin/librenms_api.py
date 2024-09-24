import requests
from django.core.cache import cache
from netbox.plugins import get_plugin_config
import urllib.parse


class LibreNMSAPI:
    """
    Client to interact with the LibreNMS API and retrieve interface data for devices.
    """
    def __init__(self):
        self.librenms_url = get_plugin_config('netbox_librenms_plugin', 'librenms_url')
        self.api_token = get_plugin_config('netbox_librenms_plugin', 'api_token')
        self.cache_timeout = get_plugin_config('netbox_librenms_plugin', 'cache_timeout', 300)

        if not self.librenms_url or not self.api_token:
            raise ValueError("LibrenMS URL or API token is not configured.")

        self.headers = {'X-Auth-Token': self.api_token}

    def get_device_info(self, device_ip):
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_ip}",
                headers=self.headers
            )
            if response.status_code == 200:
                device_data = response.json()['devices'][0]
                return True, device_data
            return False, None
        except requests.exceptions.RequestException:
            return False, None

    def get_ports(self, device_ip):
        """
        Fetch ports data from LibreNMS for a device using its primary IP.
        Caches the API response.
        """
        cache_key = f"librenms_ports_{device_ip}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return cached_data

        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_ip}/ports",
                headers=self.headers,
                params={'columns': 'port_id,ifName,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias'}
            )
            response.raise_for_status()
            data = response.json()
            cache.set(cache_key, data, timeout=self.cache_timeout)
            return data
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return {'error': 'Device not found in LibreNMS'}
            raise
        except requests.exceptions.RequestException as e:
            return {'error': f'Error connecting to LibreNMS: {str(e)}'}

    def add_device(self, hostname, community, version, location=None):
        data = {
            "hostname": hostname,
            "community": community,
            "version": version,
            "location": location,
            "force_add": False
        }

        try:
            response = requests.post(
                f"{self.librenms_url}/api/v0/devices",
                headers=self.headers,
                json=data
            )
            response.raise_for_status()

            result = response.json()
            if result.get('status') == 'ok':
                return True, "Device added successfully"
            else:
                return False, result.get('message', 'Unknown error occurred')
        except requests.exceptions.RequestException as e:
            response_json = response.json()
            error_message = response_json.get('message', 'Unknown error occurred')
            return False, error_message

    def update_device_location(self, hostname, location):
        data = {
            "field": "location",
            "data": location
        }
        try:
            response = requests.patch(
                f"{self.librenms_url}/api/v0/devices/{hostname}",
                headers=self.headers,
                json=data
            )
            response.raise_for_status()

            result = response.json()
            if result.get('status') == 'ok':
                return True, "Device location updated successfully"
            else:
                return False, result.get('message', 'Unknown error occurred')
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            return False, error_message

    def get_locations(self):
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/resources/locations/",
                headers=self.headers
            )
            response.raise_for_status()
            result = response.json()

            if 'locations' in result:
                return True, result['locations']
            else:
                return False, "No locations found or unexpected response format"
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            return False, error_message

    def add_location(self, location_data):
        try:
            response = requests.post(
                f"{self.librenms_url}/api/v0/locations",
                headers=self.headers,
                json=location_data
            )
            response.raise_for_status()

            result = response.json()
            if result.get('status') == 'ok':
                location_id = result['message'].split('#')[-1]
                return True, {'id': location_id, 'message': result['message']}
            else:
                return False, result.get('message', "Unexpected response format")
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, 'json'):
                error_details = e.response.json()
                error_message = error_details.get('message', error_message)
            return False, error_message

    def update_location(self, location_name, location_data):
        try:
            encoded_location_name = urllib.parse.quote(location_name)
            response = requests.patch(
                f"{self.librenms_url}/api/v0/locations/{encoded_location_name}",
                headers=self.headers,
                json=location_data
            )
            response.raise_for_status()
            result = response.json()
            if result.get('status') == 'ok':
                return True, result['message']
            else:
                return False, result.get('message', "Unexpected response format")
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, 'json'):
                error_details = e.response.json()
                error_message = error_details.get('message', error_message)
            return False, error_message

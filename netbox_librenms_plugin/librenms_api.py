import logging
import urllib.parse

import requests
from django.core.cache import cache
from netbox.plugins import get_plugin_config

# HTTP request timeout constants (in seconds)
DEFAULT_API_TIMEOUT = 10
EXTENDED_API_TIMEOUT = 20  # For endpoints that may take longer (e.g., device listing)

# Short-lived cache for get_device_info(). The device-info header is fetched on every sync-tab
# render (and again on the post-action redirect), but a device's identity/metadata is stable, so
# a brief cache removes a redundant synchronous LibreNMS round-trip from each render without
# meaningfully staling the displayed values.
DEVICE_INFO_CACHE_TIMEOUT = 60

logger = logging.getLogger(__name__)


def build_librenms_api(server_key):
    """
    Build a :class:`LibreNMSAPI` for a server key, tolerating bad keys.

    ``LibreNMSAPI(server_key=...)`` raises ``KeyError`` for an unknown non-default
    key and ``ValueError`` when the URL/token is missing. Views take ``server_key``
    from request POST, where a stale page or tampered request can carry a key that
    no longer exists — returning ``None`` lets the caller surface a user-facing
    error instead of an unhandled 500.

    Args:
        server_key (str): The configured LibreNMS server key to build a client for.

    Returns:
        LibreNMSAPI | None: A client for *server_key*, or None when the key is
            unknown or the server is misconfigured.
    """
    try:
        return LibreNMSAPI(server_key=server_key)
    except (KeyError, ValueError):
        return None


class LibreNMSUnreachable(Exception):
    """The LibreNMS server did not answer, so a run that needs it cannot continue.

    Raised by the device fetch itself. LibreNMS answers a search that matches nothing
    with 200 and an empty list, so a failed fetch never means "no devices matched". The
    message carries the reason the API reported, so a caller can show it verbatim.
    """


class LibreNMSAPI:
    """
    Client to interact with the LibreNMS API and retrieve interface data for devices.
    """

    @staticmethod
    def _is_usable_server_config(config):
        """
        Return True only for a server mapping that ``__init__`` can bind.

        A server entry is usable only when it is a dict carrying a non-empty
        ``librenms_url`` and ``api_token`` — the same fields ``__init__`` requires
        before it will build a client. Sharing this predicate keeps the server
        picker (``get_available_servers``) and the auto-default fallback from
        offering, or silently selecting, a partially configured entry that would
        immediately raise ``ValueError``.
        """
        return isinstance(config, dict) and bool(config.get("librenms_url")) and bool(config.get("api_token"))

    def __init__(self, server_key=None):
        """
        Initialize LibreNMS API client with support for multiple servers.

        Args:
            server_key: Key for specific server configuration. If None, uses selected server or default.
        """
        # Track whether the caller explicitly requested a specific server. A key auto-resolved
        # from LibreNMSSettings.selected_server is NOT explicit, so a stale stored key falls back
        # to the first available server rather than hard-failing (issue #110).
        # A blank/whitespace-only string is "no key", not an explicit request: treating "" as
        # explicit would mark the auto-resolved (and possibly stale) selected_server as explicit
        # and defeat that fallback, raising KeyError instead.
        if isinstance(server_key, str):
            server_key = server_key.strip() or None
        elif server_key is not None:
            # A non-string key (e.g. a list/dict from a tampered payload) is not a valid server
            # key and is unhashable — left as-is it would raise TypeError at the `not in
            # servers_config` membership check below. Treat it as unset so it fails cleanly via
            # the same fallback path as a blank string.
            server_key = None
        explicit_server_key = server_key is not None

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

        # Get server configuration
        servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

        # If the requested server_key doesn't exist but there are configured servers,
        # only fall back to the first available server when using the auto-default key.
        # If a specific (non-default) server_key was requested but not found, raise
        # immediately to avoid silently using the wrong LibreNMS instance.
        if servers_config and isinstance(servers_config, dict) and server_key not in servers_config:
            # Only fail closed for an *explicitly* requested non-default key (tampered or
            # stale-page input). An auto-resolved/default key falls back instead (issue #110).
            if explicit_server_key and server_key != "default":
                available = list(servers_config.keys())
                raise KeyError(
                    f"Server '{server_key}' not found in LibreNMS plugin configuration. Available servers: {available}"
                )
            # Skip partially configured entries so the auto-default doesn't land on a server
            # missing its url/token (which __init__ would reject below) while a later entry is
            # fully usable.
            first_key = next(
                (k for k, cfg in servers_config.items() if self._is_usable_server_config(cfg)),
                None,
            )
            # #110: a non-empty servers_config with no usable entry must surface a clear error
            # rather than silently falling through to a (possibly stale) legacy single-server
            # config. build_librenms_api() converts this ValueError into a clean None.
            if first_key is None:
                raise ValueError("No valid LibreNMS server configuration entries found.")
            logger.info(
                "Server '%s' not found in config, falling back to '%s'",
                server_key,
                first_key,
            )
            server_key = first_key

        self.server_key = server_key

        if servers_config and isinstance(servers_config, dict) and server_key in servers_config:
            # Multi-server configuration
            config = servers_config[server_key]
            # The fallback above only guards the key-not-found case; a present-but-malformed
            # entry (e.g. {"default": "not-a-dict"}) would otherwise raise an opaque TypeError
            # at the key reads below. Fail with a clear configuration error instead (issue #110).
            if not isinstance(config, dict):
                raise ValueError(
                    f"LibreNMS server '{server_key}' is misconfigured "
                    f"(expected a mapping, got {type(config).__name__})."
                )
            # Read with .get() rather than direct indexing: a dict-shaped but incomplete entry
            # (e.g. {"default": {}}) passes the isinstance check, so config["librenms_url"] would
            # raise an opaque KeyError instead of the ValueError contract callers rely on. The
            # url/token completeness is enforced by the single guard at the end of __init__.
            self.librenms_url = config.get("librenms_url")
            self.api_token = config.get("api_token")
            self.cache_timeout = config.get("cache_timeout", 300)
            self.verify_ssl = config.get("verify_ssl", True)
        else:
            # Fallback to legacy single-server configuration. Legacy mode has only the implicit
            # default server, so a stale/tampered request key (e.g. build_librenms_api("ghost"))
            # must not survive as self.server_key — it would otherwise become the cache/redirect
            # discriminator under an unconfigured value. Normalize to "default".
            self.server_key = "default"
            self.librenms_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
            self.api_token = get_plugin_config("netbox_librenms_plugin", "api_token")
            self.cache_timeout = get_plugin_config("netbox_librenms_plugin", "cache_timeout", 300)
            self.verify_ssl = get_plugin_config("netbox_librenms_plugin", "verify_ssl", True)

        if not self.librenms_url or not self.api_token:
            raise ValueError(f"LibreNMS URL or API token is not configured for server '{server_key}'.")

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
                # Only offer servers that __init__ can actually bind: a dict with a non-empty
                # librenms_url and api_token. Mirroring the constructor's validation keeps a
                # malformed (non-mapping) or partially configured entry from appearing selectable
                # and then failing the moment it is chosen.
                if not cls._is_usable_server_config(config):
                    logger.warning(
                        "Skipping unusable LibreNMS server config %r (needs a librenms_url and api_token).",
                        key,
                    )
                    continue
                if not config.get("librenms_url") or not config.get("api_token"):
                    continue
                result[key] = config.get("display_name", key)
            return result
        else:
            # Legacy single-server configuration
            legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
            if legacy_url:
                return {"default": f"Default Server ({legacy_url})"}
            return {"default": "Default Server"}

    def get_stored_librenms_id(self, obj, server_key=None):
        """
        Return the stored or cached LibreNMS ID for an object without discovery.

        This helper is safe for generic NetBox objects such as interfaces,
        where IP/hostname-based discovery would be expensive or incorrect.

        Args:
            obj: NetBox object with a librenms_id custom field or cache identity
            server_key: LibreNMS server key to read the per-server id under; defaults
                to this client's bound ``server_key``. A caller scoped to a specific
                server (e.g. the module verify path, which sets ``_active_server_key``
                but leaves the API bound to the default client) must pass its key so
                the multi-server dict CF is read under the right server rather than the
                client's default.

        Returns:
            int: LibreNMS ID if found in the custom field or cache, None otherwise
        """
        from netbox_librenms_plugin.utils import get_librenms_device_id

        resolved_key = server_key or self.server_key
        librenms_id = get_librenms_device_id(obj, resolved_key, auto_save=False)
        if librenms_id is not None:
            return librenms_id

        # Check cache (scoped to the same server the CF was read under)
        cache_key = self._get_cache_key(obj, server_key=resolved_key)
        librenms_id = cache.get(cache_key)
        if librenms_id is not None:
            return librenms_id

        return None

    def get_librenms_id(self, obj):
        """
        Args:
            obj: NetBox object with a librenms_id custom field or discovery identity

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
        librenms_id = self.get_stored_librenms_id(obj)
        if librenms_id is not None:
            return librenms_id

        # Determine dynamically from API when the object exposes device identity fields.
        primary_ip = getattr(obj, "primary_ip", None)
        primary_ip_address = getattr(primary_ip, "address", None)
        ip_address = getattr(primary_ip_address, "ip", None) if primary_ip else None
        dns_name = getattr(primary_ip, "dns_name", None) if primary_ip else None
        hostname = getattr(obj, "name", None)

        # Try IP address
        if ip_address:
            librenms_id = self._normalize_librenms_id(self.get_device_id_by_ip(ip_address))
            if librenms_id is not None:
                self._store_librenms_id(obj, librenms_id)
                return librenms_id

        # Try primary IP's DNS name
        if dns_name:
            librenms_id = self._normalize_librenms_id(self.get_device_id_by_hostname(dns_name))
            if librenms_id is not None:
                self._store_librenms_id(obj, librenms_id)
                return librenms_id

        # Try hostname if FQDN
        if hostname:
            librenms_id = self._normalize_librenms_id(self.get_device_id_by_hostname(hostname))
            if librenms_id is not None:
                self._store_librenms_id(obj, librenms_id)
                return librenms_id

        return None

    @staticmethod
    def _normalize_librenms_id(value):
        """
        Coerce a raw LibreNMS ID value to int or None.

        Thin wrapper around :func:`netbox_librenms_plugin.utils.coerce_librenms_id`
        kept for back-compat with internal callers in this module.

        Args:
            value: The raw LibreNMS id value (int, digit string, or other).

        Returns:
            int | None: The coerced id, or None if it can't be coerced.
        """
        from netbox_librenms_plugin.utils import coerce_librenms_id

        return coerce_librenms_id(value)

    def _get_cache_key(self, obj, server_key=None):
        """
        Generate a unique cache key for an object.

        Args:
            obj: NetBox device or VM object
            server_key: LibreNMS server key to scope the key to; defaults to this
                client's bound ``server_key``. Pass an explicit key when reading on
                behalf of a different (scoped) server than the client is bound to.

        Returns:
            str: Cache key
        """
        object_type = obj._meta.model_name
        resolved_key = server_key if server_key is not None else getattr(self, "server_key", "default")
        return f"librenms_device_id_{object_type}_{obj.pk}_{resolved_key}"

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
            from netbox_librenms_plugin.utils import set_librenms_device_id

            set_librenms_device_id(obj, librenms_id, self.server_key)
            obj.save(update_fields=["custom_field_data"])
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
        except (requests.exceptions.RequestException, ValueError, IndexError, KeyError, TypeError):
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
        except (requests.exceptions.RequestException, ValueError, IndexError, KeyError, TypeError):
            return None

    def get_device_info(self, device_id, use_cache=True):
        """
        Fetch device information from LibreNMS using its primary IP.

        Successful lookups are cached briefly (``DEVICE_INFO_CACHE_TIMEOUT``) per
        server/device so the device-info header doesn't re-hit LibreNMS on every
        sync-tab render. Failures are never cached, so a transient error doesn't
        persist for the cache window.

        Args:
            device_id: LibreNMS device ID
            use_cache: When False, bypass the short read cache and fetch live data
                (still refreshing the cache on success). Import decisions pass False so a
                value just corrected in LibreNMS isn't read back stale within the cache window.

        Returns:
            tuple: (success: bool, data: dict)
        """
        cache_key = f"librenms_device_info_{self.server_key}_{device_id}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            device_data = response.json()["devices"][0]
            if not isinstance(device_data, dict):
                return False, None
            # LibreNMS 26.5.0 returns the full location relationship object
            # (keys: id, location, lat, lng, timestamp, fixed_coordinates)
            # instead of a flat location name string. Normalise to the name so
            # downstream consumers receive a consistent value.
            location = device_data.get("location")
            if isinstance(location, dict):
                device_data["location"] = location.get("location")
            result = (True, device_data)
            cache.set(cache_key, result, timeout=DEVICE_INFO_CACHE_TIMEOUT)
            return result
        except (requests.exceptions.RequestException, ValueError, IndexError, KeyError, TypeError):
            return False, None

    def get_ports(self, device_id, with_vlans=True):
        """
        Fetch ports data from LibreNMS for a device using its primary IP.

        Includes VLAN assignment data (ifVlan, ifTrunk) for interface VLAN sync.
        When with_vlans=True, includes detailed VLAN associations (tagged/untagged)
        for all ports in a single API call (requires LibreNMS 24.2.0+).

        Args:
            device_id: LibreNMS device ID
            with_vlans: Include detailed VLAN data for all ports (default: True)

        Returns:
            tuple: (success: bool, data: dict)
        """
        try:
            params = {
                "columns": "port_id,ifName,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias,ifPhysAddress,ifMtu,ifVlan,ifTrunk"
            }
            if with_vlans:
                params["with"] = "vlans"

            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/ports",
                headers=self.headers,
                params=params,
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

    def get_port_stack(self, device_id: int):
        """
        Fetch ifStackTable relationships from LibreNMS for a device.

        Returns port_stack pairs showing parent/child interface relationships
        (LAG membership and sub-interface nesting).

        Args:
            device_id: LibreNMS device ID

        Returns:
            tuple: (success: bool, data: list[dict] | str)
                On success: list of {high_port_id, low_port_id, high_ifIndex, low_ifIndex} dicts
                On failure: error string
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/port_stack",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            data = response.json()
            # A non-object top-level payload (list, string, null) is malformed — not a valid
            # "no relationships" answer. Fail rather than silently returning (True, []), which
            # would be indistinguishable from "no relationships" and skip valid sync updates.
            if not isinstance(data, dict):
                logger.warning("Unexpected port_stack response for device %s: %r", device_id, data)
                return False, "Unexpected response format from LibreNMS (non-object payload)"
            # Honor an explicit error status *before* consuming mappings: an error payload can
            # still carry mappings (e.g. {"status": "error", "message": ..., "mappings": []}),
            # and treating that as "no relationships" would mask a real API failure and silently
            # skip valid LAG/sub-interface sync. A genuine answer has no status (or "ok").
            status = data.get("status")
            if status is not None and (not isinstance(status, str) or status.lower() != "ok"):
                # Only an absent status or a case-insensitive "ok" string is a genuine answer.
                # A non-string status (e.g. {"status": false, "mappings": []}) is malformed and
                # must fail the call, not be accepted as an empty "no relationships" result.
                message = data.get("message") or "LibreNMS reported an error fetching port stack"
                logger.warning("port_stack error status for device %s: %r", device_id, data)
                return False, str(message)
            mappings = data.get("mappings")
            # The documented success envelope always contains a list-valued mappings field,
            # including when no relationships exist. Missing, null, non-list, or mixed-list data
            # is malformed. It must not become an authoritative empty relationship snapshot.
            if not isinstance(mappings, list) or any(not isinstance(item, dict) for item in mappings):
                logger.warning("Unexpected port_stack response for device %s: %r", device_id, data)
                return False, "Unexpected response format from LibreNMS (invalid 'mappings' payload)"
            return True, mappings
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return False, "Device not found in LibreNMS"
            return False, f"HTTP error: {str(e)}"
        except ValueError as e:
            # response.json() raises ValueError (older requests) / JSONDecodeError on a non-JSON
            # body. requests.exceptions.JSONDecodeError subclasses BOTH ValueError and
            # RequestException, so this must precede the RequestException handler — otherwise the
            # broad handler swallows JSON decode failures and reports "Error connecting" instead.
            return False, f"Invalid JSON from LibreNMS: {str(e)}"
        except requests.exceptions.RequestException as e:
            return False, f"Error connecting to LibreNMS: {str(e)}"

    def resolve_port_relationships(
        self,
        ports: list,
        port_stack: list,
        lag_patterns: dict | None = None,
        device_os: str | None = None,
        interface_name_field: str = "ifName",
        compiled_lag_patterns: list | None = None,
        compiled_sap_patterns: list | None = None,
    ) -> dict:
        """
        Resolve LAG membership and sub-interface parent relationships from LibreNMS data.

        Universal rules (vendor-agnostic, hardcoded):
          1. The LAG aggregate is normally the 'low' entry in a port_stack pair, but the
             aggregate side is determined authoritatively by _is_lag_aggregate() (ifType
             ieee8023adLag or a configured name pattern), not by position, so a pair whose
             aggregate is on the 'high' side is still mapped member->aggregate correctly.
          2. Skip a pair when a SAP pattern matches either port's ifName or ifDescr.
          3. Strip a '.N' suffix in the active name field to resolve a physical-level port.
          4. A pair in the active name field where one name is the other plus a numeric '.N'
             suffix is a parent/child pair. The child may be on either side.

        Configurable via PortStackLagPattern model:
          - Per-OS regex patterns identify LAG aggregates when ifType is not 'ieee8023adLag'.

        Args:
            ports: Port dicts from get_ports(), each with port_id, ifName, ifType keys.
            port_stack: Port stack dicts from get_port_stack(), each with
                        high_port_id and low_port_id keys.
            lag_patterns: Optional dict of {librenms_os: pattern_str} overriding DB lookup.
                          Pass an empty dict to disable name-pattern matching entirely.
                          When None (default), patterns are fetched from PortStackLagPattern,
                          scoped to device_os when that is provided.
            device_os: LibreNMS OS of the device being resolved. When set (and lag_patterns
                       is None), only that OS's pattern is loaded so a vendor-specific regex
                       can't misclassify an interface on another platform. When None, all
                       stored patterns are loaded (legacy, unscoped behaviour).
            interface_name_field: Interface-name field selected for this device
                                  ('ifName' or 'ifDescr'). Each relationship map is built from
                                  this field alone. A map this field leaves empty is taken from
                                  the other field alone.
            compiled_lag_patterns: Optional list of pre-compiled name-pattern regexes. When
                                   provided, the PortStackLagPattern DB read and per-call
                                   compile are skipped and this list is used directly, taking
                                   priority over lag_patterns/device_os.
            compiled_sap_patterns: Optional list of pre-compiled SAP-name regexes. A port_stack
                                   row whose ifName or ifDescr names a SAP describes a service,
                                   not an interface relationship, so it is skipped. Read from the
                                   rows stored for device_os only when the LAG patterns are read
                                   from there too, so a caller that supplies its own patterns
                                   stays DB-free; an empty list disables the skip.

        Returns:
            dict with keys (port_ids are canonical normalized positive ints, so every
            consumer can look up by ``normalize_librenms_port_id(...)`` without re-deriving
            str/int fallbacks):
                'lag_members':    {member_port_id: aggregate_port_id}
                'sub_interfaces': {child_port_id: parent_port_id}
        """
        from netbox_librenms_plugin.constants import DEFAULT_INTERFACE_NAME_FIELD, INTERFACE_NAME_FIELDS
        from netbox_librenms_plugin.utils import normalize_librenms_port_id

        # Ignore malformed port items without losing valid relationships from the same payload.
        safe_ports = [port for port in ports if isinstance(port, dict)]

        # Keep nameless ports because ifType can identify a LAG aggregate without a name.
        by_id = {}
        ambiguous_port_ids = set()
        for port in safe_ports:
            port_id = normalize_librenms_port_id(port.get("port_id"))
            if port_id is None or port_id in ambiguous_port_ids:
                continue
            if port_id in by_id:
                by_id.pop(port_id)
                ambiguous_port_ids.add(port_id)
                continue
            by_id[port_id] = port
        ports_with_id = list(by_id.values())

        if compiled_lag_patterns is not None:
            compiled_patterns = compiled_lag_patterns
        elif lag_patterns is None:
            from netbox_librenms_plugin.models import PortStackLagPattern

            compiled_patterns = PortStackLagPattern.compiled_patterns_for_os(device_os)
        else:
            import re as _re

            compiled_patterns = []
            for pattern_str in lag_patterns.values():
                try:
                    compiled_patterns.append(_re.compile(pattern_str))
                except (_re.error, TypeError) as exc:
                    logger.warning("Skipping invalid LAG name pattern %r: %s", pattern_str, exc)

        if compiled_sap_patterns is None:
            if compiled_lag_patterns is None and lag_patterns is None:
                from netbox_librenms_plugin.models import PortStackLagPattern

                compiled_sap_patterns = PortStackLagPattern.compiled_sap_patterns_for_os(device_os)
            else:
                # A caller that supplies LAG patterns also supplies the SAP policy.
                compiled_sap_patterns = []

        # Validate and remove SAP rows once so fallback cannot reconsider them.
        filtered_port_pairs = []
        for entry in port_stack:
            if not isinstance(entry, dict):
                continue
            # Rows carry the ports_stack columns high_port_id/low_port_id; the documented port_id_high spelling is never sent.
            if "high_port_id" not in entry and "low_port_id" not in entry:
                logger.warning("Unrecognized port_stack entry shape, keys: %s", sorted(entry))
                continue
            high_id = normalize_librenms_port_id(entry.get("high_port_id"))
            low_id = normalize_librenms_port_id(entry.get("low_port_id"))
            if high_id is None or low_id is None:
                continue
            high_port = by_id.get(high_id)
            low_port = by_id.get(low_id)
            if not high_port or not low_port:
                continue
            names = tuple(
                name
                for port in (high_port, low_port)
                for field in INTERFACE_NAME_FIELDS
                if isinstance(name := port.get(field), str) and name
            )
            if any(pattern.search(name) for pattern in compiled_sap_patterns for name in names):
                continue
            filtered_port_pairs.append((high_port, low_port))

        def _resolve_with(field: str) -> tuple[dict, dict]:
            by_name: dict[str, dict] = {}
            ambiguous_names: set[str] = set()
            for port in ports_with_id:
                name = port.get(field)
                if not isinstance(name, str) or not name or name in ambiguous_names:
                    continue
                existing = by_name.get(name)
                if existing is not None and normalize_librenms_port_id(
                    existing.get("port_id")
                ) != normalize_librenms_port_id(port.get("port_id")):
                    by_name.pop(name)
                    ambiguous_names.add(name)
                    continue
                by_name[name] = port

            lag_members: dict = {}
            sub_interfaces: dict = {}
            conflicted_lag_members: set = set()
            conflicted_sub_interfaces: set = set()

            def _is_lag_aggregate(port: dict) -> bool:
                if port.get("ifType") == "ieee8023adLag":
                    return True
                name = port.get(field)
                return isinstance(name, str) and any(pattern.search(name) for pattern in compiled_patterns)

            def _relate(mapping: dict, conflicted_keys: set, key_port: dict, value_port: dict) -> None:
                """Store a normalized edge and drop keys that have conflicting targets."""
                key_id = normalize_librenms_port_id(key_port.get("port_id"))
                value_id = normalize_librenms_port_id(value_port.get("port_id"))
                if key_id is None or value_id is None or key_id in conflicted_keys:
                    return
                existing = mapping.get(key_id)
                if existing is None:
                    mapping[key_id] = value_id
                elif existing != value_id:
                    mapping.pop(key_id, None)
                    conflicted_keys.add(key_id)

            def _resolve_physical_port(port: dict) -> dict:
                """Resolve the physical-level port named by the active field."""
                name = port.get(field)
                if not isinstance(name, str):
                    return port
                base, separator, suffix = name.rpartition(".")
                if separator and suffix.isdigit():
                    return by_name.get(base, port)
                return port

            def _name_derived_parent(port: dict) -> dict | None:
                """Resolve the parent named by a numeric suffix in the active field."""
                name = port.get(field)
                if not isinstance(name, str):
                    return None
                base, separator, suffix = name.rpartition(".")
                if not separator or not suffix.isdigit():
                    return None
                candidate = by_name.get(base)
                return candidate if candidate is not port else None

            def _is_sub_unit_of(child_port: dict, parent_port: dict) -> bool:
                """Return whether the active field names child_port as a sub-unit."""
                child_name = child_port.get(field)
                parent_name = parent_port.get(field)
                if not isinstance(child_name, str) or not isinstance(parent_name, str) or not parent_name:
                    return False
                return child_name.startswith(parent_name + ".") and child_name[len(parent_name) + 1 :].isdigit()

            for high_port, low_port in filtered_port_pairs:
                # ifStack ordering does not determine which side is the child.
                if _is_sub_unit_of(low_port, high_port):
                    _relate(sub_interfaces, conflicted_sub_interfaces, low_port, high_port)
                    continue
                if _is_sub_unit_of(high_port, low_port):
                    _relate(sub_interfaces, conflicted_sub_interfaces, high_port, low_port)
                    continue

                high_phys = _resolve_physical_port(high_port)
                low_phys = _resolve_physical_port(low_port)
                if normalize_librenms_port_id(high_phys.get("port_id")) == normalize_librenms_port_id(
                    low_phys.get("port_id")
                ):
                    continue

                low_is_agg = _is_lag_aggregate(low_phys)
                high_is_agg = _is_lag_aggregate(high_phys)
                if low_is_agg and high_is_agg:
                    # Structural ifType disambiguates a pair where both names match a LAG pattern.
                    low_struct = low_phys.get("ifType") == "ieee8023adLag"
                    high_struct = high_phys.get("ifType") == "ieee8023adLag"
                    if low_struct and not high_struct:
                        _relate(lag_members, conflicted_lag_members, high_phys, low_phys)
                    elif high_struct and not low_struct:
                        _relate(lag_members, conflicted_lag_members, low_phys, high_phys)
                elif low_is_agg:
                    _relate(lag_members, conflicted_lag_members, high_phys, low_phys)
                elif high_is_agg:
                    _relate(lag_members, conflicted_lag_members, low_phys, high_phys)

            # Every sub-interface edge shortens the active-field name, so the graph cannot contain a cycle.
            for port in ports_with_id:
                child_id = normalize_librenms_port_id(port.get("port_id"))
                if child_id is None or child_id in sub_interfaces or child_id in conflicted_sub_interfaces:
                    continue
                parent_port = _name_derived_parent(port)
                if parent_port is None or normalize_librenms_port_id(parent_port.get("port_id")) is None:
                    continue
                _relate(sub_interfaces, conflicted_sub_interfaces, port, parent_port)

            return lag_members, sub_interfaces

        if not isinstance(interface_name_field, str) or interface_name_field not in INTERFACE_NAME_FIELDS:
            interface_name_field = DEFAULT_INTERFACE_NAME_FIELD
        lag_members, sub_interfaces = _resolve_with(interface_name_field)
        # Take each empty map from the other field without mixing fields within a map.
        if not lag_members or not sub_interfaces:
            fallback_field = next(field for field in INTERFACE_NAME_FIELDS if field != interface_name_field)
            fallback_lag_members, fallback_sub_interfaces = _resolve_with(fallback_field)
            if not lag_members:
                logger.debug(
                    "The lag_members map from %s is empty. The resolver uses %s alone.",
                    interface_name_field,
                    fallback_field,
                )
                lag_members = fallback_lag_members
            if not sub_interfaces:
                logger.debug(
                    "The sub_interfaces map from %s is empty. The resolver uses %s alone.",
                    interface_name_field,
                    fallback_field,
                )
                sub_interfaces = fallback_sub_interfaces
        return {"lag_members": lag_members, "sub_interfaces": sub_interfaces}

    def add_device(self, data):
        """
        Add a device to LibreNMS.

        Args:
            Dictionary containing device data including:
                - hostname: Device hostname or IP
                - snmp_version: SNMP version (v1, v2c, or v3)
                - force_add: Skip checks for duplicate device and SNMP reachability (optional, default False)
                - port: SNMP port (optional, defaults to config value)
                - transport: SNMP transport protocol (optional: udp, tcp, udp6, tcp6)
                - port_association_mode: Port identification method (optional: ifIndex, ifName, ifDescr, ifAlias)
                - poller_group: Poller group ID (optional, defaults to 0)
                - community: SNMP community string (for v1 or v2c)
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

        if data["snmp_version"] in ("v1", "v2c"):
            payload["community"] = data["community"]
        elif data["snmp_version"] == "v3":
            payload["authlevel"] = data["authlevel"]
            payload["authname"] = data["authname"]
            # Credential keys only apply at the auth levels that use them. Omit
            # empty values instead of sending empty strings — LibreNMS rejects
            # those for noAuthNoPriv / authNoPriv add-device requests.
            for key in ("authpass", "authalgo", "cryptopass", "cryptoalgo"):
                value = data.get(key)
                if value:
                    payload[key] = value

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
                return False, result.get("message") or "Unexpected response format"
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
                return False, result.get("message") or "Unexpected response format"
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
            data = response.json()
            addresses = data.get("addresses") if isinstance(data, dict) else None
            if not isinstance(addresses, list):
                message = data.get("message") if isinstance(data, dict) else None
                return False, message or "Unexpected response format: 'addresses' must be a list"
            return True, addresses
        except requests.exceptions.HTTPError as e:
            # LibreNMS returns this specific HTTP 404 for an existing device with no IP
            # addresses. Other 404s (for example a stale device id) remain real failures.
            if e.response is not None and e.response.status_code == 404:
                try:
                    error_data = e.response.json()
                except ValueError:
                    error_data = None
                message = error_data.get("message") if isinstance(error_data, dict) else None
                if isinstance(message, str) and "does not have any ip addresses" in message.lower():
                    return True, []
                return False, message or str(e)
            return False, str(e)
        except (requests.exceptions.RequestException, ValueError) as e:
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
            inventory_data = response.json()
            inventory = inventory_data.get("inventory") if isinstance(inventory_data, dict) else None
            if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
                msg = inventory_data.get("message", "") if isinstance(inventory_data, dict) else ""
                logger.warning(f"Unexpected inventory response for device {device_id}: {inventory_data}")
                return False, msg or "Unexpected response format: invalid 'inventory' payload"
            return True, inventory
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, str(e)

    def get_device_transceivers(self, device_id):
        """
        Fetch all transceiver data for a device from LibreNMS.

        Route: /api/v0/devices/{device_id}/transceivers

        This is a separate data source from entity inventory. Some vendors
        (e.g., Nokia/SROS) don't expose SFPs via ENTITY-MIB but do report
        them through vendor-specific MIBs which LibreNMS surfaces here.

        Args:
            device_id: LibreNMS device ID

        Returns:
            tuple: (success: bool, data: list)

        Example transceiver item:
            {
                "port_id": 519,
                "entity_physical_index": 1610899520,
                "type": "CFP2/QSFP28",
                "model": "3HE10550AARA01",
                "serial": "X42AU0D",
                "channels": 4,
                "connector": "LC",
                "wavelength": 1301,
                ...
            }
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/devices/{device_id}/transceivers",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            try:
                data = response.json()
            except ValueError:
                return False, f"Invalid JSON in transceivers response for device {device_id}"

            if not isinstance(data, dict) or "transceivers" not in data:
                msg = data.get("message") if isinstance(data, dict) else None
                return False, msg or f"Unexpected transceivers response format for device {device_id}"

            if data.get("status") != "ok":
                msg = data.get("message") or f"LibreNMS returned status={data.get('status')!r} for device {device_id}"
                return False, msg

            transceivers = data["transceivers"]
            if not isinstance(transceivers, list):
                msg = data.get("message")
                return False, msg or f"Unexpected transceivers response format for device {device_id}"

            if any(item is None or not isinstance(item, dict) for item in transceivers):
                return False, f"Malformed transceiver entry in response for device {device_id}"

            return True, transceivers
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
            result = response.json()
            if isinstance(result, dict) and result.get("status") == "ok":
                poller_groups = result.get("get_poller_group")
                if not isinstance(poller_groups, list):
                    return False, result.get("message") or "Unexpected response format: missing 'get_poller_group' list"
                if not all(isinstance(item, dict) for item in poller_groups):
                    return False, "Unexpected response format: invalid item shape in 'get_poller_group'"
                return True, poller_groups
            if isinstance(result, dict):
                return False, result.get("message") or "Unexpected response format"
            return False, "Unexpected response format: non-object JSON"
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, str(e)

    def get_inventory_filtered(self, device_id, ent_physical_class=None, ent_physical_contained_in=None):
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

            data = response.json()
            if isinstance(data, dict) and data.get("status") == "ok":
                inventory = data.get("inventory")
                if not isinstance(inventory, list) or any(not isinstance(item, dict) for item in inventory):
                    msg = data.get("message")
                    return False, msg or "Unexpected response format: invalid 'inventory' payload"
                logger.debug(f"API returned {len(inventory)} items")

                # If we got results or didn't specify filters, return
                if inventory or not params:
                    return True, inventory

            # If filtered endpoint returned empty but we have filters,
            # try /all endpoint and filter client-side
            if params:
                logger.debug("Filtered inventory API returned no results, falling back to client-side filtering")
                success, all_inventory = self.get_device_inventory(device_id)

                if not success:
                    return False, all_inventory

                # Apply client-side filters
                filtered = all_inventory
                if ent_physical_class:
                    filtered = [item for item in filtered if item.get("entPhysicalClass") == ent_physical_class]
                if ent_physical_contained_in is not None:
                    filtered = [
                        item
                        for item in filtered
                        if str(item.get("entPhysicalContainedIn")) == str(ent_physical_contained_in)
                    ]

                return True, filtered

            # LibreNMS API v0 always returns JSON objects, so data is always
            # a dict here; the isinstance guard is purely defensive.
            if isinstance(data, dict):
                return False, data.get("message") or "Unexpected response format"
            return False, "Unexpected response format"

        except (requests.exceptions.RequestException, ValueError) as e:
            logger.warning(f"Failed to fetch filtered inventory: {e}")
            return False, str(e)

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
            result = response.json()
            if isinstance(result, dict) and result.get("status") == "ok":
                devices = result.get("devices")
                if not isinstance(devices, list):
                    msg = result.get("message")
                    return False, msg or "Unexpected response format: missing 'devices' list"
                if not all(isinstance(item, dict) for item in devices):
                    return False, "Unexpected response format: invalid item shape in 'devices'"
                return True, devices

            # LibreNMS API v0 always returns JSON objects, so result is always
            # a dict here; the isinstance guard is purely defensive.
            if isinstance(result, dict):
                return False, result.get("message") or "Unexpected response format"
            return False, "Unexpected response format"
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, str(e)

    # =========================================================================
    # VLAN Methods
    # =========================================================================

    def get_device_vlans(self, device_id: int) -> tuple[bool, list | str]:
        """
        Fetch all VLANs configured on a device using the resources endpoint.

        This method uses /api/v0/resources/vlans which includes the vlan_id
        primary key, unlike /api/v0/devices/{device_id}/vlans which omits it.

        Route: /api/v0/resources/vlans

        Args:
            device_id: LibreNMS device ID

        Returns:
            tuple: (success: bool, data: list of VLAN dicts or error string)

        Example VLAN:
            {
                "vlan_id": 123,
                "device_id": 1,
                "vlan_vlan": 50,
                "vlan_domain": 1,
                "vlan_name": "ORG_DATA",
                "vlan_type": "ethernet",
                "vlan_state": 1
            }
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/resources/vlans",
                headers=self.headers,
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()
            if isinstance(result, dict) and result.get("status") == "ok":
                all_vlans = result.get("vlans")
                if not isinstance(all_vlans, list):
                    msg = result.get("message")
                    return False, msg or "Unexpected response format: missing 'vlans' list"
                if not all(isinstance(v, dict) for v in all_vlans):
                    return False, "Unexpected response format: invalid item shape in 'vlans'"
                # Filter VLANs by device_id since resources endpoint returns all VLANs
                device_vlans = [v for v in all_vlans if str(v.get("device_id")) == str(device_id)]
                return True, device_vlans
            if isinstance(result, dict):
                return False, result.get("message") or "Unexpected response format"
            return False, "Unexpected response format"

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return False, "VLANs resource not found"
            return False, f"HTTP error: {str(e)}"
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, f"Error connecting to LibreNMS: {str(e)}"

    def get_port_vlan_details(self, port_id: int) -> tuple[bool, dict | str]:
        """
        Fetch detailed VLAN associations for a single port.
        Required for trunk ports to get the tagged VLANs list.

        Route: /api/v0/ports/{port_id}?with=vlans

        Args:
            port_id: LibreNMS port ID

        Returns:
            tuple: (success: bool, data: port dict with vlans array or error string)

        Example port:
            {
                "port_id": 227011,
                "ifName": "Te1/1/1",
                "ifVlan": "90",
                "ifTrunk": "dot1Q",
                "vlans": [
                    {"vlan": 90, "untagged": 1, "state": "unknown"},
                    {"vlan": 50, "untagged": 0, "state": "forwarding"}
                ]
            }
        """
        try:
            response = requests.get(
                f"{self.librenms_url}/api/v0/ports/{port_id}",
                headers=self.headers,
                params={"with": "vlans"},
                timeout=DEFAULT_API_TIMEOUT,
                verify=self.verify_ssl,
            )
            response.raise_for_status()

            result = response.json()
            if not isinstance(result, dict):
                return False, "Unexpected response format"
            if result.get("status") != "ok":
                msg = result.get("message") or f"LibreNMS returned status={result.get('status')!r} for port"
                return False, msg
            port_data = result.get("port")
            if not isinstance(port_data, list):
                return False, result.get("message") or "Unexpected response format: missing 'port' list"
            if not port_data:
                return False, "Port not found"
            if not isinstance(port_data[0], dict):
                return False, "Unexpected response format: invalid 'port' entry"
            return True, port_data[0]

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return False, "Port not found in LibreNMS"
            return False, f"HTTP error: {str(e)}"
        except (requests.exceptions.RequestException, ValueError) as e:
            return False, f"Error connecting to LibreNMS: {str(e)}"

    def parse_port_vlan_data(self, port_data: dict, interface_name_field: str = "ifName") -> dict:
        """
        Transform LibreNMS port VLAN data into normalized structure.

        Args:
            port_data: Raw port dict from LibreNMS API
            interface_name_field: Field to use for interface name ('ifName' or 'ifDescr')

        Returns:
            dict: Normalized structure with:
                - port_id: int
                - interface_name: str (value from interface_name_field)
                - ifName: str (always included for reference)
                - ifDescr: str (always included for reference)
                - mode: 'access' | 'tagged' | None
                - untagged_vlan: int | None
                - tagged_vlans: list[int]
        """
        port_id = port_data.get("port_id")
        if_name = port_data.get("ifName", "")
        if_descr = port_data.get("ifDescr", "")
        interface_name = port_data.get(interface_name_field, "") or if_name
        if_vlan = port_data.get("ifVlan", "")
        if_trunk = port_data.get("ifTrunk")

        # Determine 802.1Q mode
        if not if_vlan:
            mode = None
        elif if_trunk == "dot1Q":
            mode = "tagged"
        else:
            mode = "access"

        # Parse VLAN assignments from vlans array if present
        vlans_data = port_data.get("vlans", [])
        untagged_vlan = None
        tagged_vlans = []

        if isinstance(vlans_data, list) and vlans_data:
            # Parse from detailed vlans array
            for vlan_entry in vlans_data:
                if not isinstance(vlan_entry, dict):
                    continue
                vlan_id = vlan_entry.get("vlan")
                if vlan_id is None:
                    continue
                try:
                    vlan_id = int(vlan_id)
                except (ValueError, TypeError):
                    continue
                if vlan_entry.get("untagged") == 1:
                    untagged_vlan = vlan_id
                else:
                    tagged_vlans.append(vlan_id)
        elif if_vlan:
            # Fallback to ifVlan field for basic port info
            try:
                untagged_vlan = int(if_vlan)
            except (ValueError, TypeError):
                pass

        return {
            "port_id": port_id,
            "interface_name": interface_name,
            "ifName": if_name,
            "ifDescr": if_descr,
            "mode": mode,
            "untagged_vlan": untagged_vlan,
            "tagged_vlans": sorted(tagged_vlans),
        }

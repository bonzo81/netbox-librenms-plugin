"""Device filtering and retrieval from LibreNMS."""

import logging
from typing import List

from django.core.cache import cache

from .cache import get_import_search_cache_key

from ..librenms_api import LibreNMSAPI

logger = logging.getLogger(__name__)


def _safe_disabled(device: dict) -> int:
    """
    Return 1 if the device is disabled, 0 otherwise.

    Handles None, booleans, numeric strings, and common truthy/falsy tokens
    (e.g. "true"/"yes"/"on" → 1, "false"/"no"/"off" → 0) without raising.
    """
    val = device.get("disabled", 0)
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return 1
        if normalized in ("0", "false", "no", "off", ""):
            return 0
    try:
        int_val = int(val)
        return 1 if int_val else 0
    except (TypeError, ValueError):
        return 0


def get_device_count_for_filters(
    api: LibreNMSAPI,
    filters: dict,
    clear_cache: bool = False,
    show_disabled: bool = True,
) -> int:
    """
    Get count of LibreNMS devices matching filters.

    This is a lightweight function to determine device count for background job
    decision making. Uses the same caching as get_librenms_devices_for_import().

    Args:
        api: LibreNMS API client instance
        filters: Filter dict with location, type, os, hostname, sysname keys
        clear_cache: Whether to force cache refresh
        show_disabled: Whether to include disabled devices

    Returns:
        int: Count of devices matching filters
    """
    devices = get_librenms_devices_for_import(api, filters=filters, force_refresh=clear_cache)

    # Filter out disabled devices if requested. LibreNMS's "disabled" field (1=disabled,
    # 0=enabled) reflects manual device disablement; "status" reflects SNMP reachability.
    # show_disabled controls the former: hidden when disabled==1, shown regardless of status.
    if not show_disabled:
        devices = [d for d in devices if _safe_disabled(d) != 1]

    return len(devices)


def get_librenms_devices_for_import(
    api: LibreNMSAPI = None,
    filters: dict = None,
    server_key: str = None,
    *,
    force_refresh: bool = False,
    return_cache_status: bool = False,
) -> List[dict] | tuple[List[dict], bool]:
    """
    Retrieve LibreNMS devices based on filters.

    Args:
        api: LibreNMSAPI instance (if not provided, creates one with server_key)
        filters: Dict containing filter parameters:
            - location: LibreNMS location/site filter
            - type: Device type filter
            - os: Operating system filter
            - hostname: Hostname filter (partial match)
            - sysname: System name filter (partial match)
            - status: Device status filter (1=up, 0=down)
            - disabled: Include disabled devices (0=active only, 1=all)
        server_key: Key for specific server configuration (used if api not provided)
        force_refresh: When True, bypass the cache and fetch fresh data
        return_cache_status: When True, returns (devices, from_cache) tuple

    Returns:
        List of device dictionaries from LibreNMS, or tuple of (devices, from_cache)
        if return_cache_status is True. from_cache=True means data was loaded from
        existing cache; from_cache=False means data was just fetched from LibreNMS.
    """
    try:
        # Use provided API instance or create a new one
        if api is None:
            api = LibreNMSAPI(server_key=server_key)

        # Build LibreNMS API filters using the type/query format
        # LibreNMS API v0 expects ?type=X&query=Y format, not direct parameters
        # NOTE: API only supports ONE type/query pair, so we'll use the most
        # specific filter for the API and apply others client-side
        api_filters = {}
        client_filters = {}  # Filters to apply after fetching from API

        if filters:
            # Check for status filter first - it has special handling
            if filters.get("status") is not None:
                # Normalize to int: form fields send strings ("1"/"0"), API may send ints
                try:
                    status_val = int(filters["status"])
                except (ValueError, TypeError):
                    status_val = None
                # Status filter uses special types that don't need query param
                if status_val == 1:
                    api_filters["type"] = "up"
                elif status_val == 0:
                    api_filters["type"] = "down"

                # Save ALL other filters for client-side filtering when status is used
                if filters.get("location"):
                    client_filters["location"] = filters["location"]
                if filters.get("type"):
                    client_filters["type"] = filters["type"]
                if filters.get("os"):
                    client_filters["os"] = filters["os"]
                if filters.get("hostname"):
                    client_filters["hostname"] = filters["hostname"]
                if filters.get("sysname"):
                    client_filters["sysname"] = filters["sysname"]
                if filters.get("hardware"):
                    client_filters["hardware"] = filters["hardware"]
            else:
                # Priority order for type/query filters: location > type > os > hostname > sysname
                # Note: When sysname is combined with other filters, it's applied client-side for partial matching
                # When sysname is alone, it uses API exact match (type=sysName)
                # Note: hardware is always applied client-side for partial matching
                # Use first available for API, save others for client-side filtering
                if filters.get("location"):
                    api_filters["type"] = "location_id"
                    api_filters["query"] = filters["location"]
                    # Save remaining filters for client-side
                    if filters.get("type"):
                        client_filters["type"] = filters["type"]
                    if filters.get("os"):
                        client_filters["os"] = filters["os"]
                    if filters.get("hostname"):
                        client_filters["hostname"] = filters["hostname"]
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                    if filters.get("hardware"):
                        client_filters["hardware"] = filters["hardware"]
                elif filters.get("type"):
                    api_filters["type"] = "type"
                    api_filters["query"] = filters["type"]
                    # Save remaining filters for client-side
                    if filters.get("os"):
                        client_filters["os"] = filters["os"]
                    if filters.get("hostname"):
                        client_filters["hostname"] = filters["hostname"]
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                    if filters.get("hardware"):
                        client_filters["hardware"] = filters["hardware"]
                elif filters.get("os"):
                    api_filters["type"] = "os"
                    api_filters["query"] = filters["os"]
                    # Save remaining filters for client-side
                    if filters.get("hostname"):
                        client_filters["hostname"] = filters["hostname"]
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                    if filters.get("hardware"):
                        client_filters["hardware"] = filters["hardware"]
                elif filters.get("hostname"):
                    api_filters["type"] = "hostname"
                    api_filters["query"] = filters["hostname"]
                    # Save sysname and hardware for client-side
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                    if filters.get("hardware"):
                        client_filters["hardware"] = filters["hardware"]
                elif filters.get("sysname"):
                    # sysname-only filter: Use API exact match (type=sysName&query=<value>)
                    # This is safe - returns empty if no exact match found
                    api_filters["type"] = "sysName"
                    api_filters["query"] = filters["sysname"]
                    # Save hardware for client-side
                    if filters.get("hardware"):
                        client_filters["hardware"] = filters["hardware"]
                elif filters.get("hardware"):
                    # hardware-only filter: apply client-side for partial matching
                    client_filters["hardware"] = filters["hardware"]

            # Note: disabled filter isn't directly supported by LibreNMS API
            # We'll filter client-side if needed

        # Use caching to avoid repeated API calls
        # Include both API and client filters in cache key (deterministic, cross-process stable).
        # Use api.server_key (always resolved) rather than the raw server_key arg (may differ).
        cache_key = get_import_search_cache_key(api.server_key, api_filters, client_filters)
        from_cache = False

        if force_refresh:
            cache.delete(cache_key)
        else:
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                # No need to deepcopy - cached data isn't mutated
                devices = cached_result
                from_cache = True
                if return_cache_status:
                    return devices, from_cache
                return devices

        success, devices = api.list_devices(api_filters if api_filters else None)

        if not success:
            logger.error(f"Failed to retrieve devices from LibreNMS: {devices}")
            # Cache a brief negative result to prevent hammering the API on repeated failures.
            cache.set(cache_key, [], timeout=min(60, api.cache_timeout))
            if return_cache_status:
                return [], False
            return []

        # Apply client-side filters if any
        if client_filters:
            devices = _apply_client_filters(devices, client_filters)

        # Cache using configured timeout (default 300s)
        # No need to deepcopy - Django's cache backend handles serialization
        cache.set(cache_key, devices, timeout=api.cache_timeout)

        if return_cache_status:
            return devices, from_cache
        return devices

    except Exception:
        logger.exception("Error retrieving LibreNMS devices for import")
        if return_cache_status:
            return [], False
        return []


def _apply_client_filters(devices: List[dict], filters: dict) -> List[dict]:
    """
    Apply client-side filters to device list.

    Args:
        devices: List of device dicts from LibreNMS
        filters: Dict of filters to apply (location, type, os, hostname, sysname)

    Returns:
        Filtered list of devices
    """
    filtered = devices

    if filters.get("location"):
        location_id = str(filters["location"])
        filtered = [d for d in filtered if str(d.get("location_id", "")) == location_id]

    if filters.get("type"):
        device_type = filters["type"].lower()
        filtered = [d for d in filtered if (d.get("type") or "").lower() == device_type]

    if filters.get("os"):
        os_filter = filters["os"].lower()
        filtered = [d for d in filtered if os_filter in (d.get("os") or "").lower()]

    if filters.get("hostname"):
        hostname_filter = filters["hostname"].lower()
        filtered = [d for d in filtered if hostname_filter in (d.get("hostname") or "").lower()]

    if filters.get("sysname"):
        sysname_filter = filters["sysname"].lower()
        filtered = [d for d in filtered if sysname_filter in (d.get("sysName") or "").lower()]

    if filters.get("hardware"):
        hardware_filter = filters["hardware"].lower()
        filtered = [d for d in filtered if hardware_filter in (d.get("hardware") or "").lower()]

    return filtered

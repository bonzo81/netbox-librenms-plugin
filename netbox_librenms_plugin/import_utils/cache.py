"""Cache key generation and management for device import operations."""

import hashlib
import json
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_location_choices_cache_key(server_key: str) -> str:
    """Return the cache key for LibreNMS location choices for a given server."""
    return f"librenms_locations_choices:{server_key}"


def get_cache_metadata_key(
    server_key: str, filters: dict, vc_enabled: bool, use_sysname: bool = True, strip_domain: bool = False
) -> str:
    """
    Generate a consistent cache metadata key from filter parameters.

    Args:
        server_key: LibreNMS server identifier
        filters: Filter dictionary
        vc_enabled: Whether VC detection is enabled
        use_sysname: Whether sysName is preferred over hostname for device naming
        strip_domain: Whether domain suffix is stripped from device names

    Returns:
        str: Consistent cache key for metadata
    """
    # Sort filter items to ensure consistent key generation; use "is not None" to preserve
    # valid falsy values like 0 and False (filtering only None/missing entries).
    # Use JSON serialization for a stable, collision-free hash (avoids issues with
    # values containing "=" or "_" that could collide with the key separators).
    filter_hash = hashlib.sha256(
        json.dumps(
            {k: v for k, v in sorted(filters.items()) if v is not None}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()[:16]
    return f"librenms_filter_cache_metadata_{server_key}_{filter_hash}_{vc_enabled}_sysname={use_sysname}_strip={strip_domain}"


def get_active_cached_searches(server_key: str) -> list[dict]:
    """
    Retrieve all active cached searches for a server and enrich with display-friendly values.

    Enriches raw filter IDs with human-readable names by looking up location names
    from cached choices and converting type codes to display names.

    Args:
        server_key: LibreNMS server identifier

    Returns:
        List of dicts containing cache metadata with enriched display_filters
    """
    from datetime import datetime, timezone

    cache_index_key = f"librenms_cache_index_{server_key}"
    cache_index = cache.get(cache_index_key, [])

    active_searches = []
    valid_cache_keys = []

    # Get location and type choices for enriching display
    location_choices = {}
    type_choices = {
        "": "All Types",
        "network": "Network",
        "server": "Server",
        "storage": "Storage",
        "wireless": "Wireless",
        "firewall": "Firewall",
        "power": "Power",
        "appliance": "Appliance",
        "printer": "Printer",
        "loadbalancer": "Load Balancer",
        "other": "Other",
    }

    # Get cached location choices for enrichment; scoped by server_key so labels
    # from different LibreNMS servers don't bleed into each other's filter summaries.
    location_cache_key = get_location_choices_cache_key(server_key)
    cached_locations = cache.get(location_cache_key)
    if cached_locations:
        location_choices = dict(cached_locations)

    for cache_key in cache_index:
        metadata = cache.get(cache_key)
        if metadata:
            # Cache still exists, calculate time remaining
            cache_timeout = metadata.get("cache_timeout", 300)
            now = datetime.now(timezone.utc)
            try:
                cached_at_raw = metadata.get("cached_at")
                cached_at = (
                    datetime.fromisoformat(cached_at_raw) if cached_at_raw else datetime.fromtimestamp(0, timezone.utc)
                )
                # Normalize naive datetimes (e.g., stored without tzinfo) to UTC
                if cached_at.tzinfo is None:
                    cached_at = cached_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                cached_at = datetime.fromtimestamp(0, timezone.utc)
            age_seconds = (now - cached_at).total_seconds()
            remaining_seconds = max(0, cache_timeout - age_seconds)

            if remaining_seconds > 0:
                # Add remaining time and cache key
                metadata["remaining_seconds"] = int(remaining_seconds)
                metadata["cache_key"] = cache_key

                # Enrich filters with human-readable display values
                if "filters" in metadata:
                    display_filters = metadata["filters"].copy()
                    # Convert location ID to location name
                    if "location" in display_filters and display_filters["location"] in location_choices:
                        display_filters["location"] = location_choices[display_filters["location"]]
                    # Convert type code to display name
                    if "type" in display_filters and display_filters["type"] in type_choices:
                        display_filters["type"] = type_choices[display_filters["type"]]
                    metadata["display_filters"] = display_filters
                else:
                    # Fallback if filters key missing
                    metadata["display_filters"] = {}

                active_searches.append(metadata)
                valid_cache_keys.append(cache_key)

    # Clean up index if any keys have expired
    if len(valid_cache_keys) < len(cache_index):
        cache.set(cache_index_key, valid_cache_keys, timeout=3600)

    # Sort by most recent first
    active_searches.sort(key=lambda x: x.get("cached_at", ""), reverse=True)

    return active_searches


def get_validated_device_cache_key(
    server_key: str,
    filters: dict,
    device_id: int | str,
    vc_enabled: bool,
    use_sysname: bool = True,
    strip_domain: bool = False,
) -> str:
    """
    Generate a consistent cache key for validated device data.

    This ensures both synchronous and background job processing use the same
    cache keys, avoiding duplicate validation work and cache entries.

    Args:
        server_key: LibreNMS server key
        filters: Filter dict with location, type, os, hostname, sysname, hardware keys
        device_id: LibreNMS device ID
        vc_enabled: Whether virtual chassis detection was enabled
        use_sysname: Whether sysName is preferred over hostname for device naming
        strip_domain: Whether domain suffix is stripped from device names

    Returns:
        str: Cache key for the validated device

    Example:
        >>> key = get_validated_device_cache_key('default', {'location': 'NYC'}, 123, True)
        >>> key
        'validated_device_default_e3b0c44298fc1c14_123_vc'
    """
    # Sort filters for a deterministic, cross-process stable hash
    filter_hash = hashlib.sha256(json.dumps(sorted(filters.items()), sort_keys=True).encode()).hexdigest()[:16]
    vc_part = "vc" if vc_enabled else "novc"
    return (
        f"validated_device_{server_key}_{filter_hash}_{device_id}_{vc_part}_sysname={use_sysname}_strip={strip_domain}"
    )


def get_import_device_cache_key(device_id: int | str, server_key: str) -> str:
    """
    Generate cache key for raw LibreNMS device data.

    This key is used to cache raw device data (without validation metadata)
    to avoid redundant API calls when users interact with dropdowns during
    the import workflow.

    Args:
        device_id: LibreNMS device ID
        server_key: LibreNMS server identifier for multi-server setups (required)

    Returns:
        str: Cache key for the device data

    Example:
        >>> get_import_device_cache_key(123, "production")
        'import_device_data_production_123'
    """
    return f"import_device_data_{server_key}_{device_id}"


def get_import_search_cache_key(server_key: str, api_filters: dict, client_filters: dict) -> str:
    """
    Generate a deterministic cache key for a LibreNMS device search result.

    The key encodes the server, API-side filters, and client-side filters so
    that different filter combinations produce distinct cache entries.

    Args:
        server_key: Resolved LibreNMS server key (use ``api.server_key``).
        api_filters: Filters forwarded to the LibreNMS API.
        client_filters: Filters applied client-side after the API response.

    Returns:
        str: Cache key for the import search result.
    """
    import hashlib
    import json

    def _hash(d):
        return hashlib.sha256(
            json.dumps(sorted(d.items()) if isinstance(d, dict) else d, sort_keys=True).encode()
        ).hexdigest()[:16]

    return f"librenms_devices_import_{server_key}_{_hash(api_filters)}_{_hash(client_filters)}"

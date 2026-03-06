"""Cache key generation and search management for device import operations."""

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_cache_metadata_key(server_key: str, filters: dict, vc_enabled: bool) -> str:
    """
    Generate a consistent cache metadata key from filter parameters.

    Args:
        server_key: LibreNMS server identifier
        filters: Filter dictionary
        vc_enabled: Whether VC detection is enabled

    Returns:
        str: Consistent cache key for metadata
    """
    # Sort filter items to ensure consistent key generation
    filter_parts = "_".join(f"{k}={v}" for k, v in sorted(filters.items()) if v)
    return f"librenms_filter_cache_metadata_{server_key}_{filter_parts}_{vc_enabled}"


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

    # Get cached location choices for enrichment
    location_cache_key = "librenms_locations_choices"
    cached_locations = cache.get(location_cache_key)
    if cached_locations:
        location_choices = dict(cached_locations)

    for cache_key in cache_index:
        metadata = cache.get(cache_key)
        if metadata:
            # Cache still exists, calculate time remaining
            cached_at = datetime.fromisoformat(metadata.get("cached_at"))
            cache_timeout = metadata.get("cache_timeout", 300)
            now = datetime.now(timezone.utc)
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


def get_validated_device_cache_key(server_key: str, filters: dict, device_id: int | str, vc_enabled: bool) -> str:
    """
    Generate a consistent cache key for validated device data.

    This ensures both synchronous and background job processing use the same
    cache keys, avoiding duplicate validation work and cache entries.

    Args:
        server_key: LibreNMS server key
        filters: Filter dict with location, type, os, hostname, sysname, hardware keys
        device_id: LibreNMS device ID
        vc_enabled: Whether virtual chassis detection was enabled

    Returns:
        str: Cache key for the validated device

    Example:
        >>> key = get_validated_device_cache_key('default', {'location': 'NYC'}, 123, True)
        >>> key
        'validated_device_default_-1234567890_123_vc'
    """
    # Sort filters for consistent hashing
    filter_hash = hash(str(sorted(filters.items())))
    vc_part = "vc" if vc_enabled else "novc"
    return f"validated_device_{server_key}_{filter_hash}_{device_id}_{vc_part}"


def get_import_device_cache_key(device_id: int | str, server_key: str = "default") -> str:
    """
    Generate cache key for raw LibreNMS device data.

    This key is used to cache raw device data (without validation metadata)
    to avoid redundant API calls when users interact with dropdowns during
    the import workflow.

    Args:
        device_id: LibreNMS device ID
        server_key: LibreNMS server identifier for multi-server setups

    Returns:
        str: Cache key for the device data

    Example:
        >>> get_import_device_cache_key(123, "production")
        'import_device_data_production_123'
    """
    return f"import_device_data_{server_key}_{device_id}"

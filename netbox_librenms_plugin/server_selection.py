"""Resolve the active LibreNMS server for an object sync page."""

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings as django_settings

from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.server_mappings import (
    PREFERRED_SERVER_FIELD,
    iter_server_mapping_entries,
)
from netbox_librenms_plugin.utils import (
    get_librenms_sync_device,
    is_legacy_librenms_id,
    resolve_server_mapping_display_id,
)


class ServerSelectionState(StrEnum):
    """The result of resolving an object sync page's active server."""

    RESOLVED = "resolved"
    INVALID = "invalid"
    SELECTION_REQUIRED = "selection_required"


@dataclass(frozen=True)
class ServerMapping:
    """One usable or stale per-server object mapping."""

    server_key: str
    display_name: str
    librenms_url: str | None
    device_id: int
    device_url: str | None
    is_configured: bool
    is_selectable: bool
    is_active: bool
    is_preferred: bool
    is_oob_only: bool


@dataclass(frozen=True)
class ServerSelectorOption:
    """One configured server offered by a transient selector."""

    server_key: str
    display_name: str
    is_selectable: bool
    is_active: bool


@dataclass(frozen=True)
class ParsedConfiguredServerKey:
    """One exact configured server key parsed from request or job data."""

    is_present: bool
    server_key: str | None


@dataclass(frozen=True)
class ObjectServerSelection:
    """All server-selection facts needed to render one object sync page."""

    state: ServerSelectionState
    active_key: str | None
    requested_key: str | None
    installation_default_key: str | None
    mapping_owner: object
    mappings: tuple[ServerMapping, ...]
    error: str | None = None
    warning: str | None = None
    preferred_key: str | None = None

    @property
    def has_multiple_usable_mappings(self) -> bool:
        """Return whether an explicit preference can change selection behavior."""
        return sum(mapping.is_selectable for mapping in self.mappings) > 1

    @property
    def active_display_name(self) -> str:
        """Return a stable label for the selector button."""
        for mapping in self.mappings:
            if mapping.is_active and mapping.is_selectable:
                return mapping.display_name
        if self.active_key:
            return LibreNMSAPI.get_available_servers().get(self.active_key, self.active_key)
        return "Select server"


def installation_default_server_key() -> str | None:
    """Return the server selected by the installation settings, if it is usable."""
    from netbox_librenms_plugin.librenms_api import build_librenms_api

    api = build_librenms_api(None)
    return api.server_key if api is not None else None


def build_configured_server_options(active_key=None) -> tuple[ServerSelectorOption, ...]:
    """Build selector options for all usable configured servers."""
    return tuple(
        ServerSelectorOption(
            server_key=server_key,
            display_name=display_name,
            is_selectable=True,
            is_active=server_key == active_key,
        )
        for server_key, display_name in LibreNMSAPI.get_available_servers().items()
    )


def parse_configured_server_key(data) -> ParsedConfiguredServerKey:
    """Parse one configured server key without defaulting malformed input."""
    getlist = getattr(data, "getlist", None)
    values = getlist("server_key") if callable(getlist) else None
    if isinstance(values, (list, tuple)):
        is_present = bool(values)
    else:
        raw_value = data.get("server_key")
        try:
            is_present = "server_key" in data
        except TypeError:
            is_present = raw_value is not None
        if isinstance(raw_value, (list, tuple)):
            values = raw_value
        else:
            values = [raw_value] if is_present else []

    if len(values) != 1 or not isinstance(values[0], str) or not values[0].strip():
        return ParsedConfiguredServerKey(is_present=is_present, server_key=None)

    server_key = values[0].strip()
    if server_key not in LibreNMSAPI.get_available_servers():
        return ParsedConfiguredServerKey(is_present=True, server_key=None)
    return ParsedConfiguredServerKey(is_present=True, server_key=server_key)


def _plugin_config() -> dict:
    return getattr(django_settings, "PLUGINS_CONFIG", {}).get("netbox_librenms_plugin", {})


def _servers_config(plugin_config) -> dict:
    servers = plugin_config.get("servers") or {}
    return servers if isinstance(servers, dict) else {}


def build_server_mappings(owner, active_key=None, *, plugin_config=None) -> tuple[ServerMapping, ...]:
    """Build the owner's per-server mapping rows, excluding reserved metadata."""
    plugin_config = _plugin_config() if plugin_config is None else plugin_config
    raw_mappings = owner.custom_field_data.get("librenms_id")
    if not isinstance(raw_mappings, dict) or not raw_mappings:
        return ()

    servers_config = _servers_config(plugin_config)
    available_servers = LibreNMSAPI.get_available_servers()
    mappings = []
    stored_preference = raw_mappings.get(PREFERRED_SERVER_FIELD)

    for server_key, entry in iter_server_mapping_entries(raw_mappings):
        device_id, is_oob_only = resolve_server_mapping_display_id(entry)
        if device_id is None:
            continue

        server_config = servers_config.get(server_key)
        if server_config is None and server_key == "default" and plugin_config.get("librenms_url"):
            server_config = {
                "librenms_url": plugin_config["librenms_url"],
                "display_name": plugin_config.get("display_name")
                or f"Default Server ({plugin_config['librenms_url']})",
            }
        is_configured = isinstance(server_config, dict)
        librenms_url = server_config.get("librenms_url") if is_configured else None
        display_name = server_config.get("display_name") or server_key if is_configured else server_key
        mappings.append(
            ServerMapping(
                server_key=server_key,
                display_name=display_name,
                librenms_url=librenms_url,
                device_id=device_id,
                device_url=f"{librenms_url}/device/device={device_id}/" if librenms_url else None,
                is_configured=is_configured,
                is_selectable=server_key in available_servers,
                is_active=server_key == active_key,
                is_preferred=server_key == stored_preference,
                is_oob_only=is_oob_only,
            )
        )

    mappings.sort(key=lambda mapping: 0 if mapping.is_active else (1 if mapping.is_selectable else 2))
    return tuple(mappings)


def resolve_object_server(page_object, requested_key=None, installation_default_key=None) -> ObjectServerSelection:
    """Resolve the active server for an object page without contacting LibreNMS."""
    requested_key = requested_key.strip() if isinstance(requested_key, str) else None
    requested_key = requested_key or None
    mapping_owner = get_librenms_sync_device(page_object, server_key=None) or page_object
    installation_default_key = installation_default_key or installation_default_server_key()
    available_servers = LibreNMSAPI.get_available_servers()
    if requested_key == "default" and requested_key not in available_servers and installation_default_key:
        requested_key = installation_default_key
    mappings = build_server_mappings(mapping_owner)
    mappings_by_key = {mapping.server_key: mapping for mapping in mappings}
    selectable = tuple(mapping for mapping in mappings if mapping.is_selectable)
    raw_mappings = mapping_owner.custom_field_data.get("librenms_id")
    legacy_mapping = is_legacy_librenms_id(raw_mappings)
    selectable_keys = {mapping.server_key for mapping in selectable}
    preference_is_stored = isinstance(raw_mappings, dict) and PREFERRED_SERVER_FIELD in raw_mappings
    stored_preference = raw_mappings.get(PREFERRED_SERVER_FIELD) if preference_is_stored else None
    preferred_key = (
        stored_preference if isinstance(stored_preference, str) and stored_preference in selectable_keys else None
    )

    warning = None
    if len(selectable) > 1 and not preference_is_stored:
        warning = "No preferred LibreNMS server is stored."
    elif preference_is_stored and preferred_key is None:
        if not isinstance(stored_preference, str) or not stored_preference.strip():
            warning = "The stored preferred LibreNMS server is malformed."
        elif stored_preference not in mappings_by_key:
            warning = f"The stored preferred LibreNMS server '{stored_preference}' has no object mapping."
        else:
            warning = f"The stored preferred LibreNMS server '{stored_preference}' is not configured or usable."

    if warning:
        if requested_key is not None:
            warning += " The transient server selection remains active."
        elif installation_default_key in selectable_keys:
            warning += f" Using installation default server '{installation_default_key}'."
        else:
            warning += " The installation default is not mapped, so select a server."

    if requested_key is not None:
        mapping = mappings_by_key.get(requested_key)
        migrated_entry = raw_mappings.get(requested_key) if isinstance(raw_mappings, dict) else None
        migrated_scope = isinstance(migrated_entry, dict) and isinstance(migrated_entry.get("_migrated_to"), dict)
        available_requested_key = requested_key in available_servers
        legacy_default_scope = legacy_mapping and available_requested_key and requested_key == installation_default_key
        if (mapping is None or not mapping.is_selectable) and not (
            (migrated_scope and available_requested_key) or legacy_default_scope
        ):
            return ObjectServerSelection(
                state=ServerSelectionState.INVALID,
                active_key=requested_key,
                requested_key=requested_key,
                installation_default_key=installation_default_key,
                mapping_owner=mapping_owner,
                mappings=build_server_mappings(mapping_owner, requested_key),
                error=f"LibreNMS server '{requested_key}' is not an available mapping for this object.",
                warning=warning,
                preferred_key=preferred_key,
            )
        active_key = requested_key
    elif not selectable and not mappings and not preference_is_stored:
        active_key = installation_default_key
    elif len(selectable) == 1:
        active_key = selectable[0].server_key
    else:
        if preferred_key is not None:
            active_key = preferred_key
        elif installation_default_key in selectable_keys:
            active_key = installation_default_key
        else:
            return ObjectServerSelection(
                state=ServerSelectionState.SELECTION_REQUIRED,
                active_key=None,
                requested_key=None,
                installation_default_key=installation_default_key,
                mapping_owner=mapping_owner,
                mappings=mappings,
                error="Select a LibreNMS server to continue.",
                warning=warning,
                preferred_key=preferred_key,
            )

    return ObjectServerSelection(
        state=ServerSelectionState.RESOLVED,
        active_key=active_key,
        requested_key=requested_key,
        installation_default_key=installation_default_key,
        mapping_owner=mapping_owner,
        mappings=build_server_mappings(mapping_owner, active_key),
        warning=warning,
        preferred_key=preferred_key,
    )

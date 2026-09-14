"""Pure name resolution shared by import rendering and persistence."""

from ..ip_addressing import parse_host_address


def _name_candidates(libre_device: dict) -> tuple[str | None, str | None]:
    """Return the sysName and hostname values that can name an object."""
    sysname = libre_device.get("sysName")
    hostname = libre_device.get("hostname")
    sysname = sysname.strip() if isinstance(sysname, str) else None
    hostname = hostname.strip() if isinstance(hostname, str) else None
    return sysname or None, hostname or None


def _resolve_device_name(
    libre_device: dict,
    use_sysname: bool = True,
    strip_domain: bool = False,
    device_id: int | str = None,
) -> tuple[str, str]:
    """
    Resolve an import name and report its LibreNMS source.

    Args:
        libre_device: Device data from LibreNMS.
        use_sysname: Prefer sysName when true, otherwise prefer hostname.
        strip_domain: Remove a domain suffix from names that are not IP addresses.
        device_id: LibreNMS device ID used to build the fallback name.

    Returns:
        The resolved name and its source.

    """
    sysname, hostname = _name_candidates(libre_device)
    if use_sysname:
        name, source = (sysname, "sysname") if sysname else (hostname, "hostname")
    else:
        name, source = (hostname, "hostname") if hostname else (sysname, "sysname")

    if strip_domain and name and "." in name:
        try:
            parse_host_address(name)
        except ValueError:
            name = name.split(".")[0]

    if not name:
        fallback_id = device_id if device_id is not None else libre_device.get("device_id", "unknown")
        name = source = f"device-{fallback_id}"

    return name, source


def import_name_variants(record):
    """Return every preference-controlled name through the importer's resolver."""
    variants = {}
    for source_key, use_sysname in (("sysname", True), ("hostname", False)):
        for suffix, strip_domain in (("full", False), ("stripped", True)):
            name, source = _resolve_device_name(
                record,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
                device_id=record.get("device_id"),
            )
            variants[f"{source_key}_{suffix}"] = {
                "name": name,
                "source": {"sysname": "sysName", "hostname": "hostname"}.get(source, "fallback name"),
            }
    return variants

"""Canonical IP address parsing for LibreNMS and NetBox boundaries."""

from ipaddress import IPv4Interface, IPv6Interface, ip_address, ip_interface


def parse_host_address(address):
    """Return a canonical IPv4 or IPv6 host from a bare or prefixed string."""
    if not isinstance(address, str):
        raise ValueError("IP address must be a string")
    address = address.strip()
    if not address:
        raise ValueError("IP address is blank")
    return ip_interface(address).ip


def parse_address_with_prefix(address, prefix_length=None) -> IPv4Interface | IPv6Interface:
    """Return one canonical IPv4 or IPv6 interface and verify separate prefix evidence."""
    if not isinstance(address, str):
        raise ValueError("IP address must be a string")

    address = address.strip()
    if not address:
        raise ValueError("IP address is blank")

    if prefix_length is None:
        expected_prefix = None
    elif isinstance(prefix_length, bool) or isinstance(prefix_length, float):
        raise ValueError("Prefix length must be an integer")
    else:
        try:
            expected_prefix = int(prefix_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("Prefix length must be an integer") from exc

    if "/" in address:
        parsed = ip_interface(address)
        if expected_prefix is not None and parsed.network.prefixlen != expected_prefix:
            raise ValueError("Address prefix conflicts with the separate prefix length")
        return parsed

    if expected_prefix is None:
        raise ValueError("Prefix length is missing")
    return ip_interface(f"{ip_address(address)}/{expected_prefix}")


def parse_librenms_ip_entry(entry) -> IPv4Interface | IPv6Interface:
    """Parse one supported LibreNMS IP row into a canonical IP interface."""
    if not isinstance(entry, dict):
        raise ValueError("LibreNMS IP row must be an object")
    if {"ip_address", "prefix_length"} <= entry.keys():
        return parse_address_with_prefix(entry["ip_address"], entry["prefix_length"])
    if {"ipv6_compressed", "ipv6_prefixlen"} <= entry.keys():
        return parse_address_with_prefix(entry["ipv6_compressed"], entry["ipv6_prefixlen"])
    if {"ipv4_address", "ipv4_prefixlen"} <= entry.keys():
        return parse_address_with_prefix(entry["ipv4_address"], entry["ipv4_prefixlen"])
    raise ValueError("LibreNMS IP row has no supported address fields")

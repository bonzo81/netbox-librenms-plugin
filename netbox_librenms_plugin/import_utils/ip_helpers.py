"""
Helpers for ensuring LibreNMS-known IP addresses exist in NetBox IPAM.

When the plugin links a LibreNMS device to a NetBox device (during initial
import, OOB attach, or promote-to-host), the IP that LibreNMS reaches the
device on may not yet exist in NetBox. To let users later assign that IP as
``primary_ip4`` / ``primary_ip6`` / ``oob_ip``, we first need an
``IPAddress`` record. This module centralises that logic so all import
paths behave the same way.

The helper never overwrites an existing IPAM record: if an ``IPAddress``
already exists for the host (matched via ``net_host`` so any prefix length
is acceptable), it is returned as-is. Only when no record exists is a new
``/32`` (IPv4) or ``/128`` (IPv6) entry created in the global scope.
"""

from __future__ import annotations

import logging
from ipaddress import ip_address as _ipaddr_parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from ipam.models import IPAddress

logger = logging.getLogger(__name__)


def get_or_create_global_ip(ip_str: str | None) -> "tuple[IPAddress | None, bool]":
    """Return ``(ipam_record, created)`` for ``ip_str``.

    Creates a ``/32`` (IPv4) or ``/128`` (IPv6) global-scope record if no
    matching ``IPAddress`` exists (matched via ``net_host`` so any prefix
    length is acceptable). The returned ``IPAddress`` is unassigned (no
    ``assigned_object``) and has no VRF. Callers may attach it to an
    interface or assign it as ``oob_ip`` afterwards.

    The ``created`` flag is ``True`` only when a new IPAM record was
    inserted by this call, so callers can surface a user-visible toast
    only on creation (and stay silent when reusing an existing record).

    Returns ``(None, False)`` if ``ip_str`` is empty, malformed, or if
    creation fails (the failure is logged but never raised, so callers
    can treat this as best-effort).
    """
    if not ip_str:
        return None, False
    ip_str = ip_str.strip()
    if not ip_str:
        return None, False

    try:
        parsed = _ipaddr_parse(ip_str)
    except ValueError:
        logger.debug("get_or_create_global_ip: invalid IP %r", ip_str)
        return None, False

    from ipam.models import IPAddress

    existing = IPAddress.objects.filter(address__net_host=ip_str).first()
    if existing is not None:
        return existing, False

    mask = "/128" if parsed.version == 6 else "/32"
    try:
        return IPAddress.objects.create(address=f"{ip_str}{mask}", status="active"), True
    except Exception:  # pragma: no cover - defensive (validation/integrity)
        logger.warning(
            "get_or_create_global_ip: failed to auto-create %s",
            ip_str,
            exc_info=True,
        )
        return None, False

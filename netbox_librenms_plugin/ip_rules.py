"""
The one reader of IP mappings (``IPAddressMapping``).

An IP mapping selects LibreNMS addresses by prefix and, optionally, by VRF. An Ignore mapping keeps
them out of IP sync. Load the mappings once per request and decide every address against that snapshot.
"""

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_network

from django.http import HttpRequest

from netbox_librenms_plugin.interface_rules import RuleDecisionKind
from netbox_librenms_plugin.models import IPAddressMapping

_REQUEST_CACHE_ATTRIBUTE = "_librenms_ip_rules"


@dataclass(frozen=True)
class IPRule:
    """An immutable snapshot of one stored Ignore mapping."""

    pk: int
    label: str
    network: IPv4Network | IPv6Network
    vrf_id: int | None

    def matches(self, address: IPv4Address | IPv6Address, vrf_id: int | None) -> bool:
        """Return whether *address*, synced into *vrf_id* (None is the global table), is in this rule."""
        if self.vrf_id is not None and self.vrf_id != vrf_id:
            return False
        return address.version == self.network.version and address in self.network

    @property
    def reason(self) -> str:
        """Say why this rule refuses an address, for a row pill and a sync message."""
        return f"IP mapping {self.label} ignores this address"


class IPRuleMatcher:
    """Decide addresses against one snapshot of the Ignore mappings, with no database access."""

    def __init__(self, rules):
        """Keep the snapshot in primary-key order."""
        self._rules = tuple(rules)

    @classmethod
    def load(cls) -> "IPRuleMatcher":
        """Read every Ignore mapping in one query."""
        return cls(
            IPRule(pk=mapping.pk, label=str(mapping), network=ip_network(str(mapping.prefix)), vrf_id=mapping.vrf_id)
            for mapping in IPAddressMapping.objects.filter(action=IPAddressMapping.ACTION_IGNORE)
            .select_related("vrf")
            .order_by("pk")
        )

    def decide(self, address: IPv4Address | IPv6Address, vrf_id: int | None) -> IPRule | None:
        """
        Return the first Ignore rule that matches *address* in the target VRF, or None.

        Args:
            address (IPv4Address | IPv6Address): The host address, without its prefix length.
            vrf_id (int | None): The pk of the NetBox VRF the address is synced into; None is the
                global table, which only rules without a VRF match.

        Returns:
            IPRule | None: The matching rule, or None when the address may sync.

        Raises:
            TypeError: The address is not a host address, or the VRF id is not an int or None.

        """
        if not isinstance(address, (IPv4Address, IPv6Address)):
            raise TypeError(f"An IP rule decides a host address, not {type(address).__name__}.")
        if vrf_id is not None and (isinstance(vrf_id, bool) or not isinstance(vrf_id, int)):
            raise TypeError(f"A VRF id must be an int or None, not {type(vrf_id).__name__}.")
        return next((rule for rule in self._rules if rule.matches(address, vrf_id)), None)


def row_ip_rule_block(rule: IPRule) -> dict:
    """Return the plain row data the IP table renders for an address *rule* ignores."""
    return {"kind": RuleDecisionKind.IGNORE.value, "reason": rule.reason}


def ip_rules_for_request(request: HttpRequest) -> IPRuleMatcher:
    """Return the request's one IP rule snapshot, loading it on first use."""
    request_state = request.__dict__
    if _REQUEST_CACHE_ATTRIBUTE not in request_state:
        setattr(request, _REQUEST_CACHE_ATTRIBUTE, IPRuleMatcher.load())
    return request_state[_REQUEST_CACHE_ATTRIBUTE]

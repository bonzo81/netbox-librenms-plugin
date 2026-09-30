import netaddr
from ipam.models import VRF, IPAddress
from netaddr import IPNetwork


def constructors(parsed, interface):
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress(address=str(parsed), assigned_object=interface)
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress(address="10.0.0.1/24")
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.create(address=f"{parsed}/32", status="active")
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.get_or_create(address=parsed, vrf=None)
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.update_or_create(address=parsed, defaults={"status": "active"})
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.get_or_create(vrf=None, defaults={"address": str(parsed)})
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.update_or_create(pk=1, create_defaults={"address": str(parsed)})
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.restrict(None, "add").create(address=str(parsed))
    # ok: ipaddress-address-needs-netaddr
    IPAddress(address=netaddr.IPNetwork(str(parsed)), assigned_object=interface)
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.create(address=netaddr.IPNetwork(f"{parsed}/32"), status="active")
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.create(address=IPNetwork(f"{parsed}/32"), status="active")
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.get_or_create(vrf=None, defaults={"address": netaddr.IPNetwork(str(parsed))})
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.update_or_create(address=IPNetwork(str(parsed)), defaults={"status": "active"})
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.create(assigned_object=interface, status="active")


def lookups(parsed):
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(address=str(parsed))
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(address__net_host=str(parsed)).first()
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.get(address=str(parsed))
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.exclude(address=str(parsed))
    # ok: ipaddress-address-needs-netaddr
    VRF.objects.create(name="red")


def assignments(ip_obj, parsed):
    # ruleid: ipaddress-address-needs-netaddr
    ip_obj.address = str(parsed)
    # ok: ipaddress-address-needs-netaddr
    ip_obj.address = netaddr.IPNetwork(str(parsed))
    # ok: ipaddress-address-needs-netaddr
    ip_obj.address = IPNetwork(str(parsed))


def other_types(parsed):
    # ok: ipaddress-address-needs-netaddr
    netaddr.IPAddress(str(parsed))
    # ok: ipaddress-address-needs-netaddr
    netaddr.IPAddress("10.0.0.1")

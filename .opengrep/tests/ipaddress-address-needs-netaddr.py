import netaddr
import ipam.models as ipam_models
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


def expanded_kwargs(values, parsed):
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress(**values)
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.create(**values)
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.get_or_create(vrf=None, **values)
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(vrf=None).update_or_create(**values)
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(**values)
    # A ** expansion hides the address, so it is reported even next to a netaddr address.
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.create(address=netaddr.IPNetwork(str(parsed)), **{"status": "active"})


def long_chains(parsed):
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(vrf=None).select_for_update().create(address=str(parsed))
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.using("default").filter(vrf=None).exclude(pk=1).get_or_create(address=str(parsed))
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(vrf=None).update_or_create(pk=1, defaults={"address": str(parsed)})
    # ruleid: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(vrf=None).select_for_update().get_or_create(create_defaults={"address": str(parsed)})
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(vrf=None).select_for_update().create(address=netaddr.IPNetwork(str(parsed)))
    # ok: ipaddress-address-needs-netaddr
    IPAddress.objects.filter(vrf=None).update_or_create(pk=1, defaults={"address": IPNetwork(str(parsed))})
    # ok: ipaddress-address-needs-netaddr
    VRF.objects.filter(pk__in=IPAddress.objects.values("vrf")).create(name=str(parsed))


def set_by_name(ip_obj, parsed):
    # ruleid: ipaddress-address-needs-netaddr
    setattr(ip_obj, "address", str(parsed))
    # ok: ipaddress-address-needs-netaddr
    setattr(ip_obj, "address", netaddr.IPNetwork(str(parsed)))
    # ok: ipaddress-address-needs-netaddr
    setattr(ip_obj, "status", "active")


def module_import(parsed):
    # ruleid: ipaddress-address-needs-netaddr
    ipam_models.IPAddress.objects.filter(vrf=None).create(address=str(parsed))
    # ruleid: ipaddress-address-needs-netaddr
    ipam_models.IPAddress(address=str(parsed))
    # ok: ipaddress-address-needs-netaddr
    ipam_models.IPAddress.objects.create(address=netaddr.IPNetwork(str(parsed)))

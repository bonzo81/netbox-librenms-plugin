import copy
from copy import deepcopy

from django.db.models import F, Q
from django.db.models.fields.json import KT

MAPPING_KEY = "librenms_id"


def direct_access(device, interface, change):
    # ruleid: no-stored-mapping-access
    device.custom_field_data["librenms_id"] = {"default": 5}
    # ruleid: no-stored-mapping-access
    entry = device.custom_field_data["librenms_id"]["default"]
    # ruleid: no-stored-mapping-access
    device.custom_field_data["librenms_id"]["default"] = {"id": 5}
    # ruleid: no-stored-mapping-access
    device.custom_field_data["librenms_id"] |= {"secondary": 6}
    # ruleid: no-stored-mapping-access
    value = interface.custom_field_data.get("librenms_id")
    # ruleid: no-stored-mapping-access
    value = interface.custom_field_data.get("librenms_id", {})
    # ruleid: no-stored-mapping-access
    device.custom_field_data.pop("librenms_id", None)
    # ruleid: no-stored-mapping-access
    device.custom_field_data.setdefault("librenms_id", {})
    # ruleid: no-stored-mapping-access
    assert "librenms_id" in device.custom_field_data
    # ruleid: no-stored-mapping-access
    assert "librenms_id" not in device.custom_field_data
    # ruleid: no-stored-mapping-access
    value = device.cf["librenms_id"]
    # ruleid: no-stored-mapping-access
    value = device.cf.get("librenms_id")
    # ruleid: no-stored-mapping-access
    assert "librenms_id" not in device.cf
    # ruleid: no-stored-mapping-access
    value = change.postchange_data["custom_fields"]["librenms_id"]
    # ruleid: no-stored-mapping-access
    value = change.prechange_data["custom_fields"].get("librenms_id")
    return entry, value


def aliases(device, change):
    stored = device.custom_field_data
    # ruleid: no-stored-mapping-access
    stored["librenms_id"] = 7
    # ruleid: no-stored-mapping-access
    value = stored.get("librenms_id")
    # ruleid: no-stored-mapping-access
    stored.update({"librenms_id": 5})
    # ruleid: no-stored-mapping-access
    stored.update(librenms_id=5)
    # ruleid: no-stored-mapping-access
    stored.update(**change.values)
    # ruleid: no-stored-mapping-access
    stored.update(change.values)
    # ruleid: no-stored-mapping-access
    stored.update({"operator_note": "kept", **change.values})
    # ok: no-stored-mapping-access
    stored.update({"operator_note": "kept"})
    # ruleid: no-stored-mapping-access
    stored.clear()
    # ruleid: no-stored-mapping-access
    stored.popitem()
    # ruleid: no-stored-mapping-access
    stored |= change.values
    fields = device.cf
    # ruleid: no-stored-mapping-access
    assert "librenms_id" in fields
    logged = change.postchange_data["custom_fields"]
    # ruleid: no-stored-mapping-access
    value = logged["librenms_id"]
    key = "librenms_id"
    # ruleid: no-stored-mapping-access
    value = device.custom_field_data[key]
    # ruleid: no-stored-mapping-access
    value = device.custom_field_data.get(MAPPING_KEY)
    return value


def _entry(container):
    # ruleid: no-stored-mapping-access
    return container["librenms_id"]


def passes_the_container(device):
    return _entry(device.custom_field_data)


def container_escape(device, other, Device, queryset):
    # ruleid: no-stored-mapping-access-container
    del device.custom_field_data["librenms_id"]
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data = {"librenms_id": {"default": 5}}
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data = {}
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data = other.custom_field_data
    # ruleid: no-stored-mapping-access-container
    device.cf = device.custom_field_data
    # ruleid: no-stored-mapping-access-container
    before = deepcopy(device.custom_field_data)
    # ruleid: no-stored-mapping-access-container
    before = copy.deepcopy(device.custom_field_data)
    # ruleid: no-stored-mapping-access-container
    before = device.custom_field_data.copy()
    # ruleid: no-stored-mapping-access-container
    before = dict(device.cf)
    # ruleid: no-stored-mapping-access-container
    stored = {**device.custom_field_data, "unrelated_field": "edited"}
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.update({"librenms_id": 5})
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.update(other.custom_field_data)
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.update(librenms_id=5)
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.update(**other.values)
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.update({"operator_note": "kept", **other.custom_field_data})
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.update({"operator_note": "kept", **other.values})
    # ok: no-stored-mapping-access-container
    device.custom_field_data.update({"operator_note": "kept"})
    # ok: no-stored-mapping-access-container
    device.cf.update(operator_note="kept")
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data |= {"librenms_id": {"default": 5}}
    # ruleid: no-stored-mapping-access-container
    device.cf |= other.cf
    # ruleid: no-stored-mapping-access-container
    device.custom_field_data.clear()
    # ruleid: no-stored-mapping-access-container
    device = Device(name="x", custom_field_data={"librenms_id": {"default": 5}})
    # ruleid: no-stored-mapping-access-container
    Device.objects.create(name="x", custom_field_data={"librenms_id": 5})
    # ruleid: no-stored-mapping-access-container
    queryset.filter(pk=1).update(custom_field_data={"librenms_id": {"default": 5}})
    # ruleid: no-stored-mapping-access-container
    Device.objects.update_or_create(name="x", defaults={"custom_field_data": {"librenms_id": 5}})
    # ruleid: no-stored-mapping-access-container
    stub = make_stub(cf={"librenms_id": {"default": 5}})
    # ruleid: no-stored-mapping-access-container
    assert device.custom_field_data == {"librenms_id": {"default": 5}}
    return before, stored, stub


def orm_lookups(Device, Interface, server_key):
    # ruleid: no-stored-mapping-access-orm
    Device.objects.filter(custom_field_data__librenms_id__default=5)
    # ruleid: no-stored-mapping-access-orm
    Device.objects.exclude(custom_field_data__librenms_id__isnull=True)
    # ruleid: no-stored-mapping-access-orm
    Device.objects.get(custom_field_data__librenms_id=5)
    # ruleid: no-stored-mapping-access-orm
    Q(custom_field_data__librenms_id__has_key="default")
    # ruleid: no-stored-mapping-access-orm
    Device.objects.values_list("custom_field_data__librenms_id", flat=True)
    # ruleid: no-stored-mapping-access-orm
    Device.objects.values("custom_field_data__librenms_id__default")
    # ruleid: no-stored-mapping-access-orm
    Device.objects.order_by("-custom_field_data__librenms_id")
    # ruleid: no-stored-mapping-access-orm
    Device.objects.annotate(mapped=F("custom_field_data__librenms_id"))
    # ruleid: no-stored-mapping-access-orm
    Device.objects.annotate(mapped=KT("custom_field_data__librenms_id__default"))
    # ruleid: no-stored-mapping-access-orm
    Device.objects.filter(**{f"custom_field_data__librenms_id__{server_key}": 5})
    # ruleid: no-stored-mapping-access-orm
    Device.objects.values_list("pk", "custom_field_data")
    # ruleid: no-stored-mapping-access-orm
    Interface.objects.filter(device__custom_field_data__librenms_id__default=5)
    # ruleid: no-stored-mapping-access-orm
    Interface.objects.exclude(device__virtual_chassis__master__custom_field_data__librenms_id=None)
    # ruleid: no-stored-mapping-access-orm
    Interface.objects.order_by("-device__custom_field_data__librenms_id")
    # ruleid: no-stored-mapping-access-orm
    Interface.objects.values_list("device__custom_field_data__librenms_id__default", flat=True)
    # ruleid: no-stored-mapping-access-orm
    Interface.objects.filter(**{f"device__custom_field_data__librenms_id__{server_key}": 5})
    # ruleid: no-stored-mapping-access-orm
    Interface.objects.values("name", "device__custom_field_data")
    # ok: no-stored-mapping-access-orm
    Interface.objects.filter(device__custom_field_data__operator_note="kept")
    # ok: no-stored-mapping-access-orm
    Interface.objects.values("device__name")
    # ruleid: no-stored-mapping-access-orm
    assert_update_logged(Device, "custom_fields.librenms_id", 5, {"default": 5})
    # ok: no-stored-mapping-access-orm
    assert_update_logged(Device, "custom_fields.operator_note", "", "kept")


# ruleid: no-stored-mapping-access-private
from netbox_librenms_plugin.server_mappings import _put_on
# ruleid: no-stored-mapping-access-private
from netbox_librenms_plugin.server_mappings import _stored_value as raw_value
# ruleid: no-stored-mapping-access-private
from ..server_mappings import _MAPPING_KEY
# ruleid: no-stored-mapping-access-private
from netbox_librenms_plugin.server_mappings import (
    read_mapping,
    _put_stored_value,
)
# ok: no-stored-mapping-access-private
from netbox_librenms_plugin.server_mappings import assign_own, persist_mapping
import netbox_librenms_plugin.server_mappings as mappings
from netbox_librenms_plugin import server_mappings


def private_seam(device, change, mock):
    # ruleid: no-stored-mapping-access-private
    mappings._put_on(device, change)
    # ruleid: no-stored-mapping-access-private
    server_mappings._stored_value(device)
    # ruleid: no-stored-mapping-access-private
    mock.patch("netbox_librenms_plugin.server_mappings._claim_device_identity")
    # ok: no-stored-mapping-access-private
    server_mappings.read_mapping(device)
    # ok: no-stored-mapping-access-private
    mock.patch("netbox_librenms_plugin.server_mappings.read_mapping")
    # ok: no-stored-mapping-access-private
    return change._stored


def unrelated(device, payload, response, cache, validation, CustomField, Device):
    """Read device.custom_field_data["librenms_id"] only through read_mapping()."""
    # A comment may name device.custom_field_data["librenms_id"].
    # ok: no-stored-mapping-access
    device_id = payload["librenms_id"]
    # ok: no-stored-mapping-access
    device_id = payload.get("librenms_id")
    # ok: no-stored-mapping-access
    device_id = response.json()["devices"][0]["librenms_id"]
    # ok: no-stored-mapping-access-container
    cache.set("key", {"inventory": [], "librenms_id": 1, "oob_librenms_id": None})
    # ok: no-stored-mapping-access-container
    body = {"librenms_id": 5, "hostname": "edge"}
    # ok: no-stored-mapping-access
    assert validation["existing_match_type"] == "librenms_id"
    # ok: no-stored-mapping-access-orm
    CustomField.objects.filter(name="librenms_id").delete()
    # ok: no-stored-mapping-access
    note = device.custom_field_data["operator_note"]
    # ok: no-stored-mapping-access
    note = device.cf.get("operator_note")
    # ok: no-stored-mapping-access-container
    device.custom_field_data["operator_note"] = "kept"
    # ok: no-stored-mapping-access-orm
    Device.objects.filter(custom_field_data__operator_note="kept")
    # ok: no-stored-mapping-access-container
    device.save(update_fields=["custom_field_data"])
    # ok: no-stored-mapping-access
    has_librenms_id = read_mapping(device).has_recorded_state
    return device_id, body, note, has_librenms_id

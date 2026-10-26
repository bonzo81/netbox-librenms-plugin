"""End-to-end checks for VLAN name normalization rules on the VLAN tab and the VLAN writer."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser

LIBRENMS_ID = 4242
JUNOS_SUFFIX_RULE = {"match_pattern": r"^(.+)\+{vid}$", "replacement": r"\1"}


@pytest.fixture
def vlan_librenms(live_librenms, configure_librenms):
    """Serve VLAN rows from a loopback LibreNMS, and keep the refreshed snapshot for the writer."""
    configure_librenms(
        {"default": {"librenms_url": live_librenms.server.url, "api_token": "test-token", "verify_ssl": False}}
    )

    def serve(rows):
        body = {"status": "ok", "vlans": [{"device_id": LIBRENMS_ID, **row} for row in rows]}
        live_librenms.server.register("/api/v0/resources/vlans", body, method="GET")

    return serve


def _device(name, manufacturer_name):
    """Return a device mapped to LibreNMS whose device type belongs to *manufacturer_name*."""
    from dcim.models import DeviceType, Manufacturer
    from django.utils.text import slugify

    manufacturer, _ = Manufacturer.objects.get_or_create(name=manufacturer_name, slug=slugify(manufacturer_name))
    device = make_device(name, librenms_cf={"default": {"id": LIBRENMS_ID}})
    device.device_type = DeviceType.objects.create(
        manufacturer=manufacturer, model=f"{name}-model", slug=slugify(f"{name}-model")
    )
    device.save()
    return device, manufacturer


def _rule(manufacturer=None, **fields):
    from netbox_librenms_plugin.models import NormalizationRule

    return NormalizationRule.objects.create(
        scope=NormalizationRule.SCOPE_VLAN_NAME, manufacturer=manufacturer, **{**JUNOS_SUFFIX_RULE, **fields}
    )


def _refresh(client, device):
    """Refresh the VLAN tab through its real endpoint and return the rendered response."""
    url = reverse("plugins:netbox_librenms_plugin:device_vlan_sync", kwargs={"pk": device.pk})
    response = client.post(url, {"server_key": "default"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    return response


def _rows(response):
    return {row["vlan_id"]: row for row in response.context["vlan_sync"]["vlan_table"].data}


def _sync(client, device, *vids):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    data = {"server_key": "default", "action": "create_vlans", "select": [str(vid) for vid in vids]}
    data.update({f"vlan_group_{vid}": "" for vid in vids})
    return client.post(url, data)


@pytest.mark.django_db
def test_a_juniper_rule_strips_only_the_suffix_that_repeats_the_vid(client, vlan_librenms):
    """The tab and the writer use the adjusted name; a suffix with another number stays."""
    from ipam.models import VLAN

    device, juniper = _device("vlan-rule-junos", "Juniper")
    _rule(juniper)
    VLAN.objects.create(vid=100, name="users", status="active")
    vlan_librenms(
        [
            {"vlan_vlan": 100, "vlan_name": "users+100"},
            {"vlan_vlan": 200, "vlan_name": "guests+300"},
            {"vlan_vlan": 300, "vlan_name": "web+300"},
            {"vlan_vlan": 400},
        ]
    )
    client.force_login(make_superuser("vlan-rule-junos-user"))

    response = _refresh(client, device)

    rows = _rows(response)
    assert (rows[100]["name"], rows[100]["name_matches"]) == ("users", True)
    assert rows[200]["name"] == "guests+300"
    assert rows[300]["name"] == "web"
    # A row without a name shows the name that the writer saves for it.
    assert rows[400]["name"] == "VLAN 400"
    # The tab still shows what LibreNMS reported for each adjusted row.
    html = response.content.decode()
    assert "users+100" in html
    assert "web+300" in html

    _sync(client, device, 200, 300)

    assert VLAN.objects.get(vid=300, group=None).name == "web"
    assert VLAN.objects.get(vid=200, group=None).name == "guests+300"


@pytest.mark.django_db
def test_a_rule_for_another_manufacturer_leaves_the_name(client, vlan_librenms):
    """A Juniper-only rule does not change the VLAN names of a device from another manufacturer."""
    from dcim.models import Manufacturer
    from ipam.models import VLAN

    device, _ = _device("vlan-rule-other", "Other Vendor")
    _rule(Manufacturer.objects.create(name="Juniper", slug="juniper"))
    vlan_librenms([{"vlan_vlan": 300, "vlan_name": "web+300"}])
    client.force_login(make_superuser("vlan-rule-other-user"))

    assert _rows(_refresh(client, device))[300]["name"] == "web+300"
    _sync(client, device, 300)

    assert VLAN.objects.get(vid=300, group=None).name == "web+300"


@pytest.mark.django_db
def test_an_unscoped_rule_without_the_placeholder_always_strips_the_suffix(client, vlan_librenms):
    """A rule without {vid} and without a manufacturer applies to every device."""
    device, _ = _device("vlan-rule-always", "Other Vendor")
    _rule(match_pattern=r"^(.+)\+\d+$")
    vlan_librenms([{"vlan_vlan": 200, "vlan_name": "guests+300"}])
    client.force_login(make_superuser("vlan-rule-always-user"))

    assert _rows(_refresh(client, device))[200]["name"] == "guests"


@pytest.mark.django_db
def test_an_adjusted_name_that_the_group_already_holds_is_reported_by_name(client, vlan_librenms):
    """A create that collides with a VLAN name in the same group names the collision, not a concurrent change."""
    from django.contrib.messages import get_messages
    from ipam.models import VLAN, VLANGroup

    device, juniper = _device("vlan-rule-collision", "Juniper")
    _rule(juniper)
    group = VLANGroup.objects.create(name="Shared", slug="shared")
    VLAN.objects.create(vid=300, group=group, name="web", status="active")
    vlan_librenms([{"vlan_vlan": 301, "vlan_name": "web+301"}])
    client.force_login(make_superuser("vlan-rule-collision-user"))
    _refresh(client, device)

    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    response = client.post(
        url, {"server_key": "default", "action": "create_vlans", "select": "301", "vlan_group_301": str(group.pk)}
    )

    assert not VLAN.objects.filter(vid=301).exists()
    errors = [str(message) for message in get_messages(response.wsgi_request)]
    assert any("web" in error and "concurrent" not in error for error in errors), errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scope", "pattern", "error_field"),
    [
        ("vlan_name", r"^(.+)\+{vid}$", None),
        ("vlan_name", r"^(.+\+{vid}$", "match_pattern"),
        ("module_type", r"^(.+)\+{vid}$", "match_pattern"),
    ],
)
def test_the_vid_placeholder_is_checked_when_a_rule_is_saved(scope, pattern, error_field):
    """A {vid} rule saves only in the VLAN name scope, and only when it compiles with a VID in place."""
    from netbox_librenms_plugin.models import NormalizationRule

    rule = NormalizationRule(scope=scope, match_pattern=pattern, replacement=r"\1")
    if error_field is None:
        rule.full_clean()
        assert rule.apply("users+100", vid=100) == "users"
        return
    with pytest.raises(ValidationError) as raised:
        rule.full_clean()
    assert error_field in raised.value.message_dict

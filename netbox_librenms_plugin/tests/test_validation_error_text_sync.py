"""
The interface move and the Create VRF action show NetBox's validation message only to a superuser.

The interface move meets NetBox's real ``Interface.clean()``: a bridge on another device makes
NetBox name that bridge and its device. The Create VRF action meets an admin ``CUSTOM_VALIDATORS``
rule whose message names a device outside the viewer's scope.
"""

import pytest
from dcim.models import Device, Interface
from django.contrib.messages import get_messages
from django.urls import reverse
from extras.validators import CustomValidator
from ipam.models import VRF

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_superuser
from netbox_librenms_plugin.tests.test_ip_row_vrf_create import RD_ROWS, _create, seeded  # noqa: F401
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
from netbox_librenms_plugin.utils import mark_librenms_migrated

HIDDEN = "(only a superuser sees the message)"


class _Refuses(CustomValidator):
    """An admin validator that refuses every clean() with *text* and no field."""

    def __init__(self, text):
        super().__init__()
        self._text = text

    def validate(self, instance, request):
        self.fail(self._text)


@pytest.mark.django_db
@pytest.mark.parametrize("superuser", [False, True], ids=["restricted", "superuser"])
def test_an_interface_move_shows_netboxs_bridge_message_only_to_a_superuser(client, superuser):
    tag = f"vetext-move-{int(superuser)}"
    donor, winner, hidden = make_device(f"{tag}-donor"), make_device(f"{tag}-winner"), make_device(f"{tag}-hidden")
    mark_librenms_migrated(donor, winner.pk, "default")
    donor.save(update_fields=["custom_field_data"])
    interface = make_interface(donor, "Ethernet1")
    bridge = make_interface(hidden, f"{tag}-bridge")
    # Written past validation, which is how a cross-device bridge reaches the database at all.
    Interface.objects.filter(pk=interface.pk).update(bridge=bridge)
    if superuser:
        viewer = make_superuser(f"{tag}-superuser")
    else:
        viewer = make_user_with_perms(f"{tag}-viewer", [("view", Interface), ("change", Interface)])
        for action in ("view", "change"):
            viewer = grant(viewer, action, Device, constraints={"pk__in": [donor.pk, winner.pk]})
    client.force_login(viewer)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk]),
        {"server_key": "default"},
        HTTP_HX_REQUEST="true",
    )

    texts = " | ".join([response.content.decode(), *(str(m) for m in get_messages(response.wsgi_request))])
    interface.refresh_from_db()
    assert interface.device == donor
    if superuser:
        assert f"belongs to a different device ({hidden.name})" in texts, texts
    else:
        assert f"NetBox refuses the bridge field {HIDDEN}" in texts, texts
        assert hidden.name not in texts and bridge.name not in texts, texts


@pytest.mark.django_db
@pytest.mark.parametrize("superuser", [False, True], ids=["restricted", "superuser"])
def test_a_vrf_create_shows_netboxs_message_only_to_a_superuser(client, settings, seeded, superuser):  # noqa: F811
    tag = f"vetext-vrf-{int(superuser)}"
    hidden = make_device(f"{tag}-hidden")
    owner = seeded(tag)
    settings.CUSTOM_VALIDATORS = {"ipam.vrf": [_Refuses(f"Conflicts with {hidden.name}.")]}
    if superuser:
        viewer = make_superuser(f"{tag}-superuser")
    else:
        viewer = grant(
            make_user_with_perms(f"{tag}-viewer", [("view", Device)], constraints={"pk": owner.pk}), "add", VRF
        )
    client.force_login(viewer)

    response = _create(client, owner, RD_ROWS[0])

    texts = " | ".join(str(m) for m in get_messages(response.wsgi_request))
    assert not VRF.objects.exists()
    if superuser:
        assert f"Conflicts with {hidden.name}." in texts, texts
    else:
        assert f"NetBox refuses the VRF {HIDDEN}" in texts, texts
        assert hidden.name not in texts, texts


@pytest.mark.django_db
@pytest.mark.parametrize(
    "errors, expected",
    [
        pytest.param({"serial": ["x"], "hidden-device": ["x"], "name": ["x"]}, "the serial and name fields", id="two"),
        pytest.param({"serial": ["x"], "name": ["x"], "site": ["x"]}, "the serial, name and site fields", id="three"),
        pytest.param({"interfaces": ["x"]}, "the device", id="reverse-relation"),
        pytest.param({"__all__": ["x"]}, "the device", id="non-field"),
    ],
)
def test_a_hidden_refusal_names_only_the_concrete_fields_of_the_model(errors, expected):
    from django.core.exceptions import ValidationError

    from netbox_librenms_plugin.utils import exception_text_for

    viewer = make_user_with_perms("vetext-helper-viewer", [("change", Device)])

    assert exception_text_for(ValidationError(errors), Device, viewer) == f"NetBox refuses {expected} {HIDDEN}"


@pytest.mark.django_db
def test_an_inactive_superuser_gets_the_hidden_refusal():
    from django.core.exceptions import ValidationError

    from netbox_librenms_plugin.utils import exception_text_for

    viewer = make_superuser("vetext-helper-inactive")
    viewer.is_active = False

    assert exception_text_for(ValidationError({"serial": ["x"]}), Device, viewer) == (
        f"NetBox refuses the serial field {HIDDEN}"
    )

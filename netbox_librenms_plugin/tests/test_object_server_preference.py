"""Request-level tests for the object preferred LibreNMS server."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import OperationalError, connection
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.import_server_helpers import (
    configure_servers,
    device_info_response,
    json_response,
)
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms


def _message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _device_page_response(request_url, device):
    if request_url.endswith(f"/api/v0/devices/{device.cf['librenms_id']['primary']['id']}"):
        return device_info_response(
            request_url,
            device.cf["librenms_id"]["primary"]["id"],
            device.name,
        )
    raise AssertionError(f"Unexpected LibreNMS request: {request_url}")


@pytest.mark.django_db
def test_preference_post_changes_only_preference_and_keeps_transient_server(client, settings):
    """The star action stores preference without changing the active page server."""
    configure_servers(settings)
    device = make_device(
        "set-object-preference",
        librenms_cf={"primary": {"id": 13501}, "secondary": {"id": 13502}},
    )
    client.force_login(make_superuser("object-preference-writer"))
    url = reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[device.pk])

    response = client.post(
        url,
        {
            "object_type": "device",
            "server_key": "secondary",
            "active_server_key": "primary",
            "tab": "modules",
        },
    )

    assert response.status_code == 302
    assert response.url == (
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
        + "?tab=modules&server_key=primary"
    )
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {
        "primary": {"id": 13501},
        "secondary": {"id": 13502},
        "_preferred_server": "secondary",
    }
    assert _message_texts(response) == ["Preferred LibreNMS server changed to 'secondary'."]


@pytest.mark.django_db
def test_preference_post_ignores_unrelated_legacy_validation_errors(client, settings):
    """A preference change validates only the custom field that it writes."""
    configure_servers(settings)
    owner = make_device(
        "preference-with-legacy-rack-fields",
        librenms_cf={"primary": 13532, "secondary": 13533},
    )
    type(owner).objects.filter(pk=owner.pk).update(face="front", status="obsolete")
    client.force_login(make_superuser("legacy-validation-preference-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 302
    assert _message_texts(response) == ["Preferred LibreNMS server changed to 'secondary'."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"]["_preferred_server"] == "secondary"


@pytest.mark.django_db
def test_preference_post_supports_virtual_machines(client, settings):
    """A VM owns and stores its preferred server in the same mapping field."""
    configure_servers(settings)
    vm = make_vm("set-vm-preference")
    vm.custom_field_data["librenms_id"] = {"primary": 13503, "secondary": 13504}
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("vm-preference-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[vm.pk]),
        {
            "object_type": "virtualmachine",
            "server_key": "secondary",
            "active_server_key": "primary",
        },
    )

    assert response.status_code == 302
    assert response.url == (
        reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", args=[vm.pk]) + "?server_key=primary"
    )
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"]["_preferred_server"] == "secondary"


@pytest.mark.django_db
def test_preference_post_reports_a_save_failure_and_rolls_back(client, settings):
    """A database save failure must return a friendly redirect and preserve the mapping."""
    from dcim.models import Device

    configure_servers(settings)
    owner = make_device(
        "preference-save-failure",
        librenms_cf={"primary": 13530, "secondary": 13531},
    )
    client.force_login(make_superuser("preference-save-failure-writer"))

    with patch.object(Device, "save", side_effect=OperationalError("simulated database failure")):
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
            {"object_type": "device", "server_key": "secondary"},
        )

    assert response.status_code == 302
    assert _message_texts(response) == ["Could not change the preferred LibreNMS server. Try again."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13530, "secondary": 13531}


@pytest.mark.django_db
def test_preference_post_requires_change_scope_on_mapping_owner(client, settings):
    """A constrained Device grant cannot change an owner outside its scope."""
    configure_servers(settings)
    owner = make_device(
        "preference-outside-scope",
        librenms_cf={"primary": 13505, "secondary": 13506},
    )
    allowed = make_device("preference-inside-scope")
    user = make_user_with_perms(
        "scoped-preference-writer",
        [("change", type(owner))],
        constraints={"pk": allowed.pk},
    )
    client.force_login(user)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 404
    owner.refresh_from_db()
    assert "_preferred_server" not in owner.custom_field_data["librenms_id"]


@pytest.mark.django_db
def test_preference_post_requires_plugin_write_permission(client, settings):
    """Object change permission alone cannot write plugin-owned preference metadata."""
    configure_servers(settings)
    owner = make_device(
        "preference-without-plugin-write",
        librenms_cf={"primary": 13524, "secondary": 13525},
    )
    user = get_user_model().objects.create_user(username="object-only-preference-writer", password="x")
    user = grant(user, "view", apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"))
    user = grant(user, "change", type(owner))
    client.force_login(user)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 302
    assert _message_texts(response) == ["You do not have permission to perform this action."]
    owner.refresh_from_db()
    assert "_preferred_server" not in owner.custom_field_data["librenms_id"]


@pytest.mark.django_db
def test_preference_post_revalidates_mapping_after_lock(client, settings):
    """The locked row, not an earlier page state, decides whether a preference is valid."""
    configure_servers(settings)
    owner = make_device(
        "preference-locked-state",
        librenms_cf={"primary": 13507, "secondary": 13508},
    )
    client.force_login(make_superuser("locked-preference-writer"))

    class RemoveTargetBeforeLock:
        fired = False

        def __call__(self, execute, sql, params, many, context):
            if not self.fired and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql.upper():
                self.fired = True
                type(owner).objects.filter(pk=owner.pk).update(custom_field_data={"librenms_id": {"primary": 13507}})
            return execute(sql, params, many, context)

    lock_hook = RemoveTargetBeforeLock()
    with connection.execute_wrapper(lock_hook):
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
            {"object_type": "device", "server_key": "secondary"},
        )

    assert response.status_code == 302
    assert lock_hook.fired
    assert _message_texts(response) == ["A preferred server requires at least two usable object mappings."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13507}


@pytest.mark.django_db
def test_single_mapping_remains_implicit_without_stored_preference(client, settings):
    """A preference cannot be stored when only one usable mapping remains."""
    configure_servers(settings)
    owner = make_device("implicit-single-server", librenms_cf={"primary": 13509})
    client.force_login(make_superuser("single-preference-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]),
        {"object_type": "device", "server_key": "primary"},
    )

    assert _message_texts(response) == ["A preferred server requires at least two usable object mappings."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13509}

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: device_info_response(url, 13509, owner.name),
    ):
        page = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]))
    assert page.status_code == 200
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]) not in page.content.decode()
    assert b'id="librenms-server-preference-warning"' not in page.content


@pytest.mark.django_db
def test_active_and_preferred_servers_render_as_distinct_states(client, settings):
    """The active check and preferred star can point at different mappings."""
    configure_servers(settings)
    owner = make_device(
        "active-versus-preferred",
        librenms_cf={
            "primary": {"id": 13510},
            "secondary": {"id": 13511},
            "_preferred_server": "secondary",
        },
    )
    client.force_login(make_superuser("preference-render-writer"))
    page_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: _device_page_response(url, owner),
    ):
        response = client.get(page_url, {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert 'data-active-server-key="primary"' in html
    assert 'data-preferred-server-key="secondary"' in html
    assert 'name="active_server_key" value="primary"' in html
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]) in html
    card_start = html.index('id="librenms-connections"')
    assert "confirm(" not in html[card_start : html.index("</table>", card_start)]


@pytest.mark.django_db
def test_view_only_user_sees_preference_but_not_star_controls(client, settings):
    """Preference is visible to a viewer, but only an authorized writer gets controls."""
    configure_servers(settings)
    owner = make_device(
        "view-only-preference",
        librenms_cf={
            "primary": {"id": 13512},
            "secondary": {"id": 13513},
            "_preferred_server": "secondary",
        },
    )
    user = get_user_model().objects.create_user(username="preference-viewer", password="x")
    user = grant(user, "view", apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"))
    user = grant(user, "view", type(owner))
    client.force_login(user)

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: _device_page_response(url, owner),
    ):
        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]),
            {"server_key": "primary"},
        )

    html = response.content.decode()
    assert response.status_code == 200
    assert 'data-preferred-server-key="secondary"' in html
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]) not in html
    assert "server_key=secondary" in html


@pytest.mark.django_db
def test_vc_member_page_uses_mapping_owner_for_preference_form(client, settings):
    """A VC member page submits preference changes to the member that owns the mappings."""
    configure_servers(settings)
    _chassis, (owner, viewed_member) = make_virtual_chassis_members("preference-owner", count=2)
    owner.custom_field_data["librenms_id"] = {
        "primary": {"id": 13514},
        "secondary": {"id": 13515},
    }
    owner.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("vc-preference-writer"))

    def response_for_vc(request_url, **_kwargs):
        if request_url.endswith("/api/v0/devices/13514"):
            return device_info_response(request_url, 13514, viewed_member.name)
        if request_url.endswith("/api/v0/inventory/13514/all"):
            return json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=response_for_vc):
        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[viewed_member.pk]),
            {"server_key": "primary"},
        )

    html = response.content.decode()
    assert response.status_code == 200
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[owner.pk]) in html
    assert reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[viewed_member.pk]) not in html


@pytest.mark.django_db
@pytest.mark.parametrize("stored_preference", [None, 13516, "retired"])
def test_invalid_or_missing_preference_warns_and_get_does_not_mutate(
    client,
    settings,
    stored_preference,
):
    """Fallback is explicit and a GET never repairs preference metadata."""
    configure_servers(settings)
    mapping = {"primary": {"id": 13517}, "secondary": {"id": 13518}}
    if stored_preference is not None:
        mapping["_preferred_server"] = stored_preference
    owner = make_device("invalid-preference-warning", librenms_cf=mapping)
    original = deepcopy(owner.custom_field_data["librenms_id"])
    client.force_login(make_superuser("invalid-preference-viewer"))

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: _device_page_response(url, owner),
    ):
        response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]))

    assert response.status_code == 200
    assert b'id="librenms-server-preference-warning"' in response.content
    assert b"Using installation default" in response.content
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == original


@pytest.mark.django_db
def test_unconfigured_preferred_mapping_warns_and_falls_back_without_mutation(client, settings):
    """A preference that still has an identity but no configured server is invalid."""
    configure_servers(settings)
    mapping = {
        "primary": {"id": 13526},
        "secondary": {"id": 13527},
        "retired": {"id": 13528},
        "_preferred_server": "retired",
    }
    owner = make_device("unconfigured-preference-warning", librenms_cf=mapping)
    client.force_login(make_superuser("unconfigured-preference-viewer"))

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: _device_page_response(url, owner),
    ):
        response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]))

    assert response.status_code == 200
    assert b"not configured or usable" in response.content
    assert b"Using installation default server &#x27;primary&#x27;" in response.content
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == mapping


@pytest.mark.django_db
def test_only_unusable_mapping_requires_selection_without_get_mutation(client, settings):
    """An unmapped default cannot replace an unusable preferred object mapping."""
    configure_servers(settings)
    mapping = {
        "retired": {"id": 13529},
        "_preferred_server": "retired",
    }
    owner = make_device("unusable-preference-selection", librenms_cf=mapping)
    client.force_login(make_superuser("unusable-preference-viewer"))

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The unmapped installation default contacted LibreNMS"),
    ) as requests_get:
        response = client.get(reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[owner.pk]))

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"The installation default is not mapped, so select a server." in response.content
    assert b"Select a LibreNMS server to continue." in response.content
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == mapping


@pytest.mark.django_db
@pytest.mark.parametrize("preferred_removed", [True, False])
def test_removing_mapping_clears_preference_when_it_is_removed_or_only_one_mapping_remains(
    client,
    settings,
    preferred_removed,
):
    """Removal keeps the mapping and preference metadata consistent in one write."""
    configure_servers(settings)
    preferred_key = "retired" if preferred_removed else "primary"
    owner = make_device(
        f"remove-preferred-{preferred_removed}",
        librenms_cf={
            "primary": 13519,
            "retired": 13520,
            "_preferred_server": preferred_key,
        },
    )
    client.force_login(make_superuser("remove-preferred-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]),
        {"object_type": "device", "server_key": "retired"},
    )

    assert response.status_code == 302
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13519}

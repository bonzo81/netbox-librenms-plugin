"""Request-level coverage for an object's preferred LibreNMS server."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_superuser,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms


def _message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _configure_servers(settings, primary, secondary):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "primary": {
            "display_name": "Primary LibreNMS",
            "librenms_url": primary.url,
            "api_token": "preference-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
        "secondary": {
            "display_name": "Secondary LibreNMS",
            "librenms_url": secondary.url,
            "api_token": "preference-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        },
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        primary = stack.enter_context(librenms_mock_server())
        secondary = stack.enter_context(librenms_mock_server())
        _configure_servers(settings, primary, secondary)
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
        yield SimpleNamespace(primary=primary, secondary=secondary)


def _register_device(server, device_id, name):
    server.register(
        f"/api/v0/devices/{device_id}",
        {
            "status": "ok",
            "devices": [
                {
                    "device_id": device_id,
                    "sysName": name,
                    "hostname": name,
                    "hardware": "Test appliance",
                }
            ],
        },
    )


def _preference_url(obj):
    return reverse("plugins:netbox_librenms_plugin:set_preferred_server", args=[obj.pk])


def _sync_url(obj):
    name = "vm_librenms_sync" if obj._meta.model_name == "virtualmachine" else "device_librenms_sync"
    return reverse(f"plugins:netbox_librenms_plugin:{name}", args=[obj.pk])


@pytest.mark.django_db
def test_preference_post_changes_only_preference_and_keeps_transient_server(client, servers):
    device = make_device(
        "set-object-preference",
        librenms_cf={"primary": {"id": 13501}, "secondary": {"id": 13502}},
    )
    client.force_login(make_superuser("object-preference-writer"))

    response = client.post(
        _preference_url(device),
        {
            "object_type": "device",
            "server_key": "secondary",
            "active_server_key": "primary",
            "tab": "modules",
        },
    )

    assert response.status_code == 302
    assert response.url == f"{_sync_url(device)}?tab=modules&server_key=primary"
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {
        "primary": {"id": 13501},
        "secondary": {"id": 13502},
        "_preferred_server": "secondary",
    }
    assert _message_texts(response) == ["Preferred LibreNMS server changed to 'secondary'."]


@pytest.mark.django_db
def test_preference_post_keeps_the_active_server_when_the_key_is_rejected(client, servers):
    """A malformed server_key must not drop a non-default page back to the default server."""
    device = make_device(
        "preference-invalid-key",
        librenms_cf={"primary": {"id": 13521}, "secondary": {"id": 13522}},
    )
    client.force_login(make_superuser("object-preference-invalid-key"))

    response = client.post(
        _preference_url(device),
        {
            "object_type": "device",
            "server_key": "   ",
            "active_server_key": "primary",
            "tab": "modules",
        },
    )

    assert response.status_code == 302
    assert response.url == f"{_sync_url(device)}?tab=modules&server_key=primary"
    assert _message_texts(response) == ["LibreNMS server key must be a non-empty string."]
    device.refresh_from_db()
    assert "_preferred_server" not in device.custom_field_data["librenms_id"]


@pytest.mark.django_db
def test_preference_post_ignores_unrelated_legacy_validation_errors(client, servers):
    owner = make_device(
        "preference-with-legacy-rack-fields",
        librenms_cf={"primary": 13532, "secondary": 13533},
    )
    type(owner).objects.filter(pk=owner.pk).update(face="front", status="obsolete")
    client.force_login(make_superuser("legacy-validation-preference-writer"))

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 302
    assert _message_texts(response) == ["Preferred LibreNMS server changed to 'secondary'."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"]["_preferred_server"] == "secondary"
    assert owner.face == "front"


@pytest.mark.django_db
def test_preference_post_supports_virtual_machines(client, servers):
    vm = make_vm("set-vm-preference")
    vm.custom_field_data["librenms_id"] = {"primary": 13503, "secondary": 13504}
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("vm-preference-writer"))

    response = client.post(
        _preference_url(vm),
        {
            "object_type": "virtualmachine",
            "server_key": "secondary",
            "active_server_key": "primary",
        },
    )

    assert response.status_code == 302
    assert response.url == f"{_sync_url(vm)}?server_key=primary"
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"]["_preferred_server"] == "secondary"


@pytest.mark.django_db
def test_preference_post_requires_change_scope_on_mapping_owner(client, servers):
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
        _preference_url(owner),
        {"object_type": "device", "server_key": "secondary"},
    )

    assert response.status_code == 404
    owner.refresh_from_db()
    assert "_preferred_server" not in owner.custom_field_data["librenms_id"]


@pytest.mark.django_db
def test_preference_post_requires_plugin_write_permission(client, servers):
    owner = make_device(
        "preference-without-plugin-write",
        librenms_cf={"primary": 13524, "secondary": 13525},
    )
    user = get_user_model().objects.create_user(username="object-only-preference-writer", password="x")
    user = grant(user, "view", apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"))
    user = grant(user, "change", type(owner))
    client.force_login(user)

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "secondary"},
        HTTP_REFERER=_sync_url(owner),
    )

    assert response.status_code == 302
    assert response.url == _sync_url(owner)
    assert _message_texts(response) == ["You do not have permission to perform this action."]
    owner.refresh_from_db()
    assert "_preferred_server" not in owner.custom_field_data["librenms_id"]


@pytest.mark.django_db
def test_preference_post_revalidates_mapping_after_lock(client, servers):
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
            _preference_url(owner),
            {"object_type": "device", "server_key": "secondary"},
        )

    assert response.status_code == 302
    assert lock_hook.fired
    assert _message_texts(response) == ["A preferred server requires at least two usable object mappings."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13507}


@pytest.mark.django_db
def test_single_mapping_remains_implicit_without_stored_preference(client, servers):
    owner = make_device("implicit-single-server", librenms_cf={"primary": 13509})
    _register_device(servers.primary, 13509, owner.name)
    client.force_login(make_superuser("single-preference-writer"))

    response = client.post(
        _preference_url(owner),
        {"object_type": "device", "server_key": "primary"},
    )

    assert _message_texts(response) == ["A preferred server requires at least two usable object mappings."]
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13509}

    page = client.get(_sync_url(owner))

    assert page.status_code == 200
    assert _preference_url(owner) not in page.content.decode()
    assert b'id="librenms-server-preference-warning"' not in page.content


@pytest.mark.django_db
def test_active_and_preferred_servers_render_as_distinct_states(client, servers):
    owner = make_device(
        "active-versus-preferred",
        librenms_cf={
            "primary": {"id": 13510},
            "secondary": {"id": 13511},
            "_preferred_server": "secondary",
        },
    )
    _register_device(servers.primary, 13510, owner.name)
    client.force_login(make_superuser("preference-render-writer"))

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert 'data-active-server-key="primary"' in html
    assert 'data-preferred-server-key="secondary"' in html
    assert 'name="active_server_key" value="primary"' in html
    assert _preference_url(owner) in html
    card_start = html.index('id="librenms-connections"')
    assert "confirm(" not in html[card_start : html.index("</table>", card_start)]


@pytest.mark.django_db
def test_remove_mapping_form_preserves_the_active_server(client, servers):
    owner = make_device(
        "remove-mapping-active-server",
        librenms_cf={"primary": {"id": 13599}, "retired": {"id": 13600}},
    )
    _register_device(servers.primary, 13599, owner.name)
    client.force_login(make_superuser("remove-mapping-active-server-writer"))

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]) in html
    assert html.count('name="active_server_key" value="primary"') == 1


@pytest.mark.django_db
def test_view_only_user_sees_preference_but_not_star_controls(client, servers):
    owner = make_device(
        "view-only-preference",
        librenms_cf={
            "primary": {"id": 13512},
            "secondary": {"id": 13513},
            "_preferred_server": "secondary",
        },
    )
    _register_device(servers.primary, 13512, owner.name)
    user = get_user_model().objects.create_user(username="preference-viewer", password="x")
    user = grant(user, "view", apps.get_model("netbox_librenms_plugin", "LibreNMSSettings"))
    user = grant(user, "view", type(owner))
    client.force_login(user)

    response = client.get(_sync_url(owner), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert 'data-preferred-server-key="secondary"' in html
    assert _preference_url(owner) not in html
    assert "server_key=secondary" in html


@pytest.mark.django_db
def test_vc_member_page_uses_mapping_owner_for_preference_form(client, servers):
    _chassis, (owner, viewed_member) = make_virtual_chassis_members("preference-owner", count=2)
    owner.custom_field_data["librenms_id"] = {
        "primary": {"id": 13514},
        "secondary": {"id": 13515},
    }
    owner.save(update_fields=["custom_field_data"])
    _register_device(servers.primary, 13514, viewed_member.name)
    servers.primary.register(
        "/api/v0/inventory/13514/all",
        {"status": "ok", "inventory": []},
    )
    client.force_login(make_superuser("vc-preference-writer"))

    response = client.get(_sync_url(viewed_member), {"server_key": "primary"})

    html = response.content.decode()
    assert response.status_code == 200
    assert _preference_url(owner) in html
    assert _preference_url(viewed_member) not in html


@pytest.mark.django_db
@pytest.mark.parametrize("stored_preference", [None, 13516, "retired"])
def test_invalid_or_missing_preference_warns_and_get_does_not_mutate(
    client,
    servers,
    stored_preference,
):
    mapping = {"primary": {"id": 13517}, "secondary": {"id": 13518}}
    if stored_preference is not None:
        mapping["_preferred_server"] = stored_preference
    owner = make_device("invalid-preference-warning", librenms_cf=mapping)
    original = deepcopy(owner.custom_field_data["librenms_id"])
    _register_device(servers.primary, 13517, owner.name)
    client.force_login(make_superuser(f"invalid-preference-{stored_preference}-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    assert b'id="librenms-server-preference-warning"' in response.content
    assert b"Using installation default" in response.content
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == original


@pytest.mark.django_db
def test_unconfigured_preferred_mapping_warns_and_falls_back_without_mutation(client, servers):
    mapping = {
        "primary": {"id": 13526},
        "secondary": {"id": 13527},
        "retired": {"id": 13528},
        "_preferred_server": "retired",
    }
    owner = make_device("unconfigured-preference-warning", librenms_cf=mapping)
    _register_device(servers.primary, 13526, owner.name)
    client.force_login(make_superuser("unconfigured-preference-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    assert b"not configured or usable" in response.content
    assert b"Using installation default server &#x27;primary&#x27;" in response.content
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == mapping


@pytest.mark.django_db
def test_only_unusable_mapping_requires_selection_without_get_mutation(client, servers):
    mapping = {
        "retired": {"id": 13529},
        "_preferred_server": "retired",
    }
    owner = make_device("unusable-preference-selection", librenms_cf=mapping)
    client.force_login(make_superuser("unusable-preference-viewer"))

    response = client.get(_sync_url(owner))

    assert response.status_code == 200
    assert b"The installation default is not mapped, so select a server." in response.content
    assert b"Select a LibreNMS server to continue." in response.content
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == mapping


@pytest.mark.django_db
@pytest.mark.parametrize("preferred_removed", [True, False])
def test_removing_mapping_clears_preference_when_removed_or_only_one_mapping_remains(
    client,
    servers,
    preferred_removed,
):
    preferred_key = "retired" if preferred_removed else "primary"
    owner = make_device(
        f"remove-preferred-{preferred_removed}",
        librenms_cf={
            "primary": 13519,
            "retired": 13520,
            "_preferred_server": preferred_key,
        },
    )
    client.force_login(make_superuser(f"remove-preferred-{preferred_removed}-writer"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[owner.pk]),
        {"object_type": "device", "server_key": "retired"},
    )

    assert response.status_code == 302
    owner.refresh_from_db()
    assert owner.custom_field_data["librenms_id"] == {"primary": 13519}

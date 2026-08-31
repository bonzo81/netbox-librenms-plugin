"""Request-level tests for confirming a same-server LibreNMS identity replacement."""

import re
from copy import deepcopy

import pytest

from netbox_librenms_plugin.identity_replacement import (
    INTENT_FIELD,
    IdentityReplacementIntent,
    load_identity_replacement_intent,
    sign_identity_replacement_intent,
)
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, make_vm
from netbox_librenms_plugin.tests.import_server_helpers import librenms_device
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

_INTENT_PATTERN = re.compile(rf'name="{INTENT_FIELD}" value="([^"]+)"')
_CANCEL_PATTERN = re.compile(r"<button[^>]*>(?:(?!</button>).)*?Cancel\s*</button>", re.DOTALL)


def _action_url(librenms_device_id):
    from django.urls import reverse

    return reverse(
        "plugins:netbox_librenms_plugin:device_conflict_action",
        kwargs={"device_id": librenms_device_id},
    )


@pytest.fixture
def secondary_server(settings, monkeypatch):
    """Run each test's secondary LibreNMS boundary over real loopback HTTP."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            "primary": {
                "display_name": "Primary LibreNMS",
                "librenms_url": "http://127.0.0.1:9",
                "api_token": "identity-replacement-test-token",
                "verify_ssl": False,
            },
            "secondary": {
                "display_name": "Secondary LibreNMS",
                "librenms_url": server.url,
                "api_token": "identity-replacement-test-token",
                "verify_ssl": False,
            },
        }
        plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
        settings.PLUGINS_CONFIG = plugin_config
        yield server


def _serve_secondary(server, librenms_device_id, hostname, *, serial="", reported_device_id=None):
    """Serve one LibreNMS device from the secondary server."""
    libre_device = librenms_device(librenms_device_id, hostname)
    libre_device["serial"] = serial
    if reported_device_id is not None:
        libre_device["device_id"] = reported_device_id

    server.register(
        f"/api/v0/devices/{librenms_device_id}",
        {"status": "ok", "devices": [libre_device]},
    )
    for suffix in ("", "/all"):
        server.register(
            f"/api/v0/inventory/{librenms_device_id}{suffix}",
            {"status": "ok", "inventory": []},
        )
    return server


def _post_action(client, librenms_device_id, obj, action, *, server, object_type="device", **extra):
    """Post the normal (unconfirmed) mapping action exactly as the validation modal does."""
    assert f"/api/v0/devices/{librenms_device_id}" in server.routes
    return client.post(
        _action_url(librenms_device_id),
        {
            "action": action,
            "existing_device_id": obj.pk,
            "existing_device_type": object_type,
            "server_key": "secondary",
            "use-sysname-toggle": "on",
            "strip-domain-toggle": "off",
            **extra,
        },
        headers={"HX-Request": "true"},
    )


def _post_confirmation(client, librenms_device_id, token, *, server):
    """Post exactly the fields the confirmation dialog carries."""
    assert f"/api/v0/devices/{librenms_device_id}" in server.routes
    return client.post(
        _action_url(librenms_device_id),
        {
            INTENT_FIELD: token,
            "use-sysname-toggle": "on",
            "strip-domain-toggle": "off",
        },
        headers={"HX-Request": "true"},
    )


def _offered_token(response):
    """Return the signed confirmation the blocked action offered."""
    match = _INTENT_PATTERN.search(response.content.decode())
    assert match, f"no replacement confirmation was offered: {response.content.decode()[:400]}"
    return match.group(1)


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["link", "update", "update_serial"])
def test_a_blocked_action_offers_a_bound_replacement_confirmation(client, secondary_server, action):
    """Every mapping action blocked by a same-server identity offers a bound confirmation."""
    device = make_device(f"replace-offer-{action}", librenms_cf={"secondary": 51301})
    user = make_superuser("identity-replacement-offer")
    client.force_login(user)

    response = _post_action(
        client, 51401, device, action, server=_serve_secondary(secondary_server, 51401, device.name)
    )

    assert response.status_code == 200
    html = response.content.decode()
    # The confirmation replaces the open modal instead of the row the action was posted from.
    assert 'id="htmx-modal-content"' in html
    assert 'hx-swap-oob="innerHTML"' in html
    assert response["HX-Reswap"] == "none"
    # It states the active server, the expected old ID, and the proposed new ID.
    assert "Secondary LibreNMS" in html
    assert "51301" in html
    assert "51401" in html
    assert device.name in html
    # The offer is bound to the exact object, server, action, and both host IDs.
    assert load_identity_replacement_intent(_offered_token(response)) == IdentityReplacementIntent(
        user_pk=user.pk,
        object_type="device",
        object_pk=device.pk,
        server_key="secondary",
        action=action,
        force=False,
        current_host_id=51301,
        proposed_host_id=51401,
    )
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 51301}


@pytest.mark.django_db
def test_the_confirmation_offers_a_cancel_control_that_writes_nothing(client, secondary_server):
    """Cancelling dismisses the dialog without a request, and re-asking never mutates."""
    device = make_device("replace-cancelled", librenms_cf={"secondary": 51501})
    client.force_login(make_superuser("identity-replacement-cancel"))

    for _ in range(2):
        response = _post_action(
            client, 51601, device, "link", server=_serve_secondary(secondary_server, 51601, device.name)
        )
        assert response.status_code == 200
        html = response.content.decode()
        # Cancel dismisses the dialog client-side, so it must carry no URL that could mutate.
        cancel = _CANCEL_PATTERN.search(html)
        assert cancel, "the confirmation offers no Cancel control"
        assert "hx-" not in cancel.group(0)
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == {"secondary": 51501}


@pytest.mark.django_db
def test_a_confirmed_replacement_changes_only_the_active_server_identity(client, secondary_server):
    """Confirmation rewrites one host ID and keeps OOB, other servers, and the preference."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    device = make_device(
        "replace-confirmed-device",
        librenms_cf={
            "primary": {"id": 51701, "oob": {"id": 51702, "type": "bmc", "version": "1.0"}},
            "secondary": {"id": 51703, "oob": {"id": 51704, "type": "ipmi"}},
            "_preferred_server": "primary",
        },
    )
    client.force_login(make_superuser("identity-replacement-confirm"))
    server = _serve_secondary(secondary_server, 51801, device.name)

    token = _offered_token(_post_action(client, 51801, device, "link", server=server))
    with CaptureQueriesContext(connection) as queries:
        confirmed = _post_confirmation(client, 51801, token, server=server)

    assert confirmed.status_code == 200
    # The compare-and-swap reads the row it locked, not the pre-request snapshot.
    assert any('FROM "dcim_device"' in query["sql"] and "FOR UPDATE" in query["sql"] for query in queries)
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {
        "primary": {"id": 51701, "oob": {"id": 51702, "type": "bmc", "version": "1.0"}},
        "secondary": {"id": 51801, "oob": {"id": 51704, "type": "ipmi"}},
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["link", "update"])
def test_a_vm_replacement_follows_the_same_confirmation_contract(client, secondary_server, action):
    """A Virtual Machine is blocked, offered, and replaced exactly like a Device."""
    vm = make_vm(f"replace-confirmed-vm-{action}")
    vm.custom_field_data["librenms_id"] = {
        "primary": 51901,
        "secondary": {"id": 51902, "oob": {"id": 51903, "type": "bmc"}},
        "_preferred_server": "primary",
    }
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("identity-replacement-vm"))
    server = _serve_secondary(secondary_server, 52001, vm.name)

    blocked = _post_action(client, 52001, vm, action, server=server, object_type="virtualmachine")
    assert load_identity_replacement_intent(_offered_token(blocked)).object_type == "virtualmachine"
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"]["secondary"] == {"id": 51902, "oob": {"id": 51903, "type": "bmc"}}

    confirmed = _post_confirmation(client, 52001, _offered_token(blocked), server=server)

    assert confirmed.status_code == 200
    vm.refresh_from_db()
    assert vm.custom_field_data["librenms_id"] == {
        "primary": 51901,
        "secondary": {"id": 52001, "oob": {"id": 51903, "type": "bmc"}},
        "_preferred_server": "primary",
    }


@pytest.mark.django_db
def test_a_stale_confirmation_fails_closed_against_a_concurrent_change(client, secondary_server):
    """A mapping changed after the confirmation was issued blocks the replacement."""
    device = make_device("replace-stale-device", librenms_cf={"secondary": 52101})
    client.force_login(make_superuser("identity-replacement-stale"))
    server = _serve_secondary(secondary_server, 52201, device.name)

    token = _offered_token(_post_action(client, 52201, device, "link", server=server))
    type(device).objects.filter(pk=device.pk).update(custom_field_data={"librenms_id": {"secondary": 52102}})

    response = _post_confirmation(client, 52201, token, server=server)

    assert response.status_code == 200
    assert b"no longer matches" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52102}


@pytest.mark.django_db
def test_a_stale_confirmation_is_detected_on_the_locked_row(client, secondary_server):
    """The confirmed host ID is compared after the target row is locked, not before."""
    from django.db import connection

    device = make_device("replace-locked-stale-device", librenms_cf={"secondary": 52151})
    client.force_login(make_superuser("identity-replacement-locked-stale"))
    server = _serve_secondary(secondary_server, 52251, device.name)
    token = _offered_token(_post_action(client, 52251, device, "link", server=server))
    changed_under_lock = False

    def change_mapping_at_the_target_lock(execute, sql, params, many, context):
        nonlocal changed_under_lock
        if not changed_under_lock and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql:
            changed_under_lock = True
            type(device).objects.filter(pk=device.pk).update(custom_field_data={"librenms_id": {"secondary": 52152}})
        return execute(sql, params, many, context)

    with connection.execute_wrapper(change_mapping_at_the_target_lock):
        response = _post_confirmation(client, 52251, token, server=server)

    assert changed_under_lock
    assert response.status_code == 200
    assert b"no longer matches" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52152}


@pytest.mark.django_db
def test_a_confirmation_cannot_be_replayed_after_it_has_been_applied(client, secondary_server):
    """Re-posting an applied confirmation is refused instead of rewriting the mapping."""
    device = make_device("replace-replay-device", librenms_cf={"secondary": 52301})
    client.force_login(make_superuser("identity-replacement-replay"))
    server = _serve_secondary(secondary_server, 52401, device.name)

    token = _offered_token(_post_action(client, 52401, device, "link", server=server))
    assert _post_confirmation(client, 52401, token, server=server).status_code == 200
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52401}

    replay = _post_confirmation(client, 52401, token, server=server)

    assert replay.status_code == 200
    assert b"no longer matches" in replay.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52401}


@pytest.mark.django_db
def test_a_confirmation_cannot_be_used_by_another_user(client, secondary_server):
    """A confirmation issued for one user is refused for everyone else."""
    from dcim.models import Device

    device = make_device("replace-other-user-device", librenms_cf={"secondary": 52901})
    client.force_login(make_superuser("identity-replacement-issuer"))
    server = _serve_secondary(secondary_server, 53001, device.name)
    token = _offered_token(_post_action(client, 53001, device, "link", server=server))

    client.force_login(make_user_with_perms("identity-replacement-borrower", [("view", Device), ("change", Device)]))
    response = _post_confirmation(client, 53001, token, server=server)

    assert response.status_code == 200
    assert b"issued for a different user" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52901}


@pytest.mark.django_db
def test_a_tampered_confirmation_is_refused(client, secondary_server):
    """Editing the signed payload invalidates the confirmation."""
    device = make_device("replace-tampered-device", librenms_cf={"secondary": 53301})
    client.force_login(make_superuser("identity-replacement-tamper"))
    server = _serve_secondary(secondary_server, 53401, device.name)
    token = _offered_token(_post_action(client, 53401, device, "link", server=server))

    payload, _, signature = token.rpartition(":")
    response = _post_confirmation(client, 53401, f"{payload}x:{signature}", server=server)

    assert response.status_code == 200
    assert b"not valid" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 53301}


@pytest.mark.django_db
def test_an_expired_confirmation_is_refused(client, secondary_server, monkeypatch):
    """A confirmation older than its lifetime is refused."""
    from netbox_librenms_plugin import identity_replacement

    device = make_device("replace-expired-device", librenms_cf={"secondary": 53501})
    client.force_login(make_superuser("identity-replacement-expired"))
    server = _serve_secondary(secondary_server, 53601, device.name)
    token = _offered_token(_post_action(client, 53601, device, "link", server=server))
    monkeypatch.setattr(identity_replacement, "INTENT_MAX_AGE_SECONDS", -1)

    response = _post_confirmation(client, 53601, token, server=server)

    assert response.status_code == 200
    assert b"has expired" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 53501}


@pytest.mark.django_db
def test_a_confirmation_is_refused_when_the_proposed_host_id_changed(client, secondary_server):
    """LibreNMS reporting a different host ID blocks the confirmed replacement."""
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import get_import_device_cache_key

    device = make_device("replace-drifted-device", librenms_cf={"secondary": 53701})
    client.force_login(make_superuser("identity-replacement-drift"))

    token = _offered_token(
        _post_action(client, 53801, device, "link", server=_serve_secondary(secondary_server, 53801, device.name))
    )
    # Expire only this device's cached payload, so LibreNMS is re-queried on the confirmation.
    cache.delete(get_import_device_cache_key(53801, "secondary"))
    response = _post_confirmation(
        client,
        53801,
        token,
        server=_serve_secondary(secondary_server, 53801, device.name, reported_device_id=53802),
    )

    assert response.status_code == 200
    assert b"proposed LibreNMS host ID changed" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 53701}


@pytest.mark.django_db
def test_a_confirmed_replacement_still_rechecks_the_cross_model_id_claim(client, secondary_server):
    """Another object claiming the proposed ID after validation blocks the replacement."""
    from django.db import connection

    device = make_device("replace-collision-target", librenms_cf={"secondary": 52501})
    claimant = make_vm("replace-collision-claimant")
    client.force_login(make_superuser("identity-replacement-collision"))
    server = _serve_secondary(secondary_server, 52601, device.name)
    token = _offered_token(_post_action(client, 52601, device, "link", server=server))
    claim_applied = False

    def claim_the_proposed_id_at_the_target_lock(execute, sql, params, many, context):
        nonlocal claim_applied
        if not claim_applied and 'FROM "dcim_device"' in sql and "FOR UPDATE" in sql:
            claim_applied = True
            type(claimant).objects.filter(pk=claimant.pk).update(
                custom_field_data={"librenms_id": {"secondary": 52601}}
            )
        return execute(sql, params, many, context)

    with connection.execute_wrapper(claim_the_proposed_id_at_the_target_lock):
        response = _post_confirmation(client, 52601, token, server=server)

    assert claim_applied
    assert response.status_code == 200
    assert b"already assigned to" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52501}


@pytest.mark.django_db
def test_a_blocked_serial_leaves_the_confirmed_replacement_unwritten(client, secondary_server):
    """An error after the mapping is applied in memory persists nothing."""
    make_device("replace-serial-owner", serial="SN-REPLACE-1")
    device = make_device("replace-rollback-device", serial="SN-ORIGINAL", librenms_cf={"secondary": 52701})
    client.force_login(make_superuser("identity-replacement-rollback"))
    server = _serve_secondary(secondary_server, 52801, device.name, serial="SN-REPLACE-1")

    token = _offered_token(_post_action(client, 52801, device, "update", server=server))
    response = _post_confirmation(client, 52801, token, server=server)

    assert response.status_code == 200
    assert b"Serial conflict" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52701}
    assert device.serial == "SN-ORIGINAL"


@pytest.mark.django_db
def test_a_database_failure_leaves_the_confirmed_replacement_unwritten(client, secondary_server):
    """A statement PostgreSQL rejects persists neither the mapping nor the serial."""
    from dcim.models import Device

    device = make_device("replace-dberror-device", serial="SN-ORIGINAL", librenms_cf={"secondary": 54101})
    client.force_login(make_superuser("identity-replacement-dberror"))
    # update writes custom_field_data, name, and serial in one statement, and save(update_fields=...)
    # skips full_clean(), so an overlong LibreNMS serial reaches PostgreSQL and the statement fails.
    overlong_serial = "S" * (Device._meta.get_field("serial").max_length + 1)
    server = _serve_secondary(secondary_server, 54201, device.name, serial=overlong_serial)

    token = _offered_token(_post_action(client, 54201, device, "update", server=server))
    response = _post_confirmation(client, 54201, token, server=server)

    assert response.status_code == 200
    assert b"too long" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 54101}
    assert device.serial == "SN-ORIGINAL"


@pytest.mark.django_db
def test_an_oob_only_mapping_needs_no_replacement_confirmation(client, secondary_server):
    """An entry with only an OOB link has no host identity to replace."""
    device = make_device(
        "replace-oob-only-device",
        librenms_cf={"secondary": {"oob": {"id": 53901, "type": "bmc"}}},
    )
    client.force_login(make_superuser("identity-replacement-oob-only"))

    response = _post_action(
        client, 54001, device, "link", server=_serve_secondary(secondary_server, 54001, device.name)
    )

    assert response.status_code == 200
    assert INTENT_FIELD.encode() not in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": {"id": 54001, "oob": {"id": 53901, "type": "bmc"}}}


@pytest.mark.django_db
def test_a_confirmation_for_an_oob_only_mapping_fails_closed(client, secondary_server):
    """A confirmation naming a host ID an OOB-only entry never had is refused."""
    device = make_device(
        "replace-oob-forged-device",
        librenms_cf={"secondary": {"oob": {"id": 54301, "type": "bmc"}}},
    )
    user = make_superuser("identity-replacement-oob-forged")
    client.force_login(user)
    token = sign_identity_replacement_intent(
        IdentityReplacementIntent(
            user_pk=user.pk,
            object_type="device",
            object_pk=device.pk,
            server_key="secondary",
            action="link",
            force=False,
            current_host_id=54301,
            proposed_host_id=54401,
        )
    )

    response = _post_confirmation(client, 54401, token, server=_serve_secondary(secondary_server, 54401, device.name))

    assert response.status_code == 200
    assert b"no longer matches" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": {"oob": {"id": 54301, "type": "bmc"}}}


@pytest.mark.django_db
def test_an_unsupported_object_type_is_refused(client, secondary_server):
    """An object type the view cannot resolve fails closed instead of defaulting to Device."""
    device = make_device("replace-bad-type-device", librenms_cf={"secondary": 54501})
    client.force_login(make_superuser("identity-replacement-bad-type"))

    response = _post_action(
        client,
        54601,
        device,
        "link",
        server=_serve_secondary(secondary_server, 54601, device.name),
        object_type="module",
    )

    assert response.status_code == 200
    assert b"Unsupported object type" in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 54501}


@pytest.mark.django_db
def test_replacement_requires_change_permission_on_the_target(client, secondary_server):
    """A user without change permission on the object cannot replace its identity."""
    from dcim.models import Device

    device = make_device("replace-unauthorized-device", librenms_cf={"secondary": 52901})
    client.force_login(make_user_with_perms("identity-replacement-viewer", [("view", Device)]))

    response = _post_action(
        client, 53001, device, "link", server=_serve_secondary(secondary_server, 53001, device.name)
    )

    assert response.status_code == 200
    assert INTENT_FIELD.encode() not in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 52901}


@pytest.mark.django_db
def test_replacement_is_scoped_by_a_constrained_change_grant(client, secondary_server):
    """A constrained change grant that excludes the target refuses the replacement."""
    from dcim.models import Device

    device = make_device("replace-out-of-scope-device", librenms_cf={"secondary": 53101})
    in_scope = make_device("replace-in-scope-device")
    user = make_user_with_perms(
        "identity-replacement-constrained",
        [("view", Device), ("change", Device)],
        constraints={"pk": in_scope.pk},
    )
    client.force_login(user)

    response = _post_action(
        client, 53201, device, "link", server=_serve_secondary(secondary_server, 53201, device.name)
    )

    assert response.status_code == 200
    assert INTENT_FIELD.encode() not in response.content
    device.refresh_from_db()
    assert device.custom_field_data["librenms_id"] == {"secondary": 53101}


def test_the_signed_schema_matches_the_intent_dataclass():
    """The validated field names cannot drift from the dataclass they describe."""
    from dataclasses import fields

    from netbox_librenms_plugin.identity_replacement import INTENT_FIELD_TYPES

    assert set(INTENT_FIELD_TYPES) == {field.name for field in fields(IdentityReplacementIntent)}

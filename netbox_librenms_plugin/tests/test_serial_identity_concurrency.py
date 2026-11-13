"""Two real database sessions that write rule-equivalent serials, and the events of a failed inner install."""

from functools import partial
from threading import Barrier

import pytest
from django.db import IntegrityError, close_old_connections, connection

from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    install_module,
    make_device,
    make_device_with_module_bays,
    make_module_bay,
    make_module_type,
    make_superuser,
    run_in_threads,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.view_test_helpers import (
    rendered_module_form,
    render_module_tab,
    trusted_module_inventory_payload,
)

SERVER_KEY = "default"


@pytest.fixture
def librenms_server(settings, monkeypatch):
    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        configure_librenms_servers(
            settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "race-token", "cache_timeout": 0}}
        )
        yield server


def _seeded_rule():
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
    NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")


def _post_serial_update(device_pk, user_pk, barrier):
    """POST Update Serial for one device in this thread's own database session; wait at the first identity lock."""
    from django.contrib.auth import get_user_model
    from django.test import Client
    from django.urls import reverse

    waited = False

    def meet_at_the_lock(execute, sql, params, many, context):
        nonlocal waited
        if "pg_advisory_xact_lock" in sql and not waited:
            waited = True
            barrier.wait(timeout=10)
        return execute(sql, params, many, context)

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
        client = Client()
        client.force_login(get_user_model().objects.get(pk=user_pk))
        with connection.execute_wrapper(meet_at_the_lock):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": device_pk}),
                {"server_key": SERVER_KEY},
            )
        return response.status_code
    finally:
        connection.close()


@transactional_db_with_all_apps()
def test_two_sessions_that_assign_rule_equivalent_serials_store_one_of_them(librenms_server):
    """
    Both sessions reach the identity lock together. Their serials share a key, so they share the lock.

    The second session resolves after the first commits, sees the equivalent serial, and refuses.
    """
    from dcim.models import Device

    _seeded_rule()
    first = seed_mapping(make_device("race-serial-first"), SERVER_KEY, own=9801)
    second = seed_mapping(make_device("race-serial-second"), SERVER_KEY, own=9802)
    librenms_server.device_info_response(device_id=9801, hostname=first.name, serial="S/N RACE-SERIAL")
    librenms_server.device_info_response(device_id=9802, hostname=second.name, serial="RACE-SERIAL")
    user = make_superuser("race-serial-user")
    barrier = Barrier(2, timeout=10)

    statuses = run_in_threads(
        partial(_post_serial_update, first.pk, user.pk, barrier),
        partial(_post_serial_update, second.pk, user.pk, barrier),
        barriers=(barrier,),
        timeout=60,
    )

    assert statuses == [302, 302]
    stored = sorted(Device.objects.filter(pk__in=[first.pk, second.pk]).values_list("serial", flat=True))
    assert stored in (["", "S/N RACE-SERIAL"], ["", "RACE-SERIAL"]), stored


@pytest.mark.django_db
def test_a_failed_install_after_its_save_leaves_events_only_for_the_committed_sibling(
    client, monkeypatch, flushed_events
):
    """The second install saves its module, then fails; its savepoint drops the module and the module's events."""
    from dcim.models import InterfaceTemplate, Module
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.views.mixins import CacheMixin
    from netbox_librenms_plugin.views.sync import modules as module_views

    device = make_device("event-safe-branch")
    for number in (1, 2):
        make_module_bay(device, f"Slot {number}", position=str(number))
    module_type = make_module_type("EVENT-SAFE-CARD", manufacturer=device.device_type.manufacturer)
    InterfaceTemplate.objects.create(module_type=module_type, name="Ethernet{module}/1", type="10gbase-x-sfpp")
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Carrier",
            "entPhysicalModelName": "EVENT-SAFE-CARRIER",
            "entPhysicalContainedIn": 0,
        },
        *(
            {
                "entPhysicalIndex": number + 1,
                "entPhysicalContainedIn": 1,
                "entPhysicalClass": "module",
                "entPhysicalName": f"Slot {number}",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": f"EVENT-SN{number}",
            }
            for number in (1, 2)
        ),
    ]
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY), trusted_module_inventory_payload(device, rows)
    )
    user = make_superuser("event-safe-branch-user")
    _url, data = rendered_module_form(render_module_tab(device, user=user), "install-branch", 1)
    normalize = module_views._normalize_module_interface_names_for_vc_member
    saved = []

    def fail_after_the_second_save(target, module, *args, **kwargs):
        saved.append(module.pk)
        if module.serial == "EVENT-SN2":
            # The module row and its interface are written; the failure comes after them.
            raise IntegrityError("injected failure after the second module save")
        return normalize(target, module, *args, **kwargs)

    monkeypatch.setattr(module_views, "_normalize_module_interface_names_for_vc_member", fail_after_the_second_save)
    client.force_login(user)

    response = client.post(reverse("plugins:netbox_librenms_plugin:install_branch", args=[device.pk]), data)

    assert response.status_code == 302
    (kept,) = Module.objects.filter(device=device)
    assert kept.serial == "EVENT-SN1"
    rolled_back = [pk for pk in saved if pk != kept.pk]
    assert len(rolled_back) == 1 and not Module.objects.filter(pk__in=rolled_back).exists()
    assert ("module", kept.pk) in flushed_events
    assert ("module", rolled_back[0]) not in flushed_events
    kept_interfaces = set(kept.interfaces.values_list("pk", flat=True))
    assert {pk for model, pk in flushed_events if model == "interface"} <= kept_interfaces


@pytest.mark.django_db
def test_a_legacy_module_serial_heals_to_the_librenms_text(client):
    """An update of a module whose stored rule output names the LibreNMS hardware writes the LibreNMS text."""
    from dcim.models import Module
    from django.core.cache import cache

    from netbox_librenms_plugin.views.mixins import CacheMixin

    _seeded_rule()
    device = make_device_with_module_bays("module-heal", ["Slot 1"])
    installed = install_module(device, "Slot 1", "HEAL-CARD", serial="HEAL-SN1")
    rows = [
        {
            "entPhysicalIndex": 7,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 1",
            "entPhysicalModelName": installed.module_type.model,
            "entPhysicalSerialNum": "S/N HEAL-SN1",
            "entPhysicalContainedIn": 0,
        }
    ]
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY), trusted_module_inventory_payload(device, rows)
    )
    user = make_superuser("module-heal-user")
    # The row matches, and the page offers the update because the stored form differs.
    url, data = rendered_module_form(render_module_tab(device, user=user), "update-module-serial", 7)
    client.force_login(user)

    response = client.post(url, data)

    assert response.status_code == 302
    assert Module.objects.get(pk=installed.pk).serial == "S/N HEAL-SN1"

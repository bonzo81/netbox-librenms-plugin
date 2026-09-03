"""Real redirect-behavior guard: server-scoped redirects must carry ``server_key``.

Module sync / interface / cable / VLAN / IP actions are server-scoped: after a POST (or an HTMX
refresh) on a non-default LibreNMS server, the follow-up URL must carry ``server_key`` so the user
returns to the same server's tab and cache namespace. This has been flagged across multiple reviews,
one ``?tab=`` builder at a time. Rather than grepping source text (which passes even when
``server_key`` is wired to the wrong param or sits in a comment), these tests drive the actual
redirect builders and assert the resolved ``server_key`` survives into the built URL. A lightweight
structural canary keeps the suite honest if a builder is renamed or removed.
"""

from pathlib import Path

import pytest
from django.test import RequestFactory

import netbox_librenms_plugin.views as views_pkg
from netbox_librenms_plugin.tests.conftest import make_device

# Per the codebase convention, every redirect/tab URL built in these packages is server-scoped.
SCOPED_SUBPACKAGES = ("sync", "base", "object_sync")


def _scoped_python_files():
    root = Path(views_pkg.__file__).parent
    for sub in SCOPED_SUBPACKAGES:
        yield from sorted((root / sub).rglob("*.py"))


@pytest.mark.django_db
def test_ip_redirect_url_propagates_server_key():
    """SyncIPAddressesView.get_ip_tab_url appends the resolved server_key to the IP tab URL."""
    from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

    device = make_device("redir-ip")
    view = object.__new__(SyncIPAddressesView)
    view._post_server_key = "prod"  # the POST-resolved server the user acted on

    url = view.get_ip_tab_url(device)

    assert "tab=ipaddresses" in url
    assert "server_key=prod" in url


@pytest.mark.django_db
def test_vlan_redirect_url_propagates_server_key():
    """SyncVLANsView._redirect carries server_key on the VLAN tab redirect."""
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    device = make_device("redir-vlan")
    view = object.__new__(SyncVLANsView)
    view._post_server_key = "prod"

    resp = view._redirect("device", device.pk)

    assert "tab=vlans" in resp.url
    assert "server_key=prod" in resp.url


def test_modules_redirect_response_propagates_server_key():
    """_modules_redirect_response carries server_key on a classic (non-HTMX) redirect."""
    from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

    request = RequestFactory().post("/")
    resp = _modules_redirect_response(request, "/plugins/librenms_plugin/x/", server_key="prod")

    assert "tab=modules" in resp.url
    assert "server_key=prod" in resp.url


@pytest.mark.django_db
def test_modules_action_fragment_keeps_the_server_key(client, settings):
    """The HTMX variant re-renders the module tab with the acted-on server_key still wired in."""
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        make_module_bay,
        make_module_type,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    configure_librenms_servers(
        settings, {"prod": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}}
    )
    device = make_device("redir-modules-fragment")
    bay = make_module_bay(device, "Redirect Bay")
    module_type = make_module_type("REDIRECT-CARD")
    cache.set(
        DeviceModuleTableView().get_cache_key(device, "inventory", server_key="prod"),
        trusted_module_inventory_payload(
            device,
            [
                {
                    "entPhysicalIndex": 8401,
                    "entPhysicalClass": "module",
                    "entPhysicalModelName": module_type.model,
                    "entPhysicalContainedIn": 0,
                    "entPhysicalName": bay.name,
                }
            ],
            server_key="prod",
            librenms_id=9401,
        ),
        300,
    )
    client.force_login(make_superuser("redir-modules-fragment-user"))

    resp = client.post(
        reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk}),
        {"server_key": "prod", "module_bay_id": str(bay.pk), "module_type_id": str(module_type.pk), "serial": ""},
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 200
    assert b'name="server_key" value="prod"' in resp.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "view_name",
    ["UpdateDeviceNameView", "UpdateDeviceSerialView", "UpdateDeviceTypeView", "UpdateDevicePlatformView"],
)
def test_field_sync_views_preserve_server_key_on_redirect(view_name):
    """The four device field-sync views rebind to the POSTed server, so their redirects must carry it.

    Each view resolves ``server_key`` via ``rebind_api_for_server`` up front; without preserving it
    on the redirect the page reloads scoped to the session/default server and the non-default tab
    context is lost. This drives the real ``post()`` to the shared "Device not found" early redirect
    (``server_key`` already resolved to 'prod') and asserts the follow-up URL carries it. Only
    ``build_librenms_api`` (the HTTP boundary) is stubbed — real request, real superuser permission
    gate, real ``_device_sync_redirect`` / ``redirect_with_server_key``.
    """
    from unittest.mock import MagicMock, patch

    from django.contrib.auth import get_user_model
    from django.contrib.messages.storage.cookie import CookieStorage

    from netbox_librenms_plugin.views.sync import device_fields

    user = get_user_model().objects.create(username=f"redir-{view_name}", is_superuser=True, is_active=True)
    device = make_device(f"redir-{view_name.lower()}")

    request = RequestFactory().post("/", {"server_key": "prod"})
    request.user = user
    request._messages = CookieStorage(request)  # messages.error() needs storage (session-free) on a bare request

    view = getattr(device_fields, view_name)()
    view.request = request  # require_all_permissions reads self.request

    prod_api = MagicMock(server_key="prod")
    prod_api.get_librenms_id.return_value = None  # → the shared "Device not found" early redirect
    with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api):
        resp = view.post(request, device.pk)

    assert resp.status_code in (301, 302), f"{view_name} did not redirect (perm gate?): {resp}"
    assert "server_key=prod" in resp.url, f"{view_name} dropped server_key on redirect: {resp.url}"


def test_scoped_tab_builders_exist():
    """Structural canary (not a behavioral assertion): the scoped view packages still contain
    ``?tab=`` redirect builders, so the behavioral tests above are pointed at a tree that actually
    builds tab URLs — a refactor that moves/removes them is noticed rather than silently leaving
    this file asserting nothing.
    """
    total = sum(path.read_text().count("?tab=") for path in _scoped_python_files())
    assert total > 0, f"Expected at least one '?tab=' builder under the scoped views, found {total}"

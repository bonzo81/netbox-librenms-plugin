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


def test_modules_redirect_response_htmx_propagates_server_key():
    """The HTMX variant carries server_key on the HX-Redirect header."""
    from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

    request = RequestFactory().post("/", HTTP_HX_REQUEST="true")
    resp = _modules_redirect_response(request, "/plugins/librenms_plugin/x/", server_key="prod")

    target = resp["HX-Redirect"]
    assert "tab=modules" in target
    assert "server_key=prod" in target


def test_scoped_tab_builders_exist():
    """Structural canary (not a behavioral assertion): the scoped view packages still contain
    ``?tab=`` redirect builders, so the behavioral tests above are pointed at a tree that actually
    builds tab URLs — a refactor that moves/removes them is noticed rather than silently leaving
    this file asserting nothing.
    """
    total = sum(path.read_text().count("?tab=") for path in _scoped_python_files())
    assert total > 0, f"Expected at least one '?tab=' builder under the scoped views, found {total}"

"""Request-level tests for cross-tab cache consistency."""

import json
from unittest.mock import patch

import pytest
from dcim.models import Cable, Module, ModuleBay, ModuleBayTemplate, ModuleType
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from ipam.models import IPAddress, VLAN
from requests import Response

from netbox_librenms_plugin.sync_cache import (
    _ACKNOWLEDGED_REVISIONS_SESSION_KEY as SYNC_CACHE_ACKNOWLEDGED_SESSION_KEY,
)
from netbox_librenms_plugin.sync_cache import (
    CacheMutationTransition,
    SyncCacheConsistency,
    SyncTab,
    SyncTabState,
    apply_request_cache_transition,
    claim_sync_subjects,
    schedule_request_cache_mutation,
    sync_cache_browser_contract,
    sync_subject_key,
)
from netbox_librenms_plugin.tests.conftest import (
    make_module_bay,
    make_module_type,
    configure_librenms_servers,
    configure_no_librenms_servers,
    ip_on,
    make_device,
    make_interface,
    make_superuser,
    make_vm,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView


def test_browser_contract_serializes_states_from_the_domain_enum():
    """The server contract must expose every state that a status response can emit."""
    contract = sync_cache_browser_contract((SyncTab.INTERFACES,))

    assert contract == {
        "tabs": {
            "interfaces": {
                "content_id": "interface-sync-content",
                "label": "Interface",
            }
        },
        "states": [state.value for state in SyncTabState],
    }


def _configure_servers(settings):
    configure_librenms_servers(
        settings,
        {
            "primary": {"librenms_url": "https://primary.example.com", "api_token": "test-token"},
            "secondary": {"librenms_url": "https://secondary.example.com", "api_token": "test-token"},
            "unmapped": {"librenms_url": "https://unmapped.example.com", "api_token": "test-token"},
        },
    )


def _configured_server_key(settings):
    servers = settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]
    return next(iter(servers))


def _cache_key(data_type, obj, server_key):
    from netbox_librenms_plugin.sync_cache import sync_snapshot_key

    return sync_snapshot_key(obj, data_type, server_key)


def _last_fetched_key(data_type, obj, server_key):
    from netbox_librenms_plugin.sync_cache import sync_last_fetched_key

    return sync_last_fetched_key(obj, data_type, server_key)


def _vlan_overrides_key(obj, server_key):
    from netbox_librenms_plugin.sync_cache import sync_vlan_overrides_key

    return sync_vlan_overrides_key(obj, server_key)


def _seed_snapshot(data_type, obj, server_key, payload=None):
    cache.set(_cache_key(data_type, obj, server_key), payload or {"snapshot": data_type}, timeout=300)


def _json_response(url, payload, status=200):
    """Return a concrete requests response for the LibreNMS HTTP boundary."""
    response = Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


def _device_info_response(url, device_id, name, hardware):
    return _json_response(
        url,
        {
            "status": "ok",
            "devices": [
                {
                    "device_id": device_id,
                    "sysName": name,
                    "hostname": name,
                    "hardware": hardware,
                }
            ],
        },
    )


def _opening_tag(html, element_id):
    """Return one rendered opening tag by its element ID."""
    marker = f'id="{element_id}"'
    position = html.index(marker)
    return html[html.rfind("<", 0, position) : html.index(">", position) + 1]


def _tab_classes(html, element_id):
    """Return the classes from one rendered tab link."""
    opening_tag = _opening_tag(html, element_id)
    assert 'class="' in opening_tag, f"{element_id} rendered without a class attribute: {opening_tag}"
    return opening_tag.split('class="', 1)[1].split('"', 1)[0].split()


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, "600", float("inf"), float("nan"), 0, -1],
)
def test_configured_cache_timeout_rejects_invalid_values(settings, invalid_timeout):
    """Invalid plugin values must fall back to a finite positive cache lifetime."""
    from netbox_librenms_plugin.librenms_api import DEFAULT_CACHE_TIMEOUT, configured_cache_timeout

    _configure_servers(settings)
    # Pin a valid global so only the rejected per-server value can produce the default.
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["cache_timeout"] = 999
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["cache_timeout"] = invalid_timeout

    assert configured_cache_timeout("primary") == DEFAULT_CACHE_TIMEOUT


def test_configured_cache_timeout_inherits_the_global_value(settings):
    """A server that omits the timeout must read the global plugin value."""
    from netbox_librenms_plugin.librenms_api import configured_cache_timeout

    _configure_servers(settings)
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["cache_timeout"] = 123
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["cache_timeout"] = None

    assert configured_cache_timeout("primary") == 123


@pytest.mark.parametrize("invalid_global", [True, "600", float("nan"), 0, -1])
def test_configured_cache_timeout_rejects_an_invalid_global_value(settings, invalid_global):
    """An omitted server timeout must still reject an unusable global value."""
    from netbox_librenms_plugin.librenms_api import DEFAULT_CACHE_TIMEOUT, configured_cache_timeout

    _configure_servers(settings)
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["cache_timeout"] = invalid_global
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["cache_timeout"] = None

    assert configured_cache_timeout("primary") == DEFAULT_CACHE_TIMEOUT


def test_configured_cache_timeout_clamps_positive_fractions(settings):
    """A positive fractional timeout must not become immediate expiry."""
    from netbox_librenms_plugin.librenms_api import configured_cache_timeout

    _configure_servers(settings)
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["cache_timeout"] = 0.5

    assert configured_cache_timeout("primary") == 1


def test_one_response_preserves_both_cache_mutation_results():
    """A response event must retain revisions and cleanup failures from both owners."""
    request = RequestFactory().post("/sync", HTTP_HX_REQUEST="true")
    donor = CacheMutationTransition(
        transition_id="donor-transition",
        removed_tabs={("primary", SyncTab.IP_ADDRESSES)},
        revisions={("primary", SyncTab.IP_ADDRESSES): "donor-revision"},
        source_tab=SyncTab.IP_ADDRESSES,
        completed=True,
    )
    winner = CacheMutationTransition(
        transition_id="winner-transition",
        removed_tabs={("primary", SyncTab.INTERFACES)},
        revisions={("primary", SyncTab.IP_ADDRESSES): "winner-revision"},
        cleanup_tabs={SyncTab.CABLES},
        source_tab=SyncTab.IP_ADDRESSES,
        error="winner cleanup failed",
        completed=True,
    )
    request._librenms_cache_transitions = [donor, winner]

    response = apply_request_cache_transition(request, HttpResponse())

    payload = json.loads(response["X-LibreNMS-Cache-Transition"])
    assert payload["cleanup_failed"] is True
    assert payload["tabs"] == ["interfaces", "ipaddresses"]
    assert payload["cleanup_tabs"] == ["cables"]
    assert payload["source_tabs"] == ["ipaddresses"]
    assert set(payload["revisions"].values()) == {"donor-revision", "winner-revision"}
    trigger = json.loads(response["HX-Trigger"])
    assert trigger["librenmsCacheChanged"]["cleanup_failed"] is True


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "sync-cache-without-wildcard-operations",
        }
    }
)
def test_mutation_succeeds_when_cache_backend_has_no_wildcard_api(
    settings,
    django_capture_on_commit_callbacks,
):
    """Exact snapshot cleanup must not fail on a standard cache backend."""
    _configure_servers(settings)
    device = make_device("cache-standard-backend", librenms_cf={"primary": {"id": 6401}})
    coordinator = SyncCacheConsistency(device)
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("links", device, "primary")
    cache.set(f"{_cache_key('links', device, 'primary')}:manual-remote:row", 1, timeout=300)

    with django_capture_on_commit_callbacks(execute=True):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is None
    assert cache.get(_cache_key("links", device, "primary")) is None
    # Without a wildcard API the snapshot-bound picks cannot be enumerated; they expire on their own.
    assert cache.get(f"{_cache_key('links', device, 'primary')}:manual-remote:row") == 1


@pytest.mark.django_db
def test_snapshot_cleanup_removes_snapshot_bound_cable_picks(
    settings,
    django_capture_on_commit_callbacks,
):
    """The configured backend must drop the picks bound to a discarded cable snapshot."""
    _configure_servers(settings)
    device = make_device("cache-cable-pick-cleanup", librenms_cf={"primary": {"id": 6406}})
    coordinator = SyncCacheConsistency(device)
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("links", device, "primary")
    pick_key = f"{_cache_key('links', device, 'primary')}:manual-remote:row"
    cache.set(pick_key, 1, timeout=300)

    with django_capture_on_commit_callbacks(execute=True):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is None
    assert cache.get(_cache_key("links", device, "primary")) is None
    assert cache.get(pick_key) is None


@pytest.mark.django_db
def test_virtual_machine_unsupported_cache_fragment_returns_404(client, settings):
    """A valid tab name that is unsupported for VMs must fail closed without a key error."""
    _configure_servers(settings)
    vm = make_vm("cache-unsupported-fragment")
    set_librenms_device_id(vm, 6402, "primary")
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("cache-unsupported-fragment-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "virtualmachine", "pk": vm.pk, "tab": SyncTab.CABLES.value},
    )

    response = client.get(url, {"server_key": "primary"})

    assert response.status_code == 404


@pytest.mark.django_db
def test_cable_cache_fragment_does_not_rewrite_the_snapshot(client, settings):
    """A cache-only cable fragment may enrich its response but must not rewrite shared state."""
    _configure_servers(settings)
    local_device = make_device("cache-only-cable-local", librenms_cf={"primary": {"id": 6403}})
    local = make_interface(local_device, "Ethernet1")
    remote_device = make_device("cache-only-cable-remote", librenms_cf={"primary": {"id": 6404}})
    remote = make_interface(remote_device, "Ethernet2")
    set_librenms_device_id(local, 7403, "primary")
    set_librenms_device_id(remote, 7404, "primary")
    local.save(update_fields=["custom_field_data"])
    remote.save(update_fields=["custom_field_data"])
    payload = {
        "links": [
            {
                "local_port_id": 7403,
                "local_port": local.name,
                "remote_port_id": 7404,
                "remote_port": remote.name,
                "remote_hostname": remote_device.name,
                "remote_device_id": 6404,
            }
        ]
    }
    cache_key = _cache_key("links", local_device, "primary")
    cache.set(cache_key, payload, timeout=300)
    client.force_login(make_superuser("cache-only-cable-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": local_device.pk, "tab": SyncTab.CABLES.value},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The cable fragment contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"server_key": "primary"})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert cache.get(cache_key) == payload


@pytest.mark.django_db
def test_module_cache_fragment_does_not_discover_a_missing_host_id(client, settings):
    """An OOB-mapped module fragment must not discover a missing host identity."""
    _configure_servers(settings)
    device = make_device(
        "cache-only-module",
        librenms_cf={"primary": {"oob": {"id": 6405}}},
    )
    cache.set(
        _cache_key("inventory", device, "primary"),
        {"inventory": [], "librenms_id": 9999, "oob_librenms_id": 6405},
        timeout=300,
    )
    client.force_login(make_superuser("cache-only-module-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.MODULES.value},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The module fragment contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"server_key": "primary"})

    requests_get.assert_not_called()
    assert response.status_code == 200


@pytest.mark.django_db
def test_cold_sync_page_load_fetches_librenms_status(client, settings):
    """The status card must load even when the active tab has no snapshot."""
    _configure_servers(settings)
    device = make_device("cache-cold-status", librenms_cf={"primary": {"id": 7401}})
    client.force_login(make_superuser("cache-cold-status-user"))
    requested_urls = []

    def librenms_response(url, **_kwargs):
        requested_urls.append(url)
        if url.endswith("/api/v0/devices/7401"):
            return _device_info_response(url, 7401, device.name, "Status Hardware 7401")
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})

    assert response.status_code == 200
    assert requested_urls == ["https://primary.example.com/api/v0/devices/7401"]
    assert b"Status Hardware 7401" in response.content
    assert b"Details unavailable" not in response.content
    tab_classes = _tab_classes(response.content.decode(), "interfaces-tab")
    assert "sync-cache-ready" not in tab_classes
    assert "sync-cache-unavailable" not in tab_classes


@pytest.mark.django_db
class TestSyncPageRenderCoordinator:
    """A page render must reuse its cache consistency inputs."""

    def test_sync_page_render_reuses_one_cache_consistency_coordinator(self, client, settings, monkeypatch):
        """One page render must reuse its cache coordinator and applicable-tabs tuple."""
        from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
        from netbox_librenms_plugin.views.base import librenms_sync_view as sync_view_module

        _configure_servers(settings)
        device = make_device("cache-single-render-coordinator", librenms_cf={"primary": {"id": 7408}})
        client.force_login(make_superuser("cache-single-render-coordinator-user"))
        constructed_subjects = []
        direct_applicable_tabs_subjects = []
        original_consistency = sync_view_module.SyncCacheConsistency

        class TrackingSyncCacheConsistency(original_consistency):
            def __init__(self, subject):
                constructed_subjects.append(subject.pk)
                self._status_in_progress = False
                super().__init__(subject)

            def applicable_tabs(self):
                if not self._status_in_progress:
                    direct_applicable_tabs_subjects.append(self.subject.pk)
                return super().applicable_tabs()

            def status_for_request(self, *args, **kwargs):
                self._status_in_progress = True
                try:
                    return super().status_for_request(*args, **kwargs)
                finally:
                    self._status_in_progress = False

        monkeypatch.setattr(sync_view_module, "SyncCacheConsistency", TrackingSyncCacheConsistency)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

        with librenms_mock_server() as server:
            settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["librenms_url"] = server.url
            server.device_info_response(device_id=7408, hostname=device.name, hardware="Render Hardware")
            response = client.get(url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})

        assert response.status_code == 200
        assert constructed_subjects == [device.pk]
        assert direct_applicable_tabs_subjects == [device.pk]


@pytest.mark.django_db
def test_mapped_device_missing_from_librenms_renders_danger_status(client, settings):
    """A stale stored LibreNMS ID must not make a failed lookup look successful."""
    _configure_servers(settings)
    device = make_device("cache-missing-status", librenms_cf={"primary": {"id": 7402}})
    client.force_login(make_superuser("cache-missing-status-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        return_value=_json_response(
            "https://primary.example.com/api/v0/devices/7402",
            {"status": "error", "message": "Device not found"},
            status=404,
        ),
    ):
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})

    assert response.status_code == 200
    html = response.content.decode()
    status_position = html.index("Not found")
    span_position = html.rfind("<span", 0, status_position)
    status_tag = html[span_position : html.index(">", span_position) + 1]
    assert "text-danger" in status_tag, status_tag
    assert "text-success" not in status_tag, status_tag


@pytest.mark.django_db
def test_committed_interface_sync_invalidates_only_mapped_page_and_shared_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A VC mutation must keep its source and clear only mapped page/shared namespaces."""
    _configure_servers(settings)
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["cache_timeout"] = 60
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["secondary"]["cache_timeout"] = 600
    _chassis, (page_device, secondary_owner, sibling) = make_virtual_chassis_members("cache-scope", count=3)
    page_device.custom_field_data["librenms_id"] = {"primary": {"id": 41}}
    page_device.save(update_fields=["custom_field_data"])
    secondary_owner.custom_field_data["librenms_id"] = {"secondary": {"id": 42}}
    secondary_owner.save(update_fields=["custom_field_data"])

    source_payload = {
        "ports": [
            {
                "port_id": 7001,
                "ifName": "Ethernet1/1",
                "ifDescr": "Ethernet1/1",
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", page_device, "primary", source_payload)
    cache.set(_last_fetched_key("ports", page_device, "primary"), "source-time", timeout=300)
    cache.set(_vlan_overrides_key(page_device, "primary"), {"100": "1"}, timeout=300)
    for data_type in ("links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, page_device, "primary")
    cache.set(_last_fetched_key("vlans", page_device, "primary"), "vlan-time", timeout=300)

    for data_type in ("ports", "links", "inventory"):
        _seed_snapshot(data_type, secondary_owner, "secondary")
        _seed_snapshot(data_type, page_device, "secondary")
    for data_type in ("ip_addresses", "vlans"):
        _seed_snapshot(data_type, page_device, "secondary")
        _seed_snapshot(data_type, sibling, "secondary")
    cache.set(_last_fetched_key("ports", secondary_owner, "secondary"), "secondary-time", timeout=300)
    cache.set(_vlan_overrides_key(secondary_owner, "secondary"), {"200": "2"}, timeout=300)
    cache.set(_last_fetched_key("vlans", page_device, "secondary"), "secondary-vlan-time", timeout=300)

    for data_type in ("ports", "links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, page_device, "unmapped")

    client.force_login(make_superuser("cache-scope-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": page_device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            sync_url,
            {
                "server_key": "primary",
                "interface_name_field": "ifName",
                "select": "7001",
                "exclude_columns": "vlans",
            },
        )

    assert response.status_code == 302
    assert cache.get(_cache_key("ports", page_device, "primary")) == source_payload
    assert cache.get(_last_fetched_key("ports", page_device, "primary")) == "source-time"
    assert cache.get(_vlan_overrides_key(page_device, "primary")) == {"100": "1"}
    for data_type in ("links", "ip_addresses", "inventory", "vlans"):
        assert cache.get(_cache_key(data_type, page_device, "primary")) is None

    for data_type in ("ports", "links", "inventory"):
        assert cache.get(_cache_key(data_type, secondary_owner, "secondary")) is None
        assert cache.get(_cache_key(data_type, page_device, "secondary")) is None
    for data_type in ("ip_addresses", "vlans"):
        assert cache.get(_cache_key(data_type, page_device, "secondary")) is None
        assert cache.get(_cache_key(data_type, sibling, "secondary")) is not None

    for data_type in ("ports", "links", "ip_addresses", "inventory", "vlans"):
        assert cache.get(_cache_key(data_type, page_device, "unmapped")) is not None

    coordinator = SyncCacheConsistency(page_device)
    primary_state = cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary"))
    assert primary_state is not None
    assert primary_state["state"] == "locally_changed"
    assert primary_state["source_tab"] == "interfaces"
    assert primary_state["actor_id"] is not None
    secondary_state = cache.get(coordinator.state_key(SyncTab.INTERFACES, "secondary"))
    assert secondary_state is not None
    assert secondary_state["state"] == "invalidated"
    assert secondary_state["revision"] == primary_state["revision"]
    assert 0 < cache.ttl(coordinator.state_key(SyncTab.INTERFACES, "primary")) <= 60
    assert 60 < cache.ttl(coordinator.state_key(SyncTab.INTERFACES, "secondary")) <= 600

    status_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_status",
        kwargs={"object_type": "device", "pk": page_device.pk},
    )
    same_user_status = client.get(status_url, {"server_key": "primary"})
    assert same_user_status.status_code == 200
    source_status = same_user_status.json()["tabs"]["interfaces"]
    assert source_status["state"] == "locally_changed"
    assert source_status["snapshot_available"] is True
    assert source_status["same_user"] is True
    ip_status = same_user_status.json()["tabs"]["ipaddresses"]
    assert ip_status["state"] == "invalidated"
    assert ip_status["snapshot_available"] is False
    assert ip_status["same_user"] is True

    other_user = get_user_model().objects.create_user(
        username="cache-scope-other-user",
        is_superuser=True,
        is_active=True,
    )
    client.force_login(other_user)
    other_user_status = client.get(status_url, {"server_key": "primary"})
    assert other_user_status.status_code == 200
    assert other_user_status.json()["tabs"]["ipaddresses"]["same_user"] is False


@pytest.mark.django_db
def test_a_sibling_refresh_clears_the_shared_tab_block_on_every_member(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A shared-tab refresh on one VC member must unblock that tab on its siblings."""
    _configure_servers(settings)
    _chassis, (owner, sibling) = make_virtual_chassis_members("cache-shared-state")
    owner.custom_field_data["librenms_id"] = {"primary": {"id": 61}}
    owner.save(update_fields=["custom_field_data"])
    remote_device = make_device("cache-shared-state-remote", librenms_cf={"primary": {"id": 62}})
    local = make_interface(sibling, "Ethernet1", iface_type="1000base-t")
    remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
    set_librenms_device_id(local, 7481, "primary")
    set_librenms_device_id(remote, 7482, "primary")
    local.save(update_fields=["custom_field_data"])
    remote.save(update_fields=["custom_field_data"])

    ports_payload = {
        "ports": [
            {
                "port_id": 7481,
                "ifName": local.name,
                "ifDescr": local.name,
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", owner, "primary", ports_payload)
    _seed_snapshot("links", owner, "primary")
    client.force_login(make_superuser("cache-shared-state-user"))

    # The sibling's own sync clears the shared cable snapshot and blocks its cable tab.
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": sibling.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        assert (
            client.post(
                sync_url,
                {
                    "server_key": "primary",
                    "interface_name_field": "ifName",
                    "select": "7481",
                    "exclude_columns": "vlans",
                },
            ).status_code
            == 302
        )
    assert cache.get(_cache_key("links", owner, "primary")) is None

    status_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_status",
        kwargs={"object_type": "device", "pk": sibling.pk},
    )
    blocked = client.get(status_url, {"server_key": "primary"}).json()["tabs"]["cables"]
    assert blocked["state"] == "invalidated"

    def librenms_response(url, **_kwargs):
        if url.endswith("/api/v0/devices/61/links"):
            return _json_response(
                url,
                {
                    "status": "ok",
                    "links": [
                        {
                            "local_port_id": 7481,
                            "local_port": local.name,
                            "remote_port_id": 7482,
                            "remote_port": remote.name,
                            "remote_hostname": remote_device.name,
                            "remote_device_id": 62,
                        }
                    ],
                },
            )
        if url.endswith("/api/v0/devices/61/ports"):
            return _json_response(url, {"status": "ok", "ports": ports_payload["ports"]})
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    # The owner refreshes the shared snapshot both members read.
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", kwargs={"pk": owner.pk})
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        assert client.post(refresh_url, {"server_key": "primary"}, HTTP_HX_REQUEST="true").status_code == 200
    assert cache.get(_cache_key("links", owner, "primary")) is not None

    unblocked = client.get(status_url, {"server_key": "primary"}).json()["tabs"]["cables"]
    assert unblocked["state"] == "ready"
    assert unblocked["snapshot_available"] is True


@pytest.mark.django_db
def test_fully_skipped_interface_request_preserves_all_snapshots(client, settings, django_capture_on_commit_callbacks):
    """A request that writes nothing must not invalidate another tab."""
    _configure_servers(settings)
    _chassis, (device, _sibling) = make_virtual_chassis_members("cache-noop", count=2)
    device.custom_field_data["librenms_id"] = {"primary": {"id": 51}}
    device.save(update_fields=["custom_field_data"])
    source_payload = {"ports": [], "port_stack_relationships": {}}
    ip_payload = {"ip_addresses": [{"ip_with_mask": "198.18.20.10/24"}]}
    _seed_snapshot("ports", device, "primary", source_payload)
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)

    client.force_login(make_superuser("cache-noop-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            sync_url,
            {
                "server_key": "primary",
                "interface_name_field": "ifName",
            },
        )

    assert response.status_code == 302
    assert cache.get(_cache_key("ports", device, "primary")) == source_payload
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    coordinator = SyncCacheConsistency(device)
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_selected_interface_that_already_matches_preserves_other_snapshots(
    client, settings, django_capture_on_commit_callbacks
):
    """A selected row with no semantic change must not clear another tab."""
    _configure_servers(settings)
    device = make_device("cache-interface-unchanged", librenms_cf={"primary": {"id": 52}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"primary": 7002}
    interface.save(update_fields=["custom_field_data"])
    source_payload = {
        "ports": [{"port_id": 7002, "ifName": interface.name}],
        "port_stack_relationships": {},
    }
    ip_payload = {"ip_addresses": [{"ip_with_mask": "198.18.20.11/24"}]}
    _seed_snapshot("ports", device, "primary", source_payload)
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)

    client.force_login(make_superuser("cache-interface-unchanged-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            sync_url,
            {
                "server_key": "primary",
                "interface_name_field": "ifName",
                "select": "7002",
                "exclude_columns": ["name", "type", "speed", "description", "mtu", "enabled", "mac_address", "vlans"],
            },
        )

    assert response.status_code == 302
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    coordinator = SyncCacheConsistency(device)
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_ip_sync_invalidates_other_tabs_after_creating_an_address(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed IP row must retain its source snapshot and clear other tabs."""
    _configure_servers(settings)
    settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"]["primary"]["cache_timeout"] = 60
    device = make_device("cache-ip-writer", librenms_cf={"primary": {"id": 61}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"primary": 7101}
    interface.save(update_fields=["custom_field_data"])
    address = "198.18.61.10/24"
    ip_payload = {
        "ip_addresses": [
            {
                "ip_with_mask": address,
                "port_id": 7101,
                "interface_name": interface.name,
            }
        ]
    }
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)
    _seed_snapshot("ports", device, "primary")

    client.force_login(make_superuser("cache-ip-writer-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {"server_key": "primary", "select": address, f"vrf_{address}": ""},
        )

    assert response.status_code == 302
    assert IPAddress.objects.filter(address=address, assigned_object_id=interface.pk).exists()
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    assert cache.get(_cache_key("ports", device, "primary")) is None
    state_key = SyncCacheConsistency(device).state_key(SyncTab.IP_ADDRESSES, "primary")
    assert 0 < cache.ttl(state_key) <= 60


@pytest.mark.django_db
def test_partial_ip_commit_invalidates_other_tabs_before_conflict_confirmation(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A safe committed row must invalidate stale tabs when another row needs confirmation."""
    _configure_servers(settings)
    device = make_device("cache-ip-partial-conflict", librenms_cf={"primary": {"id": 612}})
    conflict_target = make_interface(device, "Ethernet1", iface_type="1000base-t")
    set_librenms_device_id(conflict_target, 7121, "primary")
    conflict_target.save(update_fields=["custom_field_data"])
    safe_target = make_interface(device, "Ethernet2", iface_type="1000base-t")
    set_librenms_device_id(safe_target, 7122, "primary")
    safe_target.save(update_fields=["custom_field_data"])
    current = make_interface(device, "Ethernet3", iface_type="1000base-t")
    conflict_address = "198.18.62.10/24"
    safe_address = "198.18.62.11/24"
    IPAddress.objects.create(address=conflict_address, assigned_object=current)
    ip_payload = {
        "ip_addresses": [
            {
                "ip_with_mask": conflict_address,
                "ip_address": "198.18.62.10",
                "prefix_length": 24,
                "port_id": 7121,
                "interface": conflict_target.name,
            },
            {
                "ip_with_mask": safe_address,
                "ip_address": "198.18.62.11",
                "prefix_length": 24,
                "port_id": 7122,
                "interface": safe_target.name,
            },
        ],
        "ports_by_id": {
            7121: {"port_id": 7121, "ifName": conflict_target.name, "ifDescr": conflict_target.name},
            7122: {"port_id": 7122, "ifName": safe_target.name, "ifDescr": safe_target.name},
        },
        "interface_name_field": "ifName",
        "mgmt_ip": None,
    }
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-ip-partial-conflict-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "select": [conflict_address, safe_address],
                f"vrf_{conflict_address}": "",
                f"vrf_{safe_address}": "",
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b"Confirm IP address changes" in response.content
    assert IPAddress.objects.get(address=safe_address).assigned_object == safe_target
    assert IPAddress.objects.get(address=conflict_address).assigned_object == current
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    assert cache.get(_cache_key("ports", device, "primary")) is None


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_htmx_mutation_does_not_repeat_cache_notice_on_next_navigation(client, settings):
    """An HTMX mutation must report its cache transition only in the current response."""
    _configure_servers(settings)
    device = make_device("cache-parent-immediate-notice", librenms_cf={"primary": {"id": 611}})
    parent = make_interface(device, "Ethernet1", iface_type="1000base-t")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    set_librenms_device_id(parent, 7111, "primary")
    set_librenms_device_id(child, 7112, "primary")
    parent.save(update_fields=["custom_field_data"])
    child.save(update_fields=["custom_field_data"])
    ports_payload = {
        "ports": [
            {"port_id": 7111, "ifName": parent.name},
            {"port_id": 7112, "ifName": child.name},
        ],
        "port_stack_relationships": {
            "lag_members": {},
            "sub_interfaces": {7112: 7111},
        },
    }
    _seed_snapshot("ports", device, "primary", ports_payload)
    _seed_snapshot("ip_addresses", device, "primary")
    client.force_login(make_superuser("cache-parent-single-notice-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_interface_parent",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    response = client.post(
        sync_url,
        {
            "server_key": "primary",
            "port_id": "7112",
            "parent_port_id": "7111",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert "librenmsCacheChanged" in response["HX-Trigger"]
    child.refresh_from_db()
    assert child.parent == parent

    next_page = client.get(device.get_absolute_url())
    assert next_page.status_code == 200
    assert b"Other sync tabs were cleared" not in next_page.content


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_same_page_htmx_module_action_queues_the_cache_notice_for_the_reload(client, settings):
    """A module action that answers HX-Refresh must queue its cache notice for the reloaded page."""
    _configure_servers(settings)
    device = make_device("cache-modules-refresh-notice", librenms_cf={"primary": {"id": 612}})
    bay = make_module_bay(device, "Refresh Bay")
    module_type = make_module_type("REFRESH-CARD")
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-modules-refresh-user"))
    sync_page = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk})
    install_url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})

    response = client.post(
        install_url,
        {"server_key": "primary", "module_bay_id": bay.pk, "module_type_id": module_type.pk, "serial": "REFRESH-1"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL=f"http://testserver{sync_page}?tab=modules&server_key=primary#librenms-module-table",
    )

    assert response.status_code == 204
    assert response["HX-Refresh"] == "true"
    assert "HX-Trigger" not in response
    assert Module.objects.filter(device=device, module_bay=bay).exists()
    assert cache.get(_cache_key("ports", device, "primary")) is None
    next_page = client.get(device.get_absolute_url())
    assert next_page.status_code == 200
    assert b"Other sync tabs were cleared" in next_page.content


@pytest.mark.django_db
def test_vlan_sync_invalidates_other_tabs_after_creating_a_vlan(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed VLAN row must publish the same cache transition as other writers."""
    _configure_servers(settings)
    device = make_device("cache-vlan-writer", librenms_cf={"primary": {"id": 62}})
    vlan_payload = [{"vlan_vlan": 3062, "vlan_name": "Cache Test VLAN"}]
    _seed_snapshot("vlans", device, "primary", vlan_payload)
    _seed_snapshot("links", device, "primary")

    client.force_login(make_superuser("cache-vlan-writer-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {"server_key": "primary", "action": "create_vlans", "select": "3062"},
        )

    assert response.status_code == 302
    assert VLAN.objects.filter(vid=3062, name="Cache Test VLAN").exists()
    assert cache.get(_cache_key("vlans", device, "primary")) == vlan_payload
    assert cache.get(_cache_key("links", device, "primary")) is None


@pytest.mark.django_db
def test_cable_sync_invalidates_other_tabs_after_creating_a_cable(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed cable must clear comparisons that depend on current topology."""
    _configure_servers(settings)
    device = make_device("cache-cable-writer", librenms_cf={"primary": {"id": 63}})
    remote_device = make_device("cache-cable-remote")
    local = make_interface(device, "Ethernet1", iface_type="1000base-t")
    remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
    links_payload = {
        "links": [
            {
                "local_port_id": 7201,
                "local_port": local.name,
                "netbox_local_interface_id": local.pk,
                "netbox_remote_device_id": remote_device.pk,
                "netbox_remote_interface_id": remote.pk,
            }
        ]
    }
    _seed_snapshot("links", device, "primary", links_payload)
    _seed_snapshot("inventory", device, "primary")

    client.force_login(make_superuser("cache-cable-writer-user"))
    url = reverse("plugins:netbox_librenms_plugin:sync_device_cables", kwargs={"pk": device.pk})
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": "primary", "select": "7201"})

    assert response.status_code == 302
    assert Cable.objects.filter(terminations__termination_id=local.pk).exists()
    assert cache.get(_cache_key("links", device, "primary")) == links_payload
    assert cache.get(_cache_key("inventory", device, "primary")) is None


@pytest.mark.django_db
def test_module_install_invalidates_other_tabs_after_creating_a_module(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A committed module action must retain inventory and clear other snapshots."""
    _configure_servers(settings)
    device = make_device("cache-module-writer", librenms_cf={"primary": {"id": 64}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Cache Test Module",
    )
    inventory_payload = {
        "inventory": [
            {
                "entPhysicalIndex": 8101,
                "entPhysicalClass": "module",
                "entPhysicalModelName": module_type.model,
                "entPhysicalContainedIn": 0,
                "entPhysicalName": bay.name,
            }
        ]
    }
    _seed_snapshot("inventory", device, "primary", inventory_payload)
    _seed_snapshot("ip_addresses", device, "primary")

    client.force_login(make_superuser("cache-module-writer-user"))
    url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "CACHE-TEST-SERIAL",
            },
        )

    assert response.status_code == 302
    assert Module.objects.filter(device=device, module_bay=bay, module_type=module_type).exists()
    assert cache.get(_cache_key("inventory", device, "primary")) == inventory_payload
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is None


@pytest.mark.django_db
def test_unchanged_module_serial_preserves_other_snapshots(client, settings, django_capture_on_commit_callbacks):
    """Submitting the existing serial must not publish a cache mutation."""
    _configure_servers(settings)
    device = make_device("cache-module-unchanged", librenms_cf={"primary": {"id": 641}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Cache Existing Module",
    )
    module = Module.objects.create(
        device=device,
        module_bay=bay,
        module_type=module_type,
        serial="UNCHANGED-SERIAL",
    )
    inventory_payload = {"inventory": [{"entPhysicalIndex": 8102}]}
    _seed_snapshot("inventory", device, "primary", inventory_payload)
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-module-unchanged-user"))
    url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "module_id": str(module.pk),
                "serial": module.serial,
            },
        )

    assert response.status_code == 302
    assert cache.get(_cache_key("ports", device, "primary")) is not None
    coordinator = SyncCacheConsistency(device)
    assert cache.get(coordinator.state_key(SyncTab.MODULES, "primary")) is None


@pytest.mark.django_db
def test_module_mutation_without_posted_server_uses_active_namespace(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A module writer must resolve its active namespace before it mutates."""
    _configure_servers(settings)
    device = make_device("cache-module-missing-server", librenms_cf={"primary": {"id": 642}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Cache Scoped Module",
    )
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD")
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-module-missing-server-user"))
    url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"module_id": str(module.pk), "serial": "NEW"})

    assert response.status_code == 302
    module.refresh_from_db()
    assert module.serial == "NEW"
    assert cache.get(_cache_key("ports", device, "primary")) is None


@pytest.mark.django_db
def test_interface_delete_without_posted_server_uses_active_namespace(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """The delete writer must validate its server namespace before deleting a row."""
    _configure_servers(settings)
    device = make_device("cache-delete-missing-server", librenms_cf={"primary": {"id": 643}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("ip_addresses", device, "primary")
    client.force_login(make_superuser("cache-delete-missing-server-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"interface_ids": [str(interface.pk)]})

    assert response.status_code == 200
    assert not type(interface).objects.filter(pk=interface.pk).exists()
    assert cache.get(_cache_key("ports", device, "primary")) is not None
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is None


@pytest.mark.django_db
def test_interface_delete_without_a_usable_server_invalidates_the_source_snapshot(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A NetBox-only deletion without cleanup ownership must invalidate its source snapshot."""
    server_key = _configured_server_key(settings)
    device = make_device("cache-delete-without-server", librenms_cf={server_key: {"id": 644}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    configure_no_librenms_servers(settings)
    _seed_snapshot("ports", device, server_key)
    assert cache.get(_cache_key("ports", device, server_key)) is not None
    client.force_login(make_superuser("cache-delete-without-server-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"interface_ids": [str(interface.pk)]})

    assert response.status_code == 200
    assert not type(interface).objects.filter(pk=interface.pk).exists()
    assert cache.get(_cache_key("ports", device, server_key)) is None
    state = cache.get(SyncCacheConsistency(device).state_key(SyncTab.INTERFACES, server_key))
    assert state is not None
    assert state["state"] == SyncTabState.INVALIDATED.value


@pytest.mark.django_db(transaction=True)
def test_configured_unmapped_server_action_invalidates_mapped_snapshots_without_cleanup_failure(
    client,
    settings,
):
    """An unmapped action must not report cleanup failure and must invalidate mapped state."""
    _configure_servers(settings)
    configured_server_keys = tuple(settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"])
    mapped_server_key, acting_server_key, *_remaining_server_keys = configured_server_keys
    device = make_device(
        "cache-configured-unmapped-action",
        librenms_cf={mapped_server_key: {"id": 646}},
    )
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    snapshots = {
        SyncTab.INTERFACES: "ports",
        SyncTab.CABLES: "links",
        SyncTab.IP_ADDRESSES: "ip_addresses",
        SyncTab.MODULES: "inventory",
        SyncTab.VLANS: "vlans",
    }
    for data_type in snapshots.values():
        _seed_snapshot(data_type, device, mapped_server_key)
    client.force_login(make_superuser("cache-configured-unmapped-action-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    response = client.post(
        url,
        {
            "server_key": acting_server_key,
            "interface_ids": [str(interface.pk)],
        },
    )

    assert response.status_code == 200
    assert not type(interface).objects.filter(pk=interface.pk).exists()
    transition_header = response.get("X-LibreNMS-Cache-Transition")
    cleanup_failed = json.loads(transition_header)["cleanup_failed"] if transition_header else False
    coordinator = SyncCacheConsistency(device)
    actual = {
        "cleanup_failed": cleanup_failed,
        "remaining_snapshots": {
            tab.value
            for tab, data_type in snapshots.items()
            if cache.get(_cache_key(data_type, device, mapped_server_key)) is not None
        },
        "states": {
            tab.value: (cache.get(coordinator.state_key(tab, mapped_server_key)) or {}).get("state")
            for tab in snapshots
        },
    }
    assert actual == {
        "cleanup_failed": False,
        "remaining_snapshots": set(),
        "states": {tab.value: SyncTabState.INVALIDATED.value for tab in snapshots},
    }


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Two transitions for different servers in one request delete each other's source. "
        "No view reaches this today: ordinary views schedule once, and the migration views "
        "schedule twice for different objects on one server key. Recorded rather than fixed, "
        "because _apply_mutation preserves only its own active server by design. Remove this "
        "marker with the fix if a view ever schedules for a second server."
    ),
)
@pytest.mark.django_db(transaction=True)
def test_request_transitions_for_different_servers_preserve_both_sources(settings):
    """Verify at the lowest reachable seam that different server transitions preserve both sources when no routed view does."""
    _configure_servers(settings)
    configured_server_keys = tuple(settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"])
    first_source_server, second_source_server, idle_server = configured_server_keys
    device = make_device(
        "cache-multi-server-request-transitions",
        librenms_cf={server_key: {"id": 650 + index} for index, server_key in enumerate(configured_server_keys)},
    )
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    source_payloads = {
        first_source_server: {"snapshot": "first-source"},
        second_source_server: {"snapshot": "second-source"},
    }
    for server_key, payload in source_payloads.items():
        _seed_snapshot("ports", device, server_key, payload)
    _seed_snapshot("ports", device, idle_server, {"snapshot": "idle-server"})
    request = RequestFactory().post("/sync")
    request.user = make_superuser("cache-multi-server-request-transitions-user")

    with claim_sync_subjects(sync_subject_key(device)), transaction.atomic():
        interface.delete()
        schedule_request_cache_mutation(
            request,
            device,
            SyncTab.INTERFACES,
            first_source_server,
        )
        schedule_request_cache_mutation(
            request,
            device,
            SyncTab.INTERFACES,
            second_source_server,
        )

    response = apply_request_cache_transition(request, HttpResponse())
    payload = json.loads(response["X-LibreNMS-Cache-Transition"])
    assert len(payload["transition_ids"]) == 2
    assert cache.get(_cache_key("ports", device, idle_server)) is None
    idle_state = cache.get(SyncCacheConsistency(device).state_key(SyncTab.INTERFACES, idle_server))
    assert idle_state["state"] == SyncTabState.INVALIDATED.value
    assert {
        server_key: cache.get(_cache_key("ports", device, server_key)) for server_key in source_payloads
    } == source_payloads


@pytest.mark.django_db(transaction=True)
def test_a_response_built_inside_the_transaction_reports_the_committed_cleanup(settings):
    """Verify that a response built before commit reports cleanup deferred by ATOMIC_REQUESTS."""
    server_key = _configured_server_key(settings)
    device = make_device("cache-deferred-response", librenms_cf={server_key: {"id": 651}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    # Seed a DEPENDENT tab: schedule_mutation discards the source tab from cleanup_tabs when
    # the device is mapped to the active server, so seeding "ports" would clean nothing.
    _seed_snapshot("ip_addresses", device, server_key, {"snapshot": "pre-commit"})
    request = RequestFactory().post("/sync", HTTP_HX_REQUEST="true")
    request.user = make_superuser("cache-deferred-response-user")

    with claim_sync_subjects(sync_subject_key(device)), transaction.atomic():
        interface.delete()
        schedule_request_cache_mutation(request, device, SyncTab.INTERFACES, server_key)
        response = apply_request_cache_transition(request, HttpResponse())

    trigger = json.loads(response["HX-Trigger"])["librenmsCacheChanged"]
    assert trigger["removed"] is True, "the browser was told nothing was removed"
    assert trigger["revisions"], "the browser got no revision for the cleaned tab"


@pytest.mark.django_db
def test_module_serial_update_without_a_usable_server_invalidates_the_source_snapshot(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A NetBox-only module write without cleanup ownership must invalidate its source snapshot."""
    server_key = _configured_server_key(settings)
    device = make_device("cache-serial-without-server", librenms_cf={server_key: {"id": 645}})
    bay = ModuleBay.objects.create(device=device, name="Slot 1")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Serverless Serial Module",
    )
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="OLD")
    configure_no_librenms_servers(settings)
    _seed_snapshot("inventory", device, server_key)
    assert cache.get(_cache_key("inventory", device, server_key)) is not None
    client.force_login(make_superuser("cache-serial-without-server-user"))
    url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"module_id": str(module.pk), "serial": "NEW"})

    assert response.status_code == 302
    module.refresh_from_db()
    assert module.serial == "NEW"
    assert cache.get(_cache_key("inventory", device, server_key)) is None
    state = cache.get(SyncCacheConsistency(device).state_key(SyncTab.MODULES, server_key))
    assert state is not None
    assert state["state"] == SyncTabState.INVALIDATED.value


@pytest.mark.django_db
def test_module_move_succeeds_without_a_configured_librenms_server(client, settings):
    """A NetBox-only module move must not depend on constructing a LibreNMS client."""
    configure_no_librenms_servers(settings)
    device = make_device("cache-move-without-server")
    source_bay = ModuleBay.objects.create(device=device, name="Slot 1")
    target_bay = ModuleBay.objects.create(device=device, name="Slot 2")
    module_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Serverless Movable Module",
    )
    module = Module.objects.create(device=device, module_bay=source_bay, module_type=module_type, serial="MOVE-ME")
    client.force_login(make_superuser("cache-move-without-server-user"))
    url = reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": device.pk})

    response = client.post(url, {"conflict_module_id": str(module.pk), "target_bay_id": str(target_bay.pk)})

    assert response.status_code == 302
    module.refresh_from_db()
    assert module.module_bay == target_bay


@pytest.mark.django_db
def test_bay_template_creation_succeeds_without_a_configured_librenms_server(client, settings):
    """A NetBox-only bay template must not depend on constructing a LibreNMS client."""
    configure_no_librenms_servers(settings)
    device = make_device("cache-bay-template-without-server")
    client.force_login(make_superuser("cache-bay-template-without-server-user"))
    url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})

    response = client.post(
        url,
        {
            "target_kind": "device_type",
            "target_pk": str(device.device_type.pk),
            "name": "Serverless Slot",
        },
    )

    assert response.status_code == 302
    assert ModuleBayTemplate.objects.filter(device_type=device.device_type, name="Serverless Slot").exists()


def _mark_migrated_donor(donor, winner):
    """Persist one real server-scoped donor marker for migration endpoint tests."""
    mark_librenms_migrated(donor, winner.pk, "primary")
    donor.save(update_fields=["custom_field_data"])


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_interface_move_invalidates_winner_dependent_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """Moving an interface must publish the cache transition for its new owner too."""
    _configure_servers(settings)
    donor = make_device("cache-move-interface-donor", librenms_cf={"primary": {"id": 6410}})
    winner = make_device("cache-move-interface-winner", librenms_cf={"primary": {"id": 6411}})
    _mark_migrated_donor(donor, winner)
    interface = make_interface(donor, "Ethernet1", iface_type="1000base-t")
    _seed_snapshot("ip_addresses", winner, "primary")
    client.force_login(make_superuser("cache-move-interface-user"))
    url = reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk])

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert json.loads(response["X-LibreNMS-Cache-Transition"])["cleanup_failed"] is False
    interface.refresh_from_db()
    assert interface.device_id == winner.pk
    assert cache.get(_cache_key("ip_addresses", winner, "primary")) is None


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_interface_move_clears_winner_snapshots_when_active_server_is_unmapped(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """Moving an interface must clear the winner's snapshots on its mapped server."""
    _configure_servers(settings)
    active_server_key, winner_server_key, *_remaining_server_keys = settings.PLUGINS_CONFIG["netbox_librenms_plugin"][
        "servers"
    ]
    donor = make_device(
        "cache-move-interface-unmapped-donor",
        librenms_cf={active_server_key: {"id": 6418}},
    )
    winner = make_device(
        "cache-move-interface-unmapped-winner",
        librenms_cf={winner_server_key: {"id": 6419}},
    )
    mark_librenms_migrated(donor, winner.pk, active_server_key)
    donor.save(update_fields=["custom_field_data"])
    interface = make_interface(donor, "Ethernet1", iface_type="1000base-t")
    winner_snapshot_keys = {_cache_key(data_type, winner, winner_server_key) for data_type in ("ports", "ip_addresses")}
    for snapshot_key in winner_snapshot_keys:
        cache.set(snapshot_key, {"snapshot": snapshot_key}, timeout=300)
    client.force_login(make_superuser("cache-move-interface-unmapped-user"))
    url = reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk])

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": active_server_key}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    interface.refresh_from_db()
    assert interface.device_id == winner.pk
    assert not {snapshot_key for snapshot_key in winner_snapshot_keys if cache.get(snapshot_key) is not None}


@pytest.mark.django_db(transaction=True)
def test_interface_move_response_reports_winner_cleanup_failure(
    client,
    settings,
):
    """A winner cleanup failure must remain visible in the single response event."""
    _configure_servers(settings)
    donor = make_device("cache-move-interface-failure-donor", librenms_cf={"primary": {"id": 6416}})
    winner = make_device("cache-move-interface-failure-winner", librenms_cf={"primary": {"id": 6417}})
    _mark_migrated_donor(donor, winner)
    interface = make_interface(donor, "Ethernet1", iface_type="1000base-t")
    client.force_login(make_superuser("cache-move-interface-failure-user"))
    url = reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk])
    real_apply_mutation = SyncCacheConsistency._apply_mutation

    def fail_winner_cleanup(coordinator, *args):
        if coordinator.subject.pk == winner.pk:
            raise RuntimeError("winner cache backend failed")
        return real_apply_mutation(coordinator, *args)

    with patch.object(SyncCacheConsistency, "_apply_mutation", autospec=True) as apply_mutation:
        apply_mutation.side_effect = fail_winner_cleanup
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    payload = json.loads(response["X-LibreNMS-Cache-Transition"])
    assert payload["cleanup_failed"] is True
    assert len(payload["transition_ids"]) == 2
    assert payload["source_tabs"] == ["interfaces"]


@pytest.mark.django_db
def test_ip_move_invalidates_winner_dependent_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """Moving an address must publish the cache transition for its new owner too."""
    _configure_servers(settings)
    donor = make_device("cache-move-ip-donor", librenms_cf={"primary": {"id": 6412}})
    winner = make_device("cache-move-ip-winner", librenms_cf={"primary": {"id": 6413}})
    _mark_migrated_donor(donor, winner)
    address = ip_on(donor, "198.18.20.10/24", "Ethernet1")
    make_interface(winner, "Ethernet1", iface_type="1000base-t")
    _seed_snapshot("ports", winner, "primary")
    client.force_login(make_superuser("cache-move-ip-user"))
    url = reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", args=[address.pk])

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    address.refresh_from_db()
    assert address.assigned_object.device_id == winner.pk
    assert cache.get(_cache_key("ports", winner, "primary")) is None


@pytest.mark.django_db
def test_device_ip_transfer_invalidates_winner_dependent_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """Transferring a device IP role must publish the cache transition for the winner too."""
    _configure_servers(settings)
    donor = make_device("cache-transfer-ip-donor", librenms_cf={"primary": {"id": 6414}})
    winner = make_device("cache-transfer-ip-winner", librenms_cf={"primary": {"id": 6415}})
    _mark_migrated_donor(donor, winner)
    address = ip_on(winner, "198.18.21.10/24", "Ethernet1")
    donor.primary_ip4 = address
    donor.save(update_fields=["primary_ip4"])
    _seed_snapshot("ports", winner, "primary")
    client.force_login(make_superuser("cache-transfer-ip-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:device_transfer_ip",
        kwargs={"pk": donor.pk, "ip_kind": "primary4"},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    winner.refresh_from_db()
    assert winner.primary_ip4_id == address.pk
    assert cache.get(_cache_key("ports", winner, "primary")) is None


@pytest.mark.django_db
def test_inline_relationship_noop_preserves_other_snapshots(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """An already-applied relationship must not publish a second mutation."""
    _configure_servers(settings)
    device = make_device("cache-parent-noop", librenms_cf={"primary": {"id": 644}})
    parent = make_interface(device, "Ethernet1", iface_type="1000base-t")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    set_librenms_device_id(parent, 7441, "primary")
    set_librenms_device_id(child, 7442, "primary")
    parent.save(update_fields=["custom_field_data"])
    child.parent = parent
    child.save(update_fields=["custom_field_data", "parent"])
    ports_payload = {
        "ports": [
            {"port_id": 7441, "ifName": parent.name},
            {"port_id": 7442, "ifName": child.name},
        ],
        "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {7442: 7441}},
    }
    _seed_snapshot("ports", device, "primary", ports_payload)
    _seed_snapshot("ip_addresses", device, "primary")
    client.force_login(make_superuser("cache-parent-noop-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_interface_parent",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "port_id": "7442",
                "parent_port_id": "7441",
            },
        )

    assert response.status_code == 200
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is not None
    coordinator = SyncCacheConsistency(device)
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_htmx_ip_cache_miss_replaces_active_tab_with_warning(client, settings):
    """An HTMX writer cache miss must not navigate away from the active tab."""
    _configure_servers(settings)
    device = make_device("cache-ip-missing", librenms_cf={"primary": {"id": 645}})
    client.force_login(make_superuser("cache-ip-missing-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    response = client.post(
        url,
        {"server_key": "primary", "select": "198.18.45.1/24"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    assert response["HX-Retarget"] == "#ipaddress-sync-content"
    assert b"Refresh IP Addresses" in response.content
    assert b'name="select"' not in response.content


@pytest.mark.django_db
def test_htmx_module_cache_miss_replaces_active_tab_with_warning(client, settings):
    """An HTMX module writer cache miss must render an empty refresh state."""
    _configure_servers(settings)
    device = make_device("cache-module-missing", librenms_cf={"primary": {"id": 646}})
    client.force_login(make_superuser("cache-module-missing-user"))
    url = reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk})

    response = client.post(
        url,
        {"server_key": "primary", "select": "9001"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    assert b"Refresh Modules" in response.content
    assert b'name="select"' not in response.content


@pytest.mark.django_db
def test_partial_cable_refresh_renders_no_syncable_rows(client, settings):
    """An incomplete cable refresh must not show rows without a backing snapshot."""
    _configure_servers(settings)
    device = make_device(
        "cache-cable-partial",
        librenms_cf={"primary": {"id": 647, "oob": {"id": 649, "type": "oob"}}},
    )
    remote_device = make_device("cache-cable-partial-remote", librenms_cf={"primary": {"id": 648}})
    local = make_interface(device, "Ethernet1", iface_type="1000base-t")
    remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
    set_librenms_device_id(local, 7471, "primary")
    set_librenms_device_id(remote, 7472, "primary")
    local.save(update_fields=["custom_field_data"])
    remote.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("cache-cable-partial-user"))

    def librenms_response(url, **_kwargs):
        if url.endswith("/api/v0/devices/647/links"):
            return _json_response(
                url,
                {
                    "status": "ok",
                    "links": [
                        {
                            "local_port_id": 7471,
                            "local_port": local.name,
                            "remote_port_id": 7472,
                            "remote_port": remote.name,
                            "remote_hostname": remote_device.name,
                            "remote_device_id": 648,
                        }
                    ],
                },
            )
        if url.endswith("/api/v0/devices/647/ports"):
            return _json_response(
                url,
                {"status": "ok", "ports": [{"port_id": 7471, "ifName": local.name, "ifDescr": local.name}]},
            )
        if url.endswith("/api/v0/devices/649/links"):
            return _json_response(url, {"message": "Unavailable"}, status=503)
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", kwargs={"pk": device.pk})
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert b"Cable refresh was incomplete" in response.content
    assert b"Sync Selected Cables" not in response.content
    assert b'name="select"' not in response.content
    assert cache.get(_cache_key("links", device, "primary")) is None


@pytest.mark.django_db
def test_cable_refresh_without_a_cached_snapshot_reports_failure_not_success(client, settings):
    """The toast must agree with the tab state: no snapshot means no success message."""
    _configure_servers(settings)
    device = make_device("cache-cable-nosnap", librenms_cf={"primary": {"id": 661}})
    remote_device = make_device("cache-cable-nosnap-remote", librenms_cf={"primary": {"id": 662}})
    local = make_interface(device, "Ethernet1", iface_type="1000base-t")
    remote = make_interface(remote_device, "Ethernet2", iface_type="1000base-t")
    set_librenms_device_id(local, 7481, "primary")
    set_librenms_device_id(remote, 7482, "primary")
    local.save(update_fields=["custom_field_data"])
    remote.save(update_fields=["custom_field_data"])
    user = make_superuser("cache-cable-nosnap-user")
    client.force_login(user)

    def librenms_response(url, **_kwargs):
        if url.endswith("/api/v0/devices/661/links"):
            return _json_response(
                url,
                {
                    "status": "ok",
                    "links": [
                        {
                            "local_port_id": 7481,
                            "local_port": local.name,
                            "remote_port_id": 7482,
                            "remote_port": remote.name,
                            "remote_hostname": remote_device.name,
                            "remote_device_id": 662,
                        }
                    ],
                },
            )
        if url.endswith("/api/v0/devices/661/ports"):
            return _json_response(
                url,
                {"status": "ok", "ports": [{"port_id": 7481, "ifName": local.name, "ifDescr": local.name}]},
            )
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", kwargs={"pk": device.pk})
    # Inject the one condition the real flow cannot produce locally: the snapshot is gone by the
    # time the handler checks for it (an eviction between the write and the read).
    with (
        patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response),
        patch("netbox_librenms_plugin.views.base.cables_view.cache.has_key", return_value=False),
    ):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert b"Cable data refreshed successfully" not in response.content
    assert b"could not be cached" in response.content
    state = SyncCacheConsistency(device).status("primary", actor_id=user.pk)[SyncTab.CABLES.value]
    assert state["state"] == SyncTabState.REFRESH_FAILED.value


@pytest.mark.django_db
def test_partial_module_refresh_renders_no_inventory_rows(client, settings):
    """An incomplete module refresh must render the empty state instead of a degraded table."""
    _configure_servers(settings)
    device = make_device("cache-module-partial", librenms_cf={"primary": {"id": 651}})
    client.force_login(make_superuser("cache-module-partial-user"))
    _seed_snapshot("inventory", device, "primary")

    def librenms_response(url, **_kwargs):
        if url.endswith("/api/v0/inventory/651/all"):
            return _json_response(
                url,
                {
                    "status": "ok",
                    "inventory": [
                        {
                            "entPhysicalIndex": 1,
                            "entPhysicalName": "Slot 1",
                            "entPhysicalModelName": "MOD-1",
                            "entPhysicalSerialNum": "SER1",
                            "entPhysicalDescr": "module",
                        }
                    ],
                },
            )
        if url.endswith("/api/v0/devices/651/transceivers"):
            return _json_response(url, {"status": "ok", "transceivers": []})
        if "/api/v0/devices/651/ports" in url:
            return _json_response(url, {"message": "Unavailable"}, status=503)
        raise AssertionError(f"Unexpected LibreNMS request: {url}")

    url = reverse("plugins:netbox_librenms_plugin:device_module_sync", kwargs={"pk": device.pk})
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.post(url, {"server_key": "primary"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    html = response.content.decode()
    assert "port metadata fetch failed" in html
    assert "No inventory data loaded" in html
    assert "install-selected-form" not in html
    # The degraded snapshot must not survive as a complete one for the next render.
    assert cache.get(_cache_key("inventory", device, "primary")) is None


@pytest.mark.django_db
def test_cleanup_failure_invalidates_every_dependent_tab(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A partial cache-backend failure must leave all dependent tabs fail-closed."""
    _configure_servers(settings)
    device = make_device("cache-cleanup-failure", librenms_cf={"primary": {"id": 650}})
    coordinator = SyncCacheConsistency(device)
    for data_type in ("ports", "links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, device, "primary")
    cache.set(f"{_cache_key('links', device, 'primary')}:manual-remote:test-row", 1, timeout=300)

    with (
        patch.object(cache, "delete_pattern", side_effect=RuntimeError("cache backend failed")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is not None
    source_state = cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary"))
    assert source_state is not None
    assert source_state["state"] == "locally_changed"
    for tab in (SyncTab.CABLES, SyncTab.IP_ADDRESSES, SyncTab.MODULES, SyncTab.VLANS):
        state = cache.get(coordinator.state_key(tab, "primary"))
        assert state is not None
        assert state["state"] == "invalidated"
        assert "cleanup did not complete" in state["reason"]

    assert cache.get(_cache_key("ip_addresses", device, "primary")) is not None
    assert coordinator.status("primary")[SyncTab.IP_ADDRESSES.value]["snapshot_available"] is False
    client.force_login(make_superuser("cache-cleanup-failure-user"))
    fragment_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={
            "object_type": "device",
            "pk": device.pk,
            "tab": SyncTab.IP_ADDRESSES.value,
        },
    )
    response = client.get(fragment_url, {"server_key": "primary"})
    assert response.status_code == 404


@pytest.mark.django_db
def test_cleanup_failure_on_unmapped_active_server_invalidates_surviving_mapped_snapshots(
    settings,
    django_capture_on_commit_callbacks,
):
    """A partial cleanup must fail closed for every surviving mapped snapshot."""
    _configure_servers(settings)
    mapped_server_key, active_server_key, *_remaining_server_keys = settings.PLUGINS_CONFIG["netbox_librenms_plugin"][
        "servers"
    ]
    device = make_device(
        "cache-cleanup-failure-unmapped-active",
        librenms_cf={mapped_server_key: {"id": 652}},
    )
    coordinator = SyncCacheConsistency(device)
    snapshots = {
        SyncTab.INTERFACES: "ports",
        SyncTab.CABLES: "links",
        SyncTab.IP_ADDRESSES: "ip_addresses",
        SyncTab.MODULES: "inventory",
        SyncTab.VLANS: "vlans",
    }
    for data_type in snapshots.values():
        _seed_snapshot(data_type, device, mapped_server_key)
    cache.set(f"{_cache_key('links', device, mapped_server_key)}:manual-remote:test-row", 1, timeout=300)

    with (
        patch.object(cache, "delete_pattern", side_effect=RuntimeError("cache backend failed")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, active_server_key, actor_id=1)

    assert transition.error is not None
    assert cache.get(_cache_key("ip_addresses", device, mapped_server_key)) is not None
    assert set(transition.browser_payload()["cleanup_tabs"]) == {tab.value for tab in snapshots}
    for tab in snapshots:
        state = cache.get(coordinator.state_key(tab, mapped_server_key))
        assert state is not None
        assert state["state"] == SyncTabState.INVALIDATED.value
    assert coordinator.status(mapped_server_key)[SyncTab.IP_ADDRESSES.value]["snapshot_available"] is False


@pytest.mark.django_db
def test_cleanup_failure_does_not_mark_tabs_without_cache_state(
    settings,
    django_capture_on_commit_callbacks,
):
    """Cleanup failure reasons must be limited to tabs that had snapshot-bound state."""
    _configure_servers(settings)
    device = make_device("cache-cleanup-sparse", librenms_cf={"primary": {"id": 651}})
    coordinator = SyncCacheConsistency(device)
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("links", device, "primary")
    cache.set(f"{_cache_key('links', device, 'primary')}:manual-remote:test-row", 1, timeout=300)

    with (
        patch.object(cache, "delete_pattern", side_effect=RuntimeError("cache backend failed")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is not None
    cable_state = cache.get(coordinator.state_key(SyncTab.CABLES, "primary"))
    assert cable_state is not None
    assert cable_state["state"] == "invalidated"
    for tab in (SyncTab.IP_ADDRESSES, SyncTab.MODULES, SyncTab.VLANS):
        assert cache.get(coordinator.state_key(tab, "primary")) is None


@pytest.mark.django_db
def test_cleanup_failure_before_affected_tabs_are_known_invalidates_all_tabs(
    settings,
    django_capture_on_commit_callbacks,
):
    """An early cleanup failure must publish fail-closed states for every dependent tab."""
    _configure_servers(settings)
    device = make_device("cache-cleanup-early-failure", librenms_cf={"primary": {"id": 653}})
    coordinator = SyncCacheConsistency(device)

    with (
        patch(
            "netbox_librenms_plugin.sync_cache.mapped_server_keys",
            side_effect=[{"primary"}, RuntimeError("mapping lookup failed"), {"primary"}],
        ),
        django_capture_on_commit_callbacks(execute=True),
    ):
        transition = coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)

    assert transition.error is not None
    source_state = cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary"))
    assert source_state is not None
    assert source_state["state"] == SyncTabState.LOCALLY_CHANGED.value
    for tab in (SyncTab.CABLES, SyncTab.IP_ADDRESSES, SyncTab.MODULES, SyncTab.VLANS):
        state = cache.get(coordinator.state_key(tab, "primary"))
        assert state is not None
        assert state["state"] == SyncTabState.INVALIDATED.value


@pytest.mark.django_db
def test_legacy_ip_fragment_never_backfills_from_librenms(client, settings):
    """A cache-only fragment must not upgrade an old IP snapshot through live HTTP."""
    _configure_servers(settings)
    device = make_device("cache-ip-fragment-legacy", librenms_cf={"primary": {"id": 652}})
    _seed_snapshot(
        "ip_addresses",
        device,
        "primary",
        {
            "ip_addresses": [
                {
                    "ip_address": "198.18.52.10",
                    "prefix_length": 24,
                    "ip_with_mask": "198.18.52.10/24",
                    "port_id": 7521,
                }
            ]
        },
    )
    client.force_login(make_superuser("cache-ip-fragment-legacy-user"))
    fragment_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.IP_ADDRESSES.value},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The IP cache fragment contacted LibreNMS"),
    ) as requests_get:
        response = client.get(fragment_url, {"server_key": "primary"})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"No IP address data loaded" in response.content


@pytest.mark.django_db
def test_refresh_failure_reason_is_anonymous_across_users(settings):
    """The shared failure reason must identify only whether this user initiated it."""
    _configure_servers(settings)
    device = make_device("cache-refresh-failure-reason", librenms_cf={"primary": {"id": 653}})
    coordinator = SyncCacheConsistency(device)
    coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=101)

    same_user = coordinator.status("primary", actor_id=101)[SyncTab.IP_ADDRESSES.value]
    other_user = coordinator.status("primary", actor_id=102)[SyncTab.IP_ADDRESSES.value]

    assert "your latest LibreNMS refresh failed" in same_user["reason"]
    assert "another user attempted to refresh" in other_user["reason"]


@pytest.mark.django_db
def test_refresh_failure_marks_server_rendered_tab_without_dynamic_content(client, settings):
    """HTMX navigation must mark the tab itself and must not add variable-width content."""
    _configure_servers(settings)
    device = make_device("cache-refresh-failed-tab", librenms_cf={"primary": {"id": 655}})
    user = make_superuser("cache-refresh-failed-tab-user")
    _seed_snapshot("ports", device, "primary", {"ports": [], "port_stack_relationships": {}})
    SyncCacheConsistency(device).mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=user.pk)
    client.force_login(user)
    page_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: _device_info_response(url, 655, device.name, "Cache rail hardware"),
    ):
        response = client.get(page_url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})

    assert response.status_code == 200
    html = response.content.decode()
    tab_link = _opening_tag(html, "ipaddresses-tab")
    assert "sync-cache-unavailable" in _tab_classes(html, "ipaddresses-tab")
    assert 'title="Cached data is unavailable. Refresh this tab."' in tab_link
    assert 'aria-label="IP Addresses. Cached data is unavailable."' in tab_link
    assert "ipaddresses-cache-badge" not in html


@pytest.mark.django_db
def test_opening_unavailable_tab_acknowledges_only_its_current_revision(client, settings):
    """Opening an unavailable tab must suppress its marker until a new revision appears."""
    _configure_servers(settings)
    device = make_device("cache-tab-acknowledgement", librenms_cf={"primary": {"id": 656}})
    user = make_superuser("cache-tab-acknowledgement-user")
    _seed_snapshot("ports", device, "primary", {"ports": [], "port_stack_relationships": {}})
    coordinator = SyncCacheConsistency(device)
    coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=user.pk)
    client.force_login(user)
    page_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    status_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_status",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    def get_page(tab):
        with patch(
            "netbox_librenms_plugin.librenms_api.requests.get",
            side_effect=lambda url, **_kwargs: _device_info_response(
                url,
                656,
                device.name,
                "Cache acknowledgement hardware",
            ),
        ):
            return client.get(page_url, {"server_key": "primary", "tab": tab.value})

    first_page = get_page(SyncTab.INTERFACES)
    assert "sync-cache-unavailable" in _tab_classes(first_page.content.decode(), "ipaddresses-tab")

    selected_page = get_page(SyncTab.IP_ADDRESSES)
    assert "sync-cache-unavailable" not in _tab_classes(selected_page.content.decode(), "ipaddresses-tab")

    later_page = get_page(SyncTab.INTERFACES)
    assert "sync-cache-unavailable" not in _tab_classes(later_page.content.decode(), "ipaddresses-tab")
    acknowledged_status = client.get(status_url, {"server_key": "primary"}).json()["tabs"]
    assert acknowledged_status[SyncTab.IP_ADDRESSES.value]["attention_required"] is False

    _seed_snapshot("ip_addresses", device, "primary", {"ip_addresses": []})
    coordinator.mark_refresh_success(SyncTab.IP_ADDRESSES, "primary", actor_id=user.pk)
    coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=user.pk)

    new_revision_page = get_page(SyncTab.INTERFACES)
    assert "sync-cache-unavailable" in _tab_classes(new_revision_page.content.decode(), "ipaddresses-tab")


def test_cleanup_failure_payload_names_tabs_that_must_fail_closed():
    """A failed cleanup must tell the browser which stale controls to remove."""
    transition = CacheMutationTransition(
        source_tab=SyncTab.INTERFACES,
        cleanup_tabs={SyncTab.IP_ADDRESSES, SyncTab.MODULES},
        source_fragment_required=True,
        error="failed",
    )

    assert transition.browser_payload() == {
        "transition_id": transition.transition_id,
        "removed": False,
        "cleanup_failed": True,
        "tabs": [],
        "cleanup_tabs": ["ipaddresses", "modules"],
        "source_tab": "interfaces",
        "source_fragment_required": True,
        "revisions": {},
    }


@pytest.mark.django_db
def test_fragment_restores_from_cache_without_calling_librenms(client, settings):
    """The fragment endpoint must rebuild comparison HTML from cache and ORM only."""
    _configure_servers(settings)
    device = make_device("cache-fragment", librenms_cf={"primary": {"id": 65}})
    payload = {
        "ports": [
            {
                "port_id": 7301,
                "ifName": "Ethernet1",
                "ifDescr": "Ethernet1",
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", device, "primary", payload)
    client.force_login(make_superuser("cache-fragment-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("The cache fragment contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"server_key": "primary"})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"Ethernet1" in response.content


@pytest.mark.django_db
def test_status_fragment_and_invalidated_tab_navigation_never_call_librenms(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """Cache checks, fragment restoration, and tab navigation must remain cache-only."""
    _configure_servers(settings)
    device = make_device("cache-browser-flows", librenms_cf={"primary": {"id": 654}})
    ports_payload = {
        "ports": [
            {
                "port_id": 7541,
                "ifName": "Ethernet1",
                "ifDescr": "Access Ethernet1",
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", device, "primary", ports_payload)
    _seed_snapshot("ip_addresses", device, "primary")
    coordinator = SyncCacheConsistency(device)
    with django_capture_on_commit_callbacks(execute=True):
        coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
    client.force_login(make_superuser("cache-browser-flows-user"))
    status_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_status",
        kwargs={"object_type": "device", "pk": device.pk},
    )
    fragment_url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": SyncTab.INTERFACES.value},
    )
    page_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("A cache-only browser flow contacted LibreNMS"),
    ) as requests_get:
        status_response = client.get(status_url, {"server_key": "primary"})
        fragment_response = client.get(fragment_url, {"server_key": "primary"})
        page_response = client.get(
            page_url,
            {
                "server_key": "primary",
                "tab": SyncTab.IP_ADDRESSES.value,
                "interface_name_field": "ifDescr",
            },
        )

    requests_get.assert_not_called()
    assert status_response.status_code == 200
    assert fragment_response.status_code == 200
    assert b"Ethernet1" in fragment_response.content
    assert page_response.status_code == 200
    page_html = page_response.content.decode()
    assert "sync-cache-ready" in _tab_classes(page_html, "interfaces-tab")
    assert "sync-cache-unavailable" not in _tab_classes(page_html, "ipaddresses-tab")
    assert b"Details unavailable" in page_response.content
    assert b'id="add-device-modal"' not in page_response.content
    assert b'data-bs-target="#add-device-modal"' not in page_response.content


@pytest.mark.django_db
def test_rolled_back_mutation_keeps_snapshots_and_publishes_no_state(
    settings,
    django_capture_on_commit_callbacks,
):
    """A rollback must discard the invalidation callback."""
    _configure_servers(settings)
    device = make_device("cache-rollback", librenms_cf={"primary": {"id": 66}})
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("ip_addresses", device, "primary")
    coordinator = SyncCacheConsistency(device)

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError, match="roll back"):
            with transaction.atomic():
                coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
                raise RuntimeError("roll back")

    assert cache.get(_cache_key("ports", device, "primary")) is not None
    assert cache.get(_cache_key("ip_addresses", device, "primary")) is not None
    assert cache.get(coordinator.state_key(SyncTab.INTERFACES, "primary")) is None


@pytest.mark.django_db
def test_source_snapshot_expiry_before_commit_callback_is_not_reversed(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A successful mutation must not restore or extend a source snapshot that expires in flight."""
    _configure_servers(settings)
    device = make_device("cache-source-expiry", librenms_cf={"primary": {"id": 661}})
    ports_payload = {
        "ports": [
            {
                "port_id": 7361,
                "ifName": "Ethernet1",
                "ifDescr": "Ethernet1",
                "ifType": "ethernetCsmacd",
                "ifAdminStatus": "up",
            }
        ],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", device, "primary", ports_payload)
    _seed_snapshot("links", device, "primary")
    source_key = _cache_key("ports", device, "primary")
    client.force_login(make_superuser("cache-source-expiry-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "primary",
                "interface_name_field": "ifName",
                "select": "7361",
                "exclude_columns": "vlans",
            },
        )
        cache.delete(source_key)

    assert response.status_code == 302
    assert cache.get(source_key) is None
    assert cache.get(_cache_key("links", device, "primary")) is None
    source_status = SyncCacheConsistency(device).status("primary")[SyncTab.INTERFACES.value]
    assert source_status["state"] == "missing"
    assert source_status["snapshot_available"] is False


@pytest.mark.django_db
def test_failed_refresh_retains_an_existing_invalidation_reason(
    settings,
    django_capture_on_commit_callbacks,
):
    """A failed retry must not erase why another tab cleared this snapshot."""
    _configure_servers(settings)
    device = make_device("cache-failed-refresh", librenms_cf={"primary": {"id": 67}})
    coordinator = SyncCacheConsistency(device)
    _seed_snapshot("ports", device, "primary")
    _seed_snapshot("ip_addresses", device, "primary")
    with django_capture_on_commit_callbacks(execute=True):
        coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
    state_key = coordinator.state_key(SyncTab.IP_ADDRESSES, "primary")
    prior = cache.get(state_key)
    assert prior is not None
    cache.touch(state_key, timeout=17)

    failed = coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=2)

    assert failed["revision"] == prior["revision"]
    assert failed["reason"] == prior["reason"]
    assert failed["refresh_error"] == "The latest LibreNMS refresh failed."
    assert 0 < cache.ttl(state_key) <= 17


@pytest.mark.django_db
def test_add_bay_template_preserves_server_scope_and_invalidates_other_tabs(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """The modal must carry the active server through its committed module mutation."""
    _configure_servers(settings)
    device = make_device("cache-add-bay", librenms_cf={"secondary": {"id": 68}})
    inventory_payload = {"inventory": [{"entPhysicalIndex": 8201}]}
    _seed_snapshot("inventory", device, "secondary", inventory_payload)
    _seed_snapshot("ports", device, "secondary")
    client.force_login(make_superuser("cache-add-bay-user"))
    url = reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})

    response = client.get(
        url,
        {
            "server_key": "secondary",
            "target_kind": "device_type",
            "target_pk": device.device_type_id,
            "suggested_name": "Expansion Bay",
            "librenms_name": "Expansion Bay",
        },
    )

    assert response.status_code == 200
    assert b'name="server_key" value="secondary"' in response.content

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            url,
            {
                "server_key": "secondary",
                "target_kind": "device_type",
                "target_pk": str(device.device_type_id),
                "name": "Expansion Bay",
                "librenms_name": "Expansion Bay",
                "librenms_class": "module",
            },
        )

    assert response.status_code == 302
    assert ModuleBayTemplate.objects.filter(device_type=device.device_type, name="Expansion Bay").exists()
    assert cache.get(_cache_key("inventory", device, "secondary")) == inventory_payload
    assert cache.get(_cache_key("ports", device, "secondary")) is None


@pytest.mark.django_db
def test_cache_mutation_resolves_the_shared_owner_once_per_server(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """The post-commit cleanup must not re-resolve the chassis owner for every tab."""
    _configure_servers(settings)
    _chassis, (page_device, _sibling) = make_virtual_chassis_members("cache-owner-resolution", count=2)
    page_device.custom_field_data["librenms_id"] = {"primary": {"id": 61}}
    page_device.save(update_fields=["custom_field_data"])
    interface = make_interface(page_device, "Ethernet1", iface_type="1000base-t")
    interface.custom_field_data["librenms_id"] = {"primary": 7101}
    interface.save(update_fields=["custom_field_data"])
    source_payload = {
        "ports": [{"port_id": 7101, "ifName": interface.name, "ifDescr": interface.name}],
        "port_stack_relationships": {},
    }
    _seed_snapshot("ports", page_device, "primary", source_payload)
    for data_type in ("links", "ip_addresses", "inventory", "vlans"):
        _seed_snapshot(data_type, page_device, "primary")

    client.force_login(make_superuser("cache-owner-resolution-user"))
    sync_url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": "device", "object_id": page_device.pk},
    )

    from netbox_librenms_plugin import sync_cache

    resolved_server_keys = []
    real_get_sync_device = sync_cache.get_librenms_sync_device

    def counting_get_sync_device(device, server_key=None):
        resolved_server_keys.append(server_key)
        return real_get_sync_device(device, server_key=server_key)

    with patch("netbox_librenms_plugin.sync_cache.get_librenms_sync_device", counting_get_sync_device):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                sync_url,
                {
                    "server_key": "primary",
                    "interface_name_field": "ifName",
                    "select": "7101",
                    "description": "owner resolution",
                },
            )

    assert response.status_code == 302
    assert resolved_server_keys, "the shared owner must still be resolved for the mapped server"
    assert len(resolved_server_keys) == len(set(resolved_server_keys)), resolved_server_keys


@pytest.mark.django_db
def test_acknowledged_revisions_stay_bounded_across_objects(client, settings):
    """A user who opens many unavailable tabs must not grow the session without limit."""
    _configure_servers(settings)
    client.force_login(make_superuser("cache-acknowledgement-user"))
    acknowledged_keys = []

    with patch("netbox_librenms_plugin.sync_cache._MAX_ACKNOWLEDGED_REVISIONS", 3):
        for index in range(5):
            device = make_device(f"cache-ack-{index}", librenms_cf={"primary": {"id": 6200 + index}})
            SyncCacheConsistency(device).mark_refresh_failure(SyncTab.INTERFACES, "primary")
            acknowledged_keys.append(f"device:{device.pk}:primary:interfaces")
            with patch(
                "netbox_librenms_plugin.librenms_api.requests.get",
                side_effect=lambda url, **_kwargs: _device_info_response(url, 6200, device.name, "Hardware"),
            ):
                response = client.get(
                    reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
                    {"server_key": "primary", "tab": SyncTab.INTERFACES.value},
                )
            assert response.status_code == 200

    stored = client.session[SYNC_CACHE_ACKNOWLEDGED_SESSION_KEY]
    assert len(stored) == 3
    assert acknowledged_keys[-1] in stored
    assert acknowledged_keys[0] not in stored


@pytest.mark.django_db
def test_virtual_machine_sync_page_omits_the_cables_pane(client, settings):
    """A VM has no cable sync, so neither its tab nor its pane may render."""
    _configure_servers(settings)
    vm = make_vm("cache-vm-cables")
    set_librenms_device_id(vm, 7601, "primary")
    vm.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("cache-vm-cables-user"))
    url = reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", args=[vm.pk])

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda url, **_kwargs: _device_info_response(url, 7601, vm.name, "VM Hardware 7601"),
    ):
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})

    assert response.status_code == 200
    page_html = response.content.decode()
    assert 'id="interfaces"' in page_html
    assert 'id="cables-tab"' not in page_html
    assert 'id="cables"' not in page_html
    assert 'id="cable-sync-content"' not in page_html


@pytest.mark.django_db
@pytest.mark.parametrize("blocked_state", [SyncTabState.INVALIDATED, SyncTabState.REFRESH_FAILED])
def test_blocked_active_tab_reads_device_info_from_cache_only(
    client, settings, django_capture_on_commit_callbacks, blocked_state
):
    """Every blocked tab state must render device details without contacting LibreNMS."""
    _configure_servers(settings)
    device = make_device(f"cache-blocked-{blocked_state.value}", librenms_cf={"primary": {"id": 7801}})
    _seed_snapshot("ip_addresses", device, "primary")
    coordinator = SyncCacheConsistency(device)
    if blocked_state is SyncTabState.INVALIDATED:
        with django_capture_on_commit_callbacks(execute=True):
            coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
    else:
        coordinator.mark_refresh_failure(SyncTab.IP_ADDRESSES, "primary", actor_id=1)
    # Pin the seeding: a state that stopped matching would make the assertions below vacuous.
    assert coordinator.status("primary")[SyncTab.IP_ADDRESSES.value]["state"] == blocked_state.value

    client.force_login(make_superuser(f"cache-blocked-{blocked_state.value}-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("A blocked tab contacted LibreNMS"),
    ) as requests_get:
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.IP_ADDRESSES.value})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"Details unavailable" in response.content


@pytest.mark.django_db
def test_blocked_tab_skips_the_virtual_chassis_inventory_lookup(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A cache-only render must not fetch Virtual Chassis inventory, which has no snapshot."""
    _configure_servers(settings)
    _virtual_chassis, (member, _second) = make_virtual_chassis_members("cache-blocked-vc")
    set_librenms_device_id(member, 7803, "primary")
    member.save(update_fields=["custom_field_data"])
    cache.set(
        "librenms_device_info_primary_7803",
        (True, {"device_id": 7803, "sysName": member.name, "hostname": member.name}),
        timeout=300,
    )
    _seed_snapshot("ip_addresses", member, "primary")
    coordinator = SyncCacheConsistency(member)
    with django_capture_on_commit_callbacks(execute=True):
        coordinator.schedule_mutation(SyncTab.INTERFACES, "primary", actor_id=1)
    # Pin the seeding: a state that stopped matching would make the assertion below vacuous.
    assert coordinator.status("primary")[SyncTab.IP_ADDRESSES.value]["state"] == SyncTabState.INVALIDATED.value

    client.force_login(make_superuser("cache-blocked-vc-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[member.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("A blocked tab fetched Virtual Chassis inventory"),
    ) as requests_get:
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.IP_ADDRESSES.value})

    requests_get.assert_not_called()
    assert response.status_code == 200


@pytest.mark.django_db
def test_blocked_vlan_tab_does_not_render_its_cached_rows(client, settings):
    """A blocked tab must lose its rendered rows whatever context key its table uses."""
    _configure_servers(settings)
    device = make_device("cache-vlan-blocked", librenms_cf={"primary": {"id": 7802}})
    _seed_snapshot("vlans", device, "primary", [{"vlan_vlan": 10, "vlan_name": "CACHED-VLAN-10"}])
    coordinator = SyncCacheConsistency(device)
    coordinator.mark_refresh_failure(SyncTab.VLANS, "primary", actor_id=1)
    # Pin the seeding: the blanking only runs while the snapshot counts as unavailable.
    vlan_status = coordinator.status("primary")[SyncTab.VLANS.value]
    assert vlan_status["state"] == SyncTabState.REFRESH_FAILED.value
    assert vlan_status["snapshot_available"] is False

    client.force_login(make_superuser("cache-vlan-blocked-user"))
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])
    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=lambda request_url, **_kwargs: _device_info_response(
            request_url, 7802, device.name, "VLAN Hardware 7802"
        ),
    ):
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.VLANS.value})

    assert response.status_code == 200
    assert b"CACHED-VLAN-10" not in response.content


@pytest.mark.django_db
def test_failed_duplicate_selection_keeps_the_committed_rows_invalidation(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A failing row must not cancel the cache invalidation another row already earned."""
    _configure_servers(settings)
    device = make_device("cache-ip-duplicate-rows", librenms_cf={"primary": {"id": 68}})
    interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
    set_librenms_device_id(interface, 7801, "primary")
    interface.save(update_fields=["custom_field_data"])
    address = "2001:db8:68::10/64"
    verbose_address = "2001:0DB8:0068:0000:0000:0000:0000:0010/64"
    ip_payload = {
        "ip_addresses": [
            {
                "ip_with_mask": address,
                "port_id": 7801,
                "interface_name": interface.name,
            }
        ]
    }
    _seed_snapshot("ip_addresses", device, "primary", ip_payload)
    _seed_snapshot("ports", device, "primary")
    client.force_login(make_superuser("cache-ip-duplicate-rows-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses",
        kwargs={"object_type": "device", "pk": device.pk},
    )

    classified_rows = []
    real_classify_ip_change = SyncIPAddressesView._classify_ip_change

    def fail_after_the_first_row(self, **kwargs):
        classified_rows.append(kwargs["row_id"])
        if len(classified_rows) > 1:
            raise ValueError("Simulated failure on the repeated selection")
        return real_classify_ip_change(self, **kwargs)

    with patch.object(SyncIPAddressesView, "_classify_ip_change", fail_after_the_first_row):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                url,
                {
                    "server_key": "primary",
                    "select": [address, verbose_address],
                    f"vrf_{address}": "",
                },
            )

    assert response.status_code == 302
    # Both selections must reach the same canonical row, or the failure cannot reach another row.
    assert classified_rows == [address, address]
    assert IPAddress.objects.filter(address=address, assigned_object_id=interface.pk).exists()
    assert cache.get(_cache_key("ip_addresses", device, "primary")) == ip_payload
    assert cache.get(_cache_key("ports", device, "primary")) is None


@pytest.mark.django_db
def test_a_cache_only_render_reports_the_vc_inventory_as_not_loaded(client, settings):
    """A cache-only Virtual Chassis render must not read as a chassis with no serials."""
    _configure_servers(settings)
    _virtual_chassis, members = make_virtual_chassis_members("cacheonly-vc-inventory")
    device = members[0]
    set_librenms_device_id(device, 7801, "primary")
    device.save(update_fields=["custom_field_data"])
    user = make_superuser("cache-only-vc-inventory-user")
    client.force_login(user)
    url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk])

    def librenms_response(request_url, **_kwargs):
        if request_url.endswith("/api/v0/devices/7801"):
            return _device_info_response(request_url, 7801, device.name, "VC Hardware 7801")
        if "/inventory/" in request_url:
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    # Warm the device-info cache so the cache-only render still reports the device as found.
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        warm = client.get(url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})
    assert warm.status_code == 200

    SyncCacheConsistency(device).mark_refresh_failure(SyncTab.INTERFACES, "primary", actor_id=user.pk)

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("the cache-only render contacted LibreNMS"),
    ):
        response = client.get(url, {"server_key": "primary", "tab": SyncTab.INTERFACES.value})

    assert response.status_code == 200
    html = response.content.decode()
    # The device itself is still reported as found, which is why silence about the inventory
    # is indistinguishable from a chassis that genuinely has no serials.
    assert "VC Hardware 7801" in html
    assert "Details unavailable" not in html
    assert "VC Serials not loaded" in html

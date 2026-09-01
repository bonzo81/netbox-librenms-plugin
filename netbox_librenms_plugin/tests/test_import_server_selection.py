"""Request-level tests for transient LibreNMS import server selection."""

import json
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import make_superuser
from netbox_librenms_plugin.tests.import_server_helpers import librenms_device, selector_html
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


@pytest.fixture
def servers(settings, monkeypatch):
    """Run both configured LibreNMS servers over real loopback HTTP."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        running = SimpleNamespace(
            primary=stack.enter_context(librenms_mock_server()),
            secondary=stack.enter_context(librenms_mock_server()),
        )
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            key: {
                "display_name": f"{key.title()} LibreNMS",
                "librenms_url": server.url,
                "api_token": f"{key}-import-test-token",
                "verify_ssl": False,
            }
            for key, server in vars(running).items()
        }
        plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
        settings.PLUGINS_CONFIG = plugin_config
        for server in vars(running).values():
            server.register("/api/v0/resources/locations", {"status": "ok", "locations": []})
        yield running


def _requests_by_server(servers):
    return [(key, request) for key, server in vars(servers).items() for request in server.requests]


def _clear_requests(servers):
    for server in vars(servers).values():
        server.requests.clear()


def _register_device(server, device, *, include_search=True):
    if include_search:
        server.register("/api/v0/devices", {"status": "ok", "devices": [device]})
    server.register(f"/api/v0/devices/{device['device_id']}", {"status": "ok", "devices": [device]})
    for suffix in ("", "/all"):
        server.register(
            f"/api/v0/inventory/{device['device_id']}{suffix}",
            {"status": "ok", "inventory": []},
        )


def test_cached_search_aggregation_omits_expired_stale_and_removed_server_metadata():
    """Only metadata owned by a currently configured server appears in the aggregate."""
    from datetime import datetime, timedelta, timezone

    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import (
        get_active_cached_searches_for_servers,
        get_cache_metadata_key,
    )

    now = datetime.now(timezone.utc)

    def metadata(server_key, cached_at):
        return {
            "server_key": server_key,
            "cached_at": cached_at.isoformat(),
            "cache_timeout": 300,
            "filters": {"hostname": "shared-edge"},
            "vc_enabled": False,
            "use_sysname": True,
            "strip_domain": False,
            "device_count": 1,
        }

    primary_key = get_cache_metadata_key("primary", {"hostname": "shared-edge"}, False)
    stale_key = "librenms_filter_cache_metadata_primary_stale"
    incomplete_key = "librenms_filter_cache_metadata_primary_incomplete"
    secondary_key = get_cache_metadata_key("secondary", {"hostname": "shared-edge"}, False)
    retired_key = get_cache_metadata_key("retired", {"hostname": "shared-edge"}, False)
    cache_keys = [
        "librenms_cache_index_primary",
        "librenms_cache_index_secondary",
        "librenms_cache_index_retired",
        primary_key,
        stale_key,
        incomplete_key,
        secondary_key,
        retired_key,
    ]
    try:
        cache.set("librenms_cache_index_primary", [primary_key, stale_key, incomplete_key], timeout=300)
        cache.set(primary_key, metadata("primary", now), timeout=300)
        cache.set(stale_key, metadata("secondary", now), timeout=300)
        cache.set(
            incomplete_key,
            {
                "server_key": "primary",
                "cached_at": now.isoformat(),
                "cache_timeout": 300,
                "device_count": 1,
            },
            timeout=300,
        )
        cache.set("librenms_cache_index_secondary", [secondary_key], timeout=300)
        cache.set(secondary_key, metadata("secondary", now - timedelta(seconds=600)), timeout=300)
        cache.set("librenms_cache_index_retired", [retired_key], timeout=300)
        cache.set(retired_key, metadata("retired", now), timeout=300)

        searches = get_active_cached_searches_for_servers(
            {"primary": "Primary LibreNMS", "secondary": "Secondary LibreNMS"}
        )

        assert [(search["server_key"], search["cache_key"]) for search in searches] == [("primary", primary_key)]
        assert cache.get("librenms_cache_index_primary") == [primary_key]
        assert cache.get("librenms_cache_index_secondary") == []
        assert cache.get("librenms_cache_index_retired") == [retired_key]
    finally:
        cache.delete_many(cache_keys)


@pytest.mark.django_db
def test_import_starts_on_installation_default_and_offers_transient_server_switches(client, settings, servers):
    """The import selector lists configured servers without changing installation settings."""
    installation_settings, _created = LibreNMSSettings.objects.update_or_create(
        pk=1,
        defaults={"selected_server": "primary"},
    )
    client.force_login(make_superuser("import-server-viewer"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")

    response = client.get(import_url)

    assert response.status_code == 200
    assert [(key, request["path"]) for key, request in _requests_by_server(servers)] == [
        ("primary", "/api/v0/resources/locations")
    ]
    html = response.content.decode()
    selector = selector_html(html)
    assert 'data-active-server-key="primary"' in html
    assert 'aria-current="true"' in selector
    assert "Primary LibreNMS" in selector
    assert f'href="{import_url}?server_key=secondary"' in selector
    assert "Secondary LibreNMS" in selector
    assert "does not change the installation settings" in html
    installation_settings.refresh_from_db()
    assert installation_settings.selected_server == "primary"


@pytest.mark.django_db
def test_import_switches_transient_server_without_search_and_clear_keeps_it(client, settings, servers):
    """Switch and Clear discard search state while retaining the transient server."""
    installation_settings, _created = LibreNMSSettings.objects.update_or_create(
        pk=1,
        defaults={"selected_server": "primary"},
    )
    client.force_login(make_superuser("import-server-switcher"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    response = client.get(import_url, {"server_key": "secondary"})

    assert response.status_code == 200
    assert [(key, request["path"]) for key, request in _requests_by_server(servers)] == [
        ("secondary", "/api/v0/resources/locations")
    ]
    html = response.content.decode()
    assert 'data-active-server-key="secondary"' in html
    assert f'href="{import_url}?server_key=secondary" class="btn btn-secondary"' in html
    assert '<input type="hidden" name="server_key" value="secondary">' in html
    assert response.context["filter_form"].data.get("use_background_job") == "on"
    installation_settings.refresh_from_db()
    assert installation_settings.selected_server == "primary"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "server_key",
    ["retired", "default", " ", ["primary", "secondary"]],
    ids=["unconfigured", "unconfigured-default", "blank", "duplicate"],
)
def test_import_rejects_invalid_server_without_querying_a_fallback(client, settings, servers, server_key):
    """Invalid transient server input cannot start a search on another server."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser(f"invalid-import-server-{type(server_key).__name__}"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")

    response = client.get(
        import_url,
        {
            "server_key": server_key,
            "apply_filters": "1",
            "librenms_hostname": "edge-device",
        },
    )

    assert _requests_by_server(servers) == []
    assert response.status_code == 200
    assert b'id="librenms-import-server-error"' in response.content
    assert b"Select a configured LibreNMS server" in response.content
    assert b'data-active-server-key=""' in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "view_name",
    ["bulk_import_confirm", "bulk_import_devices"],
    ids=["confirmation", "import"],
)
@pytest.mark.parametrize(
    "server_key",
    ["default", "", ["primary", "secondary"]],
    ids=["unconfigured-default", "blank", "duplicate"],
)
def test_import_follow_up_rejects_invalid_server_without_querying_a_fallback(
    client,
    settings,
    servers,
    view_name,
    server_key,
):
    """Confirmation and import actions require one exact configured server key."""
    installation_settings, _created = LibreNMSSettings.objects.update_or_create(
        pk=1,
        defaults={"selected_server": "primary"},
    )
    client.force_login(make_superuser(f"invalid-follow-up-{view_name}-{type(server_key).__name__}"))
    action_url = reverse(f"plugins:netbox_librenms_plugin:{view_name}")

    response = client.post(
        action_url,
        {"server_key": server_key, "select": ["46190"]},
        headers={"HX-Request": "true"},
    )

    assert _requests_by_server(servers) == []
    assert response.status_code == 200
    assert b"no longer configured" in response.content
    installation_settings.refresh_from_db()
    assert installation_settings.selected_server == "primary"


@pytest.mark.django_db
def test_non_htmx_import_follow_up_keeps_invalid_server_fail_closed_after_redirect(client, settings, servers):
    """An invalid follow-up server cannot fall back after a full-page redirect."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("invalid-follow-up-redirect"))
    action_url = reverse("plugins:netbox_librenms_plugin:bulk_import_devices")
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")

    response = client.post(
        action_url,
        {"server_key": "retired", "select": ["46190"]},
        follow=True,
    )

    assert _requests_by_server(servers) == []
    assert response.status_code == 200
    assert response.redirect_chain == [(f"{import_url}?server_key=retired", 302)]
    assert b'id="librenms-import-server-error"' in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "server_key",
    ["default", "", ["primary", "secondary"]],
    ids=["unconfigured-default", "blank", "duplicate"],
)
def test_validation_details_rejects_invalid_server_without_querying_a_fallback(
    client,
    settings,
    servers,
    server_key,
):
    """A validation modal requires one exact configured server key."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser(f"invalid-validation-server-{type(server_key).__name__}"))
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 46192},
    )

    response = client.get(validation_url, {"server_key": server_key})

    assert _requests_by_server(servers) == []
    assert response.status_code == 200
    assert b"no longer configured" in response.content


@pytest.mark.django_db
def test_virtual_chassis_details_queries_only_its_transient_server(client, settings, servers):
    """A Virtual Chassis modal remains pinned to the import result server."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("secondary-vc-details-viewer"))
    vc_url = reverse(
        "plugins:netbox_librenms_plugin:device_vc_details",
        kwargs={"device_id": 46193},
    )
    _register_device(servers.secondary, librenms_device(46193, "secondary-vc-edge"), include_search=False)
    response = client.get(vc_url, {"server_key": "secondary"})

    assert response.status_code == 200
    requests = _requests_by_server(servers)
    assert requests
    assert {key for key, _request in requests} == {"secondary"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "server_key",
    ["default", "", ["primary", "secondary"]],
    ids=["unconfigured-default", "blank", "duplicate"],
)
def test_completed_filter_job_rejects_invalid_server_without_querying_a_fallback(
    client,
    settings,
    servers,
    server_key,
):
    """Persisted job metadata cannot fall back to another configured server."""
    from core.models import Job

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user = make_superuser(f"invalid-job-server-{type(server_key).__name__}")
    client.force_login(user)
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    job = Job.objects.create(
        name="Invalid transient server filter job",
        user=user,
        job_id=uuid4(),
        status="completed",
        data={
            "device_ids": [46191],
            "filters": {"hostname": "invalid-job-server"},
            "server_key": server_key,
        },
    )

    response = client.get(import_url, {"job_id": job.pk})

    assert _requests_by_server(servers) == []
    assert response.status_code == 200
    assert b'id="librenms-import-server-error"' in response.content
    assert b"no longer configured or usable" in response.content
    assert b"Job results have expired" not in response.content


@pytest.mark.django_db
def test_synchronous_import_search_and_cache_are_scoped_to_the_active_server(client, settings, servers):
    """Identical filters on two servers return and cache only each server's rows."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("scoped-import-searcher"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    confirm_url = reverse("plugins:netbox_librenms_plugin:bulk_import_confirm")
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 46102},
    )
    primary = librenms_device(46101, "primary-edge")
    secondary = librenms_device(46102, "secondary-edge")
    _register_device(servers.primary, primary)
    _register_device(servers.secondary, secondary)
    for server in (servers.primary, servers.secondary):
        server.register(
            "/api/v0/resources/locations",
            {"status": "ok", "locations": [{"id": 1, "location": "Test Lab"}]},
        )

    responses = {}
    for server_key in ("primary", "secondary"):
        responses[server_key] = client.get(
            import_url,
            {
                "server_key": server_key,
                "apply_filters": "1",
                "librenms_hostname": "scoped-edge",
                "use_background_job": "",
            },
        )
    confirmation_response = client.post(
        confirm_url,
        {
            "server_key": "secondary",
            "select": ["46102"],
            "use_sysname": "true",
            "strip_domain": "false",
        },
    )

    assert all(response.status_code == 200 for response in responses.values())
    assert b"primary-edge" in responses["primary"].content
    assert b"secondary-edge" not in responses["primary"].content
    assert b"secondary-edge" in responses["secondary"].content
    assert b"primary-edge" not in responses["secondary"].content
    assert f'hx-get="{validation_url}?server_key=secondary"'.encode() in responses["secondary"].content
    assert confirmation_response.status_code == 200
    assert b'id="bulk-import-confirm-form"' in confirmation_response.content
    assert b'<input type="hidden" name="server_key" value="secondary">' in confirmation_response.content
    secondary_html = responses["secondary"].content.decode()
    cached_search_start = secondary_html.index('id="cached-searches-collapse"')
    cached_searches_html = secondary_html[cached_search_start : secondary_html.index("</div>", cached_search_start)]
    assert cached_searches_html.count("data-cached-server-key=") == 2
    assert 'data-cached-server-key="primary"' in cached_searches_html
    assert 'data-cached-server-key="secondary"' in cached_searches_html
    assert "Primary LibreNMS" in cached_searches_html
    assert "Secondary LibreNMS" in cached_searches_html
    cached_search_end = secondary_html.index('title="Click to load this cached search"', cached_search_start)
    cached_search_link = secondary_html[cached_search_start:cached_search_end]
    assert (
        'href="?server_key=secondary&amp;apply_filters=1&amp;librenms_hostname=scoped-edge'
        '&amp;use_sysname=1&amp;strip_domain=0"'
    ) in cached_search_link
    device_searches = [
        (key, request["path"]) for key, request in _requests_by_server(servers) if request["path"] == "/api/v0/devices"
    ]
    assert device_searches == [
        ("primary", "/api/v0/devices"),
        ("secondary", "/api/v0/devices"),
    ]


@pytest.mark.django_db
def test_cached_search_navigation_restores_its_server_and_naming_namespace(client, settings, servers):
    """Opening a cached search restores its server and naming options without a new query."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("cached-search-navigator"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    save_pref_url = reverse("plugins:netbox_librenms_plugin:save_user_pref")
    for server_key, device_id in (("primary", 46111), ("secondary", 46112)):
        server = getattr(servers, server_key)
        _register_device(server, librenms_device(device_id, f"{server_key}-shared-edge"))
        server.register(
            "/api/v0/resources/locations",
            {"status": "ok", "locations": [{"id": 1, "location": f"{server_key.title()} Lab"}]},
        )

    primary_response = client.get(
        import_url,
        {
            "server_key": "primary",
            "apply_filters": "1",
            "librenms_hostname": "shared-edge",
            "use_background_job": "",
        },
    )
    assert primary_response.status_code == 200
    for key, value in (("use_sysname", False), ("strip_domain", True)):
        preference_response = client.post(
            save_pref_url,
            data=json.dumps({"key": key, "value": value}),
            content_type="application/json",
        )
        assert preference_response.status_code == 200
    secondary_response = client.get(
        import_url,
        {
            "server_key": "secondary",
            "apply_filters": "1",
            "librenms_hostname": "shared-edge",
            "use_background_job": "",
        },
    )

    assert secondary_response.status_code == 200
    _clear_requests(servers)
    restored_response = client.get(
        import_url,
        {
            "server_key": "primary",
            "apply_filters": "1",
            "librenms_hostname": "shared-edge",
            "use_sysname": "1",
            "strip_domain": "0",
            "use_background_job": "",
        },
    )

    assert restored_response.status_code == 200
    assert b"primary-shared-edge" in restored_response.content
    assert b"secondary-shared-edge" not in restored_response.content
    assert restored_response.context["use_sysname"] is True
    assert restored_response.context["strip_domain"] is False
    assert _requests_by_server(servers) == []


@pytest.mark.django_db
def test_import_clear_controls_preserve_other_server_cache_namespaces(client, settings, servers):
    """Clear preserves all searches, while Clear cache refreshes only the active namespace."""
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("cached-search-clearer"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    primary_generation = {"fresh": False, "empty": False}

    original_primary = librenms_device(46121, "primary-original-edge")
    refreshed_primary = librenms_device(46123, "primary-refreshed-edge")
    secondary = librenms_device(46122, "secondary-original-edge")

    def primary_search(**_request):
        if primary_generation["empty"]:
            return 200, {"status": "ok", "devices": []}
        device = refreshed_primary if primary_generation["fresh"] else original_primary
        return 200, {"status": "ok", "devices": [device]}

    servers.primary.register("/api/v0/devices", primary_search)
    _register_device(servers.primary, original_primary, include_search=False)
    _register_device(servers.primary, refreshed_primary, include_search=False)
    _register_device(servers.secondary, secondary)
    for server_key, server in vars(servers).items():
        server.register(
            "/api/v0/resources/locations",
            {"status": "ok", "locations": [{"id": 1, "location": f"{server_key.title()} Lab"}]},
        )

    for server_key in ("primary", "secondary"):
        search_response = client.get(
            import_url,
            {
                "server_key": server_key,
                "apply_filters": "1",
                "librenms_hostname": "original-edge",
                "use_background_job": "",
            },
        )
        assert search_response.status_code == 200

    _clear_requests(servers)
    clear_response = client.get(import_url, {"server_key": "primary"})
    clear_html = clear_response.content.decode()
    assert clear_response.status_code == 200
    assert clear_html.count("data-cached-server-key=") == 2
    assert 'data-active-server-key="primary"' in clear_html
    assert _requests_by_server(servers) == []

    primary_generation["fresh"] = True
    refresh_response = client.get(
        import_url,
        {
            "server_key": "primary",
            "apply_filters": "1",
            "librenms_hostname": "original-edge",
            "clear_cache": "on",
            "use_background_job": "",
        },
    )
    assert refresh_response.status_code == 200
    assert b"primary-refreshed-edge" in refresh_response.content
    assert b"secondary-original-edge" not in refresh_response.content
    requests = _requests_by_server(servers)
    assert requests
    assert {key for key, _request in requests} == {"primary"}

    _clear_requests(servers)
    secondary_response = client.get(
        import_url,
        {
            "server_key": "secondary",
            "apply_filters": "1",
            "librenms_hostname": "original-edge",
            "use_sysname": "1",
            "strip_domain": "0",
            "use_background_job": "",
        },
    )

    primary_generation["empty"] = True
    _clear_requests(servers)
    empty_refresh_response = client.get(
        import_url,
        {
            "server_key": "primary",
            "apply_filters": "1",
            "librenms_hostname": "original-edge",
            "clear_cache": "on",
            "use_background_job": "",
        },
    )

    secondary_html = secondary_response.content.decode()
    assert secondary_response.status_code == 200
    assert b"secondary-original-edge" in secondary_response.content
    assert b"primary-refreshed-edge" not in secondary_response.content
    assert secondary_html.count("data-cached-server-key=") == 2
    empty_refresh_html = empty_refresh_response.content.decode()
    assert empty_refresh_response.status_code == 200
    assert 'data-cached-server-key="primary"' not in empty_refresh_html
    assert empty_refresh_html.count('data-cached-server-key="secondary"') == 1
    requests = _requests_by_server(servers)
    assert requests
    assert {key for key, _request in requests} == {"primary"}


@pytest.mark.django_db
def test_completed_filter_job_results_are_private_to_the_job_owner(client, settings, servers):
    """A superuser cannot load another user's cached import results by job ID."""
    from core.models import Job
    from django.contrib.auth import get_user_model
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user_model = get_user_model()
    owner = user_model.objects.create(username="private-job-owner", is_superuser=True, is_active=True)
    requester = user_model.objects.create(username="private-job-requester", is_superuser=True, is_active=True)
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    device_id = 46105
    filters = {"hostname": "private-job-result"}
    cached_device = librenms_device(device_id, "private-job-result")
    cache_key = get_validated_device_cache_key(
        server_key="secondary",
        filters=filters,
        device_id=device_id,
        vc_enabled=False,
        use_sysname=True,
        strip_domain=False,
    )
    cache.set(cache_key, cached_device, timeout=300)
    job = Job.objects.create(
        name="Private transient server filter job",
        user=owner,
        job_id=uuid4(),
        status="completed",
        data={
            "device_ids": [device_id],
            "filters": filters,
            "server_key": "secondary",
            "vc_detection_enabled": False,
            "use_sysname": True,
            "strip_domain": False,
        },
    )
    client.force_login(requester)

    try:
        response = client.get(import_url, {"job_id": job.pk})
    finally:
        cache.delete(cache_key)

    assert response.status_code == 200
    html = response.content.decode()
    marker = "private-job-result"
    marker_index = html.find(marker)
    assert marker_index == -1, html[max(0, marker_index - 200) : marker_index + 200]


@pytest.mark.django_db
def test_non_superuser_job_url_falls_back_to_synchronous_server_search(client, settings, servers):
    """A non-superuser cannot load job data and can still run a server-scoped search."""
    from core.models import Job
    from dcim.models import Device
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import get_validated_device_cache_key
    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user = make_user_with_perms("non-superuser-job-requester", [("view", Device)])
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    job_device_id = 46106
    filters = {"hostname": "private-non-superuser-job-result"}
    cache_key = get_validated_device_cache_key(
        server_key="secondary",
        filters=filters,
        device_id=job_device_id,
        vc_enabled=False,
        use_sysname=True,
        strip_domain=False,
    )
    cache.set(cache_key, librenms_device(job_device_id, "private-non-superuser-job-result"), timeout=300)
    job = Job.objects.create(
        name="Non-superuser transient server filter job",
        user=user,
        job_id=uuid4(),
        status="completed",
        data={
            "device_ids": [job_device_id],
            "filters": filters,
            "server_key": "secondary",
            "vc_detection_enabled": False,
            "use_sysname": True,
            "strip_domain": False,
        },
    )
    client.force_login(user)
    _register_device(servers.primary, librenms_device(46107, "non-superuser-sync-result"))

    try:
        job_response = client.get(import_url, {"job_id": job.pk})
        search_response = client.get(
            import_url,
            {
                "server_key": "primary",
                "apply_filters": "1",
                "librenms_hostname": "sync-result",
                "use_background_job": "1",
            },
        )
    finally:
        cache.delete(cache_key)

    assert job_response.status_code == 200
    assert b"private-non-superuser-job-result" not in job_response.content
    assert search_response.status_code == 200
    assert b"non-superuser-sync-result" in search_response.content
    assert search_response.context["can_use_background_jobs"] is False
    assert ("primary", "/api/v0/devices") in [(key, request["path"]) for key, request in _requests_by_server(servers)]


@pytest.mark.django_db
def test_completed_filter_job_rebinds_results_and_follow_up_forms_to_its_server(client, settings, servers):
    """A completed job remains authoritative when its result URL omits server_key."""
    from core.models import Job

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user = make_superuser("import-job-server-owner")
    client.force_login(user)
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    _register_device(servers.secondary, librenms_device(46103, "secondary-job-edge"))
    search_response = client.get(
        import_url,
        {
            "server_key": "secondary",
            "apply_filters": "1",
            "librenms_hostname": "job-edge",
            "use_background_job": "",
        },
    )

    assert search_response.status_code == 200
    job = Job.objects.create(
        name="Transient server filter job",
        user=user,
        job_id=uuid4(),
        status="completed",
        data={
            "device_ids": [46103],
            "filters": {"hostname": "job-edge"},
            "server_key": "secondary",
            "vc_detection_enabled": False,
            "use_sysname": True,
            "strip_domain": False,
        },
    )
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"]["primary"]["api_token"] = ""
    settings.PLUGINS_CONFIG = plugin_config
    _clear_requests(servers)

    job_response = client.get(import_url, {"job_id": job.pk})

    assert job_response.status_code == 200
    assert [(key, request["path"]) for key, request in _requests_by_server(servers)] == [
        ("secondary", "/api/v0/resources/locations")
    ]
    html = job_response.content.decode()
    assert b"secondary-job-edge" in job_response.content
    assert 'data-active-server-key="secondary"' in html
    assert '<input type="hidden" name="server_key" value="secondary">' in html


@pytest.mark.django_db
def test_background_filter_job_uses_and_records_its_transient_server(settings, servers):
    """The real filter runner queries, caches, and records only its selected server."""
    from core.models import Job
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import get_import_device_cache_key
    from netbox_librenms_plugin.jobs import FilterDevicesJob

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user = make_superuser("import-background-server-owner")
    _register_device(servers.secondary, librenms_device(46104, "secondary-background-edge"))

    job = Job.objects.create(
        name="Transient server background filter",
        user=user,
        job_id=uuid4(),
        data={},
    )
    filters = {"hostname": "background-edge"}

    FilterDevicesJob(job).run(
        filters=filters,
        vc_detection_enabled=False,
        clear_cache=True,
        show_disabled=False,
        server_key="secondary",
    )

    job.refresh_from_db()
    assert job.data["server_key"] == "secondary"
    assert job.data["device_ids"] == [46104]
    assert cache.get(get_import_device_cache_key(46104, "secondary"))["hostname"] == "secondary-background-edge"
    assert cache.get(get_import_device_cache_key(46104, "primary")) is None
    requests = _requests_by_server(servers)
    assert requests
    assert {key for key, _request in requests} == {"secondary"}


@pytest.mark.django_db
@pytest.mark.parametrize("job_kind", ["filter", "import"])
def test_queued_job_rejects_a_server_key_that_is_no_longer_configured(settings, servers, job_kind):
    """A queued job cannot reinterpret a stale server key as the new default."""
    from core.models import Job

    from netbox_librenms_plugin.jobs import FilterDevicesJob, ImportDevicesJob

    user = make_superuser(f"stale-{job_kind}-job-server-owner")
    job = Job.objects.create(
        name=f"Stale transient server {job_kind} job",
        user=user,
        job_id=uuid4(),
        data={},
    )

    with pytest.raises(ValueError, match="configured LibreNMS server"):
        if job_kind == "filter":
            FilterDevicesJob(job).run(
                filters={"hostname": "stale-job-server"},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=False,
                server_key="default",
            )
        else:
            ImportDevicesJob(job).run(
                device_ids=[],
                vm_imports={},
                server_key="default",
            )

    assert _requests_by_server(servers) == []

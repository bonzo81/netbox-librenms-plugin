"""Request-level tests for transient LibreNMS import server selection."""

import json
from copy import deepcopy
from urllib.parse import urlsplit
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.urls import reverse
from requests import Response

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import make_superuser


def _configure_servers(settings):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "primary": {
            "display_name": "Primary LibreNMS",
            "librenms_url": "https://primary.example.com",
            "api_token": "test-token",
        },
        "secondary": {
            "display_name": "Secondary LibreNMS",
            "librenms_url": "https://secondary.example.com",
            "api_token": "test-token",
        },
    }
    settings.PLUGINS_CONFIG = plugin_config


def _selector_html(html):
    start = html.index('id="librenms-server-selector"')
    return html[start : html.index("</ul>", start)]


def _json_response(url, payload):
    response = Response()
    response.status_code = 200
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    return response


def _import_device(device_id, hostname):
    return {
        "device_id": device_id,
        "hostname": hostname,
        "sysName": hostname,
        "location": "",
        "hardware": "",
        "os": "linux",
        "type": "server",
        "serial": "",
        "ip": None,
        "disabled": 0,
        "status": 1,
    }


def _librenms_request_server_key(request_url):
    """Return the configured test server for an exact request hostname."""
    server_keys_by_hostname = {
        "primary.example.com": "primary",
        "secondary.example.com": "secondary",
    }
    hostname = urlsplit(request_url).hostname
    try:
        return server_keys_by_hostname[hostname]
    except KeyError:
        raise AssertionError(f"Unexpected LibreNMS request host: {hostname}") from None


def test_librenms_request_server_key_rejects_hostname_suffix():
    """A hostname that only starts with a configured host is not that server."""
    with pytest.raises(AssertionError, match="Unexpected LibreNMS request host"):
        _librenms_request_server_key("https://primary.example.com.attacker.invalid/api/v0/devices")


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


@pytest.mark.django_db
def test_import_starts_on_installation_default_and_offers_transient_server_switches(client, settings):
    """The import selector lists configured servers without changing installation settings."""
    _configure_servers(settings)
    installation_settings, _created = LibreNMSSettings.objects.update_or_create(
        pk=1,
        defaults={"selected_server": "primary"},
    )
    client.force_login(make_superuser("import-server-viewer"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")

    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://primary.example.com/api/v0/resources/locations":
            return _json_response(request_url, {"status": "ok", "locations": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(import_url)

    assert response.status_code == 200
    assert requested_urls == ["https://primary.example.com/api/v0/resources/locations"]
    html = response.content.decode()
    selector = _selector_html(html)
    assert 'data-active-server-key="primary"' in html
    assert 'aria-current="true"' in selector
    assert "Primary LibreNMS" in selector
    assert f'href="{import_url}?server_key=secondary"' in selector
    assert "Secondary LibreNMS" in selector
    assert "does not change the installation settings" in html
    installation_settings.refresh_from_db()
    assert installation_settings.selected_server == "primary"


@pytest.mark.django_db
def test_import_switches_transient_server_without_search_and_clear_keeps_it(client, settings):
    """Switch and Clear discard search state while retaining the transient server."""
    _configure_servers(settings)
    installation_settings, _created = LibreNMSSettings.objects.update_or_create(
        pk=1,
        defaults={"selected_server": "primary"},
    )
    client.force_login(make_superuser("import-server-switcher"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/resources/locations":
            return _json_response(request_url, {"status": "ok", "locations": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(import_url, {"server_key": "secondary"})

    assert response.status_code == 200
    assert requested_urls == ["https://secondary.example.com/api/v0/resources/locations"]
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
def test_import_rejects_invalid_server_without_querying_a_fallback(client, settings, server_key):
    """Invalid transient server input cannot start a search on another server."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser(f"invalid-import-server-{type(server_key).__name__}"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("An invalid import server contacted LibreNMS"),
    ) as requests_get:
        response = client.get(
            import_url,
            {
                "server_key": server_key,
                "apply_filters": "1",
                "librenms_hostname": "edge-device",
            },
        )

    requests_get.assert_not_called()
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
    view_name,
    server_key,
):
    """Confirmation and import actions require one exact configured server key."""
    _configure_servers(settings)
    installation_settings, _created = LibreNMSSettings.objects.update_or_create(
        pk=1,
        defaults={"selected_server": "primary"},
    )
    client.force_login(make_superuser(f"invalid-follow-up-{view_name}-{type(server_key).__name__}"))
    action_url = reverse(f"plugins:netbox_librenms_plugin:{view_name}")

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("An invalid follow-up server contacted LibreNMS"),
    ) as requests_get:
        response = client.post(
            action_url,
            {"server_key": server_key, "select": ["46190"]},
            headers={"HX-Request": "true"},
        )

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"no longer configured" in response.content
    installation_settings.refresh_from_db()
    assert installation_settings.selected_server == "primary"


@pytest.mark.django_db
def test_non_htmx_import_follow_up_keeps_invalid_server_fail_closed_after_redirect(client, settings):
    """An invalid follow-up server cannot fall back after a full-page redirect."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("invalid-follow-up-redirect"))
    action_url = reverse("plugins:netbox_librenms_plugin:bulk_import_devices")
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("An invalid follow-up server contacted LibreNMS"),
    ) as requests_get:
        response = client.post(
            action_url,
            {"server_key": "retired", "select": ["46190"]},
            follow=True,
        )

    requests_get.assert_not_called()
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
    server_key,
):
    """A validation modal requires one exact configured server key."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser(f"invalid-validation-server-{type(server_key).__name__}"))
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 46192},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("An invalid validation server contacted LibreNMS"),
    ) as requests_get:
        response = client.get(validation_url, {"server_key": server_key})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b"no longer configured" in response.content


@pytest.mark.django_db
def test_virtual_chassis_details_queries_only_its_transient_server(client, settings):
    """A Virtual Chassis modal remains pinned to the import result server."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("secondary-vc-details-viewer"))
    vc_url = reverse(
        "plugins:netbox_librenms_plugin:device_vc_details",
        kwargs={"device_id": 46193},
    )
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/devices/46193":
            return _json_response(
                request_url,
                {"status": "ok", "devices": [_import_device(46193, "secondary-vc-edge")]},
            )
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/46193"):
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
        response = client.get(vc_url, {"server_key": "secondary"})

    assert response.status_code == 200
    assert requested_urls
    assert all(_librenms_request_server_key(url) == "secondary" for url in requested_urls)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "server_key",
    ["default", "", ["primary", "secondary"]],
    ids=["unconfigured-default", "blank", "duplicate"],
)
def test_completed_filter_job_rejects_invalid_server_without_querying_a_fallback(
    client,
    settings,
    server_key,
):
    """Persisted job metadata cannot fall back to another configured server."""
    from core.models import Job

    _configure_servers(settings)
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

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("An invalid job server contacted LibreNMS"),
    ) as requests_get:
        response = client.get(import_url, {"job_id": job.pk})

    requests_get.assert_not_called()
    assert response.status_code == 200
    assert b'id="librenms-import-server-error"' in response.content
    assert b"no longer configured or usable" in response.content
    assert b"Job results have expired" not in response.content


@pytest.mark.django_db
def test_synchronous_import_search_and_cache_are_scoped_to_the_active_server(client, settings):
    """Identical filters on two servers return and cache only each server's rows."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("scoped-import-searcher"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    confirm_url = reverse("plugins:netbox_librenms_plugin:bulk_import_confirm")
    validation_url = reverse(
        "plugins:netbox_librenms_plugin:device_validation_details",
        kwargs={"device_id": 46102},
    )
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url.endswith("/api/v0/resources/locations"):
            return _json_response(
                request_url,
                {"status": "ok", "locations": [{"id": 1, "location": "Test Lab"}]},
            )
        if request_url.endswith("/api/v0/devices"):
            if _librenms_request_server_key(request_url) == "primary":
                devices = [_import_device(46101, "primary-edge")]
            else:
                devices = [_import_device(46102, "secondary-edge")]
            return _json_response(request_url, {"status": "ok", "devices": devices})
        if request_url.endswith("/api/v0/devices/46101"):
            return _json_response(request_url, {"status": "ok", "devices": [_import_device(46101, "primary-edge")]})
        if request_url.endswith("/api/v0/devices/46102"):
            return _json_response(
                request_url,
                {"status": "ok", "devices": [_import_device(46102, "secondary-edge")]},
            )
        if "/api/v0/inventory/" in request_url:
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    responses = {}
    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
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
    device_searches = [url for url in requested_urls if url.endswith("/api/v0/devices")]
    assert device_searches == [
        "https://primary.example.com/api/v0/devices",
        "https://secondary.example.com/api/v0/devices",
    ]


@pytest.mark.django_db
def test_cached_search_navigation_restores_its_server_and_naming_namespace(client, settings):
    """Opening a cached search restores its server and naming options without a new query."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("cached-search-navigator"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    save_pref_url = reverse("plugins:netbox_librenms_plugin:save_user_pref")
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        server_key = _librenms_request_server_key(request_url)
        device_id = 46111 if server_key == "primary" else 46112
        device = _import_device(device_id, f"{server_key}-shared-edge")
        if request_url.endswith("/api/v0/resources/locations"):
            return _json_response(
                request_url,
                {"status": "ok", "locations": [{"id": 1, "location": f"{server_key.title()} Lab"}]},
            )
        if request_url.endswith("/api/v0/devices"):
            return _json_response(request_url, {"status": "ok", "devices": [device]})
        if request_url.endswith(f"/api/v0/devices/{device_id}"):
            return _json_response(request_url, {"status": "ok", "devices": [device]})
        if "/api/v0/inventory/" in request_url:
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
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
        requested_urls.clear()
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
    assert requested_urls == []


@pytest.mark.django_db
def test_import_clear_controls_preserve_other_server_cache_namespaces(client, settings):
    """Clear preserves all searches, while Clear cache refreshes only the active namespace."""
    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    client.force_login(make_superuser("cached-search-clearer"))
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    requested_urls = []
    primary_generation = {"fresh": False, "empty": False}

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        server_key = _librenms_request_server_key(request_url)
        if server_key == "primary" and primary_generation["fresh"]:
            device_id = 46123
            hostname = "primary-refreshed-edge"
        elif server_key == "primary":
            device_id = 46121
            hostname = "primary-original-edge"
        else:
            device_id = 46122
            hostname = "secondary-original-edge"
        device = _import_device(device_id, hostname)
        if request_url.endswith("/api/v0/resources/locations"):
            return _json_response(
                request_url,
                {"status": "ok", "locations": [{"id": 1, "location": f"{server_key.title()} Lab"}]},
            )
        if request_url.endswith("/api/v0/devices"):
            if server_key == "primary" and primary_generation["empty"]:
                return _json_response(request_url, {"status": "ok", "devices": []})
            return _json_response(request_url, {"status": "ok", "devices": [device]})
        if request_url.endswith(f"/api/v0/devices/{device_id}"):
            return _json_response(request_url, {"status": "ok", "devices": [device]})
        if "/api/v0/inventory/" in request_url:
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
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

        requested_urls.clear()
        clear_response = client.get(import_url, {"server_key": "primary"})
        clear_html = clear_response.content.decode()
        assert clear_response.status_code == 200
        assert clear_html.count("data-cached-server-key=") == 2
        assert 'data-active-server-key="primary"' in clear_html
        assert requested_urls == []

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
        assert requested_urls
        assert all(_librenms_request_server_key(url) == "primary" for url in requested_urls)

        requested_urls.clear()
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
        requested_urls.clear()
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
    assert requested_urls
    assert all(_librenms_request_server_key(url) == "primary" for url in requested_urls)


@pytest.mark.django_db
def test_completed_filter_job_rebinds_results_and_follow_up_forms_to_its_server(client, settings):
    """A completed job remains authoritative when its result URL omits server_key."""
    from core.models import Job

    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user = make_superuser("import-job-server-owner")
    client.force_login(user)
    import_url = reverse("plugins:netbox_librenms_plugin:librenms_import")
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/resources/locations":
            return _json_response(request_url, {"status": "ok", "locations": []})
        if request_url == "https://secondary.example.com/api/v0/devices":
            return _json_response(
                request_url,
                {"status": "ok", "devices": [_import_device(46103, "secondary-job-edge")]},
            )
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/46103"):
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
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
        requested_urls.clear()

        job_response = client.get(import_url, {"job_id": job.pk})

    assert job_response.status_code == 200
    assert requested_urls == ["https://secondary.example.com/api/v0/resources/locations"]
    html = job_response.content.decode()
    assert b"secondary-job-edge" in job_response.content
    assert 'data-active-server-key="secondary"' in html
    assert '<input type="hidden" name="server_key" value="secondary">' in html


@pytest.mark.django_db
def test_background_filter_job_uses_and_records_its_transient_server(settings):
    """The real filter runner queries, caches, and records only its selected server."""
    from core.models import Job
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import get_import_device_cache_key
    from netbox_librenms_plugin.jobs import FilterDevicesJob

    _configure_servers(settings)
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "primary"})
    user = make_superuser("import-background-server-owner")
    requested_urls = []

    def librenms_response(request_url, **_kwargs):
        requested_urls.append(request_url)
        if request_url == "https://secondary.example.com/api/v0/devices":
            return _json_response(
                request_url,
                {"status": "ok", "devices": [_import_device(46104, "secondary-background-edge")]},
            )
        if request_url.startswith("https://secondary.example.com/api/v0/inventory/46104"):
            return _json_response(request_url, {"status": "ok", "inventory": []})
        raise AssertionError(f"Unexpected LibreNMS request: {request_url}")

    job = Job.objects.create(
        name="Transient server background filter",
        user=user,
        job_id=uuid4(),
        data={},
    )
    filters = {"hostname": "background-edge"}

    with patch("netbox_librenms_plugin.librenms_api.requests.get", side_effect=librenms_response):
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
    assert requested_urls
    assert all(_librenms_request_server_key(url) == "secondary" for url in requested_urls)


@pytest.mark.django_db
@pytest.mark.parametrize("job_kind", ["filter", "import"])
def test_queued_job_rejects_a_server_key_that_is_no_longer_configured(settings, job_kind):
    """A queued job cannot reinterpret a stale server key as the new default."""
    from core.models import Job

    from netbox_librenms_plugin.jobs import FilterDevicesJob, ImportDevicesJob

    _configure_servers(settings)
    user = make_superuser(f"stale-{job_kind}-job-server-owner")
    job = Job.objects.create(
        name=f"Stale transient server {job_kind} job",
        user=user,
        job_id=uuid4(),
        data={},
    )

    with patch(
        "netbox_librenms_plugin.librenms_api.requests.get",
        side_effect=AssertionError("A stale queued-job server contacted LibreNMS"),
    ) as requests_get:
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

    requests_get.assert_not_called()

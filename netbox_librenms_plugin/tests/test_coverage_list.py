"""Behavior tests for the LibreNMS device import list."""

from copy import deepcopy
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.http import QueryDict
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


SERVER_KEY = "test-server"


def _configure_server(settings, server):
    """Point the configured server at the local HTTP test server."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "display_name": "Test LibreNMS",
            "librenms_url": server.url,
            "api_token": "test-token",
            "verify_ssl": False,
            "cache_timeout": 300,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(settings, monkeypatch):
    """Run a local HTTP server for real LibreNMS client requests."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        _configure_server(settings, server)
        server.register(
            "/api/v0/resources/locations",
            {
                "status": "ok",
                "locations": [{"id": 7, "location": "Test Lab"}],
            },
        )
        yield server


def _device(device_id=4101, *, disabled=0):
    """Return one complete LibreNMS device payload."""
    return {
        "device_id": device_id,
        "hostname": f"edge-{device_id}.example.test",
        "sysName": f"edge-{device_id}",
        "location": "Test Lab",
        "hardware": "Test Router",
        "os": "linux",
        "type": "network",
        "serial": f"SERIAL-{device_id}",
        "ip": f"198.18.0.{device_id % 250 + 1}",
        "disabled": disabled,
        "status": 1,
    }


def _register_devices(server, devices, requests=None):
    """Serve device searches and inventory through the real HTTP client."""
    if requests is None:
        requests = []

    def device_search(**request):
        requests.append(request)
        return 200, {"status": "ok", "devices": devices}

    server.register("/api/v0/devices", device_search, method="GET")
    for device in devices:
        server.inventory_response(device["device_id"], [])
        server.register(
            f"/api/v0/inventory/{device['device_id']}",
            {"status": "ok", "inventory": []},
            method="GET",
        )
    return requests


def _create_user(django_user_model, username, *, superuser=True):
    """Create one active real user for an import request."""
    return django_user_model.objects.create_user(
        username=username,
        is_active=True,
        is_superuser=superuser,
    )


def _import_url():
    return reverse("plugins:netbox_librenms_plugin:librenms_import")


def _search_params(**overrides):
    params = {
        "server_key": SERVER_KEY,
        "apply_filters": "1",
        "librenms_hostname": "edge",
        "use_background_job": "",
    }
    params.update(overrides)
    return params


class TestCachedSearchOption:
    """Explicit cached-search toggles override preferences without ambiguity."""

    @pytest.mark.parametrize(
        ("values", "fallback", "expected"),
        [
            (["1"], False, True),
            (["0"], True, False),
            ([], True, True),
            (["1", "0"], False, False),
        ],
    )
    def test_only_one_explicit_boolean_overrides_the_fallback(self, values, fallback, expected):
        from netbox_librenms_plugin.views.imports.list import _cached_search_option

        data = QueryDict("", mutable=True)
        data.setlist("use_sysname", values)

        assert _cached_search_option(data, "use_sysname", fallback) is expected


class TestImportListContract:
    """Small view contracts use real users, requests, querysets, and tables."""

    def test_required_permission_is_the_real_device_view_permission(self):
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        assert LibreNMSImportView().get_required_permission() == "dcim.view_device"

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("superuser", "form_value", "expected"),
        [
            (False, True, False),
            (True, True, True),
            (True, False, False),
            (True, None, True),
        ],
    )
    def test_background_processing_follows_the_actor_and_submitted_option(
        self,
        django_user_model,
        superuser,
        form_value,
        expected,
    ):
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        request = RequestFactory().get("/")
        request.user = _create_user(
            django_user_model,
            f"background-{superuser}-{form_value}",
            superuser=superuser,
        )
        view = LibreNMSImportView()
        view.request = request
        view._filter_form_data = {} if form_value is None else {"use_background_job": form_value}

        assert view.should_use_background_job() is expected

    @pytest.mark.django_db
    def test_queryset_and_table_use_real_netbox_types(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tables.device_status import DeviceImportTable
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        import_data = [_device()]
        view = LibreNMSImportView()
        view._job_results_loaded = True
        view._import_data = import_data
        view._active_server_key = SERVER_KEY
        request = RequestFactory().get(_import_url(), {"sort": "hostname"})

        queryset = view.get_queryset(request)
        table = view.get_table(queryset, request)

        assert queryset.model is Device
        assert not queryset.exists()
        assert isinstance(table, DeviceImportTable)
        assert list(table.data) == import_data
        assert table.server_key == SERVER_KEY

    def test_import_queryset_stays_empty_until_a_valid_search_exists(self):
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        view = LibreNMSImportView()
        view._job_results_loaded = False
        view._filters_submitted = False

        assert view._get_import_queryset() == []
        assert view._libre_filters == {}

        view._filters_submitted = True
        view._filter_warning = "Select at least one filter"
        assert view._get_import_queryset() == []
        assert view._libre_filters == {}


class TestCompletedJobResults:
    """Completed jobs are resolved through the real Job model and cache."""

    @pytest.mark.django_db
    def test_completed_job_loads_only_live_cached_rows_and_restores_its_options(
        self,
        django_user_model,
        librenms_server,
    ):
        from core.models import Job

        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        user = _create_user(django_user_model, "completed-job")
        filters = {"hostname": "edge"}
        cached_device = _device(4201)
        job = Job.objects.create(
            name="Completed import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={
                "device_ids": [4201, 4202],
                "filters": filters,
                "server_key": SERVER_KEY,
                "vc_detection_enabled": True,
                "use_sysname": False,
                "strip_domain": True,
                "cached_at": "2026-08-30T12:00:00+00:00",
                "cache_timeout": 600,
            },
        )
        cache.set(
            get_validated_device_cache_key(
                server_key=SERVER_KEY,
                filters=filters,
                device_id=4201,
                vc_enabled=True,
                use_sysname=False,
                strip_domain=True,
            ),
            cached_device,
            timeout=600,
        )

        view = LibreNMSImportView()
        result = view._load_job_results(job.pk, user)

        assert result == [cached_device]
        assert view._active_server_key == SERVER_KEY
        assert view._cache_timestamp == "2026-08-30T12:00:00+00:00"
        assert view._cache_timeout == 600
        assert view._vc_detection_enabled is True
        assert view._use_sysname is False
        assert view._strip_domain is True

    @pytest.mark.django_db
    @pytest.mark.parametrize("status", ["pending", "running", "failed"])
    def test_unfinished_job_has_no_import_rows(self, django_user_model, librenms_server, status):
        from core.models import Job

        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        user = _create_user(django_user_model, f"unfinished-job-{status}")
        job = Job.objects.create(
            name="Unfinished import search",
            user=user,
            job_id=uuid4(),
            status=status,
            data={"device_ids": [4301], "server_key": SERVER_KEY},
        )

        assert LibreNMSImportView()._load_job_results(job.pk, user) == []

    @pytest.mark.django_db
    def test_job_owned_by_another_user_is_not_visible(self, django_user_model, librenms_server):
        from core.models import Job

        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        owner = _create_user(django_user_model, "job-owner")
        viewer = _create_user(django_user_model, "job-viewer")
        job = Job.objects.create(
            name="Private import search",
            user=owner,
            job_id=uuid4(),
            status="completed",
            data={"device_ids": [4401], "server_key": SERVER_KEY},
        )

        assert LibreNMSImportView()._load_job_results(job.pk, viewer) == []

    @pytest.mark.django_db
    def test_job_for_removed_server_fails_closed(self, django_user_model, settings, librenms_server):
        from core.models import Job

        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        user = _create_user(django_user_model, "removed-job-server")
        job = Job.objects.create(
            name="Removed server import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={"device_ids": [4501], "server_key": "retired"},
        )

        view = LibreNMSImportView()
        assert view._load_job_results(job.pk, user) == []
        assert view._active_server_key is None
        assert view._librenms_api is None
        assert "no longer configured or usable" in view._server_selection_error


class TestImportListRequest:
    """The routed import page exercises real HTTP, forms, cache, ORM, and tables."""

    @pytest.mark.django_db
    def test_initial_page_uses_the_requested_server_and_real_location_choices(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        user = _create_user(django_user_model, "initial-import-page")
        client.force_login(user)

        response = client.get(_import_url(), {"server_key": SERVER_KEY})

        assert response.status_code == 200
        assert response.context["server_key"] == SERVER_KEY
        assert response.context["server_selection_active_name"] == "Test LibreNMS"
        assert response.context["can_use_background_jobs"] is True
        assert list(response.context["filter_form"].fields["librenms_location"].choices) == [
            ("", "All Locations"),
            ("7", "Test Lab"),
        ]
        assert list(response.context["table"].data) == []

    @pytest.mark.django_db
    def test_synchronous_search_renders_the_real_validated_device(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        from dcim.models import DeviceType, Manufacturer, Site

        manufacturer = Manufacturer.objects.create(name="Test Vendor", slug="test-vendor")
        DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Test Router",
            slug="test-router",
        )
        Site.objects.create(name="Test Lab", slug="test-lab")
        device = _device()
        requests = _register_devices(librenms_server, [device])
        user = _create_user(django_user_model, "synchronous-search")
        client.force_login(user)

        response = client.get(_import_url(), _search_params())

        assert response.status_code == 200
        rows = list(response.context["table"].data)
        assert [row["device_id"] for row in rows] == [device["device_id"]]
        assert rows[0]["_validation"]["site"]["site"].name == "Test Lab"
        assert rows[0]["_validation"]["device_type"]["device_type"].model == "Test Router"
        assert response.context["filters_submitted"] is True
        assert response.context["cache_timestamp"]
        assert response.context["cache_metadata_missing"] is False
        assert len(requests) == 1

    @pytest.mark.django_db
    def test_every_filter_reaches_the_real_search_and_client_side_matching(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        device = _device(4601)
        requests = _register_devices(librenms_server, [device])
        user = _create_user(django_user_model, "all-import-filters")
        client.force_login(user)

        response = client.get(
            _import_url(),
            _search_params(
                librenms_location="7",
                librenms_type="network",
                librenms_os="linux",
                librenms_hostname="edge-4601",
                librenms_sysname="edge-4601",
                librenms_hardware="Test Router",
            ),
        )

        assert response.status_code == 200
        assert [row["device_id"] for row in response.context["table"].data] == [4601]
        assert requests[0]["query"] == {"type": ["location_id"], "query": ["7"]}

    @pytest.mark.django_db
    def test_repeated_search_uses_the_real_cache_without_another_http_fetch(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        requests = _register_devices(librenms_server, [_device(4701)])
        user = _create_user(django_user_model, "cached-search")
        client.force_login(user)

        first = client.get(_import_url(), _search_params())
        second = client.get(_import_url(), _search_params())

        assert first.status_code == second.status_code == 200
        assert len(requests) == 1
        assert [row["device_id"] for row in second.context["table"].data] == [4701]
        assert second.context["from_cache"] is True

    @pytest.mark.django_db
    def test_clear_cache_forces_a_fresh_http_search(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        requests = _register_devices(librenms_server, [_device(4801)])
        user = _create_user(django_user_model, "clear-search-cache")
        client.force_login(user)

        first = client.get(_import_url(), _search_params())
        refreshed = client.get(_import_url(), _search_params(clear_cache="on"))

        assert first.status_code == refreshed.status_code == 200
        assert len(requests) >= 2
        assert refreshed.context["cache_cleared"] is True

    @pytest.mark.django_db
    @pytest.mark.parametrize(("show_disabled", "expected_ids"), [("", []), ("on", [4901])])
    def test_disabled_device_visibility_follows_the_submitted_option(
        self,
        client,
        django_user_model,
        librenms_server,
        show_disabled,
        expected_ids,
    ):
        _register_devices(librenms_server, [_device(4901, disabled=1)])
        user = _create_user(django_user_model, f"disabled-search-{bool(show_disabled)}")
        client.force_login(user)

        response = client.get(_import_url(), _search_params(show_disabled=show_disabled))

        assert response.status_code == 200
        assert [row["device_id"] for row in response.context["table"].data] == expected_ids

    @pytest.mark.django_db
    def test_invalid_search_reports_the_real_form_error_without_querying_devices(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        requests = _register_devices(librenms_server, [_device(5001)])
        user = _create_user(django_user_model, "invalid-search")
        client.force_login(user)

        response = client.get(
            _import_url(),
            {
                "server_key": SERVER_KEY,
                "apply_filters": "1",
                "use_background_job": "",
            },
        )

        assert response.status_code == 200
        assert response.context["show_filter_warning"] is True
        assert "Please select at least one LibreNMS filter" in response.context["filter_warning"]
        assert requests == []

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"enable_vc_detection": "1"}, True),
            ({"enable_vc_detection": "0"}, False),
            ({"skip_vc_detection": "1"}, False),
            ({"skip_vc_detection": "0"}, True),
        ],
    )
    def test_current_and_legacy_vc_options_render_the_same_intent(
        self,
        client,
        django_user_model,
        librenms_server,
        params,
        expected,
    ):
        user = _create_user(django_user_model, f"vc-option-{expected}-{len(params)}")
        client.force_login(user)

        response = client.get(_import_url(), {"server_key": SERVER_KEY, **params})

        assert response.status_code == 200
        assert response.context["vc_detection_enabled"] is expected

    @pytest.mark.django_db
    def test_settings_defaults_and_cached_search_overrides_are_rendered(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.update_or_create(
            pk=1,
            defaults={
                "selected_server": SERVER_KEY,
                "use_sysname_default": False,
                "strip_domain_default": True,
            },
        )
        user = _create_user(django_user_model, "import-settings-defaults")
        client.force_login(user)

        defaults = client.get(_import_url(), {"server_key": SERVER_KEY})
        overrides = client.get(
            _import_url(),
            {
                "server_key": SERVER_KEY,
                "use_sysname": "1",
                "strip_domain": "0",
            },
        )

        assert defaults.context["use_sysname"] is False
        assert defaults.context["strip_domain"] is True
        assert overrides.context["use_sysname"] is True
        assert overrides.context["strip_domain"] is False

    @pytest.mark.django_db
    def test_expired_completed_job_renders_one_actionable_warning(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        from core.models import Job

        user = _create_user(django_user_model, "expired-job-results")
        client.force_login(user)
        job = Job.objects.create(
            name="Expired import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={
                "device_ids": [5101],
                "filters": {"hostname": "edge"},
                "server_key": SERVER_KEY,
            },
        )

        response = client.get(_import_url(), {"job_id": job.pk})

        assert response.status_code == 200
        assert b"Job results have expired. Please re-apply your filters." in response.content
        assert list(response.context["table"].data) == []

    @pytest.mark.django_db
    def test_invalid_job_identifier_does_not_break_the_import_page(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        user = _create_user(django_user_model, "invalid-job-id")
        client.force_login(user)

        response = client.get(_import_url(), {"job_id": "not-an-integer"})

        assert response.status_code == 200
        assert response.context["filters_submitted"] is False
        assert list(response.context["table"].data) == []

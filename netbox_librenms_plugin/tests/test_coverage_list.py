"""Behavior tests for the LibreNMS device import list."""

from copy import deepcopy
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.http import QueryDict
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
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
            (["1", "0"], True, True),
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
        request = RequestFactory().get(_import_url(), {"import_sort": "-hostname"})
        request.user = make_superuser()

        queryset = view.get_queryset(request)
        table = view.get_table(queryset, request)

        assert queryset.model is Device
        assert not queryset.exists()
        assert isinstance(table, DeviceImportTable)
        assert list(table.data) == import_data
        assert table.server_key == SERVER_KEY
        assert tuple(table.order_by) == ("-hostname",)

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
        from netbox_librenms_plugin.import_utils.cache import serial_rules_version
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        user = _create_user(django_user_model, "completed-job")
        filters = {"hostname": "edge"}
        cached_device = _device(4201)
        cached_device["_validation"] = validate_device_for_import(
            dict(cached_device), api=None, server_key=SERVER_KEY, include_vc_detection=False
        )
        job = Job.objects.create(
            name="Completed import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={
                "device_ids": [4201, 4202],
                "filters": filters,
                "server_key": SERVER_KEY,
                "serial_rules": serial_rules_version(),
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
        # Mapped after the job cached the row: the loader must refresh the cached match.
        mapped = make_device("completed-job-mapped", librenms_cf={SERVER_KEY: {"id": 4201}})

        view = LibreNMSImportView()
        result = view._load_job_results(job.pk, user)

        assert [row["device_id"] for row in result] == [4201]
        assert result[0]["_validation"]["existing_device"] == mapped
        assert result[0]["_validation"]["existing_match_type"] == "librenms_id"
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
    def test_an_unreachable_search_reports_the_outage_instead_of_queueing_a_job(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """
        The device-count preflight failing means LibreNMS did not answer.

        should_use_background_job() never looks at that count, so a superuser still queued a job
        that only repeats the failing call, and the outage disappeared behind a polling response.
        """
        from unittest.mock import patch

        from core.models import Job

        def unreachable(**request):
            return 500, {"status": "error", "message": "LibreNMS is down"}

        librenms_server.register("/api/v0/devices", unreachable, method="GET")
        user = _create_user(django_user_model, "unreachable-preflight")
        client.force_login(user)

        # Workers are available and background mode is on, so the preflight is the only thing
        # that can keep the job from being enqueued.
        with patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue", return_value=1):
            response = client.get(_import_url(), _search_params(use_background_job="1"))

        assert response.status_code == 200
        # A rendered page, not the JSON polling payload the background branch returns.
        assert response.context is not None and "table" in response.context
        assert not Job.objects.exists()

    @pytest.mark.django_db
    def test_a_serial_rule_edit_makes_a_warm_search_cold_for_the_background_decision(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """The validated rows key on the serial rules, so after a rule edit the search queues a job instead."""
        from unittest.mock import patch

        from core.models import Job

        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")
        _register_devices(librenms_server, [_device()])
        client.force_login(_create_user(django_user_model, "serial-rule-readiness"))
        # A synchronous search caches the validated rows and the search metadata.
        assert client.get(_import_url(), _search_params()).status_code == 200

        with patch("netbox_librenms_plugin.views.imports.list.get_workers_for_queue", return_value=1):
            warm = client.get(_import_url(), _search_params(use_background_job="1"))
            assert warm.context is not None and "table" in warm.context
            assert not Job.objects.exists()

            # An edit keeps the rule PK and the rule count; only the pattern changes.
            NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern=r"^SN:\s*(.+)$")
            cold = client.get(_import_url(), _search_params(use_background_job="1"))

        assert cold.json()["use_polling"] is True
        assert Job.objects.count() == 1

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
    def test_a_search_binds_no_device_that_stores_the_text_of_a_json_boolean_serial(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """A Device stores "True" and LibreNMS sends `serial: true`; the batch serial lookup reads the value."""
        make_device("search-boolean-text-holder", serial="True")
        _register_devices(librenms_server, [{**_device(4116), "serial": True}])
        client.force_login(_create_user(django_user_model, "search-boolean-serial"))

        response = client.get(_import_url(), _search_params())

        (row,) = response.context["table"].data
        assert row["_validation"]["existing_device"] is None
        assert row["_validation"]["existing_match_type"] is None

    @pytest.mark.django_db
    def test_a_warm_search_validates_a_row_again_that_stopped_at_an_unavailable_serial(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """Validation stops before the site and type checks, so the cached row must not be reused once the lookup reads."""
        from dcim.models import Device, DeviceType, Manufacturer, Site

        from netbox_librenms_plugin.import_utils.device_operations import is_serial_unavailable_issue
        from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member

        manufacturer = Manufacturer.objects.create(name="Test Vendor", slug="test-vendor")
        DeviceType.objects.create(manufacturer=manufacturer, model="Test Router", slug="test-router")
        Site.objects.create(name="Test Lab", slug="test-lab")
        device = _device(4111)
        stray = _stray_chassis_member("warm-unavailable", device["serial"])
        _register_devices(librenms_server, [device])
        client.force_login(_create_user(django_user_model, "warm-unavailable-serial"))

        def validation():
            response = client.get(_import_url(), _search_params())
            (row,) = response.context["table"].data
            return response, row["_validation"]

        _first, blocked = validation()
        assert any(is_serial_unavailable_issue(issue) for issue in blocked["issues"])
        assert blocked["site"]["found"] is False

        # The stray member gets another serial, so the lookup reads and finds no holder.
        Device.objects.filter(pk=stray.pk).update(serial="WARM-UNAVAILABLE-OTHER")
        second, revalidated = validation()

        assert second.context["from_cache"] is True
        assert not any(is_serial_unavailable_issue(issue) for issue in revalidated["issues"])
        assert revalidated["site"]["site"].name == "Test Lab"
        assert revalidated["device_type"]["device_type"].model == "Test Router"

    @pytest.mark.django_db
    def test_a_warm_search_checks_the_holders_of_a_legacy_linked_serial_again(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """The cached row confirms the migration on the serial, so a Device that takes the serial later must show."""
        from django.template.loader import render_to_string

        payload = _device(4121)
        make_device(payload["sysName"], serial=payload["serial"], librenms_cf=payload["device_id"])
        _register_devices(librenms_server, [payload])
        client.force_login(_create_user(django_user_model, "warm-legacy-serial"))

        def validation():
            response = client.get(_import_url(), _search_params())
            (row,) = response.context["table"].data
            return response, row["_validation"]

        _first, cached = validation()
        assert cached["existing_match_type"] == "librenms_id"
        assert cached["librenms_id_needs_migration"] is True
        assert cached["legacy_id_serial_confirmed"] is True

        make_device("warm-legacy-serial-rival", serial=payload["serial"])
        second, warm = validation()

        assert second.context["from_cache"] is True
        assert warm["legacy_id_serial_confirmed"] is False
        assert warm["legacy_id_serial_unavailable"] is False
        assert warm["legacy_id_serial_shared"] is True
        details = render_to_string(
            "netbox_librenms_plugin/htmx/device_validation_details.html",
            {"validation": warm, "libre_device": payload, "server_key": SERVER_KEY},
        )
        assert 'data-serial-state="shared"' in details
        assert 'class="btn btn-sm btn-warning migrate-btn" disabled>' in details

    @pytest.mark.django_db
    def test_a_warm_search_drops_the_serial_confirmation_of_a_changed_stored_serial(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """A stored serial that changed after caching confirms nothing, so the row reads neither confirmed nor shared."""
        from dcim.models import Device

        payload = _device(4122)
        device = make_device(payload["sysName"], serial=payload["serial"], librenms_cf=payload["device_id"])
        _register_devices(librenms_server, [payload])
        client.force_login(_create_user(django_user_model, "warm-legacy-changed-serial"))

        def validation():
            response = client.get(_import_url(), _search_params())
            (row,) = response.context["table"].data
            return response, row["_validation"]

        _first, cached = validation()
        assert cached["serial_confirmed"] is True
        assert cached["legacy_id_serial_confirmed"] is True

        Device.objects.filter(pk=device.pk).update(serial="REPLACED-CHASSIS")
        second, warm = validation()

        assert second.context["from_cache"] is True
        assert warm["serial_confirmed"] is False
        assert warm["legacy_id_serial_confirmed"] is False
        assert warm["legacy_id_serial_shared"] is False

    @pytest.mark.django_db
    def test_a_search_shows_no_countdown_for_metadata_of_other_serial_rules(
        self,
        client,
        django_user_model,
        librenms_server,
        monkeypatch,
    ):
        """Searches under other serial rules share the metadata key, so the countdown must not use their time."""
        from netbox_librenms_plugin.import_utils import process_device_filters
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import SerialRules

        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")
        _register_devices(librenms_server, [_device(4131)])
        client.force_login(_create_user(django_user_model, "countdown-other-rules"))

        def search_then_other_rules_search(**kwargs):
            result = process_device_filters(**kwargs)
            # Another request edits a rule and runs the same search before this response reads the metadata.
            NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern=r"^SN:\s*(.+)$")
            process_device_filters(**{**kwargs, "serial_rules": SerialRules.load()})
            return result

        monkeypatch.setattr(
            "netbox_librenms_plugin.views.imports.list.process_device_filters", search_then_other_rules_search
        )
        response = client.get(_import_url(), _search_params())

        assert [row["device_id"] for row in response.context["table"].data] == [4131]
        assert response.context["cache_timestamp"] is None
        assert response.context["cache_metadata_missing"] is False
        assert 'id="cache-info-display"' not in response.content.decode()

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
    def test_the_background_results_page_reapplies_exclude_existing(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """The page that a finished search opens keeps the form options, so the cached search filters again."""
        from core.models import Job

        requests = _register_devices(librenms_server, [_device(4801), _device(4802)])
        user = _create_user(django_user_model, "results-exclude-existing")
        client.force_login(user)
        params = _search_params(exclude_existing="on")
        first = client.get(_import_url(), params)
        assert sorted(row["device_id"] for row in first.context["table"].data) == [4801, 4802]
        make_device("results-exclude-existing-mapped", librenms_cf={SERVER_KEY: {"id": 4801}})
        job = Job.objects.create(
            name="Completed import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={"device_ids": [4801, 4802], "filters": {"hostname": "edge"}, "server_key": SERVER_KEY},
        )

        # The background search opens this URL: the form parameters and the job ID.
        results = client.get(_import_url(), {**params, "use_background_job": "on", "job_id": job.pk})

        assert results.status_code == 200
        # Only the mapped row goes: the unmapped row stays in the cached result.
        assert [row["device_id"] for row in results.context["table"].data] == [4802]
        assert results.context["from_cache"] is True
        assert len(requests) == 1

    @pytest.mark.django_db
    def test_the_background_results_page_keeps_a_row_blocked_that_stopped_at_an_unavailable_serial(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """The results page cannot validate again, so a partial row keeps its blocker until a new search."""
        from core.models import Job
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.cache import serial_rules_version
        from netbox_librenms_plugin.import_utils.device_operations import is_serial_unavailable_issue
        from netbox_librenms_plugin.tests.test_coverage_device_operations import _stray_chassis_member

        device = _device(4112)
        stray = _stray_chassis_member("results-unavailable", device["serial"])
        _register_devices(librenms_server, [device])
        user = _create_user(django_user_model, "results-unavailable-serial")
        client.force_login(user)
        client.get(_import_url(), _search_params())
        Device.objects.filter(pk=stray.pk).update(serial="RESULTS-UNAVAILABLE-OTHER")
        job = Job.objects.create(
            name="Completed import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={
                "device_ids": [4112],
                "filters": {"hostname": "edge"},
                "server_key": SERVER_KEY,
                "serial_rules": serial_rules_version(),
            },
        )

        # Without filter parameters the page reads the job's cached rows.
        results = client.get(_import_url(), {"job_id": job.pk})

        (row,) = results.context["table"].data
        assert any(is_serial_unavailable_issue(issue) for issue in row["_validation"]["issues"])
        assert row["_validation"]["can_import"] is False

    @pytest.mark.django_db
    @pytest.mark.parametrize("linked", [True, False])
    def test_the_background_results_page_refreshes_a_row_whose_location_quotes_the_unavailable_text(
        self,
        client,
        django_user_model,
        librenms_server,
        linked,
    ):
        """The site issue quotes the LibreNMS location, so a location with the blocker's words is no blocker."""
        from core.models import Job

        from netbox_librenms_plugin.import_utils.cache import serial_rules_version
        from netbox_librenms_plugin.import_validation_helpers import SITE_ISSUE_PREFIX

        device_id = 4114 if linked else 4115
        location = "Site cannot be matched: annex"
        device = {**_device(device_id), "location": location}
        _register_devices(librenms_server, [device])
        user = _create_user(django_user_model, f"results-quoted-location-{linked}")
        client.force_login(user)
        (searched,) = client.get(_import_url(), _search_params()).context["table"].data
        site_issue = f"{SITE_ISSUE_PREFIX} '{location}'"
        assert site_issue in searched["_validation"]["issues"]
        if linked:
            holder = make_device("results-quoted-location-linked", librenms_cf={SERVER_KEY: {"id": device_id}})
        job = Job.objects.create(
            name="Completed import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={
                "device_ids": [device_id],
                "filters": {"hostname": "edge"},
                "server_key": SERVER_KEY,
                "serial_rules": serial_rules_version(),
            },
        )

        results = client.get(_import_url(), {"job_id": job.pk})

        (row,) = results.context["table"].data
        validation = row["_validation"]
        if linked:
            # The refresh finds the Device that was linked after the search.
            assert validation["existing_device"] == holder
            assert site_issue not in validation["issues"]
        else:
            # The refresh keeps the site issue: it is no stale serial blocker.
            assert validation["existing_device"] is None
            assert site_issue in validation["issues"]
        assert validation["can_import"] is False

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
    def test_a_serial_rule_edit_removes_the_cached_search_link_it_made_stale(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        """A link to a search of other serial rules would only start validation again, so the page omits it."""
        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")
        _register_devices(librenms_server, [{**_device(5301), "hostname": "stale-link-edge.example.test"}])
        client.force_login(_create_user(django_user_model, "stale-cached-search-link"))
        filters = {"hostname": "stale-link-edge"}
        assert client.get(_import_url(), _search_params(librenms_hostname="stale-link-edge")).status_code == 200

        def linked_filters():
            response = client.get(_import_url(), {"server_key": SERVER_KEY})
            assert response.status_code == 200
            return [search["filters"] for search in response.context["cached_searches"]]

        assert filters in linked_filters()
        NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern=r"^SN:\s*(.+)$")
        assert filters not in linked_filters()

    @pytest.mark.django_db
    def test_a_synchronous_search_reads_the_serial_rules_of_its_cached_search_link(
        self, client, django_user_model, librenms_server, monkeypatch
    ):
        """A rule edit after the request read its rules must not split the rows from the link to them."""
        from netbox_librenms_plugin.import_utils.cache import serial_rules_version
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.serial_identity import SerialRules

        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")
        _register_devices(librenms_server, [{**_device(5302), "hostname": "one-version-edge.example.test"}])
        client.force_login(_create_user(django_user_model, "one-serial-rule-version"))
        load = SerialRules.load
        loads = []

        def edit_after_the_first_load():
            rules = load()
            if not loads:
                # The request's snapshot reads its rows before the edit commits.
                assert rules.digest
                NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern=r"^SN:\s*(.+)$")
            loads.append(rules)
            return rules

        monkeypatch.setattr(SerialRules, "load", edit_after_the_first_load)

        response = client.get(_import_url(), _search_params(librenms_hostname="one-version-edge"))

        assert response.status_code == 200
        (snapshot,) = loads
        (search,) = [
            search
            for search in response.context["cached_searches"]
            if search["filters"] == {"hostname": "one-version-edge"}
        ]
        assert search["serial_rules"] == serial_rules_version(snapshot)

    @pytest.mark.django_db
    @pytest.mark.parametrize("version", ["changed", "missing"])
    def test_job_only_results_require_the_serial_rule_version_used_by_the_job(
        self, client, django_user_model, librenms_server, version
    ):
        from core.choices import JobStatusChoices
        from core.models import Job

        from netbox_librenms_plugin.jobs import FilterDevicesJob
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts

        user = _create_user(django_user_model, "versioned-job-results")
        client.force_login(user)
        rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")

        def run_search(*ids):
            _register_devices(librenms_server, [_device(device_id) for device_id in ids])
            job = Job.objects.create(name="Serial rule search", user=user, job_id=uuid4(), data={})
            FilterDevicesJob.handle(
                job=job,
                filters={"hostname": "edge"},
                vc_detection_enabled=False,
                clear_cache=True,
                show_disabled=False,
                server_key=SERVER_KEY,
            )
            job.refresh_from_db()
            assert job.status == JobStatusChoices.STATUS_COMPLETED, job.error
            return job

        def open_results(job):
            response = client.get(_import_url(), {"job_id": job.pk})
            assert response.status_code == 200
            return response

        first = run_search(41, 42)
        assert sorted(row["device_id"] for row in open_results(first).context["table"].data) == [41, 42]

        if version == "changed":
            NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern=r"^SN:\s*(.+)$")
            second = run_search(42, 43)
            assert sorted(row["device_id"] for row in open_results(second).context["table"].data) == [42, 43]
        else:
            first.data.pop("serial_rules", None)
            first.save(update_fields=["data"])

        response = open_results(first)

        assert list(response.context["table"].data) == []
        assert len(message_texts(response.wsgi_request, "warning")) == 1

    @pytest.mark.django_db
    def test_expired_completed_job_renders_one_actionable_warning(
        self,
        client,
        django_user_model,
        librenms_server,
    ):
        from core.models import Job

        from netbox_librenms_plugin.import_utils.cache import serial_rules_version

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
                "serial_rules": serial_rules_version(),
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

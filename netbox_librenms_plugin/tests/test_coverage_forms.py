"""Form behavior tests with real settings, cache, HTTP, and ORM objects."""

import pytest
from django.core.cache import cache
from django.http import QueryDict

from netbox_librenms_plugin.tests.conftest import configure_no_librenms_servers, make_device


pytestmark = pytest.mark.django_db


def _filter_form(data=None):
    from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

    if data is None:
        return LibreNMSImportFilterForm(librenms_api=None)
    return LibreNMSImportFilterForm(data, librenms_api=None)


class TestLibreNMSFilterFormBackgroundJobDefault:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({}, "on"),
            ({"show_disabled": "on"}, None),
            ({"librenms_hostname": "switch-01.example"}, None),
            ({"use_background_job": "off"}, "off"),
            ({"page": "2"}, None),
            ({"server_key": "default"}, "on"),
        ],
    )
    def test_default_is_only_injected_for_an_initial_bound_form(self, data, expected):
        assert _filter_form(data).data.get("use_background_job") == expected

    def test_unbound_form_does_not_inject_bound_data(self):
        assert _filter_form().data.get("use_background_job") is None

    def test_original_querydict_is_not_mutated(self):
        data = QueryDict("")

        form = _filter_form(data)

        assert form.data.get("use_background_job") == "on"
        assert data.get("use_background_job") is None

    def test_explicit_filter_submission_requires_a_filter(self):
        form = _filter_form({"apply_filters": "1"})

        assert form.is_valid() is False
        assert "Please select at least one LibreNMS filter" in form.non_field_errors()[0]


class TestPollerGroupChoices:
    def test_real_http_result_is_cached_per_server(self, live_librenms):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        live_librenms.api.cache_timeout = 300
        live_librenms.server.register(
            "/api/v0/poller_group",
            {
                "status": "ok",
                "get_poller_group": [
                    {"id": 2, "group_name": "Edge", "descr": "Edge pollers"},
                    {"id": 1, "group_name": "Core", "descr": ""},
                ],
            },
        )
        cache_key = "librenms_poller_group_choices_default"
        cache.delete(cache_key)

        first = _get_librenms_poller_group_choices("default")
        second = _get_librenms_poller_group_choices("default")

        assert first == [
            ("0", "Default (0)"),
            ("2", "Edge - Edge pollers (2)"),
            ("1", "Core (1)"),
        ]
        assert second == first
        assert cache.get(cache_key) == first
        assert [request["path"] for request in live_librenms.server.requests] == ["/api/v0/poller_group"]

    def test_unconfigured_server_returns_only_the_default(self, settings):
        from netbox_librenms_plugin.forms import _get_librenms_poller_group_choices

        configure_no_librenms_servers(settings)
        assert _get_librenms_poller_group_choices("missing") == [("0", "Default (0)")]


class TestDeviceImportConfigForm:
    def test_real_model_querysets_and_suggestions_set_initial_values(self):
        from dcim.models import DeviceRole, DeviceType, Platform, Site

        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        device = make_device("form-import-prerequisites")
        site = Site.objects.get(pk=device.site_id)
        device_type = DeviceType.objects.get(pk=device.device_type_id)
        role = DeviceRole.objects.get(pk=device.role_id)
        platform = Platform.objects.create(name="Form Platform", slug="form-platform")
        validation = {
            "site": {"site": site},
            "device_type": {"device_type": device_type, "suggestions": [{"device_type": device_type}]},
            "device_role": {"role": role},
            "platform": {"platform": platform},
        }

        form = DeviceImportConfigForm(
            libre_device={
                "device_id": 42,
                "hostname": "switch-01.example",
                "hardware": "MODEL-42",
                "location": "Lab",
            },
            validation=validation,
        )

        assert form.fields["device_id"].initial == 42
        assert form.fields["hostname"].initial == "switch-01.example"
        assert form.fields["hardware"].initial == "MODEL-42"
        assert form.fields["site"].initial == site
        assert form.fields["device_type"].initial == device_type
        assert form.fields["device_role"].initial == role
        assert form.fields["platform"].initial == platform
        assert list(form.fields["device_type"].queryset).index(device_type) == 0

    def test_empty_inputs_leave_librenms_initial_values_blank(self):
        from netbox_librenms_plugin.forms import DeviceImportConfigForm

        form = DeviceImportConfigForm(libre_device={}, validation={})

        assert form.fields["hostname"].initial in (None, "")
        assert form.fields["hardware"].initial in (None, "")


class TestAddToLibreSNMPV3:
    _BASE_DATA = {
        "hostname": "198.18.0.1",
        "snmp_version": "v3",
        "authname": "test-user",
    }

    def _form(self, extra, live_librenms):
        from netbox_librenms_plugin.forms import AddToLibreSNMPV3

        live_librenms.server.register(
            "/api/v0/poller_group",
            {"status": "ok", "get_poller_group": []},
        )
        cache.delete("librenms_poller_group_choices_default")
        return AddToLibreSNMPV3(data={**self._BASE_DATA, **extra}, server_key="default")

    def test_no_auth_no_priv_needs_no_secret(self, live_librenms):
        form = self._form({"authlevel": "noAuthNoPriv"}, live_librenms)

        assert form.is_valid(), form.errors

    def test_auth_no_priv_requires_authentication_fields(self, live_librenms):
        form = self._form({"authlevel": "authNoPriv"}, live_librenms)

        assert form.is_valid() is False
        assert {"authpass", "authalgo"} <= set(form.errors)

    def test_auth_priv_accepts_all_required_fields(self, live_librenms):
        form = self._form(
            {
                "authlevel": "authPriv",
                "authpass": "test-auth-value",
                "authalgo": "SHA",
                "cryptopass": "test-crypto-value",
                "cryptoalgo": "AES",
            },
            live_librenms,
        )

        assert form.is_valid(), form.errors

    def test_auth_priv_requires_privacy_fields(self, live_librenms):
        form = self._form(
            {"authlevel": "authPriv", "authpass": "test-auth-value", "authalgo": "SHA"},
            live_librenms,
        )

        assert form.is_valid() is False
        assert {"cryptopass", "cryptoalgo"} <= set(form.errors)

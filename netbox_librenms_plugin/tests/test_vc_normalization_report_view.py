"""End-to-end tests for the VC name-rewrite no-op report modal."""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_device_with_module_bays,
    make_module_type,
    make_superuser,
)

pytestmark = pytest.mark.django_db


def _url(device):
    return reverse("plugins:netbox_librenms_plugin:vc_normalization_report", kwargs={"pk": device.pk})


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _module_on(device, tag, template_names):
    """Install a module whose interface templates carry *template_names*."""
    from dcim.models import InterfaceTemplate, Module

    module_type = make_module_type(f"vc-report-type-{tag}")
    for name in template_names:
        InterfaceTemplate.objects.create(module_type=module_type, name=name, type="other")
    return Module.objects.create(
        device=device,
        module_bay=device.modulebays.get(name="Bay 0"),
        module_type=module_type,
        status="active",
    )


def _vc_member_with_module(tag, template_names=("2/x1/1/c9",)):
    """Return (page_device, module) where the device is VC member 2 and the names never rewrite."""
    from dcim.models import VirtualChassis

    vc = VirtualChassis.objects.create(name=f"vc-report-{tag}")
    first = make_device(f"vc-report-{tag}-1")
    first.virtual_chassis = vc
    first.vc_position = 1
    first.save()
    device = make_device_with_module_bays(f"vc-report-{tag}-2", ["Bay 0"])
    device.virtual_chassis = vc
    device.vc_position = 2
    device.save()
    return device, _module_on(device, tag, template_names)


class TestVCNormalizationReportView:
    """The report answers only for a real no-op on a module the user may actually see."""

    def test_missing_module_id_returns_400(self, client):
        device, _module = _vc_member_with_module("missing-id")
        client.force_login(make_superuser())

        response = client.get(_url(device))

        assert response.status_code == 400
        assert b"Missing or invalid module_id" in response.content

    def test_non_numeric_module_id_returns_400(self, client):
        device, _module = _vc_member_with_module("bad-id")
        client.force_login(make_superuser())

        response = client.get(_url(device), {"module_id": "nope"})

        assert response.status_code == 400
        assert b"Missing or invalid module_id" in response.content

    def test_a_module_on_another_device_is_not_reportable(self, client):
        other, other_module = _vc_member_with_module("other-owner")
        device = make_device("vc-report-requester")
        client.force_login(make_superuser())

        response = client.get(_url(device), {"module_id": str(other_module.pk)})

        assert response.status_code == 404
        other_module.refresh_from_db()
        assert other_module.device_id == other.pk

    def test_a_module_that_rewrites_cleanly_has_nothing_to_report(self, client):
        device, module = _vc_member_with_module("clean", template_names=("TenGigabitEthernet1/1/1",))
        client.force_login(make_superuser())

        response = client.get(_url(device), {"module_id": str(module.pk)})

        assert response.status_code == 400
        assert b"No VC name-rewrite no-op detected" in response.content

    def test_the_report_carries_the_real_diagnostic(self, client):
        device, module = _vc_member_with_module("report")
        client.force_login(make_superuser())

        response = client.get(_url(device), {"module_id": str(module.pk)})

        assert response.status_code == 200
        markdown = response.context["report_markdown"]
        assert module.module_type.model in markdown
        assert "2/x1/1/c9" in markdown
        assert "Bay 0" in markdown
        assert "- VC position (target): 2" in markdown
        assert b'id="vc-report-textarea"' in response.content

    def test_an_invalid_selected_device_warns_and_falls_back_to_the_page_device(self, client):
        device, module = _vc_member_with_module("bad-selection")
        client.force_login(make_superuser())

        response = client.get(_url(device), {"module_id": str(module.pk), "selected_device_id": "not-a-pk"})

        assert response.status_code == 200
        assert any("falling back to the page device" in text for text in _messages(response))

    def test_a_sibling_member_selection_reports_that_member_s_module(self, client):
        device, module = _vc_member_with_module("sibling")
        page_device = device.virtual_chassis.members.exclude(pk=device.pk).first()
        client.force_login(make_superuser())

        response = client.get(
            _url(page_device),
            {"module_id": str(module.pk), "selected_device_id": str(device.pk)},
        )

        assert response.status_code == 200
        assert module.module_type.model in response.context["report_markdown"]
        assert not _messages(response)

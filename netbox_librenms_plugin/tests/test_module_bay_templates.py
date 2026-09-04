"""Real view and ORM tests for the add-bay-template modal and its mapping suggestion."""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_superuser,
)

pytestmark = pytest.mark.django_db


def _url(device):
    return reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk})


def _messages(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def _mapping(**kwargs):
    from netbox_librenms_plugin.models import ModuleBayMapping

    return ModuleBayMapping.objects.create(**kwargs)


def _shared_manufacturer():
    """The manufacturer the shared test device type belongs to."""
    return make_device("bay-mapping-scope-anchor").device_type.manufacturer


class TestDeriveMappingPattern:
    """The regex rule derived from the operator's two names must round-trip, or not be written."""

    @pytest.mark.parametrize(
        ("librenms_name", "netbox_name", "pattern", "replacement", "digits"),
        [
            ("Sfm 1", "SFM 1", r"^Sfm\ (\d+)$", r"SFM \1", 1),
            ("0/FT0", "Fan Tray 0", r"^(\d+)/FT\1$", r"Fan Tray \1", 1),
            ("slot1/sub2", "Slot 1 Sub 2", r"^slot(\d+)/sub(\d+)$", r"Slot \1 Sub \2", 2),
        ],
    )
    def test_matching_digit_runs_become_capture_groups(self, librenms_name, netbox_name, pattern, replacement, digits):
        import re

        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        derived = AddBayTemplateView._derive_mapping_pattern(librenms_name, netbox_name)

        assert derived == {
            "kind": "regex",
            "librenms_pattern": pattern,
            "netbox_replacement": replacement,
            "digit_count": digits,
        }
        assert re.compile(pattern).sub(replacement, librenms_name) == netbox_name

    @pytest.mark.parametrize(
        ("librenms_name", "netbox_name"),
        [
            ("", "Slot 1"),
            ("Slot 1", ""),
            ("Slot A", "Bay A"),
            ("0/FT0", "Fan Tray 1"),
            ("Slot 1", "Bay 1 of 2"),
        ],
    )
    def test_names_that_cannot_round_trip_derive_nothing(self, librenms_name, netbox_name):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        assert AddBayTemplateView._derive_mapping_pattern(librenms_name, netbox_name) is None

    def test_a_repeated_digit_becomes_a_backreference_that_rejects_mismatched_siblings(self):
        import re

        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        derived = AddBayTemplateView._derive_mapping_pattern("0/FT0", "Fan Tray 0")

        compiled = re.compile(derived["librenms_pattern"])
        assert compiled.fullmatch("1/FT1")
        assert compiled.fullmatch("0/FT1") is None


class TestExistingMappingLookup:
    """The modal only offers a new mapping when no real row already covers the LibreNMS name."""

    def test_blank_librenms_name_matches_nothing(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        _mapping(librenms_name="PSU 1", librenms_class="powerSupply", netbox_bay_name="PS1")
        _mapping(librenms_name=r"^PSU (\d+)$", librenms_class="", netbox_bay_name=r"PS\1", is_regex=True)

        assert AddBayTemplateView._existing_bay_mapping("", "powerSupply", None) is False
        assert AddBayTemplateView._existing_regex_mapping_covers("", "", None) is False

    def test_regex_row_covering_the_name_is_found_and_a_broken_row_is_skipped(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        # A row saved before pattern validation existed still has to be walked past, not raise.
        from netbox_librenms_plugin.models import ModuleBayMapping

        ModuleBayMapping.objects.bulk_create(
            [
                ModuleBayMapping(librenms_name="PSU(", librenms_class="", netbox_bay_name="PS", is_regex=True),
                ModuleBayMapping(
                    librenms_name=r"^PSU (\d+)$", librenms_class="", netbox_bay_name=r"PS\1", is_regex=True
                ),
            ]
        )

        assert AddBayTemplateView._existing_regex_mapping_covers("PSU 1", "", None) is True
        assert AddBayTemplateView._existing_regex_mapping_covers("FAN 1", "", None) is False

    def test_vendor_scope_narrows_an_exact_row(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        manufacturer = _shared_manufacturer()
        _mapping(
            librenms_name="PSU 1",
            librenms_class="powerSupply",
            netbox_bay_name="PS1",
            manufacturer=manufacturer,
        )

        assert AddBayTemplateView._existing_bay_mapping("PSU 1", "powerSupply", manufacturer) is True
        # The global scope must not see a vendor-scoped row.
        assert AddBayTemplateView._existing_bay_mapping("PSU 1", "powerSupply", None) is False


class TestAddBayTemplateModalRender:
    """The GET render decides, server-side, whether a mapping can be offered at all."""

    def _get(self, client, device, **params):
        return client.get(_url(device), params)

    def test_tampered_target_kind_returns_400(self, client):
        device = make_device("bay-modal-tampered-kind")
        client.force_login(make_superuser())

        response = self._get(client, device, target_kind="bogus", target_pk="1")

        assert response.status_code == 400
        assert b"Invalid target_kind" in response.content

    def test_non_numeric_target_pk_returns_400(self, client):
        device = make_device("bay-modal-bad-pk")
        client.force_login(make_superuser())

        response = self._get(client, device, target_kind="device_type", target_pk="not-a-pk")

        assert response.status_code == 400
        assert b"Missing or invalid target_pk" in response.content

    def test_digit_names_render_a_regex_default(self, client):
        device = make_device("bay-modal-regex-default")
        client.force_login(make_superuser())

        response = self._get(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            librenms_name="0/FT0",
            librenms_class="fan",
            suggested_name="Fan Tray 0",
        )

        assert response.status_code == 200
        assert response.context["offer_mapping_checkbox"] is True
        assert response.context["mapping_exists"] is False
        assert response.context["mapping_default_kind"] == "regex"
        assert response.context["mapping_pattern"]["librenms_pattern"] == r"^(\d+)/FT\1$"

    def test_without_a_librenms_name_no_mapping_is_offered(self, client):
        device = make_device("bay-modal-no-libre-name")
        client.force_login(make_superuser())

        response = self._get(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            suggested_name="Slot 1",
        )

        assert response.status_code == 200
        assert response.context["offer_mapping_checkbox"] is False
        assert response.context["mapping_exists"] is False
        assert response.context["mapping_pattern"] is None
        assert response.context["mapping_default_kind"] == "exact"

    def test_an_existing_regex_row_suppresses_the_offer(self, client):
        device = make_device("bay-modal-covered")
        _mapping(librenms_name=r"^(\d+)/FT\d+$", librenms_class="fan", netbox_bay_name=r"Fan Tray \1", is_regex=True)
        client.force_login(make_superuser())

        response = self._get(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            librenms_name="0/FT0",
            librenms_class="fan",
            suggested_name="Fan Tray 0",
        )

        assert response.status_code == 200
        assert response.context["mapping_exists"] is True
        assert response.context["offer_mapping_checkbox"] is False


class TestAddBayTemplateSubmission:
    """A submitted bay template lands on the type, on existing instances, and optionally as a mapping."""

    def _post(self, client, device, **data):
        return client.post(_url(device), data)

    def test_non_numeric_target_pk_writes_nothing(self, client):
        from dcim.models import ModuleBayTemplate

        device = make_device("bay-post-bad-pk")
        client.force_login(make_superuser())

        response = self._post(client, device, target_kind="device_type", target_pk="", name="Slot 1")

        assert response.status_code == 302
        assert "Missing or invalid target_pk for bay template." in _messages(response)
        assert not ModuleBayTemplate.objects.filter(name="Slot 1").exists()

    def test_blank_name_writes_nothing(self, client):
        from dcim.models import ModuleBayTemplate

        device = make_device("bay-post-blank-name")
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            name="   ",
        )

        assert response.status_code == 302
        assert "Bay template name is required." in _messages(response)
        assert not ModuleBayTemplate.objects.filter(device_type=device.device_type).exists()

    def test_regex_mapping_is_derived_and_the_bay_lands_on_the_existing_device(self, client):
        from dcim.models import ModuleBay, ModuleBayTemplate

        from netbox_librenms_plugin.models import ModuleBayMapping

        device = make_device_with_module_bays("bay-post-regex", [])
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            name="Fan Tray 0",
            librenms_name="0/FT0",
            librenms_class="fan",
            also_create_mapping="1",
            mapping_kind="regex",
        )

        assert response.status_code == 302
        assert ModuleBayTemplate.objects.filter(device_type=device.device_type, name="Fan Tray 0").exists()
        assert ModuleBay.objects.filter(device=device, module__isnull=True, name="Fan Tray 0").exists()
        mapping = ModuleBayMapping.objects.get(librenms_class="fan")
        assert mapping.is_regex is True
        assert mapping.librenms_name == r"^(\d+)/FT\1$"
        assert mapping.netbox_bay_name == r"Fan Tray \1"
        assert mapping.manufacturer == device.device_type.manufacturer
        text = " ".join(_messages(response))
        assert "regex ModuleBayMapping" in text
        assert f"(scoped to {device.device_type.manufacturer})" in text
        assert "Bay added to 1 existing device." in text

    def test_a_name_pair_without_digits_falls_back_to_an_exact_mapping(self, client):
        from netbox_librenms_plugin.models import ModuleBayMapping

        device = make_device_with_module_bays("bay-post-exact-fallback", [])
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            name="Bay A",
            librenms_name="Slot A",
            librenms_class="container",
            also_create_mapping="1",
            mapping_kind="regex",
        )

        assert response.status_code == 302
        mapping = ModuleBayMapping.objects.get(librenms_class="container")
        assert mapping.is_regex is False
        assert (mapping.librenms_name, mapping.netbox_bay_name) == ("Slot A", "Bay A")
        text = " ".join(_messages(response))
        assert "regex ModuleBayMapping" not in text
        assert "ModuleBayMapping 'Slot A'" in text

    def test_a_mapping_added_since_the_modal_rendered_is_not_duplicated(self, client):
        from dcim.models import ModuleBayTemplate

        from netbox_librenms_plugin.models import ModuleBayMapping

        device = make_device_with_module_bays("bay-post-race", [])
        _mapping(
            librenms_name="PSU 1",
            librenms_class="powerSupply",
            netbox_bay_name="PS1",
            manufacturer=device.device_type.manufacturer,
        )
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            name="PS1",
            librenms_name="PSU 1",
            librenms_class="powerSupply",
            also_create_mapping="1",
            mapping_kind="exact",
        )

        assert response.status_code == 302
        assert ModuleBayMapping.objects.filter(librenms_name="PSU 1").count() == 1
        assert ModuleBayTemplate.objects.filter(device_type=device.device_type, name="PS1").exists()
        text = " ".join(_messages(response))
        assert "ModuleBayMapping" not in text

    def test_an_already_present_bay_is_not_instantiated_twice(self, client):
        from dcim.models import ModuleBay

        device = make_device_with_module_bays("bay-post-already-there", [])
        ModuleBay.objects.create(device=device, name="Slot 1")
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="device_type",
            target_pk=str(device.device_type.pk),
            name="Slot 1",
        )

        assert response.status_code == 302
        assert ModuleBay.objects.filter(device=device, module__isnull=True, name="Slot 1").count() == 1
        text = " ".join(_messages(response))
        assert "Bay added to" not in text

    def test_a_module_type_template_lands_on_the_installed_module(self, client):
        from dcim.models import ModuleBay, ModuleBayTemplate

        device = make_device_with_module_bays("bay-post-module-type", ["Slot 1"])
        module = install_module(device, "Slot 1", "BAY-POST-CARD")
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="module_type",
            target_pk=str(module.module_type.pk),
            name="Sub Bay 1",
        )

        assert response.status_code == 302
        assert ModuleBayTemplate.objects.filter(module_type=module.module_type, name="Sub Bay 1").exists()
        assert ModuleBay.objects.filter(device=device, module=module, name="Sub Bay 1").exists()
        assert "Bay added to 1 existing module." in " ".join(_messages(response))

    def test_a_module_type_bay_already_present_is_left_alone(self, client):
        from dcim.models import ModuleBay

        device = make_device_with_module_bays("bay-post-module-type-dupe", ["Slot 1"])
        module = install_module(device, "Slot 1", "BAY-POST-DUPE-CARD")
        ModuleBay.objects.create(device=device, module=module, name="Sub Bay 1")
        client.force_login(make_superuser())

        response = self._post(
            client,
            device,
            target_kind="module_type",
            target_pk=str(module.module_type.pk),
            name="Sub Bay 1",
        )

        assert response.status_code == 302
        assert ModuleBay.objects.filter(device=device, module=module, name="Sub Bay 1").count() == 1
        assert "Bay added to" not in " ".join(_messages(response))

"""VC member numbering rules: precedence (A6) and the native operator surface (A7), against the real database."""

import pytest
import yaml
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_librenms_plugin.models import RuleConfigurationError, VCMemberNumberingRule
from netbox_librenms_plugin.tests.conftest import make_superuser

pytestmark = pytest.mark.django_db

API_LIST = "plugins-api:netbox_librenms_plugin-api:vcmembernumberingrule-list"


def _url(name, *args):
    return reverse(f"plugins:netbox_librenms_plugin:vcmembernumberingrule_{name}", args=args)


@pytest.fixture
def cisco():
    from dcim.models import Manufacturer

    return Manufacturer.objects.create(name="Numbering Cisco", slug="numbering-cisco")


@pytest.fixture
def juniper():
    from dcim.models import Manufacturer

    return Manufacturer.objects.create(name="Numbering Juniper", slug="numbering-juniper")


def _type(manufacturer, model):
    from dcim.models import DeviceType

    return DeviceType.objects.create(manufacturer=manufacturer, model=model, slug=model.lower())


def _rule(manufacturer=None, pattern="", mode="one_based"):
    return VCMemberNumberingRule.objects.create(
        manufacturer=manufacturer, device_type_pattern=pattern, numbering_mode=mode
    )


def _winner(device_type):
    from netbox_librenms_plugin.import_utils.virtual_chassis import load_numbering_rules, select_numbering_rule

    return select_numbering_rule(load_numbering_rules(), device_type)


class TestPrecedence:
    """A6: manufacturer and pattern, then pattern, then manufacturer, then unscoped; the lowest id in a tier."""

    def test_each_tier_beats_the_tiers_below_it(self, cisco):
        device_type = _type(cisco, "C9300-48P")
        unscoped = _rule()
        assert _winner(device_type) == unscoped
        by_manufacturer = _rule(cisco)
        assert _winner(device_type) == by_manufacturer
        by_pattern = _rule(pattern="C9300-.*")
        assert _winner(device_type) == by_pattern
        both = _rule(cisco, "C9300-48P")
        assert _winner(device_type) == both

    def test_the_lowest_id_wins_between_two_matching_patterns_of_one_tier(self, cisco):
        device_type = _type(cisco, "C9300-48P")
        first = _rule(cisco, "C9300-.*", "reported")
        _rule(cisco, "C.*-48P", "one_based")

        assert _winner(device_type) == first

    def test_another_manufacturers_rule_never_applies(self, cisco, juniper):
        _rule(juniper)

        assert _winner(_type(cisco, "C9300-48P")) is None

    @pytest.mark.parametrize("pattern", ["c9300-48p", "C9300", "9300-48P"])
    def test_the_pattern_matches_the_whole_model_case_sensitively(self, cisco, pattern):
        _rule(cisco, pattern)

        assert _winner(_type(cisco, "C9300-48P")) is None

    def test_a_broken_candidate_refuses_even_when_another_rule_would_win(self, cisco, juniper):
        _rule(cisco, "C9300-48P")
        (broken,) = VCMemberNumberingRule.objects.bulk_create(
            [VCMemberNumberingRule(manufacturer=cisco, device_type_pattern="C9300-(", numbering_mode="reported")]
        )
        VCMemberNumberingRule.objects.bulk_create(
            [VCMemberNumberingRule(manufacturer=juniper, device_type_pattern="EX-(", numbering_mode="reported")]
        )

        with pytest.raises(RuleConfigurationError) as excinfo:
            _winner(_type(cisco, "C9300-48P"))
        assert excinfo.value.rule_pk == broken.pk

    @pytest.mark.parametrize("scoped", [True, False])
    def test_an_exact_selector_is_unique(self, cisco, scoped):
        manufacturer = cisco if scoped else None
        _rule(manufacturer, "C9300-.*")

        with pytest.raises(ValidationError):
            _rule(manufacturer, "C9300-.*", "reported")

    def test_a_pattern_that_does_not_compile_is_refused_on_save(self, cisco):
        with pytest.raises(ValidationError) as excinfo:
            _rule(cisco, "C9300-(")
        assert "device_type_pattern" in excinfo.value.message_dict

    def test_a_manufacturer_with_a_rule_cannot_be_deleted(self, cisco):
        from django.db.models import ProtectedError

        _rule(cisco)

        with pytest.raises(ProtectedError):
            cisco.delete()


@pytest.fixture
def ui(client):
    client.force_login(make_superuser("vc-numbering-ui"))
    return client


@pytest.fixture
def api():
    api_client = APIClient()
    api_client.force_authenticate(make_superuser("vc-numbering-api"))
    return api_client


class TestTheOperatorSurface:
    """A7: list, detail, add, edit, delete, bulk delete, import, YAML export, change log and REST API."""

    def test_the_list_and_the_detail_render_with_the_rules_tab(self, ui, cisco):
        rule = _rule(cisco, "C9300-.*")

        listed = ui.get(_url("list"))
        detail = ui.get(_url("detail", rule.pk))
        changelog = ui.get(_url("changelog", rule.pk))

        assert listed.status_code == 200
        assert _url("detail", rule.pk) in listed.content.decode()
        assert 'aria-current="page">VC Member Numbering<' in listed.content.decode()
        assert detail.status_code == 200
        assert "C9300-.*" in detail.content.decode()
        assert changelog.status_code == 200

    def test_the_edit_form_adds_edits_and_refuses_a_broken_pattern(self, ui, cisco):
        added = ui.post(
            _url("add"), {"manufacturer": cisco.pk, "numbering_mode": "one_based", "device_type_pattern": ""}
        )
        rule = VCMemberNumberingRule.objects.get()
        edited = ui.post(
            _url("edit", rule.pk),
            {"manufacturer": cisco.pk, "numbering_mode": "reported", "device_type_pattern": "C.*"},
        )
        broken = ui.post(
            _url("edit", rule.pk), {"manufacturer": cisco.pk, "numbering_mode": "reported", "device_type_pattern": "C("}
        )

        assert added.status_code == 302
        assert edited.status_code == 302
        assert broken.status_code == 200
        rule.refresh_from_db()
        assert (rule.numbering_mode, rule.device_type_pattern) == ("reported", "C.*")

    def test_a_yaml_export_imports_again_and_a_csv_imports(self, ui, cisco):
        exported = VCMemberNumberingRule(manufacturer=cisco, device_type_pattern="C9300-.*", numbering_mode="one_based")

        from_yaml = ui.post(_url("bulk_import"), {"data": exported.to_yaml(), "format": "yaml", "csv_delimiter": ","})
        from_csv = ui.post(
            _url("bulk_import"),
            {
                "data": "manufacturer,device_type_pattern,numbering_mode\n,EX.*,reported",
                "format": "csv",
                "csv_delimiter": ",",
            },
        )

        assert from_yaml.status_code == 302, from_yaml.content.decode()
        assert from_csv.status_code == 302, from_csv.content.decode()
        assert set(
            VCMemberNumberingRule.objects.values_list("manufacturer_id", "device_type_pattern", "numbering_mode")
        ) == {
            (cisco.pk, "C9300-.*", "one_based"),
            (None, "EX.*", "reported"),
        }

    def test_the_selected_rules_export_as_yaml_and_delete_in_bulk(self, ui, cisco):
        first, second = _rule(cisco), _rule(pattern="EX.*", mode="reported")

        exported = ui.post(_url("bulk_export_yaml"), {"pk": [first.pk, second.pk]})
        deleted = ui.post(_url("bulk_delete"), {"pk": [first.pk, second.pk], "confirm": "true", "_confirm": "true"})

        assert exported.status_code == 200
        documents = [yaml.safe_load(part) for part in exported.content.decode().split("---\n")]
        assert [document["numbering_mode"] for document in documents] == ["one_based", "reported"]
        assert documents[0]["manufacturer"] == cisco.name
        assert deleted.status_code == 302
        assert not VCMemberNumberingRule.objects.exists()

    def test_a_rule_deletes_from_its_page(self, ui, cisco):
        rule = _rule(cisco)

        response = ui.post(_url("delete", rule.pk), {"confirm": "true"})

        assert response.status_code == 302
        assert not VCMemberNumberingRule.objects.exists()

    def test_the_api_round_trips_and_validates_a_rule(self, api, cisco):
        created = api.post(reverse(API_LIST), {"manufacturer": cisco.pk, "numbering_mode": "one_based"}, format="json")
        duplicate = api.post(reverse(API_LIST), {"manufacturer": cisco.pk, "numbering_mode": "reported"}, format="json")
        broken = api.post(reverse(API_LIST), {"device_type_pattern": "C(", "numbering_mode": "reported"}, format="json")
        bad_mode = api.post(reverse(API_LIST), {"numbering_mode": "zero_based"}, format="json")

        assert created.status_code == 201, created.content
        assert (created.json()["manufacturer"], created.json()["numbering_mode"]) == (cisco.pk, "one_based")
        assert duplicate.status_code == 400
        assert broken.status_code == 400
        assert "device_type_pattern" in broken.json()
        assert bad_mode.status_code == 400
        listed = api.get(reverse(API_LIST), {"manufacturer_id": cisco.pk}).json()
        assert [row["id"] for row in listed["results"]] == [created.json()["id"]]

    def test_a_user_without_the_plugin_permission_sees_no_rule(self, client, cisco):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="vc-numbering-nobody")
        client.force_login(user)
        rule = _rule(cisco)

        assert client.get(_url("list")).status_code == 403
        assert client.get(_url("detail", rule.pk)).status_code in (403, 404)
        assert client.post(_url("add"), {"numbering_mode": "reported"}).status_code == 403

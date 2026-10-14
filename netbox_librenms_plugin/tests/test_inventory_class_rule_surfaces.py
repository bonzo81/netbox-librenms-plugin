"""Include rule filters through the model and the real management surfaces: form, import, YAML, REST, list, detail."""

import json

import pytest
import yaml
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_librenms_plugin.models import InventoryIgnoreRule
from netbox_librenms_plugin.tests.conftest import make_superuser

pytestmark = pytest.mark.django_db

API_LIST = "plugins-api:netbox_librenms_plugin-api:inventoryignorerule-list"
API_DETAIL = "plugins-api:netbox_librenms_plugin-api:inventoryignorerule-detail"
# A trailing space is part of a regex, so every surface must keep it.
FILTERS = {
    "name_pattern": "^SCB ",
    "description_pattern": r"^CB \d+$",
    "model_pattern": "(?i)^750-",
    "prefer_description": True,
}


def _url(name, *args):
    return reverse(f"plugins:netbox_librenms_plugin:inventoryignorerule_{name}", args=args)


@pytest.fixture
def ui(client):
    client.force_login(make_superuser("inventory-rule-ui"))
    return client


@pytest.fixture
def api():
    api_client = APIClient()
    api_client.force_authenticate(make_superuser("inventory-rule-api"))
    return api_client


def _include_fields(**fields):
    return {
        "name": "Test boards",
        "match_type": InventoryIgnoreRule.MATCH_CLASS_IS,
        "pattern": "backplane",
        "action": InventoryIgnoreRule.ACTION_INCLUDE,
        "require_serial_match_parent": False,
        **fields,
    }


def _stored(rule):
    return {field: getattr(rule, field) for field in FILTERS}


def _rule(**fields):
    return InventoryIgnoreRule(**_include_fields(**fields))


class TestIncludeFilterMatching:
    """Within one rule the class and every non-blank filter must match."""

    @pytest.mark.parametrize(
        ("item", "admitted"),
        [
            ({"entPhysicalName": "SCBE2-MX-S", "entPhysicalDescr": "CB 0"}, True),
            ({"entPhysicalName": "SCBE2-MX-S", "entPhysicalDescr": "Backplane"}, False),
            ({"entPhysicalName": "JNP10K-RE1", "entPhysicalDescr": "CB 0"}, False),
            ({"entPhysicalDescr": "CB 0"}, False),
        ],
    )
    def test_every_non_blank_filter_must_match(self, item, admitted):
        rule = _rule(name_pattern="^SCB", description_pattern="^CB")

        assert rule.matches_inventory_item({"entPhysicalClass": "Backplane", **item}) is admitted

    def test_a_missing_value_compares_as_empty_text(self):
        rule = _rule(model_pattern="^$")

        assert rule.matches_inventory_item({"entPhysicalClass": "backplane"}) is True
        assert rule.matches_inventory_item({"entPhysicalClass": "backplane", "entPhysicalModelName": "X"}) is False

    def test_a_filter_is_case_sensitive_unless_the_regex_says_otherwise(self):
        item = {"entPhysicalClass": "backplane", "entPhysicalName": "SCBE2-MX-S"}

        assert _rule(name_pattern="^scb").matches_inventory_item(item) is False
        assert _rule(name_pattern="(?i)^scb").matches_inventory_item(item) is True

    @pytest.mark.parametrize("source", ["entPhysicalName", "entPhysicalDescr", "entPhysicalModelName"])
    def test_a_filter_searches_the_text_with_its_spaces(self, source):
        field = {"entPhysicalName": "name_pattern", "entPhysicalDescr": "description_pattern"}.get(
            source, "model_pattern"
        )
        item = {"entPhysicalClass": "backplane", source: " SCB "}

        assert _rule(**{field: "^ SCB $"}).matches_inventory_item(item) is True
        assert _rule(**{field: "^SCB$"}).matches_inventory_item(item) is False

    def test_numeric_values_match_as_text(self):
        rule = _rule(pattern="7", model_pattern="^750")

        assert rule.matches_inventory_item({"entPhysicalClass": 7, "entPhysicalModelName": 750062572}) is True

    def test_a_rule_of_another_action_admits_nothing(self):
        rule = InventoryIgnoreRule(name="Skip", match_type="contains", pattern="SCB", action="skip")

        assert rule.matches_inventory_item({"entPhysicalClass": "backplane", "entPhysicalName": "SCB"}) is False


class TestModelValidation:
    @pytest.mark.parametrize("field", ["name_pattern", "description_pattern", "model_pattern"])
    def test_a_filter_on_a_rule_of_another_action_is_refused(self, field):
        from django.core.exceptions import ValidationError

        rule = InventoryIgnoreRule(name="Skip", match_type="contains", pattern="IDPROM", action="skip", **{field: "x"})

        with pytest.raises(ValidationError) as excinfo:
            rule.full_clean()

        assert set(excinfo.value.message_dict) == {field}

    def test_prefer_description_on_a_rule_of_another_action_is_refused(self):
        from django.core.exceptions import ValidationError

        rule = InventoryIgnoreRule(
            name="Transparent", match_type="serial_matches_device", action="transparent", prefer_description=True
        )

        with pytest.raises(ValidationError) as excinfo:
            rule.full_clean()

        assert set(excinfo.value.message_dict) == {"prefer_description"}

    def test_an_invalid_filter_regex_is_a_field_error(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as excinfo:
            _rule(model_pattern="(").full_clean()

        assert set(excinfo.value.message_dict) == {"model_pattern"}

    def test_a_filter_is_stored_as_typed(self):
        rule = InventoryIgnoreRule.objects.create(**_include_fields(**FILTERS))

        rule.refresh_from_db()
        assert _stored(rule) == FILTERS


class TestEditForm:
    @staticmethod
    def _form_data(**fields):
        data = {**_include_fields(), "manufacturer": "", "enabled": "on", "description": ""}
        data.update(fields)
        return {key: value for key, value in data.items() if value is not False}

    def test_a_create_keeps_every_field(self, ui):
        response = ui.post(_url("add"), self._form_data(**FILTERS))

        assert response.status_code == 302, response.content.decode()[:2000]
        assert _stored(InventoryIgnoreRule.objects.get(name="Test boards")) == FILTERS

    @pytest.mark.parametrize(
        ("fields", "error_field"),
        [
            ({"model_pattern": "("}, "model_pattern"),
            ({"match_type": "contains", "pattern": "SCB", "action": "skip", "name_pattern": "^SCB"}, "name_pattern"),
        ],
    )
    def test_an_invalid_combination_is_a_field_error(self, ui, fields, error_field):
        response = ui.post(_url("add"), self._form_data(**fields))

        assert response.status_code == 200
        assert error_field in response.context["form"].errors
        assert not InventoryIgnoreRule.objects.filter(name="Test boards").exists()


class TestRestApi:
    def test_create_retrieve_and_patch_keep_every_field(self, api):
        created = api.post(reverse(API_LIST), _include_fields(**FILTERS), format="json")
        assert created.status_code == 201, created.json()
        detail = reverse(API_DETAIL, args=[created.json()["id"]])

        retrieved = api.get(detail).json()
        patched = api.patch(detail, {"name_pattern": "^SCBE ", "prefer_description": False}, format="json")

        assert {field: retrieved[field] for field in FILTERS} == FILTERS
        assert patched.status_code == 200, patched.json()
        assert _stored(InventoryIgnoreRule.objects.get(pk=created.json()["id"])) == {
            **FILTERS,
            "name_pattern": "^SCBE ",
            "prefer_description": False,
        }

    def test_an_invalid_filter_regex_is_a_field_error(self, api):
        response = api.post(reverse(API_LIST), _include_fields(description_pattern="(?P<"), format="json")

        assert response.status_code == 400
        assert "description_pattern" in response.json()

    def test_a_filter_on_a_skip_rule_is_a_field_error(self, api):
        payload = {"name": "Skip", "match_type": "contains", "pattern": "x", "action": "skip", "model_pattern": "^7"}

        response = api.post(reverse(API_LIST), payload, format="json")

        assert response.status_code == 400
        assert "model_pattern" in response.json()


class TestYamlRoundTrip:
    def test_a_yaml_export_and_import_keep_every_field(self, ui):
        rule = InventoryIgnoreRule.objects.create(**_include_fields(**FILTERS))
        exported = ui.post(_url("bulk_export_yaml"), {"pk": [rule.pk]}).content.decode()
        assert {field: yaml.safe_load(exported)[field] for field in FILTERS} == FILTERS
        rule.delete()

        response = ui.post(_url("bulk_import"), {"data": exported, "format": "yaml", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert _stored(InventoryIgnoreRule.objects.get(name="Test boards")) == FILTERS

    def test_a_json_import_keeps_every_field(self, ui):
        data = json.dumps([_include_fields(**FILTERS)])

        response = ui.post(_url("bulk_import"), {"data": data, "format": "json", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert _stored(InventoryIgnoreRule.objects.get(name="Test boards")) == FILTERS

    @pytest.mark.parametrize("column", ["name_pattern", "description_pattern", "model_pattern"])
    def test_a_csv_import_with_a_filter_column_is_refused(self, ui, column):
        """NetBox's CSV parser strips every cell, so a CSV regex cannot be trusted."""
        data = f"name,match_type,pattern,action,{column}\nTest boards,class_is,backplane,include,^SCB\n"

        response = ui.post(_url("bulk_import"), {"data": data, "format": "csv", "csv_delimiter": ","})

        assert response.status_code == 200
        assert f"rules with a {column} column must be imported as YAML or JSON" in response.content.decode()
        assert not InventoryIgnoreRule.objects.filter(name="Test boards").exists()


class TestListAndDetail:
    def test_the_filterset_searches_the_stored_text_and_never_runs_it(self):
        from netbox_librenms_plugin.filters import InventoryIgnoreRuleFilterSet

        wanted = InventoryIgnoreRule.objects.create(**_include_fields(name="Paren rule", name_pattern=r"^\(SCB"))

        def search(**params):
            return set(InventoryIgnoreRuleFilterSet(params, queryset=InventoryIgnoreRule.objects.all()).qs)

        # "(" is no valid regex; a text search must not compile it.
        assert search(name_pattern="(") == {wanted}
        assert wanted in search(prefer_description="false")
        assert wanted not in search(prefer_description="true")

    def test_the_list_shows_the_filters_and_no_serial_match_for_an_include_rule(self, ui):
        InventoryIgnoreRule.objects.create(**_include_fields(**FILTERS))

        # No seeded rule has a model filter, so this lists the one rule.
        response = ui.get(_url("list"), {"model_pattern": "750-"})

        (row,) = response.context["table"].rows
        dash = '<span class="text-muted">—</span>'
        assert str(row.get_cell("require_serial_match_parent")) == dash
        assert str(row.get_cell("name_pattern")) == "<code>^SCB </code>"
        assert str(row.get_cell("description_pattern")) == r"<code>^CB \d+$</code>"
        assert str(row.get_cell("prefer_description")) == '<span class="text-success">Yes</span>'

    def test_the_list_shows_no_display_flag_for_a_skip_rule(self, ui):
        InventoryIgnoreRule.objects.create(name="Skip", match_type="contains", pattern="x", action="skip")

        response = ui.get(_url("list"), {"match_type": "contains", "action": "skip"})

        (row,) = response.context["table"].rows
        assert str(row.get_cell("prefer_description")) == '<span class="text-muted">—</span>'

    def test_the_detail_page_shows_the_filters_of_an_include_rule(self, ui):
        rule = InventoryIgnoreRule.objects.create(**_include_fields(**FILTERS))

        content = ui.get(rule.get_absolute_url()).content.decode()

        assert "<code>^SCB </code>" in content
        assert "<code>(?i)^750-</code>" in content
        assert "Prefer Description" in content

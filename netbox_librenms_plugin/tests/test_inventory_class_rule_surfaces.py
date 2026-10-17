"""Rule filters on every action, through the model and the real surfaces: form, import, YAML, REST, list, detail."""

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


# One valid rule shape per action and match type kind: name, class and device match types.
RULE_SHAPES = {
    "include-class": {"match_type": "class_is", "pattern": "backplane", "action": "include"},
    "skip-name": {"match_type": "contains", "pattern": "IDPROM", "action": "skip"},
    "skip-class": {"match_type": "class_is", "pattern": "module", "action": "skip"},
    "transparent-class": {"match_type": "class_is", "pattern": "module", "action": "transparent"},
    "transparent-serial": {"match_type": "serial_matches_device", "pattern": "", "action": "transparent"},
    "transparent-model": {"match_type": "model_matches_device_type", "pattern": "", "action": "transparent"},
}
SHAPES = pytest.mark.parametrize("shape", sorted(RULE_SHAPES))
# Whether require_serial_match_parent takes effect for each shape.
SERIAL_GUARD = {
    "include-class": False,
    "skip-name": True,
    "skip-class": True,
    "transparent-class": True,
    "transparent-serial": False,
    "transparent-model": False,
}


def _shape_fields(shape, **fields):
    """Return the fields of a named rule shape, with the filters every action accepts."""
    data = {"name": "Test boards", "require_serial_match_parent": False, **RULE_SHAPES[shape], **fields}
    if data["action"] == "include":
        return {**data, **FILTERS, **fields}
    return {**data, **{field: FILTERS[field] for field in InventoryIgnoreRule.FILTER_SOURCES}, **fields}


def _stored_filters(rule, shape):
    """Return what the rule stores of the fields its shape carries."""
    keys = FILTERS if RULE_SHAPES[shape]["action"] == "include" else InventoryIgnoreRule.FILTER_SOURCES
    return {field: getattr(rule, field) for field in keys}


def _expected_filters(shape):
    if RULE_SHAPES[shape]["action"] == "include":
        return FILTERS
    return {field: FILTERS[field] for field in InventoryIgnoreRule.FILTER_SOURCES}


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


class TestFiltersOnEveryAction:
    """matches_filters() does not read the action; only an include rule admits a row."""

    FRONT_PANEL = {"entPhysicalClass": "module", "entPhysicalDescr": "Front Panel 6 ports Gigabitethernet Module"}
    REAL_NIM = {"entPhysicalClass": "module", "entPhysicalDescr": "16 ports Async Serial NIM"}

    @pytest.mark.parametrize("action", ["include", "skip", "transparent"])
    def test_the_filters_match_the_same_rows_whatever_the_action(self, action):
        rule = InventoryIgnoreRule(
            match_type="class_is", pattern="module", action=action, description_pattern="^Front Panel"
        )

        assert (rule.matches_filters(self.FRONT_PANEL), rule.matches_filters(self.REAL_NIM)) == (True, False)

    @pytest.mark.parametrize("action", ["include", "skip", "transparent"])
    @pytest.mark.parametrize("phys_class", ["module", "port"])
    @pytest.mark.parametrize("descr", ["Front Panel 6 ports", "16 ports Async Serial NIM"])
    def test_only_an_include_rule_admits_a_row(self, action, phys_class, descr):
        rule = InventoryIgnoreRule(match_type="class_is", pattern="module", action=action, description_pattern="^Front")
        item = {"entPhysicalClass": phys_class, "entPhysicalDescr": descr}

        admitted = action == "include" and phys_class == "module" and descr.startswith("Front")
        assert rule.matches_inventory_item(item) is admitted

    def test_a_blank_filter_matches_any_value(self):
        rule = InventoryIgnoreRule(match_type="contains", pattern="IDPROM", action="skip")

        assert rule.matches_filters({}) is True
        assert rule.compiled_filters == ()

    @pytest.mark.parametrize("action", ["skip", "transparent", "include"])
    def test_a_stored_filter_that_does_not_compile_raises_for_every_action(self, action):
        from netbox_librenms_plugin.models import RuleConfigurationError

        rule = InventoryIgnoreRule(pk=7, name="Broken", match_type="class_is", pattern="module", action=action)
        rule.model_pattern = "("

        with pytest.raises(RuleConfigurationError) as excinfo:
            rule.matches_filters({"entPhysicalClass": "module"})

        assert (excinfo.value.rule_pk, excinfo.value.field) == (7, "model_pattern")


class TestModelValidation:
    @SHAPES
    def test_the_filters_are_valid_on_every_action_and_stored_as_typed(self, shape):
        rule = InventoryIgnoreRule(**_shape_fields(shape))

        rule.full_clean()
        rule.save()

        rule.refresh_from_db()
        assert _stored_filters(rule, shape) == _expected_filters(shape)

    @pytest.mark.parametrize("shape", ["skip-name", "transparent-class", "transparent-model"])
    def test_prefer_description_on_a_rule_of_another_action_is_refused(self, shape):
        from django.core.exceptions import ValidationError

        rule = InventoryIgnoreRule(**_shape_fields(shape, prefer_description=True))

        with pytest.raises(ValidationError) as excinfo:
            rule.full_clean()

        assert set(excinfo.value.message_dict) == {"prefer_description"}

    @pytest.mark.parametrize("match_type", ["contains", "regex", "serial_matches_device", "model_matches_device_type"])
    def test_the_include_action_still_needs_class_is(self, match_type):
        from django.core.exceptions import ValidationError

        rule = InventoryIgnoreRule(name="Include", match_type=match_type, pattern="x", action="include")

        with pytest.raises(ValidationError) as excinfo:
            rule.full_clean()

        assert set(excinfo.value.message_dict) == {"action"}

    @SHAPES
    def test_an_invalid_filter_regex_is_a_field_error(self, shape):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as excinfo:
            InventoryIgnoreRule(**_shape_fields(shape, model_pattern="(")).full_clean()

        assert set(excinfo.value.message_dict) == {"model_pattern"}

    @pytest.mark.parametrize(
        ("shape", "flag", "note"),
        [
            ("skip-name", True, True),
            ("skip-name", False, False),
            ("transparent-class", True, True),
            ("transparent-class", False, False),
            ("include-class", True, False),
            ("transparent-model", True, False),
            ("transparent-serial", True, False),
        ],
    )
    def test_the_serial_match_note_shows_only_where_the_guard_applies(self, shape, flag, note):
        rule = InventoryIgnoreRule(**_shape_fields(shape, require_serial_match_parent=flag))

        assert rule.serial_guard_applies is SERIAL_GUARD[shape]
        assert str(rule).endswith(" [serial match]") is note

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

    @SHAPES
    def test_a_create_keeps_every_field(self, ui, shape):
        response = ui.post(_url("add"), self._form_data(**_shape_fields(shape)))

        assert response.status_code == 302, response.content.decode()[:2000]
        assert _stored_filters(InventoryIgnoreRule.objects.get(name="Test boards"), shape) == _expected_filters(shape)

    def test_the_unbound_create_form_checks_require_serial_match_parent(self, ui):
        """The model default is True, so an operator must clear it for rows without a serial."""
        response = ui.get(_url("add"))

        assert response.context["form"]["require_serial_match_parent"].value() is True

    @SHAPES
    def test_an_omitted_require_serial_match_parent_is_an_unchecked_box(self, ui, shape):
        data = self._form_data(**_shape_fields(shape))
        data.pop("require_serial_match_parent", None)

        response = ui.post(_url("add"), data)

        assert response.status_code == 302, response.content.decode()[:2000]
        assert InventoryIgnoreRule.objects.get(name="Test boards").require_serial_match_parent is False

    @pytest.mark.parametrize(
        ("fields", "error_field"),
        [
            ({"model_pattern": "("}, "model_pattern"),
            ({**RULE_SHAPES["transparent-class"], "description_pattern": "(?P<"}, "description_pattern"),
            ({**RULE_SHAPES["skip-name"], "prefer_description": True}, "prefer_description"),
            ({"match_type": "contains", "pattern": "SCB"}, "action"),
        ],
    )
    def test_an_invalid_combination_is_a_field_error(self, ui, fields, error_field):
        response = ui.post(_url("add"), self._form_data(**fields))

        assert response.status_code == 200
        assert error_field in response.context["form"].errors
        assert not InventoryIgnoreRule.objects.filter(name="Test boards").exists()


class TestRestApi:
    @SHAPES
    def test_every_action_creates_and_retrieves_its_filters(self, api, shape):
        created = api.post(reverse(API_LIST), _shape_fields(shape), format="json")
        assert created.status_code == 201, created.json()

        retrieved = api.get(reverse(API_DETAIL, args=[created.json()["id"]])).json()

        assert {field: retrieved[field] for field in _expected_filters(shape)} == _expected_filters(shape)

    @SHAPES
    def test_an_omitted_require_serial_match_parent_takes_the_model_default(self, api, shape):
        payload = _shape_fields(shape)
        payload.pop("require_serial_match_parent")

        created = api.post(reverse(API_LIST), payload, format="json")

        assert created.status_code == 201, created.json()
        assert InventoryIgnoreRule.objects.get(pk=created.json()["id"]).require_serial_match_parent is True

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

    def test_a_display_flag_on_a_skip_rule_is_a_field_error(self, api):
        payload = {
            "name": "Skip",
            "match_type": "contains",
            "pattern": "x",
            "action": "skip",
            "prefer_description": True,
        }

        response = api.post(reverse(API_LIST), payload, format="json")

        assert response.status_code == 400
        assert "prefer_description" in response.json()


class TestYamlRoundTrip:
    @SHAPES
    def test_a_yaml_export_and_import_keep_every_field(self, ui, shape):
        rule = InventoryIgnoreRule.objects.create(**_shape_fields(shape))
        exported = ui.post(_url("bulk_export_yaml"), {"pk": [rule.pk]}).content.decode()
        assert {field: yaml.safe_load(exported)[field] for field in FILTERS} == {
            "prefer_description": False,
            **_expected_filters(shape),
        }
        rule.delete()

        response = ui.post(_url("bulk_import"), {"data": exported, "format": "yaml", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert _stored_filters(InventoryIgnoreRule.objects.get(name="Test boards"), shape) == _expected_filters(shape)

    @SHAPES
    def test_a_json_import_keeps_every_field(self, ui, shape):
        data = json.dumps([_shape_fields(shape)])

        response = ui.post(_url("bulk_import"), {"data": data, "format": "json", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert _stored_filters(InventoryIgnoreRule.objects.get(name="Test boards"), shape) == _expected_filters(shape)

    @pytest.mark.parametrize("data_format", ["yaml", "json"])
    @SHAPES
    def test_an_omitted_require_serial_match_parent_is_stored_as_false(self, ui, shape, data_format):
        """The import form reads a missing boolean as unchecked, like the edit form."""
        record = _shape_fields(shape)
        record.pop("require_serial_match_parent")
        data = yaml.safe_dump([record]) if data_format == "yaml" else json.dumps([record])

        response = ui.post(_url("bulk_import"), {"data": data, "format": data_format, "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert InventoryIgnoreRule.objects.get(name="Test boards").require_serial_match_parent is False

    @pytest.mark.parametrize("row", ["class_is,backplane,include", "class_is,module,transparent", "contains,x,skip"])
    @pytest.mark.parametrize("column", ["name_pattern", "description_pattern", "model_pattern"])
    def test_a_csv_import_with_a_filter_column_is_refused(self, ui, column, row):
        """NetBox's CSV parser strips every cell, so a CSV regex cannot be trusted."""
        data = f"name,match_type,pattern,action,{column}\nTest boards,{row},^SCB\n"

        response = ui.post(_url("bulk_import"), {"data": data, "format": "csv", "csv_delimiter": ","})

        assert response.status_code == 200
        assert f"rules with a {column} column must be imported as YAML or JSON" in response.content.decode()
        assert not InventoryIgnoreRule.objects.filter(name="Test boards").exists()


class TestListAndDetail:
    @SHAPES
    def test_the_filterset_searches_the_stored_filters_of_every_action(self, shape):
        from netbox_librenms_plugin.filters import InventoryIgnoreRuleFilterSet

        wanted = InventoryIgnoreRule.objects.create(**_shape_fields(shape))

        def search(**params):
            return set(InventoryIgnoreRuleFilterSet(params, queryset=InventoryIgnoreRule.objects.all()).qs)

        assert search(model_pattern="750-", action=RULE_SHAPES[shape]["action"]) == {wanted}
        assert search(description_pattern=r"CB \d", model_pattern="750-") == {wanted}

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

    def test_the_list_shows_only_the_core_columns_by_default(self, ui):
        response = ui.get(_url("list"))

        assert [column.name for column in response.context["table"].columns] == [
            "pk",
            "name",
            "match_type",
            "action",
            "pattern",
            "require_serial_match_parent",
            "manufacturer",
            "enabled",
            "actions",
        ]

    def test_the_list_shows_no_display_flag_for_a_skip_rule(self, ui):
        InventoryIgnoreRule.objects.create(name="Skip", match_type="contains", pattern="x", action="skip")

        response = ui.get(_url("list"), {"match_type": "contains", "action": "skip"})

        (row,) = response.context["table"].rows
        assert str(row.get_cell("prefer_description")) == '<span class="text-muted">—</span>'

    @SHAPES
    def test_the_list_shows_the_filters_of_every_action_and_the_serial_match_where_it_applies(self, ui, shape):
        InventoryIgnoreRule.objects.create(**_shape_fields(shape, require_serial_match_parent=True))

        response = ui.get(_url("list"), {"model_pattern": "750-"})

        (row,) = response.context["table"].rows
        assert str(row.get_cell("name_pattern")) == "<code>^SCB </code>"
        assert str(row.get_cell("description_pattern")) == r"<code>^CB \d+$</code>"
        assert str(row.get_cell("model_pattern")) == "<code>(?i)^750-</code>"
        serial = '<span class="text-success">Yes</span>' if SERIAL_GUARD[shape] else '<span class="text-muted">—</span>'
        assert str(row.get_cell("require_serial_match_parent")) == serial

    def test_the_detail_page_shows_the_filters_of_an_include_rule(self, ui):
        rule = InventoryIgnoreRule.objects.create(**_include_fields(**FILTERS))

        content = ui.get(rule.get_absolute_url()).content.decode()

        assert "<code>^SCB </code>" in content
        assert "<code>(?i)^750-</code>" in content
        assert "Prefer Description" in content

    @pytest.mark.parametrize("flag", [True, False])
    @SHAPES
    def test_the_detail_page_shows_the_filters_and_serial_match_of_every_action(self, ui, shape, flag):
        rule = InventoryIgnoreRule.objects.create(**_shape_fields(shape, require_serial_match_parent=flag))

        content = ui.get(rule.get_absolute_url()).content.decode()

        assert "<code>^SCB </code>" in content
        assert r"<code>^CB \d+$</code>" in content
        assert "<code>(?i)^750-</code>" in content
        assert ("Prefer Description" in content) is (RULE_SHAPES[shape]["action"] == "include")
        cells = {
            True: '<td><span class="text-success">Yes</span></td>',
            False: '<td><span class="text-danger">No</span></td>',
        }
        # The enabled flag and an include rule's display flag are the other Yes/No cells, and both are Yes.
        if SERIAL_GUARD[shape]:
            assert cells[flag] in content
        assert content.count('<span class="text-danger">No</span>') == int(SERIAL_GUARD[shape] and not flag)


def test_the_list_help_explains_class_actions_and_their_serial_guard(ui):
    """Operators can use class matches for Skip and Transparent as well as Include."""
    response = ui.get(_url("list"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Include action only" not in content
    assert "Skip and Transparent" in content
    assert "including class_is" in content

"""Interface rules through the real management surfaces: edit form, bulk import, list, YAML export and REST API."""

import json

import pytest
import yaml
from dcim.models import Platform
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher, RuleDecisionKind
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tests.conftest import make_superuser

pytestmark = pytest.mark.django_db

IGNORE = InterfaceTypeMapping.ACTION_IGNORE
SET = InterfaceTypeMapping.ACTION_SET_TYPE
API_LIST = "plugins-api:netbox_librenms_plugin-api:interfacetypemapping-list"
API_DETAIL = "plugins-api:netbox_librenms_plugin-api:interfacetypemapping-detail"


def _url(name, *args):
    return reverse(f"plugins:netbox_librenms_plugin:interfacetypemapping_{name}", args=args)


@pytest.fixture
def platform():
    return Platform.objects.create(name="Cisco IOS", slug="ios")


@pytest.fixture
def ui(client):
    client.force_login(make_superuser("interface-rule-ui"))
    return client


@pytest.fixture
def api():
    api_client = APIClient()
    api_client.force_authenticate(make_superuser("interface-rule-api"))
    return api_client


def _form_data(**fields):
    data = {"action": SET, "platform": "", "name_pattern": "", "librenms_type": "", "librenms_speed": ""}
    data.update(netbox_type="", description="")
    data.update(fields)
    return data


# Whitespace is part of a regex: " " matches a name with a space, so it must never become "any name".
_VERBATIM_PATTERNS = [" ", "  ^Te", "^Te "]


class TestNamePatternIsKeptVerbatim:
    @pytest.mark.parametrize("pattern", _VERBATIM_PATTERNS)
    def test_the_edit_form_keeps_the_pattern(self, ui, platform, pattern):
        response = ui.post(_url("add"), _form_data(action=IGNORE, platform=platform.pk, name_pattern=pattern))

        assert response.status_code == 302
        assert list(InterfaceTypeMapping.objects.filter(platform=platform).values_list("name_pattern", flat=True)) == [
            pattern
        ]

    @pytest.mark.parametrize("pattern", _VERBATIM_PATTERNS)
    def test_the_api_keeps_the_pattern(self, api, platform, pattern):
        created = api.post(
            reverse(API_LIST), {"action": IGNORE, "platform": platform.pk, "name_pattern": pattern}, format="json"
        )

        assert created.status_code == 201, created.json()
        assert created.json()["name_pattern"] == pattern
        assert InterfaceTypeMapping.objects.get(pk=created.json()["id"]).name_pattern == pattern

    @pytest.mark.parametrize("data_format", ["yaml", "json"])
    @pytest.mark.parametrize("pattern", _VERBATIM_PATTERNS)
    def test_a_bulk_import_keeps_the_pattern(self, ui, platform, data_format, pattern):
        record = {"action": IGNORE, "platform": "ios", "name_pattern": pattern}
        data = yaml.dump([record]) if data_format == "yaml" else json.dumps([record])

        response = ui.post(_url("bulk_import"), {"data": data, "format": data_format, "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert list(InterfaceTypeMapping.objects.filter(platform=platform).values_list("name_pattern", flat=True)) == [
            pattern
        ]

    def test_a_yaml_export_and_import_keep_the_pattern(self, ui, platform):
        rule = InterfaceTypeMapping.objects.create(platform=platform, name_pattern="  ^Te ", netbox_type="other")
        exported = ui.post(_url("bulk_export_yaml"), {"pk": [rule.pk]}).content.decode()
        rule.delete()

        response = ui.post(_url("bulk_import"), {"data": exported, "format": "yaml", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        assert InterfaceTypeMapping.objects.get(platform=platform).name_pattern == "  ^Te "


def _csv_import(ui, data, **extra):
    return ui.post(_url("bulk_import"), {"data": data, "format": "csv", "csv_delimiter": ",", **extra})


_CSV_REFUSAL = "rules with a name_pattern column must be imported as YAML or JSON"


class TestCsvImportRefusesANamePatternColumn:
    """NetBox's CSV parser strips every cell, so a CSV pattern cannot be trusted and the whole import is refused."""

    @pytest.mark.parametrize("cell", ['" "', '"  ^Te"', "", "^Vlan"], ids=["space", "padded", "empty", "plain"])
    def test_any_name_pattern_column_refuses_the_import(self, ui, platform, cell):
        response = _csv_import(ui, f"action,platform,name_pattern\nignore,ios,{cell}\n")

        assert response.status_code == 200
        assert _CSV_REFUSAL in response.content.decode()
        assert not InterfaceTypeMapping.objects.filter(platform=platform).exists()

    def test_a_header_with_an_attribute_suffix_is_refused_not_an_error(self, ui, platform):
        response = _csv_import(ui, "action,platform,name_pattern.foo\nignore,ios,^Vlan\n")

        assert response.status_code == 200
        assert _CSV_REFUSAL in response.content.decode()
        assert not InterfaceTypeMapping.objects.filter(platform=platform).exists()

    @pytest.mark.parametrize(
        ("delimiter", "data"),
        [
            (";", 'action;platform;name_pattern\nignore;ios;" "\n'),
            ("auto", "action,platform,name_pattern\nignore,ios, ^Vlan\n"),
        ],
        ids=["semicolon", "auto"],
    )
    def test_every_delimiter_is_refused(self, ui, platform, delimiter, data):
        response = _csv_import(ui, data, csv_delimiter=delimiter)

        assert response.status_code == 200
        assert _CSV_REFUSAL in response.content.decode()
        assert not InterfaceTypeMapping.objects.filter(platform=platform).exists()

    def test_an_uploaded_csv_file_is_refused_too(self, ui, platform):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("rules.csv", b"action,platform,name_pattern\nignore,ios,^Vlan\n", "text/csv")
        response = ui.post(
            _url("bulk_import"),
            {"import_method": "upload", "upload_file": upload, "format": "csv", "csv_delimiter": ","},
        )

        assert response.status_code == 200
        assert _CSV_REFUSAL in response.content.decode()
        assert not InterfaceTypeMapping.objects.filter(platform=platform).exists()

    def test_a_csv_data_file_is_refused_too(self, ui, platform):
        from core.models import DataFile, DataSource
        from django.utils import timezone

        content = b"action,platform,name_pattern\nignore,ios,^Vlan\n"
        source = DataSource.objects.create(name="rules-source", type="local", source_url="file:///tmp/rules")
        data_file = DataFile.objects.create(
            source=source, path="rules.csv", last_updated=timezone.now(), size=len(content), hash="0" * 64, data=content
        )
        response = ui.post(
            _url("bulk_import"),
            {
                "import_method": "datafile",
                "data_source": source.pk,
                "data_file": data_file.pk,
                "format": "csv",
                "csv_delimiter": ",",
            },
        )

        assert response.status_code == 200
        assert _CSV_REFUSAL in response.content.decode()
        assert not InterfaceTypeMapping.objects.filter(platform=platform).exists()

    def test_a_csv_without_the_column_still_imports(self, ui, platform):
        response = _csv_import(ui, "action,platform\nignore,ios\n")

        assert response.status_code == 302, response.content.decode()[:2000]
        rule = InterfaceTypeMapping.objects.get(platform=platform)
        assert (rule.action, rule.name_pattern) == (IGNORE, "")


class TestImportUpdates:
    """An import row with an id updates only the columns it names; model errors on other fields stay row errors."""

    def test_changing_a_set_rule_to_ignore_reports_the_type_as_a_row_error(self, ui):
        rule = InterfaceTypeMapping.objects.create(name_pattern="^Te", netbox_type="other")

        response = ui.post(
            _url("bulk_import"), {"data": json.dumps([{"id": rule.pk, "action": IGNORE}]), "format": "json"}
        )

        assert response.status_code == 200
        assert "Record 1" in response.content.decode()
        assert "netbox_type: An Ignore rule sets no NetBox type." in response.content.decode()
        rule.refresh_from_db()
        assert (rule.action, rule.netbox_type) == (SET, "other")

    def test_clearing_the_type_while_a_speed_stays_reports_a_row_error(self, ui):
        rule = InterfaceTypeMapping.objects.create(
            librenms_type="ethernetCsmacd", librenms_speed=1_000_000, netbox_type="1000base-t"
        )

        response = ui.post(
            _url("bulk_import"), {"data": json.dumps([{"id": rule.pk, "librenms_type": ""}]), "format": "json"}
        )

        assert response.status_code == 200
        assert "librenms_speed: A speed needs a LibreNMS type." in response.content.decode()
        rule.refresh_from_db()
        assert rule.librenms_type == "ethernetCsmacd"


class TestBlankCsvAction:
    """A blank action cell means the default on create and the current action on update, on every NetBox version."""

    def test_a_blank_action_creates_a_set_type_rule(self, ui):
        response = _csv_import(ui, "action,librenms_type,netbox_type\n,ethernetCsmacd,1000base-t\n")

        assert response.status_code == 302, response.content.decode()[:2000]
        rule = InterfaceTypeMapping.objects.get(librenms_type="ethernetCsmacd", librenms_speed=None)
        assert (rule.action, rule.netbox_type) == (SET, "1000base-t")

    def test_a_blank_action_keeps_the_current_action_on_update(self, ui, platform):
        rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")

        response = _csv_import(ui, f"id,action,description\n{rule.pk},,SVIs\n")

        assert response.status_code == 302, response.content.decode()[:2000]
        rule.refresh_from_db()
        assert (rule.action, rule.description) == (IGNORE, "SVIs")


class TestEditForm:
    def test_an_ignore_rule_created_in_the_form_ignores_matching_ports(self, ui, platform):
        response = ui.post(_url("add"), _form_data(action=IGNORE, platform=platform.pk, name_pattern="^Vlan"))

        assert response.status_code == 302
        rule = InterfaceTypeMapping.objects.get(name_pattern="^Vlan")
        assert (rule.action, rule.platform_id, rule.netbox_type) == (IGNORE, platform.pk, None)
        decision = InterfaceRuleMatcher.load().decide(
            {"ifName": "Vlan10", "ifDescr": "Vlan10", "ifType": "propVirtual", "ifSpeed": None},
            platform_id=platform.pk,
        )
        assert (decision.kind, [r.pk for r in decision.rules]) == (RuleDecisionKind.IGNORE, [rule.pk])

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"action": SET, "name_pattern": "^Te"}, "A Set type rule needs a NetBox type."),
            (
                {"action": IGNORE, "name_pattern": "^Te", "netbox_type": "virtual"},
                "An Ignore rule sets no NetBox type.",
            ),
            ({"action": SET, "name_pattern": "(", "netbox_type": "other"}, "Invalid regex"),
            ({"action": SET, "netbox_type": "other"}, "Give at least one of platform, LibreNMS type or name pattern."),
        ],
        ids=["set-without-type", "ignore-with-type", "invalid-regex", "no-selector"],
    )
    def test_the_form_rejects_an_invalid_rule(self, ui, fields, message):
        before = InterfaceTypeMapping.objects.count()

        response = ui.post(_url("add"), _form_data(**fields))

        assert response.status_code == 200
        assert message in response.content.decode()
        assert InterfaceTypeMapping.objects.count() == before

    def test_the_form_rejects_a_duplicate_with_a_readable_error(self, ui, platform):
        InterfaceTypeMapping.objects.create(platform=platform, name_pattern="^Te", netbox_type="10gbase-x-sfpp")

        response = ui.post(_url("add"), _form_data(action=IGNORE, platform=platform.pk, name_pattern="^Te"))

        assert response.status_code == 200
        assert "A rule with the same platform, LibreNMS type, name pattern and speed already exists." in (
            response.content.decode()
        )


class TestBulkImport:
    def test_csv_import_takes_the_platform_by_slug(self, ui, platform):
        data = (
            "action,platform,librenms_type,librenms_speed,netbox_type\n"
            "ignore,ios,propVirtual,,\n"
            "set_type,,ethernetCsmacd,10000000,10gbase-x-sfpp\n"
        )

        response = ui.post(_url("bulk_import"), {"data": data, "format": "csv", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()[:2000]
        ignore = InterfaceTypeMapping.objects.get(librenms_type="propVirtual")
        assert (ignore.action, ignore.platform_id, ignore.netbox_type) == (IGNORE, platform.pk, None)
        global_set = InterfaceTypeMapping.objects.get(librenms_type="ethernetCsmacd", librenms_speed=10_000_000)
        assert (global_set.action, global_set.platform_id, global_set.netbox_type) == (SET, None, "10gbase-x-sfpp")

    def test_an_import_without_the_new_columns_creates_global_set_type_rules(self, ui):
        data = "librenms_type,librenms_speed,netbox_type\nethernetCsmacd,1000000,1000base-t\n"

        response = ui.post(_url("bulk_import"), {"data": data, "format": "csv", "csv_delimiter": ","})

        assert response.status_code == 302
        rule = InterfaceTypeMapping.objects.get(librenms_type="ethernetCsmacd", librenms_speed=1_000_000)
        assert (rule.action, rule.platform_id, rule.name_pattern, rule.netbox_type) == (SET, None, "", "1000base-t")

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ("ignore,no-such-platform,propVirtual,", "no-such-platform"),
            ("set_type,,ethernetCsmacd,", "A Set type rule needs a NetBox type."),
            ("ignore,,propVirtual,virtual", "An Ignore rule sets no NetBox type."),
        ],
        ids=["unknown-slug", "set-without-type", "ignore-with-type"],
    )
    def test_an_invalid_import_row_is_rejected(self, ui, platform, row, message):
        before = InterfaceTypeMapping.objects.count()
        data = f"action,platform,librenms_type,netbox_type\n{row}\n"

        response = ui.post(_url("bulk_import"), {"data": data, "format": "csv", "csv_delimiter": ","})

        assert response.status_code == 200
        assert message in response.content.decode()
        assert InterfaceTypeMapping.objects.count() == before

    def test_a_yaml_export_imports_back_to_the_same_rules(self, ui, platform):
        ignore = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^Vlan")
        scoped = InterfaceTypeMapping.objects.create(
            platform=platform, name_pattern="^Te", netbox_type="10gbase-x-sfpp"
        )
        exported = ui.post(_url("bulk_export_yaml"), {"pk": [ignore.pk, scoped.pk]})
        assert exported.status_code == 200
        documents = [yaml.safe_load(part) for part in exported.content.decode().split("---\n")]
        assert [doc["platform"] for doc in documents] == ["ios", "ios"]
        assert documents[0]["netbox_type"] is None
        InterfaceTypeMapping.objects.filter(pk__in=[ignore.pk, scoped.pk]).delete()

        response = ui.post(
            _url("bulk_import"), {"data": exported.content.decode(), "format": "yaml", "csv_delimiter": ","}
        )

        assert response.status_code == 302, response.content.decode()[:2000]
        restored = InterfaceTypeMapping.objects.filter(platform=platform).order_by("name_pattern")
        assert [(r.action, r.name_pattern, r.netbox_type) for r in restored] == [
            (SET, "^Te", "10gbase-x-sfpp"),
            (IGNORE, "^Vlan", None),
        ]


class TestListAndDetail:
    def test_the_list_shows_and_filters_the_rule_columns(self, ui, platform):
        InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^VlanIgnored")
        InterfaceTypeMapping.objects.create(name_pattern="^TeGlobal", netbox_type="10gbase-x-sfpp")

        page = ui.get(_url("list")).content.decode()
        assert all(header in page for header in ("Action", "Platform", "Name Pattern"))
        assert "^VlanIgnored" in page
        assert "^TeGlobal" in page

        filtered = ui.get(_url("list"), {"action": IGNORE, "platform_id": platform.pk}).content.decode()
        assert "^VlanIgnored" in filtered
        assert "^TeGlobal" not in filtered
        global_only = ui.get(_url("list"), {"platform__isnull": "true", "name_pattern": "Te"}).content.decode()
        assert "^TeGlobal" in global_only
        assert "^VlanIgnored" not in global_only

    def test_the_detail_page_shows_the_rule(self, ui, platform):
        rule = InterfaceTypeMapping.objects.create(action=IGNORE, platform=platform, name_pattern="^VlanDetail")

        page = ui.get(_url("detail", rule.pk)).content.decode()

        assert "^VlanDetail" in page
        assert "Ignore" in page
        assert platform.get_absolute_url() in page


class TestRestApi:
    def test_an_ignore_rule_round_trips_through_the_api(self, api, platform):
        created = api.post(
            reverse(API_LIST),
            {"action": IGNORE, "platform": platform.pk, "name_pattern": "^Vlan", "netbox_type": None},
            format="json",
        )
        assert created.status_code == 201, created.json()
        body = created.json()
        assert (body["action"], body["platform"], body["netbox_type"]) == (IGNORE, platform.pk, None)

        patched = api.patch(reverse(API_DETAIL, args=[body["id"]]), {"name_pattern": "^Vlan1"}, format="json")
        assert patched.status_code == 200, patched.json()

        listed = api.get(reverse(API_LIST), {"action": IGNORE, "platform_id": platform.pk}).json()
        assert [(r["id"], r["name_pattern"]) for r in listed["results"]] == [(body["id"], "^Vlan1")]

    def test_a_global_rule_needs_no_platform_key(self, api):
        created = api.post(reverse(API_LIST), {"name_pattern": "^Te", "netbox_type": "10gbase-x-sfpp"}, format="json")

        assert created.status_code == 201, created.json()
        assert (created.json()["platform"], created.json()["action"]) == (None, SET)

    @pytest.mark.parametrize(
        ("payload", "field"),
        [
            ({"action": SET, "name_pattern": "^Te"}, "netbox_type"),
            ({"action": IGNORE, "name_pattern": "^Te", "netbox_type": "virtual"}, "netbox_type"),
            ({"action": "delete", "name_pattern": "^Te", "netbox_type": "other"}, "action"),
        ],
        ids=["set-without-type", "ignore-with-type", "unknown-action"],
    )
    def test_the_api_rejects_an_invalid_rule(self, api, payload, field):
        before = InterfaceTypeMapping.objects.count()

        response = api.post(reverse(API_LIST), payload, format="json")

        assert response.status_code == 400
        assert field in response.json()
        assert InterfaceTypeMapping.objects.count() == before

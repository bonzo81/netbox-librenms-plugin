"""IP mappings through the model, the one reader, and the real management surfaces: edit form, import and REST API."""

from ipaddress import ip_address

import pytest
import yaml
from django.core.exceptions import ValidationError
from django.urls import reverse
from ipam.models import VRF
from rest_framework.test import APIClient

from netbox_librenms_plugin.ip_rules import IPRuleMatcher
from netbox_librenms_plugin.models import IPAddressMapping
from netbox_librenms_plugin.tests.conftest import make_superuser

pytestmark = pytest.mark.django_db

API_LIST = "plugins-api:netbox_librenms_plugin-api:ipaddressmapping-list"


def _url(name, *args):
    return reverse(f"plugins:netbox_librenms_plugin:ipaddressmapping_{name}", args=args)


@pytest.fixture
def blue():
    return VRF.objects.create(name="ip-mapping-blue")


@pytest.fixture
def red():
    return VRF.objects.create(name="ip-mapping-red")


@pytest.fixture
def ui(client):
    client.force_login(make_superuser("ip-mapping-ui"))
    return client


@pytest.fixture
def api():
    api_client = APIClient()
    api_client.force_authenticate(make_superuser("ip-mapping-api"))
    return api_client


class TestTheModel:
    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("fe80::1/10", "fe80::/10"),
            ("fe80::/10", "fe80::/10"),
            ("10.1.2.3/8", "10.0.0.0/8"),
            ("0.0.0.0/0", "0.0.0.0/0"),
        ],
    )
    def test_the_prefix_is_stored_as_its_network(self, given, stored):
        mapping = IPAddressMapping.objects.create(prefix=given)

        mapping.refresh_from_db()
        assert str(mapping.prefix) == stored

    def test_a_prefix_that_does_not_parse_is_refused_on_the_field(self):
        with pytest.raises(ValidationError) as refused:
            IPAddressMapping.objects.create(prefix="fe80::zz/10")

        assert set(refused.value.message_dict) == {"prefix"}

    def test_a_prefix_is_unique_in_the_global_scope(self):
        IPAddressMapping.objects.create(prefix="fe80::/10")

        with pytest.raises(ValidationError, match="An IP mapping for this prefix and VRF already exists."):
            IPAddressMapping.objects.create(prefix="fe80::5/10")

    def test_a_prefix_is_unique_in_one_vrf(self, blue, red):
        IPAddressMapping.objects.create(prefix="fe80::/10")
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=red)

        with pytest.raises(ValidationError, match="An IP mapping for this prefix and VRF already exists."):
            IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)

    def test_ignore_is_the_only_action(self):
        assert IPAddressMapping.ACTION_CHOICES == [(IPAddressMapping.ACTION_IGNORE, "Ignore")]
        assert IPAddressMapping.objects.create(prefix="fe80::/10").action == IPAddressMapping.ACTION_IGNORE
        with pytest.raises(ValidationError) as refused:
            IPAddressMapping.objects.create(prefix="10.0.0.0/8", action="set_type")
        assert set(refused.value.message_dict) == {"action"}


class TestTheReader:
    def test_an_address_matches_only_a_prefix_of_its_family(self):
        v6 = IPAddressMapping.objects.create(prefix="fe80::/10")
        v4 = IPAddressMapping.objects.create(prefix="0.0.0.0/0")
        rules = IPRuleMatcher.load()

        assert rules.decide(ip_address("fe80::1"), None).pk == v6.pk
        assert rules.decide(ip_address("10.0.0.1"), None).pk == v4.pk
        assert rules.decide(ip_address("2001:db8::1"), None) is None

    def test_a_vrf_rule_matches_only_its_vrf_and_a_global_rule_every_vrf(self, blue, red):
        IPAddressMapping.objects.create(prefix="fe80::/10", vrf=blue)
        IPAddressMapping.objects.create(prefix="169.254.0.0/16")
        rules = IPRuleMatcher.load()

        assert rules.decide(ip_address("fe80::1"), blue.pk).reason == (
            "IP mapping fe80::/10 (VRF ip-mapping-blue) ignores this address"
        )
        assert rules.decide(ip_address("fe80::1"), red.pk) is None
        assert rules.decide(ip_address("fe80::1"), None) is None
        assert (
            rules.decide(ip_address("169.254.1.1"), red.pk).reason == "IP mapping 169.254.0.0/16 ignores this address"
        )

    @pytest.mark.parametrize(
        ("address", "vrf_id"), [("fe80::1", None), (ip_address("fe80::1"), "1"), (ip_address("fe80::1"), True)]
    )
    def test_an_unexpected_input_is_refused(self, address, vrf_id):
        with pytest.raises(TypeError):
            IPRuleMatcher(()).decide(address, vrf_id)


class TestTheManagementSurfaces:
    def test_the_edit_form_stores_the_network(self, ui, blue):
        response = ui.post(_url("add"), {"prefix": "fe80::1/10", "vrf": blue.pk, "action": "ignore", "description": ""})

        assert response.status_code == 302
        assert [(str(m.prefix), m.vrf_id) for m in IPAddressMapping.objects.all()] == [("fe80::/10", blue.pk)]

    def test_a_yaml_export_imports_again(self, ui, blue):
        exported = IPAddressMapping(prefix="fe80::/10", vrf=blue, description="link-local").to_yaml()

        response = ui.post(_url("bulk_import"), {"data": exported, "format": "yaml", "csv_delimiter": ","})

        assert response.status_code == 302, response.content.decode()
        mapping = IPAddressMapping.objects.get()
        assert (str(mapping.prefix), mapping.vrf_id, mapping.action) == ("fe80::/10", blue.pk, "ignore")
        assert yaml.safe_load(mapping.to_yaml())["vrf"] == blue.name

    def test_the_api_round_trips_a_mapping(self, api, blue):
        created = api.post(reverse(API_LIST), {"prefix": "fe80::1/10"}, format="json")
        scoped = api.post(reverse(API_LIST), {"prefix": "fe80::/10", "vrf": blue.pk}, format="json")
        duplicate = api.post(reverse(API_LIST), {"prefix": "fe80::/10"}, format="json")

        assert created.status_code == 201, created.content
        assert (created.json()["prefix"], created.json()["vrf"], created.json()["action"]) == (
            "fe80::/10",
            None,
            "ignore",
        )
        assert scoped.status_code == 201, scoped.content
        assert duplicate.status_code == 400
        listed = api.get(reverse(API_LIST), {"vrf__isnull": "true"}).json()
        assert [row["id"] for row in listed["results"]] == [created.json()["id"]]
        found = api.get(reverse(API_LIST), {"prefix": "fe80"}).json()
        assert {row["id"] for row in found["results"]} == {created.json()["id"], scoped.json()["id"]}

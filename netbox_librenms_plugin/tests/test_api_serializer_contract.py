"""Contract tests for the plugin's REST serializers.

Every serializer reachable through the API router must expose the NetBox identity fields and
declare ``brief_fields``, so ``?brief=true`` and nested representations match the NetBox 4.x
contract. The registrations drive the parameters, so a serializer added later fails here until
it declares the contract.
"""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.api.urls import router
from netbox_librenms_plugin.tests.conftest import make_superuser


def _registered_serializers():
    """Return (prefix, serializer_class) for every viewset the router exposes."""
    return [(prefix, viewset.serializer_class) for prefix, viewset, _basename in router.registry]


def test_router_exposes_every_serializer_under_test():
    """Guard the parameter source itself: an empty registry would make the suite vacuous."""
    assert len(_registered_serializers()) >= 9


@pytest.mark.parametrize("prefix,serializer_class", _registered_serializers(), ids=lambda v: getattr(v, "__name__", v))
def test_serializer_declares_the_identity_and_brief_contract(prefix, serializer_class):
    """`url` and `display` must be serialized, and brief mode must be declared explicitly."""
    meta = serializer_class.Meta
    fields = list(meta.fields)

    assert "url" in fields, f"{serializer_class.__name__} does not serialize url"
    assert "display" in fields, f"{serializer_class.__name__} does not serialize display"

    brief_fields = getattr(meta, "brief_fields", None)
    assert brief_fields is not None, f"{serializer_class.__name__} does not declare brief_fields"
    assert list(brief_fields)[:3] == ["id", "url", "display"], (
        f"{serializer_class.__name__} brief_fields must start with id, url, display"
    )
    unknown = set(brief_fields) - set(fields)
    assert not unknown, f"{serializer_class.__name__} brief_fields not in fields: {sorted(unknown)}"


@pytest.mark.django_db
def test_brief_mode_returns_exactly_the_declared_fields(client):
    """End-to-end proof that the declaration shapes the real API response."""
    from netbox_librenms_plugin.api.serializers import PortStackLagPatternSerializer
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.create(librenms_os="contractos", lag_name_pattern=r"^Po\d+$")
    client.force_login(make_superuser("api-brief-contract-user"))
    url = reverse("plugins-api:netbox_librenms_plugin-api:portstacklagpattern-list")

    brief = client.get(f"{url}?brief=true", HTTP_ACCEPT="application/json")
    assert brief.status_code == 200
    row = brief.json()["results"][0]
    assert set(row) == set(PortStackLagPatternSerializer.Meta.brief_fields)

    full = client.get(url, HTTP_ACCEPT="application/json")
    assert full.status_code == 200
    full_row = full.json()["results"][0]
    # The identity fields must be usable, not just present.
    assert full_row["display"]
    assert full_row["url"].endswith(f"{full_row['id']}/")

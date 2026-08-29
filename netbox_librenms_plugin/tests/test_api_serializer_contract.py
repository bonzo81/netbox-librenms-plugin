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


# ===========================================================================
# OpenAPI component names
# ===========================================================================
#
# drf-spectacular derives a component name from the serializer class name minus the
# "Serializer" suffix. Two plugins whose models share a name then emit two different
# schemas under one component name, and NetBox logs "This will very likely result in an
# incorrect schema". Namespacing every component keeps the plugin's half unambiguous.

COMPONENT_PREFIX = "LibreNMS"


def _declared_component_name(serializer_class):
    """Return the component name a serializer declares, or None when it leaves the default."""
    return (getattr(serializer_class, "_spectacular_annotation", None) or {}).get("component_name")


@pytest.mark.parametrize("prefix,serializer_class", _registered_serializers(), ids=lambda v: getattr(v, "__name__", v))
def test_serializer_declares_a_namespaced_component_name(prefix, serializer_class):
    """Every routed serializer must name its own component so a co-installed plugin cannot collide."""
    component_name = _declared_component_name(serializer_class)
    assert component_name is not None, (
        f"{serializer_class.__name__} does not declare component_name; drf-spectacular would derive "
        f"{serializer_class.__name__.removesuffix('Serializer')!r}, which any other plugin with a model "
        "of that name also claims"
    )
    assert component_name.startswith(COMPONENT_PREFIX), (
        f"{serializer_class.__name__} component {component_name!r} is not namespaced with {COMPONENT_PREFIX!r}"
    )


def test_component_names_are_unique_across_the_plugin():
    """Two serializers sharing one component name would collide with each other."""
    declared = [_declared_component_name(cls) for _prefix, cls in _registered_serializers()]
    duplicates = {name for name in declared if declared.count(name) > 1}
    assert not duplicates, f"component names reused within the plugin: {sorted(duplicates)}"


@pytest.mark.django_db
def test_generated_schema_uses_the_namespaced_components():
    """End-to-end proof: the real generator emits the namespaced names, not the bare model names."""
    from drf_spectacular.generators import SchemaGenerator

    from netbox_librenms_plugin.api import urls as api_urls

    schema = SchemaGenerator(patterns=api_urls.urlpatterns).get_schema(request=None, public=True)
    components = set(schema["components"]["schemas"])

    for _prefix, serializer_class in _registered_serializers():
        bare = serializer_class.__name__.removesuffix("Serializer")
        component_name = _declared_component_name(serializer_class)
        if bare != component_name:
            assert bare not in components, f"{bare} is still emitted unnamespaced and can collide with another plugin"
        assert component_name in components


@pytest.mark.django_db
def test_sync_job_status_documents_the_shape_it_actually_returns(client):
    """The generated 200 schema must name the component whose fields the real endpoint returns."""
    import uuid

    from core.choices import JobStatusChoices
    from core.models import Job, ObjectType
    from django.urls import reverse
    from drf_spectacular.generators import SchemaGenerator

    from netbox_librenms_plugin.api import urls as api_urls
    from netbox_librenms_plugin.api.serializers import SyncJobStatusSerializer
    from netbox_librenms_plugin.jobs import FilterDevicesJob

    schema = SchemaGenerator(patterns=api_urls.urlpatterns).get_schema(request=None, public=True)
    path = next(p for p in schema["paths"] if "sync-status" in p)
    operation = schema["paths"][path]["post"]
    documented = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert documented.endswith("/LibreNMSSyncJobStatus"), documented

    user = make_superuser("api-job-status-contract-user")
    # A job_id that was never enqueued makes RQJob.fetch raise NoSuchJobError. That is the one
    # branch reachable without a live worker, and it returns the documented success shape.
    job = Job.objects.create(
        object_type=ObjectType.objects.get_for_model(Job),
        name=FilterDevicesJob.Meta.name,
        user=user,
        status=JobStatusChoices.STATUS_PENDING,
        job_id=uuid.uuid4(),
    )
    client.force_login(user)

    response = client.post(reverse("plugins-api:netbox_librenms_plugin-api:sync_job_status", args=[job.pk]))

    assert response.status_code == 200
    assert set(response.json()) == set(SyncJobStatusSerializer().fields)

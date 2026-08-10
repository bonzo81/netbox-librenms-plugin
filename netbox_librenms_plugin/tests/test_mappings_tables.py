"""Selection-column config and bulk-export contract for the mapping tables."""

import pytest


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "expected_os"),
    [
        ("review-os-token", "review-os-token"),
        ("ReviewPatternToken", "review-pattern-os"),
        ("review description token", "review-description-os"),
    ],
)
def test_port_stack_lag_pattern_searches_all_exposed_fields(query, expected_os):
    """The list search filters OS, pattern, and description through its q field."""
    from netbox_librenms_plugin.filters import PortStackLagPatternFilterSet
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.bulk_create(
        [
            PortStackLagPattern(
                librenms_os="review-os-token",
                lag_name_pattern=r"^ReviewOs\d+$",
                description="OS field match",
            ),
            PortStackLagPattern(
                librenms_os="review-pattern-os",
                lag_name_pattern=r"^ReviewPatternToken\d+$",
                description="Pattern field match",
            ),
            PortStackLagPattern(
                librenms_os="review-description-os",
                lag_name_pattern=r"^ReviewDescription\d+$",
                description="Review description token",
            ),
        ]
    )

    filterset = PortStackLagPatternFilterSet({"q": query}, queryset=PortStackLagPattern.objects.all())

    assert filterset.is_valid(), filterset.errors
    assert list(filterset.qs.values_list("librenms_os", flat=True)) == [expected_os]


@pytest.mark.django_db
class TestPortStackLagPatternTableSelection:
    """PortStackLagPatternTable's selection column must submit name='pk'."""

    def test_selection_checkbox_submits_pk(self):
        """The rendered selection checkbox uses name='pk' (what the generic/export views read), not 'select'."""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.models import PortStackLagPattern
        from netbox_librenms_plugin.tables.mappings import PortStackLagPatternTable

        PortStackLagPattern.objects.create(librenms_os="seltestos", lag_name_pattern=r"^Po\d+$")
        table = PortStackLagPatternTable(PortStackLagPattern.objects.all())
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        RequestConfig(request).configure(table)

        # Render only the selection cell so the actions column's URL resolution stays out of scope.
        pk_cell = str(table.rows[0].get_cell("pk"))
        assert 'name="pk"' in pk_cell
        assert 'name="select"' not in pk_cell


@pytest.mark.django_db
class TestPortStackLagPatternBulkExportContract:
    """BulkExportYAMLView reads getlist('pk'); the table must submit the same key."""

    def _superuser(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username="psl-exporter")
        user.is_superuser = True
        user.is_active = True
        user.save()
        return user

    def test_export_returns_yaml_for_pk_selected_rows(self):
        """A POST carrying the selected pks (name='pk') exports those rows as YAML."""
        from django.test import RequestFactory

        from netbox_librenms_plugin.models import PortStackLagPattern
        from netbox_librenms_plugin.views.mapping_views import PortStackLagPatternBulkExportYAMLView

        obj = PortStackLagPattern.objects.create(librenms_os="exportos", lag_name_pattern=r"^Po\d+$")
        request = RequestFactory().post("/", {"pk": [str(obj.pk)]})
        request.user = self._superuser()

        response = PortStackLagPatternBulkExportYAMLView.as_view()(request)

        assert response.status_code == 200
        assert response["Content-Type"] == "text/yaml; charset=utf-8"
        assert response["Content-Disposition"] == 'attachment; filename="export.yaml"'
        assert "exportos" in response.content.decode()

    def test_select_named_inputs_alone_export_nothing(self):
        """The old table contract (name='select') submits a key the view never reads, so it exports nothing — proving the table must submit 'pk'."""
        from django.test import RequestFactory

        from netbox_librenms_plugin.models import PortStackLagPattern
        from netbox_librenms_plugin.views.mapping_views import PortStackLagPatternBulkExportYAMLView

        obj = PortStackLagPattern.objects.create(librenms_os="ignoredos", lag_name_pattern=r"^Po\d+$")
        request = RequestFactory().post("/", {"select": [str(obj.pk)]})
        request.user = self._superuser()

        response = PortStackLagPatternBulkExportYAMLView.as_view()(request)

        assert response.status_code == 400


@pytest.mark.django_db
class TestSerialSensorTypePatternBulkExportScope:
    """Serial sensor mapping exports must keep the user's object constraints."""

    def test_export_omits_a_selected_row_outside_the_view_grant(self):
        from django.test import Client
        from django.urls import reverse

        from netbox_librenms_plugin.models import SerialSensorTypePattern
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        visible = SerialSensorTypePattern.objects.create(
            sensor_type="visibleSerialState",
            port_name_pattern="tty{N}",
        )
        hidden = SerialSensorTypePattern.objects.create(
            sensor_type="hiddenSerialState",
            port_name_pattern="line{N}",
        )
        user = make_user_with_perms(
            "serial-pattern-export-scope",
            [("view", SerialSensorTypePattern)],
            constraints={"pk": visible.pk},
        )
        client = Client()
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:serialsensortypepattern_bulk_export_yaml"),
            {"pk": [visible.pk, hidden.pk]},
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert visible.sensor_type in content
        assert hidden.sensor_type not in content

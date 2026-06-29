"""Selection-column config and bulk-export contract for the mapping tables."""

import pytest


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

"""The plugin Settings page: the cable-sync provenance tab (DB/UI-managed, not PLUGINS_CONFIG)."""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import make_superuser


@pytest.mark.django_db
class TestCableSyncSettingsTab:
    """End-to-end through the real view: render, persist, and validate the cable-sync form."""

    def _url(self):
        return reverse("plugins:netbox_librenms_plugin:settings")

    def test_settings_page_renders_the_cable_sync_form(self, client):
        client.force_login(make_superuser())
        response = client.get(self._url())
        assert response.status_code == 200
        html = response.content.decode()
        for field_id in ("id_cable_sync_tag", "id_cable_sync_tag_color", "id_cable_sync_description"):
            assert field_id in html, f"settings page is missing the {field_id} input"
        assert 'value="cable_sync_settings"' in html  # the tab's form_type dispatch value
        # NetBoxModelForm auto-adds tags/changelog fields; this plain settings model supports
        # neither, so the template must render ONLY the cable-sync fields (uniform plugin
        # no-tags convention — a rendered tags picker would crash on save).
        assert "id_tags" not in html, "the settings page must not render the NetBoxModelForm tags field"

    def test_post_persists_cable_sync_settings(self, client):
        client.force_login(make_superuser())
        response = client.post(
            self._url(),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": "custom-prov",
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "Stamped by custom sync",
            },
        )
        assert response.status_code == 302
        row = LibreNMSSettings.objects.get(pk=1)
        assert row.cable_sync_tag == "custom-prov"
        assert row.cable_sync_tag_color == "ff5722"
        assert row.cable_sync_description == "Stamped by custom sync"

    def test_blank_tag_name_is_rejected_and_nothing_persists(self, client):
        """A blank provenance tag would slugify to '' and break the ownership get_or_create — the form must reject it (whitespace-only strips to '' → required-field error) and the stored settings must keep their previous value."""
        client.force_login(make_superuser())
        response = client.post(
            self._url(),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": "   ",
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "whatever",
            },
        )
        # Validation failure re-renders the page (no redirect) with the error attached.
        assert response.status_code == 200
        assert "This field is required." in response.content.decode()
        row = LibreNMSSettings.objects.get(pk=1)
        assert row.cable_sync_tag == "librenms"  # untouched default

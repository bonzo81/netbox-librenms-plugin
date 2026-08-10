"""The plugin Settings page: the cable-sync provenance tab (DB/UI-managed, not PLUGINS_CONFIG)."""

import pytest
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import cable_together, make_serial_device, make_superuser


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

    def test_renaming_provenance_tag_preserves_existing_cable_identity(self, client):
        """A settings rename must update the managed Tag instead of abandoning old cables."""
        from extras.models import Tag

        from netbox_librenms_plugin.utils import cable_has_librenms_tag, get_librenms_cable_tag

        client.force_login(make_superuser())
        settings, _ = LibreNMSSettings.objects.get_or_create()
        tag = get_librenms_cable_tag(sync_settings=settings)
        original_tag_pk = tag.pk
        local, (csp,), _ = make_serial_device("settings-rename-local", csp_names=["ttyS1"])
        _remote, _, (cp,) = make_serial_device("settings-rename-remote", cp_names=["console"])
        cable = cable_together(csp, cp)
        cable.tags.add(tag)

        response = client.post(
            self._url(),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": "managed-cables",
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "Managed cable",
            },
        )

        assert response.status_code == 302
        renamed = Tag.objects.get(pk=original_tag_pk)
        assert renamed.name == "managed-cables"
        assert renamed.color == "ff5722"
        cable.refresh_from_db()
        assert cable_has_librenms_tag(cable) is True

    def test_tag_rename_requires_permission_for_the_existing_tag(self, client):
        """Plugin settings access must not authorize a global Tag mutation."""
        from extras.models import Tag

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        settings, _ = LibreNMSSettings.objects.get_or_create()
        tag = get_librenms_cable_tag(sync_settings=settings)
        user = make_user_with_perms("settings-no-tag-change", [])
        client.force_login(user)

        response = client.post(
            self._url(),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": "renamed-without-tag-permission",
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "Managed cable",
            },
        )

        assert response.status_code == 403
        settings.refresh_from_db()
        tag.refresh_from_db()
        assert settings.cable_sync_tag == "librenms"
        assert tag.name == "librenms"
        assert Tag.objects.count() == 1

    def test_existing_target_tag_cannot_be_recolored_without_tag_permission(self, client):
        """A missing old Tag must not make an unrelated target Tag writable."""
        from extras.models import Tag

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        settings, _ = LibreNMSSettings.objects.get_or_create()
        settings.cable_sync_tag = "missing-old-provenance"
        settings.save(update_fields=["cable_sync_tag"])
        target = Tag.objects.create(name="shared-provenance", slug="shared-provenance", color="00aa00")
        user = make_user_with_perms("settings-target-tag-no-change", [])
        client.force_login(user)

        response = client.post(
            self._url(),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": target.name,
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "Managed cable",
            },
        )

        assert response.status_code == 403
        settings.refresh_from_db()
        target.refresh_from_db()
        assert settings.cable_sync_tag == "missing-old-provenance"
        assert target.color == "00aa00"

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

    def test_punctuation_only_tag_is_rejected_before_cable_sync(self, client):
        """The settings form must reject a tag name that cannot produce a valid slug."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        client.force_login(make_superuser())
        response = client.post(
            self._url(),
            {
                "form_type": "cable_sync_settings",
                "cable_sync_tag": "!!!",
                "cable_sync_tag_color": "ff5722",
                "cable_sync_description": "Managed cable",
            },
        )

        assert response.status_code == 200
        assert "must contain letters or numbers" in response.content.decode()
        assert LibreNMSSettings.objects.get(pk=1).cable_sync_tag == "librenms"

        local, (csp,), _ = make_serial_device("settings-tag-local", csp_names=["ttyS1"])
        remote, _, (cp,) = make_serial_device("settings-tag-remote", cp_names=["console"])
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        row = {
            "local_port": csp.name,
            "local_port_id": f"serial:{csp.pk}",
            "_source": "serial",
            "device_id": local.pk,
            "remote_device": remote.name,
            "sensor_id": csp.pk,
            "sensor_index_int": 1,
            "is_configured": True,
        }
        cache_key = object.__new__(SyncCablesView).get_cache_key(local, "links", server_key)
        cache.set(cache_key, {"links": [row]}, timeout=300)

        synced = client.post(
            reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[local.pk]),
            {"select": row["local_port_id"], "server_key": server_key},
        )

        assert synced.status_code == 302
        csp.refresh_from_db()
        cp.refresh_from_db()
        assert csp.cable_id == cp.cable_id
        assert set(csp.cable.tags.values_list("slug", flat=True)) == {"librenms"}

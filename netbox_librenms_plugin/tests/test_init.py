"""Integration tests for the plugin's post-migrate custom-field bootstrap."""

import logging

import pytest


pytestmark = pytest.mark.django_db


def _required_content_type_ids():
    from dcim.models import Device, Interface
    from django.contrib.contenttypes.models import ContentType
    from virtualization.models import VirtualMachine, VMInterface

    return {ContentType.objects.get_for_model(model).pk for model in (Device, VirtualMachine, Interface, VMInterface)}


class TestEnsureLibreNMSIdCustomField:
    """Exercise the signal handler against the real NetBox schema."""

    def setup_method(self):
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        _ensure_librenms_id_custom_field._executed_aliases = set()

    def test_creates_the_field_with_all_required_object_types(self, caplog):
        from extras.models import CustomField

        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        CustomField.objects.filter(name="librenms_id").delete()

        with caplog.at_level(logging.INFO, logger="netbox_librenms_plugin"):
            _ensure_librenms_id_custom_field(sender=None)

        custom_field = CustomField.objects.get(name="librenms_id")
        assert custom_field.type == "json"
        assert custom_field.label == "LibreNMS ID"
        assert custom_field.description == "LibreNMS Device ID for synchronization (auto-created by plugin)"
        assert custom_field.required is False
        assert custom_field.ui_visible == "if-set"
        assert custom_field.ui_editable == "yes"
        assert custom_field.is_cloneable is False
        assert set(custom_field.object_types.values_list("pk", flat=True)) == _required_content_type_ids()
        assert "Auto-created 'librenms_id' custom field" in caplog.text

    def test_second_call_for_the_same_alias_is_a_no_op(self):
        from extras.models import CustomField

        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        custom_field = CustomField.objects.get(name="librenms_id")
        _ensure_librenms_id_custom_field._executed_aliases = {"default"}
        custom_field.label = "Unchanged by skipped handler"
        custom_field.save(update_fields=["label"])

        _ensure_librenms_id_custom_field(sender=None)

        custom_field.refresh_from_db()
        assert custom_field.label == "Unchanged by skipped handler"

    def test_existing_field_keeps_its_identity(self):
        from extras.models import CustomField

        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        custom_field = CustomField.objects.get(name="librenms_id")
        original_pk = custom_field.pk

        _ensure_librenms_id_custom_field(sender=None)

        assert CustomField.objects.get(name="librenms_id").pk == original_pk

    def test_adds_only_missing_required_object_types(self):
        from dcim.models import Device
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        custom_field = CustomField.objects.get(name="librenms_id")
        device_type = ContentType.objects.get_for_model(Device)
        custom_field.object_types.remove(device_type)
        assert device_type.pk not in set(custom_field.object_types.values_list("pk", flat=True))

        _ensure_librenms_id_custom_field(sender=None)

        assert set(custom_field.object_types.values_list("pk", flat=True)) == _required_content_type_ids()

    def test_database_failure_is_logged_and_can_be_retried(self, caplog):
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        with caplog.at_level(logging.ERROR, logger="netbox_librenms_plugin"):
            _ensure_librenms_id_custom_field(sender=None, using="missing-database-alias")

        assert "Failed to auto-create 'librenms_id' custom field" in caplog.text
        assert "missing-database-alias" not in _ensure_librenms_id_custom_field._executed_aliases

    def test_existing_json_field_does_not_emit_creation_or_migration_log(self, caplog):
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        with caplog.at_level(logging.INFO, logger="netbox_librenms_plugin"):
            _ensure_librenms_id_custom_field(sender=None)

        assert "Auto-created" not in caplog.text
        assert "Migrated" not in caplog.text

    def test_integer_field_is_migrated_to_json(self, caplog):
        from extras.models import CustomField

        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        custom_field = CustomField.objects.get(name="librenms_id")
        custom_field.type = "integer"
        custom_field.save(update_fields=["type"])

        with caplog.at_level(logging.INFO, logger="netbox_librenms_plugin"):
            _ensure_librenms_id_custom_field(sender=None, using="default")

        custom_field.refresh_from_db()
        assert custom_field.type == "json"
        assert "Migrated 'librenms_id' custom field type from integer to json" in caplog.text
        assert "default" in _ensure_librenms_id_custom_field._executed_aliases

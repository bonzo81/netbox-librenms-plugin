"""Tests for netbox_librenms_plugin.__init__ module.

Covers the _ensure_librenms_id_custom_field post_migrate signal handler.
"""

from unittest.mock import MagicMock, patch


# =============================================================================
# TestEnsureLibreNMSIdCustomField - 6 tests
# =============================================================================


class TestEnsureLibreNMSIdCustomField:
    """Test _ensure_librenms_id_custom_field signal handler."""

    def setup_method(self):
        """Reset the _executed flag before each test for consistent isolation."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        _ensure_librenms_id_custom_field._executed = False

    @patch("dcim.models.Interface", new_callable=MagicMock)
    @patch("dcim.models.Device", new_callable=MagicMock)
    @patch("virtualization.models.VMInterface", new_callable=MagicMock)
    @patch("virtualization.models.VirtualMachine", new_callable=MagicMock)
    @patch("django.contrib.contenttypes.models.ContentType")
    @patch("extras.models.CustomField")
    def test_creates_custom_field_when_missing(
        self, MockCustomField, MockContentType, mock_vm, mock_vmif, mock_device, mock_iface
    ):
        """Custom field is created with correct defaults when it does not exist."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        mock_cf = MagicMock()
        mock_cf.object_types.values_list.return_value = []
        MockCustomField.objects.get_or_create.return_value = (mock_cf, True)

        mock_ct = MagicMock()
        mock_ct.pk = 1
        MockContentType.objects.get_for_model.return_value = mock_ct

        with patch("logging.getLogger") as mock_get_logger:
            _ensure_librenms_id_custom_field(sender=None)

        MockCustomField.objects.get_or_create.assert_called_once_with(
            name="librenms_id",
            defaults={
                "type": "integer",
                "label": "LibreNMS ID",
                "description": "LibreNMS Device ID for synchronization (auto-created by plugin)",
                "required": False,
                "ui_visible": "if-set",
                "ui_editable": "yes",
                "is_cloneable": False,
            },
        )

        # Should have added content types for all 4 models
        assert mock_cf.object_types.add.call_count == 4

        # Should log when created
        mock_get_logger.assert_called_with("netbox_librenms_plugin")

    def test_skips_when_already_executed(self):
        """Handler is a no-op on second invocation (per-migrate dedup)."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        _ensure_librenms_id_custom_field._executed = True

        with patch("extras.models.CustomField") as MockCustomField:
            _ensure_librenms_id_custom_field(sender=None)
            MockCustomField.objects.get_or_create.assert_not_called()

    @patch("dcim.models.Interface", new_callable=MagicMock)
    @patch("dcim.models.Device", new_callable=MagicMock)
    @patch("virtualization.models.VMInterface", new_callable=MagicMock)
    @patch("virtualization.models.VirtualMachine", new_callable=MagicMock)
    @patch("django.contrib.contenttypes.models.ContentType")
    @patch("extras.models.CustomField")
    def test_existing_field_not_recreated(
        self, MockCustomField, MockContentType, mock_vm, mock_vmif, mock_device, mock_iface
    ):
        """When custom field already exists, it is not recreated but types are checked."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        mock_cf = MagicMock()
        mock_cf.object_types.values_list.return_value = [1, 2, 3, 4]
        MockCustomField.objects.get_or_create.return_value = (mock_cf, False)

        mock_ct = MagicMock()
        mock_ct.pk = 1
        MockContentType.objects.get_for_model.return_value = mock_ct

        _ensure_librenms_id_custom_field(sender=None)

        # All pks already present, no types should be added
        mock_cf.object_types.add.assert_not_called()

    @patch("dcim.models.Interface", new_callable=MagicMock)
    @patch("dcim.models.Device", new_callable=MagicMock)
    @patch("virtualization.models.VMInterface", new_callable=MagicMock)
    @patch("virtualization.models.VirtualMachine", new_callable=MagicMock)
    @patch("django.contrib.contenttypes.models.ContentType")
    @patch("extras.models.CustomField")
    def test_adds_missing_content_types(
        self, MockCustomField, MockContentType, mock_vm, mock_vmif, mock_device, mock_iface
    ):
        """When some content types are missing, only those are added."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        mock_cf = MagicMock()
        mock_cf.object_types.values_list.return_value = [1, 2]
        MockCustomField.objects.get_or_create.return_value = (mock_cf, False)

        ct_existing = MagicMock()
        ct_existing.pk = 1
        ct_new = MagicMock()
        ct_new.pk = 99
        MockContentType.objects.get_for_model.side_effect = [ct_existing, ct_existing, ct_new, ct_new]

        _ensure_librenms_id_custom_field(sender=None)

        assert mock_cf.object_types.add.call_count == 2
        mock_cf.object_types.add.assert_any_call(ct_new)

    @patch("extras.models.CustomField")
    def test_exception_does_not_propagate(self, MockCustomField):
        """Exceptions during custom field creation are caught and logged."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        MockCustomField.objects.get_or_create.side_effect = Exception("DB not ready")

        with patch("logging.getLogger") as mock_get_logger:
            # Should not raise
            _ensure_librenms_id_custom_field(sender=None)

            # Verify the exception was logged
            logger_instance = mock_get_logger.return_value
            logger_instance.exception.assert_called_once()
            call_args = logger_instance.exception.call_args
            assert "librenms_id" in call_args[0][0]

    @patch("dcim.models.Interface", new_callable=MagicMock)
    @patch("dcim.models.Device", new_callable=MagicMock)
    @patch("virtualization.models.VMInterface", new_callable=MagicMock)
    @patch("virtualization.models.VirtualMachine", new_callable=MagicMock)
    @patch("django.contrib.contenttypes.models.ContentType")
    @patch("extras.models.CustomField")
    def test_no_log_when_field_already_exists(
        self, MockCustomField, MockContentType, mock_vm, mock_vmif, mock_device, mock_iface
    ):
        """No log message when the custom field already existed."""
        from netbox_librenms_plugin import _ensure_librenms_id_custom_field

        mock_cf = MagicMock()
        mock_cf.object_types.values_list.return_value = [1, 2, 3, 4]
        MockCustomField.objects.get_or_create.return_value = (mock_cf, False)

        mock_ct = MagicMock()
        mock_ct.pk = 1
        MockContentType.objects.get_for_model.return_value = mock_ct

        with patch("logging.getLogger") as mock_get_logger:
            _ensure_librenms_id_custom_field(sender=None)
            # When the field already exists (created=False), the info log should
            # not be emitted.  We verify via the logger instance rather than
            # asserting getLogger was never called, which is fragile.
            logger_instance = mock_get_logger.return_value
            logger_instance.info.assert_not_called()

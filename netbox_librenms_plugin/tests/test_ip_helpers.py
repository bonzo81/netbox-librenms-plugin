"""
Tests for netbox_librenms_plugin.import_utils.ip_helpers.

All DB interactions are mocked — no @pytest.mark.django_db used.
"""

from unittest.mock import MagicMock, patch


class TestGetOrCreateGlobalIP:
    """Tests for ``get_or_create_global_ip``."""

    def test_returns_none_for_empty_string(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        assert get_or_create_global_ip("") is None
        assert get_or_create_global_ip(None) is None
        assert get_or_create_global_ip("   ") is None

    def test_returns_none_for_invalid_ip(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        assert get_or_create_global_ip("not-an-ip") is None
        assert get_or_create_global_ip("192.168.1.999") is None

    def test_returns_existing_when_present(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        existing = MagicMock(name="existing-ip")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = existing

            result = get_or_create_global_ip("10.0.0.1")

            mock_model.objects.filter.assert_called_once_with(address__net_host="10.0.0.1")
            mock_model.objects.create.assert_not_called()
            assert result is existing

    def test_creates_ipv4_with_slash32_when_missing(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        created = MagicMock(name="created-ip")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.return_value = created

            result = get_or_create_global_ip("10.0.0.1")

            mock_model.objects.create.assert_called_once_with(address="10.0.0.1/32", status="active")
            assert result is created

    def test_creates_ipv6_with_slash128_when_missing(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        created = MagicMock(name="created-ip-v6")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.return_value = created

            result = get_or_create_global_ip("2001:db8::1")

            mock_model.objects.create.assert_called_once_with(address="2001:db8::1/128", status="active")
            assert result is created

    def test_strips_whitespace_before_lookup(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.return_value = MagicMock()

            get_or_create_global_ip("  10.1.2.3  ")

            mock_model.objects.filter.assert_called_once_with(address__net_host="10.1.2.3")
            mock_model.objects.create.assert_called_once_with(address="10.1.2.3/32", status="active")

    def test_returns_none_and_logs_when_create_raises(self, caplog):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.side_effect = RuntimeError("integrity failure")

            with caplog.at_level("WARNING"):
                result = get_or_create_global_ip("10.0.0.1")

            assert result is None
            assert any("failed to auto-create" in r.message for r in caplog.records)

    def test_reexported_from_import_utils_package(self):
        """The helper is re-exported from the package root for convenience."""
        from netbox_librenms_plugin.import_utils import get_or_create_global_ip as pkg_helper
        from netbox_librenms_plugin.import_utils.ip_helpers import (
            get_or_create_global_ip as module_helper,
        )

        assert pkg_helper is module_helper

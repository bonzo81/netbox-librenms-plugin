"""
Tests for netbox_librenms_plugin.import_utils.ip_helpers.

All DB interactions are mocked — no @pytest.mark.django_db used.
"""

from unittest.mock import MagicMock, patch


class TestGetOrCreateGlobalIP:
    """Tests for ``get_or_create_global_ip``."""

    def test_returns_none_for_empty_string(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        assert get_or_create_global_ip("") == (None, False)
        assert get_or_create_global_ip(None) == (None, False)
        assert get_or_create_global_ip("   ") == (None, False)

    def test_returns_none_for_invalid_ip(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        assert get_or_create_global_ip("not-an-ip") == (None, False)
        assert get_or_create_global_ip("192.168.1.999") == (None, False)

    def test_returns_existing_when_present(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        existing = MagicMock(name="existing-ip")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = existing

            result, created = get_or_create_global_ip("10.0.0.1")

            mock_model.objects.filter.assert_called_once_with(address__net_host="10.0.0.1", vrf__isnull=True)
            mock_model.objects.create.assert_not_called()
            assert result is existing
            assert created is False

    def test_creates_ipv4_with_slash32_when_missing(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        created = MagicMock(name="created-ip")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.return_value = created

            result, was_created = get_or_create_global_ip("10.0.0.1")

            mock_model.objects.create.assert_called_once_with(address="10.0.0.1/32", status="active")
            assert result is created
            assert was_created is True

    def test_creates_ipv6_with_slash128_when_missing(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        created = MagicMock(name="created-ip-v6")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.return_value = created

            result, was_created = get_or_create_global_ip("2001:db8::1")

            mock_model.objects.create.assert_called_once_with(address="2001:db8::1/128", status="active")
            assert result is created
            assert was_created is True

    def test_strips_whitespace_before_lookup(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.return_value = MagicMock()

            result, was_created = get_or_create_global_ip("  10.1.2.3  ")

            mock_model.objects.filter.assert_called_once_with(address__net_host="10.1.2.3", vrf__isnull=True)
            mock_model.objects.create.assert_called_once_with(address="10.1.2.3/32", status="active")
            assert was_created is True

    def test_integrity_error_on_create_returns_concurrently_created_record(self):
        """A concurrent insert that races us is re-fetched and returned."""
        from django.db import IntegrityError

        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        racing_winner = MagicMock(name="racing-winner")
        with patch("ipam.models.IPAddress") as mock_model:
            # First filter (pre-create) → None, second filter (post-IntegrityError) → winner.
            mock_model.objects.filter.return_value.first.side_effect = [None, racing_winner]
            mock_model.objects.create.side_effect = IntegrityError("duplicate key")

            result, was_created = get_or_create_global_ip("10.0.0.1")

            assert result is racing_winner
            assert was_created is False

    def test_returns_none_and_logs_when_create_raises(self, caplog):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None
            mock_model.objects.create.side_effect = RuntimeError("integrity failure")

            with caplog.at_level("WARNING"):
                result, was_created = get_or_create_global_ip("10.0.0.1")

            assert result is None
            assert was_created is False
            assert any("failed to auto-create" in r.message for r in caplog.records)

    def test_reexported_from_import_utils_package(self):
        """The helper is re-exported from the package root for convenience."""
        from netbox_librenms_plugin.import_utils import get_or_create_global_ip as pkg_helper
        from netbox_librenms_plugin.import_utils.ip_helpers import (
            get_or_create_global_ip as module_helper,
        )

        assert pkg_helper is module_helper


class TestAutoCreateOptIn:
    """``auto_create=False`` opt-out and ``auto_create_ipam_enabled()`` helper."""

    def test_auto_create_false_returns_existing(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        existing = MagicMock(name="existing-ip")
        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = existing

            result, created = get_or_create_global_ip("10.0.0.1", auto_create=False)

            assert result is existing
            assert created is False
            mock_model.objects.create.assert_not_called()

    def test_auto_create_false_skips_creation_when_missing(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import get_or_create_global_ip

        with patch("ipam.models.IPAddress") as mock_model:
            mock_model.objects.filter.return_value.first.return_value = None

            result, created = get_or_create_global_ip("10.0.0.1", auto_create=False)

            assert result is None
            assert created is False
            mock_model.objects.create.assert_not_called()

    def test_auto_create_ipam_enabled_false_when_no_settings(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import auto_create_ipam_enabled

        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_model:
            mock_model.objects.first.return_value = None
            assert auto_create_ipam_enabled() is False

    def test_auto_create_ipam_enabled_reads_field(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import auto_create_ipam_enabled

        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_model:
            settings = MagicMock(auto_create_ipam_default=True)
            mock_model.objects.first.return_value = settings
            assert auto_create_ipam_enabled() is True

            settings.auto_create_ipam_default = False
            assert auto_create_ipam_enabled() is False

    def test_auto_create_ipam_enabled_swallows_exceptions(self):
        from netbox_librenms_plugin.import_utils.ip_helpers import auto_create_ipam_enabled

        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_model:
            mock_model.objects.first.side_effect = RuntimeError("db down")
            assert auto_create_ipam_enabled() is False

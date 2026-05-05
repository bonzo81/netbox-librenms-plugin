"""
Tests for PlatformMapping model, to_yaml() on all mapping models,
find_matching_platform ordering, and BulkExportYAML view.

TDD: these tests are written before implementation.
"""

from unittest.mock import MagicMock, patch


def _set_fk_cache(instance, field_name, value):
    """Set a FK field value directly via Django's _state.fields_cache, bypassing descriptor validation."""
    from django.db.models.base import ModelState

    if not hasattr(instance, "_state"):
        instance._state = ModelState()
    instance._state.fields_cache[field_name] = value


# =============================================================================
# TestPlatformMappingModel
# =============================================================================


class TestPlatformMappingModel:
    """Tests for PlatformMapping model behaviour."""

    def test_str_representation(self):
        """__str__ shows librenms_os -> platform."""
        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "ios"
        platform = MagicMock()
        platform.__str__ = lambda s: "Cisco IOS"
        _set_fk_cache(mapping, "netbox_platform", platform)
        assert str(mapping) == "ios -> Cisco IOS"

    def test_clean_strips_whitespace(self):
        """clean() strips leading/trailing whitespace from librenms_os."""
        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "  ios  "
        mapping.description = ""
        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()
        assert mapping.librenms_os == "ios"

    def test_clean_normalizes_to_lowercase(self):
        """clean() lowercases librenms_os to prevent case-variant duplicates."""
        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "IOS"
        mapping.description = ""
        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()
        assert mapping.librenms_os == "ios"

    def test_clean_strips_and_lowercases(self):
        """clean() strips and lowercases in one pass."""
        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "  EOS  "
        mapping.description = ""
        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()
        assert mapping.librenms_os == "eos"

    def test_clean_raises_on_blank(self):
        """clean() raises ValidationError when librenms_os is blank after strip."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "   "
        mapping.description = ""

        import pytest

        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                mapping.clean()
        assert "librenms_os" in str(exc_info.value)

    def test_get_absolute_url(self):
        """get_absolute_url returns correct URL."""
        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.pk = 42

        with patch("netbox_librenms_plugin.models.reverse") as mock_reverse:
            mock_reverse.return_value = "/plugins/librenms/platform-mappings/42/"
            url = mapping.get_absolute_url()
        mock_reverse.assert_called_once_with("plugins:netbox_librenms_plugin:platformmapping_detail", args=[42])
        assert url == "/plugins/librenms/platform-mappings/42/"

    def test_meta_ordering(self):
        """Model Meta ordering is by librenms_os."""
        from netbox_librenms_plugin.models import PlatformMapping

        assert PlatformMapping._meta.ordering == ["librenms_os"]


# =============================================================================
# TestDeviceTypeMappingModel
# =============================================================================


class TestDeviceTypeMappingModel:
    """Tests for DeviceTypeMapping.clean() normalization."""

    def test_clean_strips_whitespace(self):
        """clean() strips leading/trailing whitespace from librenms_hardware."""
        from netbox_librenms_plugin.models import DeviceTypeMapping

        mapping = DeviceTypeMapping.__new__(DeviceTypeMapping)
        mapping.librenms_hardware = "  Cisco 4321  "
        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()
        assert mapping.librenms_hardware == "cisco 4321"

    def test_clean_normalizes_to_lowercase(self):
        """clean() lowercases librenms_hardware to prevent case-variant duplicates."""
        from netbox_librenms_plugin.models import DeviceTypeMapping

        mapping = DeviceTypeMapping.__new__(DeviceTypeMapping)
        mapping.librenms_hardware = "Juniper MX480"
        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()
        assert mapping.librenms_hardware == "juniper mx480"

    def test_clean_raises_on_blank(self):
        """clean() raises ValidationError when librenms_hardware is blank after strip."""
        import pytest
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import DeviceTypeMapping

        mapping = DeviceTypeMapping.__new__(DeviceTypeMapping)
        mapping.librenms_hardware = "   "
        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                mapping.clean()
        assert "librenms_hardware" in str(exc_info.value)


class TestPlatformMappingToYaml:
    """to_yaml() returns valid YAML string with expected keys."""

    def test_to_yaml_returns_string(self):
        """to_yaml() returns a string."""
        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "ios"
        platform = MagicMock()
        platform.__str__ = lambda s: "Cisco IOS"
        _set_fk_cache(mapping, "netbox_platform", platform)
        mapping.description = "Test description"

        result = mapping.to_yaml()
        assert isinstance(result, str)

    def test_to_yaml_contains_expected_keys(self):
        """to_yaml() output contains librenms_os, netbox_platform, description."""
        import yaml

        from netbox_librenms_plugin.models import PlatformMapping

        mapping = PlatformMapping.__new__(PlatformMapping)
        mapping.librenms_os = "ios"
        platform = MagicMock()
        platform.__str__ = lambda s: "Cisco IOS"
        _set_fk_cache(mapping, "netbox_platform", platform)
        mapping.description = "A description"

        result = yaml.safe_load(mapping.to_yaml())
        assert result["librenms_os"] == "ios"
        assert result["netbox_platform"] == "Cisco IOS"
        assert result["description"] == "A description"


# =============================================================================
# TestToYamlOnAllMappingModels
# =============================================================================


class TestToYamlOnAllMappingModels:
    """All mapping models must have to_yaml() returning a YAML string."""

    def test_device_type_mapping_has_to_yaml(self):
        """DeviceTypeMapping.to_yaml() returns a YAML string."""
        import yaml

        from netbox_librenms_plugin.models import DeviceTypeMapping

        mapping = DeviceTypeMapping.__new__(DeviceTypeMapping)
        mapping.librenms_hardware = "Cisco 4321"
        device_type = MagicMock()
        device_type.__str__ = lambda s: "Cisco 4321"
        _set_fk_cache(mapping, "netbox_device_type", device_type)
        mapping.description = ""

        result = mapping.to_yaml()
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert "librenms_hardware" in data
        assert data["librenms_hardware"] == "Cisco 4321"

    def test_module_type_mapping_has_to_yaml(self):
        """ModuleTypeMapping.to_yaml() returns a YAML string."""
        import yaml

        from netbox_librenms_plugin.models import ModuleTypeMapping

        mapping = ModuleTypeMapping.__new__(ModuleTypeMapping)
        mapping.librenms_model = "WS-X4748-RJ45"
        module_type = MagicMock()
        module_type.__str__ = lambda s: "WS-X4748"
        _set_fk_cache(mapping, "netbox_module_type", module_type)
        mapping.description = ""

        result = mapping.to_yaml()
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert "librenms_model" in data

    def test_interface_type_mapping_has_to_yaml(self):
        """InterfaceTypeMapping.to_yaml() returns a YAML string."""
        import yaml

        from netbox_librenms_plugin.models import InterfaceTypeMapping

        mapping = InterfaceTypeMapping.__new__(InterfaceTypeMapping)
        mapping.librenms_type = "ether"
        mapping.librenms_speed = 1000000
        mapping.netbox_type = "1000base-t"
        mapping.description = ""

        result = mapping.to_yaml()
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert "librenms_type" in data

    def test_module_bay_mapping_has_to_yaml(self):
        """ModuleBayMapping.to_yaml() returns a YAML string."""
        import yaml

        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping.__new__(ModuleBayMapping)
        mapping.librenms_name = "Slot 1"
        mapping.librenms_class = "container"
        mapping.netbox_bay_name = "Slot 1"
        mapping.is_regex = False
        mapping.description = ""

        result = mapping.to_yaml()
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert "librenms_name" in data

    def test_normalization_rule_has_to_yaml(self):
        """NormalizationRule.to_yaml() returns a YAML string."""
        import yaml

        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.__new__(NormalizationRule)
        rule.scope = "hardware"
        _set_fk_cache(rule, "manufacturer", None)
        rule.match_pattern = r"^Cisco\s+"
        rule.replacement = "Cisco"
        rule.priority = 10
        rule.description = ""

        result = rule.to_yaml()
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert "scope" in data
        assert "match_pattern" in data

    def test_inventory_ignore_rule_has_to_yaml(self):
        """InventoryIgnoreRule.to_yaml() returns a YAML string."""
        import yaml

        from netbox_librenms_plugin.models import InventoryIgnoreRule

        rule = InventoryIgnoreRule.__new__(InventoryIgnoreRule)
        rule.name = "Skip IDPROM"
        rule.match_type = "ends_with"
        rule.pattern = "IDPROM"
        rule.action = "skip"
        rule.require_serial_match_parent = False
        rule.enabled = True
        rule.description = ""

        result = rule.to_yaml()
        assert isinstance(result, str)
        data = yaml.safe_load(result)
        assert "name" in data
        assert "match_type" in data


# =============================================================================
# TestFindMatchingPlatformWithMapping
# =============================================================================


class TestFindMatchingPlatformWithMapping:
    """find_matching_platform checks PlatformMapping before direct name match."""

    def test_platform_mapping_used_as_fallback_when_no_name_match(self):
        """When no Platform name matches, PlatformMapping is used as fallback."""
        from netbox_librenms_plugin.utils import find_matching_platform

        mock_mapped_platform = MagicMock(name="mapped_platform")
        mock_mapping = MagicMock()
        mock_mapping.netbox_platform = mock_mapped_platform

        mock_pm_class = MagicMock()
        mock_pm_class.objects.get.return_value = mock_mapping
        mock_pm_class.DoesNotExist = type("DoesNotExist", (Exception,), {})

        mock_platform_model = MagicMock()
        mock_platform_model.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_platform_model.objects.get.side_effect = mock_platform_model.DoesNotExist

        with (
            patch("netbox_librenms_plugin.models.PlatformMapping", mock_pm_class),
            patch("dcim.models.Platform", mock_platform_model),
        ):
            result = find_matching_platform("ios")

        assert result["found"] is True
        assert result["platform"] is mock_mapped_platform
        assert result["match_type"] == "mapping"

    def test_falls_back_to_name_match_when_no_platform_mapping(self):
        """When no PlatformMapping exists, falls back to exact Platform name match."""
        from netbox_librenms_plugin.utils import find_matching_platform

        mock_platform = MagicMock()

        mock_pm_class = MagicMock()
        mock_pm_class.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_pm_class.objects.get.side_effect = mock_pm_class.DoesNotExist

        mock_platform_model = MagicMock()
        mock_platform_model.objects.get.return_value = mock_platform

        with (
            patch("netbox_librenms_plugin.models.PlatformMapping", mock_pm_class),
            patch("dcim.models.Platform", mock_platform_model),
        ):
            result = find_matching_platform("ios")

        assert result["found"] is True
        assert result["platform"] is mock_platform
        assert result["match_type"] == "exact"

    def test_returns_not_found_when_neither_mapping_nor_platform(self):
        """Returns found=False when neither PlatformMapping nor Platform name match exists."""
        from netbox_librenms_plugin.utils import find_matching_platform

        DoesNotExist = type("DoesNotExist", (Exception,), {})

        mock_pm_class = MagicMock()
        mock_pm_class.DoesNotExist = DoesNotExist
        mock_pm_class.objects.get.side_effect = DoesNotExist

        mock_platform_model = MagicMock()
        mock_platform_model.DoesNotExist = DoesNotExist
        mock_platform_model.objects.get.side_effect = DoesNotExist

        with (
            patch("netbox_librenms_plugin.models.PlatformMapping", mock_pm_class),
            patch("dcim.models.Platform", mock_platform_model),
        ):
            result = find_matching_platform("unknown_os")

        assert result["found"] is False
        assert result["platform"] is None

    def test_multiple_platform_mappings_returns_ambiguous(self):
        """When exact name fails and PlatformMapping.MultipleObjectsReturned, returns ambiguous."""
        from netbox_librenms_plugin.utils import find_matching_platform

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        mock_pm_class = MagicMock()
        mock_pm_class.DoesNotExist = DoesNotExist
        mock_pm_class.MultipleObjectsReturned = MultipleObjectsReturned
        mock_pm_class.objects.get.side_effect = MultipleObjectsReturned

        mock_platform_model = MagicMock()
        mock_platform_model.DoesNotExist = DoesNotExist
        mock_platform_model.objects.get.side_effect = DoesNotExist

        with (
            patch("netbox_librenms_plugin.models.PlatformMapping", mock_pm_class),
            patch("dcim.models.Platform", mock_platform_model),
        ):
            result = find_matching_platform("ios")

        assert result == {"found": False, "platform": None, "match_type": "ambiguous", "ambiguity_source": "mapping"}


# =============================================================================
# TestBulkExportYAMLView
# =============================================================================


class TestBulkExportYAMLView:
    """BulkExportYAMLView returns YAML for selected PKs."""

    def _make_request(self, pk_list):
        request = MagicMock()
        request.POST = MagicMock()
        request.POST.getlist = MagicMock(return_value=pk_list)
        return request

    def test_returns_yaml_content_type(self):
        """Response has content-type text/yaml."""

        from netbox_librenms_plugin.views.mapping_views import DeviceTypeMappingBulkExportYAMLView

        view = DeviceTypeMappingBulkExportYAMLView.__new__(DeviceTypeMappingBulkExportYAMLView)
        request = self._make_request(["1", "2"])

        mock_mapping = MagicMock()
        mock_mapping.to_yaml.return_value = "librenms_hardware: Cisco 4321\n"

        mock_qs = MagicMock()
        mock_qs.filter.return_value.order_by.return_value = [mock_mapping, mock_mapping]
        view.queryset = mock_qs

        with patch.object(view, "require_object_permissions", return_value=None):
            response = view.post(request)

        assert "text/yaml" in response.get("Content-Type", "")

    def test_returns_yaml_for_selected_pks(self):
        """Response body contains YAML from selected objects."""
        from netbox_librenms_plugin.views.mapping_views import DeviceTypeMappingBulkExportYAMLView

        view = DeviceTypeMappingBulkExportYAMLView.__new__(DeviceTypeMappingBulkExportYAMLView)
        request = self._make_request(["1"])

        mock_mapping = MagicMock()
        mock_mapping.to_yaml.return_value = "librenms_hardware: Cisco 4321\n"

        mock_qs = MagicMock()
        mock_qs.filter.return_value.order_by.return_value = [mock_mapping]
        view.queryset = mock_qs

        with patch.object(view, "require_object_permissions", return_value=None):
            response = view.post(request)

        content = response.content.decode()
        assert "Cisco 4321" in content

    def test_filters_by_selected_pks(self):
        """View filters queryset by the selected PKs from POST data."""
        from netbox_librenms_plugin.views.mapping_views import DeviceTypeMappingBulkExportYAMLView

        view = DeviceTypeMappingBulkExportYAMLView.__new__(DeviceTypeMappingBulkExportYAMLView)
        request = self._make_request(["3", "7"])

        mock_qs = MagicMock()
        view.queryset = mock_qs

        with patch.object(view, "require_object_permissions", return_value=None):
            view.post(request)

        mock_qs.filter.assert_called_once_with(pk__in=[3, 7])
        mock_qs.filter.return_value.order_by.assert_called_once_with("pk")

    def test_returns_200_with_empty_selection(self):
        """Response is 200 even when no PKs are selected (empty YAML)."""
        from netbox_librenms_plugin.views.mapping_views import DeviceTypeMappingBulkExportYAMLView

        view = DeviceTypeMappingBulkExportYAMLView.__new__(DeviceTypeMappingBulkExportYAMLView)
        request = self._make_request([])

        mock_qs = MagicMock()
        mock_qs.filter.return_value.order_by.return_value = []
        view.queryset = mock_qs

        with patch.object(view, "require_object_permissions", return_value=None):
            response = view.post(request)

        assert response.status_code == 200

    def test_all_mapping_bulk_export_yaml_views_exist(self):
        """All mapping model BulkExportYAML views exist."""
        from netbox_librenms_plugin.views.mapping_views import (
            DeviceTypeMappingBulkExportYAMLView,
            InterfaceTypeMappingBulkExportYAMLView,
            InventoryIgnoreRuleBulkExportYAMLView,
            ModuleBayMappingBulkExportYAMLView,
            ModuleTypeMappingBulkExportYAMLView,
            NormalizationRuleBulkExportYAMLView,
            PlatformMappingBulkExportYAMLView,
        )

        for cls in [
            DeviceTypeMappingBulkExportYAMLView,
            InterfaceTypeMappingBulkExportYAMLView,
            InventoryIgnoreRuleBulkExportYAMLView,
            ModuleBayMappingBulkExportYAMLView,
            ModuleTypeMappingBulkExportYAMLView,
            NormalizationRuleBulkExportYAMLView,
            PlatformMappingBulkExportYAMLView,
        ]:
            assert cls is not None


# =============================================================================
# TestPlatformMappingViewsExist
# =============================================================================


class TestPlatformMappingViewsExist:
    """All PlatformMapping CRUD views must exist in mapping_views."""

    def test_list_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingListView

        assert PlatformMappingListView is not None

    def test_create_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingCreateView

        assert PlatformMappingCreateView is not None

    def test_edit_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingEditView

        assert PlatformMappingEditView is not None

    def test_delete_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingDeleteView

        assert PlatformMappingDeleteView is not None

    def test_bulk_delete_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingBulkDeleteView

        assert PlatformMappingBulkDeleteView is not None

    def test_bulk_import_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingBulkImportView

        assert PlatformMappingBulkImportView is not None

    def test_detail_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingView

        assert PlatformMappingView is not None

    def test_changelog_view_exists(self):
        from netbox_librenms_plugin.views.mapping_views import PlatformMappingChangeLogView

        assert PlatformMappingChangeLogView is not None


# =============================================================================
# TestPlatformMappingFormsExist
# =============================================================================


class TestPlatformMappingFormsExist:
    """PlatformMapping form classes must exist."""

    def test_form_exists(self):
        from netbox_librenms_plugin.forms import PlatformMappingForm

        assert PlatformMappingForm is not None

    def test_filter_form_exists(self):
        from netbox_librenms_plugin.forms import PlatformMappingFilterForm

        assert PlatformMappingFilterForm is not None

    def test_import_form_exists(self):
        from netbox_librenms_plugin.forms import PlatformMappingImportForm

        assert PlatformMappingImportForm is not None

    def test_filter_form_has_librenms_os_field(self):
        """PlatformMappingFilterForm has librenms_os as a filter field."""
        from netbox_librenms_plugin.forms import PlatformMappingFilterForm

        assert "librenms_os" in PlatformMappingFilterForm.base_fields

    def test_import_form_fields_cover_required_columns(self):
        """PlatformMappingImportForm covers librenms_os and netbox_platform."""
        from netbox_librenms_plugin.forms import PlatformMappingImportForm

        assert "librenms_os" in PlatformMappingImportForm._meta.fields
        assert "netbox_platform" in PlatformMappingImportForm._meta.fields


# =============================================================================
# TestReplacementTemplateValidation (Issue #64)
# =============================================================================


class TestReplacementTemplateValidation:
    """Replacement template is validated against a guaranteed-match synthetic pattern."""

    # --- ModuleBayMapping ---

    def test_module_bay_mapping_rejects_out_of_range_backref(self):
        """Referencing group \\2 when pattern has only one group should raise ValidationError."""
        import pytest
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping.__new__(ModuleBayMapping)
        mapping.librenms_name = r"^(\d+)$"
        mapping.librenms_class = ""
        mapping.netbox_bay_name = r"Slot \2"  # only 1 group
        mapping.is_regex = True
        mapping.description = ""

        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                mapping.clean()
        assert "netbox_bay_name" in exc_info.value.message_dict

    def test_module_bay_mapping_accepts_valid_backref(self):
        """Valid \\1 back-reference on a single-group pattern is accepted."""
        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping.__new__(ModuleBayMapping)
        mapping.librenms_name = r"^(\d+)$"
        mapping.librenms_class = ""
        mapping.netbox_bay_name = r"Slot \1"
        mapping.is_regex = True
        mapping.description = ""

        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()  # Should not raise

    def test_module_bay_mapping_accepts_named_backref(self):
        """Valid \\g<n> back-reference on a named-group pattern is accepted."""
        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping.__new__(ModuleBayMapping)
        mapping.librenms_name = r"^(?P<n>\d+)$"
        mapping.librenms_class = ""
        mapping.netbox_bay_name = r"Slot \g<n>"
        mapping.is_regex = True
        mapping.description = ""

        with patch("netbox.models.NetBoxModel.clean"):
            mapping.clean()  # Should not raise

    def test_module_bay_mapping_rejects_invalid_named_backref(self):
        """Referencing a named group that does not exist raises ValidationError."""
        import pytest
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping.__new__(ModuleBayMapping)
        mapping.librenms_name = r"^(\d+)$"
        mapping.librenms_class = ""
        mapping.netbox_bay_name = r"Slot \g<missing>"
        mapping.is_regex = True
        mapping.description = ""

        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                mapping.clean()
        assert "netbox_bay_name" in exc_info.value.message_dict

    # --- NormalizationRule ---

    def test_normalization_rule_rejects_out_of_range_backref(self):
        """Replacement referencing group \\2 when pattern has only 1 group raises ValidationError."""
        import pytest
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.__new__(NormalizationRule)
        rule.match_pattern = r"^(\w+)$"
        rule.replacement = r"\2"  # only 1 group
        rule.scope = "hardware"
        rule.description = ""
        _set_fk_cache(rule, "manufacturer", None)

        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                rule.clean()
        assert "replacement" in exc_info.value.message_dict

    def test_normalization_rule_accepts_valid_backref(self):
        """Replacement \\1 on single-group pattern is accepted."""
        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.__new__(NormalizationRule)
        rule.match_pattern = r"^(\w+)$"
        rule.replacement = r"\1"
        rule.scope = "hardware"
        rule.description = ""
        _set_fk_cache(rule, "manufacturer", None)

        with patch("netbox.models.NetBoxModel.clean"):
            rule.clean()  # Should not raise

    def test_normalization_rule_accepts_named_backref(self):
        """Replacement \\g<name> on named-group pattern is accepted."""
        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.__new__(NormalizationRule)
        rule.match_pattern = r"^(?P<hw>\w+)$"
        rule.replacement = r"\g<hw>"
        rule.scope = "hardware"
        rule.description = ""
        _set_fk_cache(rule, "manufacturer", None)

        with patch("netbox.models.NetBoxModel.clean"):
            rule.clean()  # Should not raise

    def test_normalization_rule_empty_replacement_no_backref(self):
        """Empty replacement string is valid for a pattern with capture groups."""
        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.__new__(NormalizationRule)
        rule.match_pattern = r"^(\d+)$"
        rule.replacement = ""
        rule.scope = "hardware"
        rule.description = ""
        _set_fk_cache(rule, "manufacturer", None)

        with patch("netbox.models.NetBoxModel.clean"):
            rule.clean()  # Should not raise

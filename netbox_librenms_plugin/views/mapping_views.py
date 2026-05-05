from django.http import HttpResponse, HttpResponseBadRequest
from django.views import View
from netbox.views import generic
from utilities.views import register_model_view

from netbox_librenms_plugin.filters import (
    DeviceTypeMappingFilterSet,
    InterfaceTypeMappingFilterSet,
    InventoryIgnoreRuleFilterSet,
    ModuleBayMappingFilterSet,
    ModuleTypeMappingFilterSet,
    NormalizationRuleFilterSet,
    PlatformMappingFilterSet,
)
from netbox_librenms_plugin.forms import (
    DeviceTypeMappingFilterForm,
    DeviceTypeMappingForm,
    DeviceTypeMappingImportForm,
    InterfaceTypeMappingFilterForm,
    InterfaceTypeMappingForm,
    InterfaceTypeMappingImportForm,
    InventoryIgnoreRuleFilterForm,
    InventoryIgnoreRuleForm,
    InventoryIgnoreRuleImportForm,
    ModuleBayMappingFilterForm,
    ModuleBayMappingForm,
    ModuleBayMappingImportForm,
    ModuleTypeMappingFilterForm,
    ModuleTypeMappingForm,
    ModuleTypeMappingImportForm,
    NormalizationRuleFilterForm,
    NormalizationRuleForm,
    NormalizationRuleImportForm,
    PlatformMappingFilterForm,
    PlatformMappingForm,
    PlatformMappingImportForm,
)
from netbox_librenms_plugin.models import (
    DeviceTypeMapping,
    InterfaceTypeMapping,
    InventoryIgnoreRule,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
    PlatformMapping,
)
from netbox_librenms_plugin.tables.mappings import (
    DeviceTypeMappingTable,
    InterfaceTypeMappingTable,
    InventoryIgnoreRuleTable,
    ModuleBayMappingTable,
    ModuleTypeMappingTable,
    NormalizationRuleTable,
    PlatformMappingTable,
)
from netbox_librenms_plugin.views.mixins import (
    LibreNMSPermissionMixin,
    LibreNMSWritePermissionMixin,
    NetBoxObjectPermissionMixin,
)


class InterfaceTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """
    Provides a view for listing all `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable
    filterset = InterfaceTypeMappingFilterSet
    filterset_form = InterfaceTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/interfacetypemapping_list.html"


class InterfaceTypeMappingCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """
    Provides a view for creating a new `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


@register_model_view(InterfaceTypeMapping, "bulk_import", path="import", detail=False)
class InterfaceTypeMappingBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """
    Provides a view for bulk importing `InterfaceTypeMapping` objects from CSV, JSON, or YAML.
    Supports three import methods: direct import, file upload, and data file.
    """

    queryset = InterfaceTypeMapping.objects.all()
    model_form = InterfaceTypeMappingImportForm


class InterfaceTypeMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """
    Provides a view for displaying details of a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """
    Provides a view for editing a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """
    Provides a view for deleting a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """
    Provides a view for deleting multiple `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable


class InterfaceTypeMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """
    Provides a view for displaying the change log of a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


# --- DeviceTypeMapping views ---


class DeviceTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all DeviceTypeMapping objects."""

    queryset = DeviceTypeMapping.objects.select_related("netbox_device_type")
    table = DeviceTypeMappingTable
    filterset = DeviceTypeMappingFilterSet
    filterset_form = DeviceTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/devicetypemapping_list.html"


class DeviceTypeMappingCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm


@register_model_view(DeviceTypeMapping, "bulk_import", path="import", detail=False)
class DeviceTypeMappingBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing DeviceTypeMapping objects."""

    queryset = DeviceTypeMapping.objects.all()
    model_form = DeviceTypeMappingImportForm


class DeviceTypeMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()


class DeviceTypeMappingEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm


class DeviceTypeMappingDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()


class DeviceTypeMappingBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple DeviceTypeMapping objects."""

    queryset = DeviceTypeMapping.objects.all()
    table = DeviceTypeMappingTable


class DeviceTypeMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific DeviceTypeMapping object."""

    queryset = DeviceTypeMapping.objects.all()


# --- ModuleTypeMapping views ---


class ModuleTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all ModuleTypeMapping objects."""

    queryset = ModuleTypeMapping.objects.select_related("netbox_module_type")
    table = ModuleTypeMappingTable
    filterset = ModuleTypeMappingFilterSet
    filterset_form = ModuleTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/moduletypemapping_list.html"


class ModuleTypeMappingCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()
    form = ModuleTypeMappingForm


@register_model_view(ModuleTypeMapping, "bulk_import", path="import", detail=False)
class ModuleTypeMappingBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing ModuleTypeMapping objects."""

    queryset = ModuleTypeMapping.objects.all()
    model_form = ModuleTypeMappingImportForm


class ModuleTypeMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()


class ModuleTypeMappingEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()
    form = ModuleTypeMappingForm


class ModuleTypeMappingDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()


class ModuleTypeMappingBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple ModuleTypeMapping objects."""

    queryset = ModuleTypeMapping.objects.all()
    table = ModuleTypeMappingTable


class ModuleTypeMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific ModuleTypeMapping object."""

    queryset = ModuleTypeMapping.objects.all()


# --- ModuleBayMapping views ---


class ModuleBayMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all ModuleBayMapping objects."""

    queryset = ModuleBayMapping.objects.all()
    table = ModuleBayMappingTable
    filterset = ModuleBayMappingFilterSet
    filterset_form = ModuleBayMappingFilterForm
    template_name = "netbox_librenms_plugin/modulebaymapping_list.html"


class ModuleBayMappingCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()
    form = ModuleBayMappingForm


@register_model_view(ModuleBayMapping, "bulk_import", path="import", detail=False)
class ModuleBayMappingBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing ModuleBayMapping objects."""

    queryset = ModuleBayMapping.objects.all()
    model_form = ModuleBayMappingImportForm


class ModuleBayMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()


class ModuleBayMappingEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()
    form = ModuleBayMappingForm


class ModuleBayMappingDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()


class ModuleBayMappingBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple ModuleBayMapping objects."""

    queryset = ModuleBayMapping.objects.all()
    table = ModuleBayMappingTable


class ModuleBayMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific ModuleBayMapping object."""

    queryset = ModuleBayMapping.objects.all()


# --- NormalizationRule views ---


class NormalizationRuleListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all NormalizationRule objects."""

    queryset = NormalizationRule.objects.select_related("manufacturer")
    table = NormalizationRuleTable
    filterset = NormalizationRuleFilterSet
    filterset_form = NormalizationRuleFilterForm
    template_name = "netbox_librenms_plugin/normalizationrule_list.html"


class NormalizationRuleCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new NormalizationRule object."""

    queryset = NormalizationRule.objects.all()
    form = NormalizationRuleForm


@register_model_view(NormalizationRule, "bulk_import", path="import", detail=False)
class NormalizationRuleBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing NormalizationRule objects."""

    queryset = NormalizationRule.objects.all()
    model_form = NormalizationRuleImportForm


class NormalizationRuleView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific NormalizationRule object."""

    queryset = NormalizationRule.objects.all()


class NormalizationRuleEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific NormalizationRule object."""

    queryset = NormalizationRule.objects.all()
    form = NormalizationRuleForm


class NormalizationRuleDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific NormalizationRule object."""

    queryset = NormalizationRule.objects.all()


class NormalizationRuleBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple NormalizationRule objects."""

    queryset = NormalizationRule.objects.all()
    table = NormalizationRuleTable


class NormalizationRuleChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific NormalizationRule object."""

    queryset = NormalizationRule.objects.all()


# --- InventoryIgnoreRule views ---


class InventoryIgnoreRuleListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all InventoryIgnoreRule objects."""

    queryset = InventoryIgnoreRule.objects.all()
    table = InventoryIgnoreRuleTable
    filterset = InventoryIgnoreRuleFilterSet
    filterset_form = InventoryIgnoreRuleFilterForm
    template_name = "netbox_librenms_plugin/inventoryignorerule_list.html"


class InventoryIgnoreRuleCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new InventoryIgnoreRule object."""

    queryset = InventoryIgnoreRule.objects.all()
    form = InventoryIgnoreRuleForm


@register_model_view(InventoryIgnoreRule, "bulk_import", path="import", detail=False)
class InventoryIgnoreRuleBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing InventoryIgnoreRule objects."""

    queryset = InventoryIgnoreRule.objects.all()
    model_form = InventoryIgnoreRuleImportForm


class InventoryIgnoreRuleView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific InventoryIgnoreRule object."""

    queryset = InventoryIgnoreRule.objects.all()


class InventoryIgnoreRuleEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific InventoryIgnoreRule object."""

    queryset = InventoryIgnoreRule.objects.all()
    form = InventoryIgnoreRuleForm


class InventoryIgnoreRuleDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific InventoryIgnoreRule object."""

    queryset = InventoryIgnoreRule.objects.all()


class InventoryIgnoreRuleBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple InventoryIgnoreRule objects."""

    queryset = InventoryIgnoreRule.objects.all()
    table = InventoryIgnoreRuleTable


class InventoryIgnoreRuleChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific InventoryIgnoreRule object."""

    queryset = InventoryIgnoreRule.objects.all()


# --- BulkExportYAML views ---


class BulkExportYAMLView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Base view that exports selected mapping objects as YAML."""

    queryset = None

    @property
    def required_object_permissions(self):
        return {"POST": [("view", self.queryset.model)]}

    def post(self, request):
        if error := self.require_object_permissions("POST"):
            return error
        pks = request.POST.getlist("pk")
        try:
            int_pks = [int(pk) for pk in pks]
        except (ValueError, TypeError):
            return HttpResponseBadRequest("Invalid pk value.")
        objects = self.queryset.filter(pk__in=int_pks).order_by("pk")
        yaml_parts = [obj.to_yaml() for obj in objects]
        content = "---\n".join(yaml_parts)
        response = HttpResponse(content, content_type="text/yaml; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="export.yaml"'
        return response


class InterfaceTypeMappingBulkExportYAMLView(BulkExportYAMLView):
    queryset = InterfaceTypeMapping.objects.all()


class DeviceTypeMappingBulkExportYAMLView(BulkExportYAMLView):
    queryset = DeviceTypeMapping.objects.select_related("netbox_device_type")


class ModuleTypeMappingBulkExportYAMLView(BulkExportYAMLView):
    queryset = ModuleTypeMapping.objects.select_related("netbox_module_type")


class ModuleBayMappingBulkExportYAMLView(BulkExportYAMLView):
    queryset = ModuleBayMapping.objects.all()


class NormalizationRuleBulkExportYAMLView(BulkExportYAMLView):
    queryset = NormalizationRule.objects.select_related("manufacturer")


class InventoryIgnoreRuleBulkExportYAMLView(BulkExportYAMLView):
    queryset = InventoryIgnoreRule.objects.all()


class PlatformMappingBulkExportYAMLView(BulkExportYAMLView):
    queryset = PlatformMapping.objects.select_related("netbox_platform")


# --- PlatformMapping views ---


class PlatformMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """Provides a view for listing all PlatformMapping objects."""

    queryset = PlatformMapping.objects.select_related("netbox_platform")
    table = PlatformMappingTable
    filterset = PlatformMappingFilterSet
    filterset_form = PlatformMappingFilterForm
    template_name = "netbox_librenms_plugin/platformmapping_list.html"


class PlatformMappingCreateView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for creating a new PlatformMapping object."""

    queryset = PlatformMapping.objects.all()
    form = PlatformMappingForm


@register_model_view(PlatformMapping, "bulk_import", path="import", detail=False)
class PlatformMappingBulkImportView(LibreNMSWritePermissionMixin, generic.BulkImportView):
    """Provides a view for bulk importing PlatformMapping objects."""

    queryset = PlatformMapping.objects.all()
    model_form = PlatformMappingImportForm


class PlatformMappingView(LibreNMSPermissionMixin, generic.ObjectView):
    """Provides a view for displaying details of a specific PlatformMapping object."""

    queryset = PlatformMapping.objects.all()


class PlatformMappingEditView(LibreNMSWritePermissionMixin, generic.ObjectEditView):
    """Provides a view for editing a specific PlatformMapping object."""

    queryset = PlatformMapping.objects.all()
    form = PlatformMappingForm


class PlatformMappingDeleteView(LibreNMSWritePermissionMixin, generic.ObjectDeleteView):
    """Provides a view for deleting a specific PlatformMapping object."""

    queryset = PlatformMapping.objects.all()


class PlatformMappingBulkDeleteView(LibreNMSWritePermissionMixin, generic.BulkDeleteView):
    """Provides a view for deleting multiple PlatformMapping objects."""

    queryset = PlatformMapping.objects.all()
    table = PlatformMappingTable


class PlatformMappingChangeLogView(LibreNMSPermissionMixin, generic.ObjectChangeLogView):
    """Provides a view for displaying the change log of a specific PlatformMapping object."""

    queryset = PlatformMapping.objects.all()

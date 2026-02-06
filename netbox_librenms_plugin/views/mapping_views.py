from netbox.views import generic
from utilities.views import register_model_view

from netbox_librenms_plugin.filters import InterfaceTypeMappingFilterSet
from netbox_librenms_plugin.forms import (
    InterfaceTypeMappingFilterForm,
    InterfaceTypeMappingForm,
    InterfaceTypeMappingImportForm,
)
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tables.mappings import InterfaceTypeMappingTable
from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin


class InterfaceTypeMappingListView(LibreNMSPermissionMixin, generic.ObjectListView):
    """
    Provides a view for listing all `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable
    filterset = InterfaceTypeMappingFilterSet
    filterset_form = InterfaceTypeMappingFilterForm
    template_name = "netbox_librenms_plugin/interfacetypemapping_list.html"


class InterfaceTypeMappingCreateView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """
    Provides a view for creating a new `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


@register_model_view(InterfaceTypeMapping, "bulk_import", path="import", detail=False)
class InterfaceTypeMappingBulkImportView(LibreNMSPermissionMixin, generic.BulkImportView):
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


class InterfaceTypeMappingEditView(LibreNMSPermissionMixin, generic.ObjectEditView):
    """
    Provides a view for editing a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingDeleteView(LibreNMSPermissionMixin, generic.ObjectDeleteView):
    """
    Provides a view for deleting a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingBulkDeleteView(LibreNMSPermissionMixin, generic.BulkDeleteView):
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

from netbox.views import generic

from netbox_librenms_plugin.filters import InterfaceTypeMappingFilterSet
from netbox_librenms_plugin.forms import InterfaceTypeMappingFilterForm, InterfaceTypeMappingForm
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tables.mappings import InterfaceTypeMappingTable


class InterfaceTypeMappingListView(generic.ObjectListView):
    """
    Provides a view for listing all `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable
    filterset = InterfaceTypeMappingFilterSet
    filterset_form = InterfaceTypeMappingFilterForm
    template_name = 'netbox_librenms_plugin/interfacetypemapping_list.html'


class InterfaceTypeMappingCreateView(generic.ObjectEditView):
    """
    Provides a view for creating a new `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingView(generic.ObjectView):
    """
    Provides a view for displaying details of a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingEditView(generic.ObjectEditView):
    """
    Provides a view for editing a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()
    form = InterfaceTypeMappingForm


class InterfaceTypeMappingDeleteView(generic.ObjectDeleteView):
    """
    Provides a view for deleting a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()


class InterfaceTypeMappingBulkDeleteView(generic.BulkDeleteView):
    """
    Provides a view for deleting multiple `InterfaceTypeMapping` objects.
    """

    queryset = InterfaceTypeMapping.objects.all()
    table = InterfaceTypeMappingTable


class InterfaceTypeMappingChangeLogView(generic.ObjectChangeLogView):
    """
    Provides a view for displaying the change log of a specific `InterfaceTypeMapping` object.
    """

    queryset = InterfaceTypeMapping.objects.all()

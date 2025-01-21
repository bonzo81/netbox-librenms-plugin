from dcim.models import Device
from django import forms
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet
from virtualization.models import VirtualMachine


class SiteLocationFilterSet:
    """
    Filter sites and locations by search term.
    """

    def __init__(self, data, queryset):
        self.form_data = data
        self.queryset = queryset

    @property
    def qs(self):
        queryset = self.queryset
        if q := self.form_data.get("q"):
            return self._filter_queryset(q)
        return queryset

    def _filter_queryset(self, search_term):
        search_term = str(search_term).lower()
        return [
            item
            for item in self.queryset
            if self._matches_search_criteria(item, search_term)
        ]

    def _matches_search_criteria(self, item, search_term):
        searchable_fields = [
            str(item.netbox_site.name),
            str(item.netbox_site.latitude),
            str(item.netbox_site.longitude),
            str(item.librenms_location) if item.librenms_location else "",
        ]
        return any(search_term in field.lower() for field in searchable_fields)

    @property
    def form(self):
        class FilterForm(forms.Form):
            q = forms.CharField(
                required=False,
                label="Search sites and locations",
                widget=forms.TextInput(
                    attrs={
                        "placeholder": "Search by site name, coordinates or location"
                    }
                ),
            )

        return FilterForm(self.form_data)


class DeviceStatusFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = Device
        fields = ["site", "location", "device_type", "rack", "role"]
        search_fields = ["name", "site", "device_type", "rack", "role"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(site__name__icontains=value)
            | Q(device_type__model__icontains=value)
            | Q(rack__name__icontains=value)
            | Q(role__name__icontains=value)
        )


class VMStatusFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = VirtualMachine
        fields = ["site", "cluster", "role", "platform"]
        search_fields = ["name", "site", "cluster", "role", "platform"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(site__name__icontains=value)
            | Q(cluster__name__icontains=value)
            | Q(role__name__icontains=value)
            | Q(platform__name__icontains=value)
        )

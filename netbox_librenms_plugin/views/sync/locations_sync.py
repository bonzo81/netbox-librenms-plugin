from collections import namedtuple

from dcim.models import Site
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import redirect
from django_tables2 import SingleTableView

from netbox_librenms_plugin.filtersets import SiteLocationFilterSet
from netbox_librenms_plugin.tables.locations import SiteLocationSyncTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class SyncSiteLocationView(LibreNMSAPIMixin, SingleTableView):
    """
    Provides a view for synchronizing Netbox site with LibreNMS locations.
    """

    table_class = SiteLocationSyncTable
    template_name = "netbox_librenms_plugin/site_location_sync.html"
    filterset = SiteLocationFilterSet

    COORDINATE_TOLERANCE = 0.0001
    SyncData = namedtuple("SyncData", ["netbox_site", "librenms_location", "is_synced"])

    def get_table(self, *args, **kwargs):
        table = super().get_table(*args, **kwargs)
        table.configure(self.request)
        return table

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()
        context["filter_form"] = self.filterset(
            self.request.GET, queryset=queryset
        ).form
        return context

    def get_queryset(self):
        netbox_sites = Site.objects.all()
        success, librenms_locations = self.get_librenms_locations()
        if not success or not isinstance(librenms_locations, list):
            return []

        sync_data = [
            self.create_sync_data(site, librenms_locations) for site in netbox_sites
        ]
        # Initialize the filterset correctly
        if self.request.GET and self.filterset:
            return self.filterset(self.request.GET, queryset=sync_data).qs

        # Handle quicksearch
        if "q" in self.request.GET:
            q = self.request.GET.get("q", "").lower()
            sync_data = [
                item for item in sync_data if q in item.netbox_site.name.lower()
            ]

        return sync_data

    def get_librenms_locations(self):
        """
        Retrieve locations from LibreNMS.
        """
        return self.librenms_api.get_locations()

    def create_sync_data(self, site, librenms_locations):
        """
        Create a SyncData object for a given site.
        """
        matched_location = self.match_site_with_location(site, librenms_locations)
        if matched_location:
            is_synced = self.check_coordinates_match(
                site.latitude,
                site.longitude,
                matched_location.get("lat"),
                matched_location.get("lng"),
            )
            return self.SyncData(site, matched_location, is_synced)
        else:
            return self.SyncData(site, None, False)

    def match_site_with_location(self, site, librenms_locations):
        """
        Match a NetBox site with a LibreNMS location.
        """
        for location in librenms_locations:
            if location["location"].lower() == site.name.lower():
                return location
        return None

    def check_coordinates_match(self, site_lat, site_lng, librenms_lat, librenms_lng):
        """
        Check if the coordinates of the site and LibreNMS location match.
        """
        if None in (site_lat, site_lng, librenms_lat, librenms_lng):
            return False
        lat_match = (
            abs(float(site_lat) - float(librenms_lat)) < self.COORDINATE_TOLERANCE
        )
        lng_match = (
            abs(float(site_lng) - float(librenms_lng)) < self.COORDINATE_TOLERANCE
        )
        return lat_match and lng_match

    def post(self, request):
        """
        Handle the POST request for synchronizing Netbox site with LibreNMS locations.
        """
        action = request.POST.get("action")
        pk = request.POST.get("pk")
        if not pk:
            messages.error(request, "No site ID provided.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        site = self.get_site_by_pk(pk)
        if not site:
            messages.error(request, "Site not found.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        if action == "update":
            return self.update_librenms_location(request, site)
        elif action == "create":
            return self.create_librenms_location(request, site)
        else:
            messages.error(request, f"Unknown action '{action}'.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def get_site_by_pk(self, pk):
        """
        Retrieve a Site object by its primary key.
        """
        try:
            return Site.objects.get(pk=pk)
        except ObjectDoesNotExist:
            return None

    def create_librenms_location(self, request, site):
        """
        Create a new location in LibreNMS based on the site's coordinates.
        """
        location_data = self.build_location_data(site)
        success, message = self.librenms_api.add_location(location_data)
        if success:
            messages.success(
                request, f"Location '{site.name}' created in LibreNMS successfully."
            )
        else:
            messages.error(
                request,
                f"Failed to create location '{site.name}' in LibreNMS: {message}",
            )
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def update_librenms_location(self, request, site):
        """
        Update LibreNMS api with the site's updated coordinates.
        """
        if site.latitude is None or site.longitude is None:
            messages.warning(
                request,
                f"Latitude and/or longitude is missing. Cannot update location '{site.name}' in LibreNMS.",
            )
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        location_data = self.build_location_data(site, include_name=False)
        success, message = self.librenms_api.update_location(site.name, location_data)
        if success:
            messages.success(
                request, f"Location '{site.name}' updated in LibreNMS successfully."
            )
        else:
            messages.error(
                request,
                f"Failed to update location '{site.name}' in LibreNMS: {message}",
            )
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def build_location_data(self, site, include_name=True):
        """
        Build the location data for a given site object.
        """
        data = {"lat": str(site.latitude), "lng": str(site.longitude)}
        if include_name:
            data["location"] = site.name
        return data

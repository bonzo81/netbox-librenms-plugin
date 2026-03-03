from collections import namedtuple

from dcim.models import Site
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import redirect
from django_tables2 import SingleTableView

from netbox_librenms_plugin.filtersets import SiteLocationFilterSet
from netbox_librenms_plugin.tables.locations import SiteLocationSyncTable
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin


class SyncSiteLocationView(LibreNMSPermissionMixin, LibreNMSAPIMixin, SingleTableView):
    """Synchronize NetBox Sites with LibreNMS locations."""

    table_class = SiteLocationSyncTable
    template_name = "netbox_librenms_plugin/site_location_sync.html"
    filterset = SiteLocationFilterSet

    COORDINATE_TOLERANCE = 0.0001
    SyncData = namedtuple("SyncData", ["netbox_site", "librenms_location", "is_synced"])

    def get_table(self, *args, **kwargs):
        """Return the configured sync table."""
        table = super().get_table(*args, **kwargs)
        table.configure(self.request)
        return table

    def get_context_data(self, **kwargs):
        """Return context with filter form for site-location sync."""
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()
        context["filter_form"] = self.filterset(self.request.GET, queryset=queryset).form
        return context

    def get_queryset(self):
        """Return sync data pairing NetBox sites with LibreNMS locations."""
        netbox_sites = Site.objects.all()
        success, librenms_locations = self.get_librenms_locations()
        if not success or not isinstance(librenms_locations, list):
            return []

        sync_data = [self.create_sync_data(site, librenms_locations) for site in netbox_sites]

        if self.request.GET and self.filterset:
            return self.filterset(self.request.GET, queryset=sync_data).qs

        return sync_data

    def get_librenms_locations(self):
        """Fetch all locations from LibreNMS."""
        return self.librenms_api.get_locations()

    def create_sync_data(self, site, librenms_locations):
        """Create a SyncData tuple pairing a site with its LibreNMS location."""
        matched_location = self.match_site_with_location(site, librenms_locations)
        if matched_location:
            is_synced = self.check_coordinates_match(
                site.latitude,
                site.longitude,
                matched_location.get("lat"),
                matched_location.get("lng"),
            )
            return self.SyncData(site, matched_location, is_synced)
        return self.SyncData(site, None, False)

    def match_site_with_location(self, site, librenms_locations):
        """Return the LibreNMS location matching the given site, or None."""
        for location in librenms_locations:
            if location["location"].lower() == site.name.lower() or location["location"].lower() == site.slug.lower():
                return location
        return None

    def check_coordinates_match(self, site_lat, site_lng, librenms_lat, librenms_lng):
        """Return True if site and LibreNMS coordinates match within tolerance."""
        if None in (site_lat, site_lng, librenms_lat, librenms_lng):
            return False
        lat_match = abs(float(site_lat) - float(librenms_lat)) < self.COORDINATE_TOLERANCE
        lng_match = abs(float(site_lng) - float(librenms_lng)) < self.COORDINATE_TOLERANCE
        return lat_match and lng_match

    def post(self, request):
        """Handle create or update of a LibreNMS location from a NetBox site."""
        # Check write permission before modifying LibreNMS locations
        if error := self.require_write_permission():
            return error

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
        if action == "create":
            return self.create_librenms_location(request, site)

        messages.error(request, f"Unknown action '{action}'.")
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def get_site_by_pk(self, pk):
        """Return the Site for the given pk, or None if not found."""
        try:
            return Site.objects.get(pk=pk)
        except ObjectDoesNotExist:
            return None

    def create_librenms_location(self, request, site):
        """Create a new location in LibreNMS from the given site."""
        if site.latitude is None or site.longitude is None:
            messages.warning(
                request,
                f"Latitude and/or longitude is missing. Cannot create location '{site.name}' in LibreNMS.",
            )
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        location_data = self.build_location_data(site)
        success, message = self.librenms_api.add_location(location_data)
        if success:
            messages.success(request, f"Location '{site.name}' created in LibreNMS successfully.")
        else:
            messages.error(
                request,
                f"Failed to create location '{site.name}' in LibreNMS: {message}",
            )
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def update_librenms_location(self, request, site):
        """Update an existing LibreNMS location with the site coordinates."""
        if site.latitude is None or site.longitude is None:
            messages.warning(
                request,
                f"Latitude and/or longitude is missing. Cannot update location '{site.name}' in LibreNMS.",
            )
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        success, librenms_locations = self.get_librenms_locations()
        if not success:
            messages.error(request, "Failed to retrieve LibreNMS locations.")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        matched_location = self.match_site_with_location(site, librenms_locations)
        if not matched_location:
            messages.error(request, f"Could not find matching location for site '{site.name}'")
            return redirect("plugins:netbox_librenms_plugin:site_location_sync")

        location_data = self.build_location_data(site, include_name=False)
        success, message = self.librenms_api.update_location(matched_location["location"], location_data)
        if success:
            messages.success(request, f"Location '{site.name}' updated in LibreNMS successfully.")
        else:
            messages.error(
                request,
                f"Failed to update location '{site.name}' in LibreNMS: {message}",
            )
        return redirect("plugins:netbox_librenms_plugin:site_location_sync")

    def build_location_data(self, site, include_name=True):
        """Build a location data dict from the given site."""
        data = {"lat": str(site.latitude), "lng": str(site.longitude)}
        if include_name:
            data["location"] = site.name
        return data

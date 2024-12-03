from django.views import View

from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin


class BaseIPAddressTableView(LibreNMSAPIMixin, View):
    """
    Base view for synchronizing IP address information from LibreNMS.
    """

    template_name = "netbox_librenms_plugin/_ipaddress_sync.html"

    def get_context_data(self, request, device):
        """
        Get context data for IP address sync view.
        """
        context = {
            "device": device,
            "ip_sync_message": "IP address sync coming soon",
        }
        return context

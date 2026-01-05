from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from netbox_librenms_plugin.forms import ImportSettingsForm, ServerConfigForm
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.models import LibreNMSSettings


class LibreNMSSettingsView(PermissionRequiredMixin, View):
    """
    View for managing plugin settings including server selection and import options.
    Uses two separate forms for cleaner validation and separation of concerns.
    """

    permission_required = "netbox_librenms_plugin.change_librenmssettings"
    template_name = "netbox_librenms_plugin/settings.html"

    def get(self, request):
        """Display both settings forms."""
        # Get or create the settings object
        settings, created = LibreNMSSettings.objects.get_or_create()

        # Instantiate both forms
        server_form = ServerConfigForm(instance=settings)
        import_form = ImportSettingsForm(instance=settings)

        return render(
            request,
            self.template_name,
            {
                "server_form": server_form,
                "import_form": import_form,
                "object": settings,
            },
        )

    def post(self, request):
        """Handle form submission - process the appropriate form based on form_type."""
        # Get or create the settings object
        settings, created = LibreNMSSettings.objects.get_or_create()

        # Determine which form was submitted
        form_type = request.POST.get("form_type")

        if form_type == "server_config":
            # Process server configuration form
            server_form = ServerConfigForm(request.POST, instance=settings)
            import_form = ImportSettingsForm(
                instance=settings
            )  # Unbound form for display

            if server_form.is_valid():
                server_form.save()
                messages.success(
                    request,
                    f"LibreNMS server settings updated successfully. Active server: {server_form.cleaned_data['selected_server']}",
                )
                return redirect("plugins:netbox_librenms_plugin:settings")

        elif form_type == "import_settings":
            # Process import settings form
            server_form = ServerConfigForm(
                instance=settings
            )  # Unbound form for display
            import_form = ImportSettingsForm(request.POST, instance=settings)

            if import_form.is_valid():
                import_form.save()
                messages.success(
                    request,
                    "Import settings updated successfully.",
                )
                return redirect("plugins:netbox_librenms_plugin:settings")

        else:
            # Unknown form_type - shouldn't happen, but handle gracefully
            messages.error(request, "Invalid form submission.")
            return redirect("plugins:netbox_librenms_plugin:settings")

        # If we get here, validation failed - render both forms
        return render(
            request,
            self.template_name,
            {
                "server_form": server_form,
                "import_form": import_form,
                "object": settings,
                "active_tab": form_type,  # Pass which tab should be active
            },
        )


class TestLibreNMSConnectionView(View):
    """
    HTMX view to test LibreNMS server connection.
    Returns HTML fragment instead of JSON for HTMX compatibility.
    """

    def post(self, request):
        """Test connection to selected LibreNMS server."""
        server_key = request.POST.get("selected_server")

        if not server_key:
            return HttpResponse(
                '<div class="alert alert-warning">'
                '<i class="ti ti-alert-circle me-2"></i>'
                "<strong>No server selected.</strong> Please select a server first."
                "</div>"
            )

        try:
            # Initialize LibreNMS API client
            api_client = LibreNMSAPI(server_key=server_key)

            # Test the connection by calling the /system endpoint
            system_info = api_client.test_connection()

            if system_info and not system_info.get("error"):
                version = system_info.get("local_ver", "Unknown")
                database = system_info.get("database_ver", "Unknown")
                php_version = system_info.get("php_ver", "Unknown")

                return HttpResponse(
                    f'<div class="alert alert-success">'
                    f'<i class="ti ti-check me-2"></i>'
                    f"<strong>Connection successful!</strong><br>"
                    f"LibreNMS Version: {version}<br>"
                    f"Database: {database}<br>"
                    f"PHP Version: {php_version}"
                    f"</div>"
                )
            elif system_info and system_info.get("error"):
                error_msg = system_info.get("message", "Unknown error occurred")
                return HttpResponse(
                    f'<div class="alert alert-danger">'
                    f'<i class="ti ti-alert-circle me-2"></i>'
                    f"<strong>Connection failed:</strong><br>{error_msg}"
                    f"</div>"
                )
            else:
                return HttpResponse(
                    '<div class="alert alert-danger">'
                    '<i class="ti ti-alert-circle me-2"></i>'
                    "<strong>Connection failed:</strong><br>"
                    "Failed to retrieve system information"
                    "</div>"
                )

        except ValueError as e:
            return HttpResponse(
                f'<div class="alert alert-danger">'
                f'<i class="ti ti-alert-circle me-2"></i>'
                f"<strong>Configuration error:</strong><br>{str(e)}"
                f"</div>"
            )
        except Exception as e:
            return HttpResponse(
                f'<div class="alert alert-danger">'
                f'<i class="ti ti-alert-circle me-2"></i>'
                f"<strong>Connection failed:</strong><br>{str(e)}"
                f"</div>"
            )

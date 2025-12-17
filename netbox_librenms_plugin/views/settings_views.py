import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views import View

from netbox_librenms_plugin.forms import LibreNMSSettingsForm
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.models import LibreNMSSettings


class LibreNMSSettingsView(View):
    """
    View for managing plugin settings including server selection and import options.
    """

    template_name = "netbox_librenms_plugin/settings.html"

    def get(self, request):
        """Display the settings form."""
        # Get or create the settings object
        settings, created = LibreNMSSettings.objects.get_or_create(
            defaults={"selected_server": "default"}
        )

        form = LibreNMSSettingsForm(instance=settings)

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "object": settings,
            },
        )

    def post(self, request):
        """Handle form submission."""
        # Get or create the settings object
        settings, created = LibreNMSSettings.objects.get_or_create(
            defaults={"selected_server": "default"}
        )

        # Determine which form was submitted
        form_type = request.POST.get("form_type")

        # Create a mutable copy of POST data to handle partial form submissions
        post_data = request.POST.copy()

        # If submitting import settings, populate server field with current value
        if form_type == "import_settings" and not post_data.get("selected_server"):
            post_data["selected_server"] = settings.selected_server

        form = LibreNMSSettingsForm(post_data, instance=settings)

        if form.is_valid():
            # Only update the relevant field based on form_type
            if form_type == "server_config":
                settings.selected_server = form.cleaned_data["selected_server"]
                settings.save()
                messages.success(
                    request,
                    f"LibreNMS server settings updated successfully. Active server: {form.cleaned_data['selected_server']}",
                )
            elif form_type == "import_settings":
                settings.vc_member_name_pattern = form.cleaned_data[
                    "vc_member_name_pattern"
                ]
                settings.use_sysname_default = form.cleaned_data["use_sysname_default"]
                settings.strip_domain_default = form.cleaned_data[
                    "strip_domain_default"
                ]
                settings.save()
                messages.success(
                    request,
                    "Import settings updated successfully.",
                )
            else:
                # Fallback: save all fields if form_type not specified
                form.save()
                messages.success(request, "Settings updated successfully.")

            return redirect("plugins:netbox_librenms_plugin:settings")

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "object": settings,
            },
        )


class TestLibreNMSConnectionView(View):
    """
    AJAX view to test LibreNMS server connection.
    """

    def post(self, request):
        """Test connection to selected LibreNMS server."""
        try:
            data = json.loads(request.body)
            server_key = data.get("server_key")

            if not server_key:
                return JsonResponse(
                    {"success": False, "error": "No server key provided"}, status=400
                )

            # Initialize LibreNMS API client
            api_client = LibreNMSAPI(server_key=server_key)

            # Test the connection by calling the /system endpoint
            system_info = api_client.test_connection()

            if system_info and not system_info.get("error"):
                return JsonResponse(
                    {
                        "success": True,
                        "message": "Connection successful!",
                        "system_info": {
                            "version": system_info.get("local_ver", "Unknown"),
                            "database": system_info.get("database_ver", "Unknown"),
                            "php_version": system_info.get("php_ver", "Unknown"),
                        },
                    }
                )
            elif system_info and system_info.get("error"):
                return JsonResponse(
                    {
                        "success": False,
                        "error": system_info.get("message", "Unknown error occurred"),
                    }
                )
            else:
                return JsonResponse(
                    {"success": False, "error": "Failed to retrieve system information"}
                )

        except ValueError as e:
            return JsonResponse(
                {"success": False, "error": f"Configuration error: {str(e)}"}
            )
        except Exception as e:
            return JsonResponse(
                {"success": False, "error": f"Connection failed: {str(e)}"}
            )

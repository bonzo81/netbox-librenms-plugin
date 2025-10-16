from django.contrib import messages
from django.shortcuts import render, redirect
from django.views import View
from django.http import JsonResponse
import json
from netbox_librenms_plugin.forms import LibreNMSSettingsForm
from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.librenms_api import LibreNMSAPI


class LibreNMSSettingsView(View):
    """
    View for managing LibreNMS plugin settings, specifically server selection.
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

        form = LibreNMSSettingsForm(request.POST, instance=settings)

        if form.is_valid():
            form.save()
            messages.success(
                request,
                f"LibreNMS server settings updated successfully. Active server: {form.cleaned_data['selected_server']}",
            )
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
            server_key = data.get('server_key')
            
            if not server_key:
                return JsonResponse({
                    'success': False,
                    'error': 'No server key provided'
                }, status=400)

            # Initialize LibreNMS API client
            api_client = LibreNMSAPI(server_key=server_key)
            
            # Test the connection by calling the /system endpoint
            system_info = api_client.test_connection()
            
            if system_info and not system_info.get('error'):
                return JsonResponse({
                    'success': True,
                    'message': 'Connection successful!',
                    'system_info': {
                        'version': system_info.get('local_ver', 'Unknown'),
                        'database': system_info.get('database_ver', 'Unknown'),
                        'php_version': system_info.get('php_ver', 'Unknown')
                    }
                })
            elif system_info and system_info.get('error'):
                return JsonResponse({
                    'success': False,
                    'error': system_info.get('message', 'Unknown error occurred')
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': 'Failed to retrieve system information'
                })
                
        except ValueError as e:
            return JsonResponse({
                'success': False,
                'error': f'Configuration error: {str(e)}'
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Connection failed: {str(e)}'
            })

from django.contrib import messages
from django.shortcuts import render, redirect
from django.views import View
from netbox_librenms_plugin.forms import LibreNMSSettingsForm
from netbox_librenms_plugin.models import LibreNMSSettings


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

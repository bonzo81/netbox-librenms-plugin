from django.core.exceptions import ImproperlyConfigured
from netbox.plugins import PluginConfig

__author__ = "Andy Norwood"
__version__ = "0.4.2"


class LibreNMSSyncConfig(PluginConfig):
    name = "netbox_librenms_plugin"
    verbose_name = "NetBox Librenms Plugin"
    description = "Netbox plugin to sync data between LibreNMS and Netbox."
    author = __author__
    version = __version__
    base_url = "librenms_plugin"
    min_version = "4.2.0"
    required_settings = []  # Custom validation in ready() method
    default_settings = {
        "enable_caching": True,
        "verify_ssl": True,
        "interface_name_field": "ifName",
    }

    def ready(self):
        """
        Perform custom validation for plugin configuration.
        Supports both legacy single-server and new multi-server configurations.
        """
        super().ready()

        from django.conf import settings
        from django.db.models.signals import post_migrate

        plugin_config = getattr(settings, "PLUGINS_CONFIG", {}).get(self.name, {})

        # Check if using new multi-server configuration
        if "servers" in plugin_config:
            self._validate_multi_server_config(plugin_config["servers"])
        else:
            self._validate_legacy_config(plugin_config)

        # Auto-create the librenms_id custom field after migrations complete
        post_migrate.connect(
            _ensure_librenms_id_custom_field,
            dispatch_uid="netbox_librenms_plugin_ensure_cf",
        )

    def _validate_multi_server_config(self, servers_config):
        """Validate multi-server configuration."""
        if not servers_config or not isinstance(servers_config, dict):
            raise ImproperlyConfigured(
                f"Plugin {self.name} requires at least one server configuration in the 'servers' section."
            )

        for server_key, server_config in servers_config.items():
            if not isinstance(server_config, dict):
                raise ImproperlyConfigured(f"Plugin {self.name} server '{server_key}' must be a dictionary.")

            for setting in ["librenms_url", "api_token"]:
                if setting not in server_config:
                    raise ImproperlyConfigured(f"Plugin {self.name} server '{server_key}' requires '{setting}'.")

    def _validate_legacy_config(self, plugin_config):
        """Validate legacy single-server configuration."""
        for setting in ["librenms_url", "api_token"]:
            if setting not in plugin_config:
                raise ImproperlyConfigured(
                    f"Plugin {self.name} requires either 'servers' configuration or legacy '{setting}' setting."
                )


def _ensure_librenms_id_custom_field(sender, **kwargs):
    """
    Auto-create the 'librenms_id' custom field if it doesn't exist.
    Runs after migrations via post_migrate signal to ensure tables exist.
    Uses dispatch_uid to avoid duplicate connections.
    """
    # Only run once per migrate invocation (post_migrate fires per-app).
    # The _executed flag is intentionally never reset: migrations are expected to
    # run in short-lived CLI processes (manage.py migrate) where the flag is
    # naturally cleared on exit.  Long-running processes (e.g. gunicorn workers)
    # should not rely on this handler re-executing after startup.
    if getattr(_ensure_librenms_id_custom_field, "_executed", False):
        return
    _ensure_librenms_id_custom_field._executed = True  # not reset; see comment above

    try:
        from django.contrib.contenttypes.models import ContentType

        from extras.models import CustomField

        cf, created = CustomField.objects.get_or_create(
            name="librenms_id",
            defaults={
                "type": "integer",
                "label": "LibreNMS ID",
                "description": "LibreNMS Device ID for synchronization (auto-created by plugin)",
                "required": False,
                "ui_visible": "if-set",
                "ui_editable": "yes",
                "is_cloneable": False,
            },
        )

        # Ensure the field is assigned to the required object types
        from dcim.models import Device, Interface
        from virtualization.models import VirtualMachine, VMInterface

        required_models = [Device, VirtualMachine, Interface, VMInterface]
        current_types = set(cf.object_types.values_list("pk", flat=True))

        for model in required_models:
            ct = ContentType.objects.get_for_model(model)
            if ct.pk not in current_types:
                cf.object_types.add(ct)

        if created:
            import logging

            logging.getLogger("netbox_librenms_plugin").info(
                "Auto-created 'librenms_id' custom field for Device, VirtualMachine, Interface, VMInterface"
            )
    except Exception as e:
        # Don't break startup if custom field creation fails (e.g., during initial migration),
        # but log the error so it's not silently swallowed.
        import logging

        logging.getLogger("netbox_librenms_plugin").exception("Failed to auto-create 'librenms_id' custom field: %s", e)


config = LibreNMSSyncConfig

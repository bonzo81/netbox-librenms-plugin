from django.core.exceptions import ImproperlyConfigured
from netbox.plugins import PluginConfig

__author__ = "Andy Norwood"
__version__ = "0.4.3"


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
    Auto-create (or migrate) the 'librenms_id' custom field.
    Runs after migrations via post_migrate signal to ensure tables exist.
    Uses dispatch_uid to avoid duplicate connections.

    librenms_id stores a per-server JSON mapping {"server_key": device_id}.
    Legacy installations may have this field typed as 'integer'; we upgrade it
    to 'json' automatically so the UI and API accept the dict format.
    """
    # Track per-alias execution so each database alias is bootstrapped exactly once.
    db_alias = kwargs.get("using") or "default"
    executed_aliases = getattr(_ensure_librenms_id_custom_field, "_executed_aliases", set())
    if db_alias in executed_aliases:
        return

    import logging

    try:
        from django.contrib.contenttypes.models import ContentType

        from extras.models import CustomField

        cf, created = CustomField.objects.using(db_alias).get_or_create(
            name="librenms_id",
            defaults={
                "type": "json",
                "label": "LibreNMS ID",
                "description": "LibreNMS Device ID for synchronization (auto-created by plugin)",
                "required": False,
                "ui_visible": "if-set",
                "ui_editable": "yes",
                "is_cloneable": False,
            },
        )

        # Migrate legacy integer-typed field to JSON so the multi-server
        # dict format {"server_key": device_id} is accepted by the UI/API.
        if not created and cf.type == "integer":
            cf.type = "json"
            cf.save(using=db_alias, update_fields=["type"])
            logging.getLogger("netbox_librenms_plugin").info(
                "Migrated 'librenms_id' custom field type from integer to json"
            )

        # Ensure the field is assigned to the required object types
        from dcim.models import Device, Interface
        from virtualization.models import VirtualMachine, VMInterface

        required_models = [Device, VirtualMachine, Interface, VMInterface]
        current_types = set(cf.object_types.values_list("pk", flat=True))

        for model in required_models:
            ct = ContentType.objects.db_manager(db_alias).get_for_model(model)
            if ct.pk not in current_types:
                cf.object_types.add(ct)

        if created:
            logging.getLogger("netbox_librenms_plugin").info(
                "Auto-created 'librenms_id' custom field for Device, VirtualMachine, Interface, VMInterface"
            )

        # Mark this alias as executed after successful completion to allow retry on failure.
        executed_aliases.add(db_alias)
        _ensure_librenms_id_custom_field._executed_aliases = executed_aliases
    except Exception as e:
        # Don't break startup if custom field creation fails (e.g., during initial migration),
        # but log the error so it's not silently swallowed.
        logging.getLogger("netbox_librenms_plugin").exception("Failed to auto-create 'librenms_id' custom field: %s", e)


config = LibreNMSSyncConfig

# forms.py
from dcim.choices import InterfaceTypeChoices
from dcim.models import Device, DeviceRole, DeviceType, Location, Rack, Site
from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import (
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from netbox.plugins import get_plugin_config
from utilities.forms.fields import CSVChoiceField, DynamicModelMultipleChoiceField
from virtualization.models import Cluster, VirtualMachine

from .models import InterfaceTypeMapping, LibreNMSSettings


class LibreNMSSettingsForm(NetBoxModelForm):
    """
    Form for selecting the active LibreNMS server from configured servers.
    """

    selected_server = forms.ChoiceField(
        label="LibreNMS Server",
        help_text="Select which LibreNMS server to use for synchronization operations",
    )

    class Meta:
        model = LibreNMSSettings
        fields = ["selected_server"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Get available servers from configuration
        self.fields["selected_server"].choices = self._get_server_choices()

    def _get_server_choices(self):
        """Get server choices from plugin configuration."""
        choices = []

        # Try to get multi-server configuration
        servers_config = get_plugin_config("netbox_librenms_plugin", "servers")

        if servers_config and isinstance(servers_config, dict):
            # Multi-server configuration
            for key, config in servers_config.items():
                display_name = config.get("display_name", key)
                url = config.get("librenms_url", "Unknown URL")
                choices.append((key, f"{display_name} ({url})"))
        else:
            # Legacy single-server configuration
            legacy_url = get_plugin_config("netbox_librenms_plugin", "librenms_url")
            if legacy_url:
                choices.append(("default", f"Default Server ({legacy_url})"))
            else:
                choices.append(("default", "Default Server"))

        return choices


class InterfaceTypeMappingForm(NetBoxModelForm):
    """
    Form for creating and editing interface type mappings between LibreNMS and NetBox.
    Allows mapping of LibreNMS interface types and speeds to NetBox interface types.
    """

    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type", "description"]


class InterfaceTypeMappingImportForm(NetBoxModelImportForm):
    """
    Form for bulk importing interface type mappings from CSV/JSON/YAML.
    Supports importing LibreNMS interface type and speed mappings to NetBox interface types.
    """

    netbox_type = CSVChoiceField(
        label=_("NetBox Type"),
        choices=InterfaceTypeChoices,
        help_text=_("NetBox interface type"),
    )

    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type", "description"]


class InterfaceTypeMappingFilterForm(NetBoxModelFilterSetForm):
    """
    Form for filtering interface type mappings based on LibreNMS and NetBox attributes.
    Provides filtering options for LibreNMS type, speed, and NetBox type.
    """

    librenms_type = forms.CharField(required=False, label="LibreNMS Type")
    librenms_speed = forms.IntegerField(
        required=False,
        label="LibreNMS Speed (Kbps)",
        help_text="Filter by interface speed in Kbps",
    )
    netbox_type = forms.ChoiceField(
        required=False,
        label="NetBox Type",
        choices=[("", "---------")] + list(InterfaceTypeChoices),
    )
    description = forms.CharField(
        required=False,
        label="Description",
        help_text="Filter by description (partial match)",
    )

    model = InterfaceTypeMapping


class AddToLIbreSNMPV2(forms.Form):
    """
    Form for adding devices to LibreNMS using SNMPv2 authentication.
    Collects hostname/IP and SNMP community string information.
    """

    hostname = forms.CharField(
        label="Hostname/IP",
        max_length=255,
        required=True,
    )
    snmp_version = forms.CharField(widget=forms.HiddenInput())
    community = forms.CharField(label="SNMP Community", max_length=255, required=True)
    port = forms.IntegerField(
        label="SNMP Port",
        required=False,
        help_text="Leave blank to use default SNMP port (161)",
        widget=forms.NumberInput(attrs={"placeholder": "161"}),
    )
    transport = forms.ChoiceField(
        label="Transport",
        choices=[
            ("udp", "UDP"),
            ("tcp", "TCP"),
            ("udp6", "UDP6"),
            ("tcp6", "TCP6"),
        ],
        required=False,
        initial="udp",
    )
    port_association_mode = forms.ChoiceField(
        label="Port Association Mode",
        choices=[
            ("ifIndex", "ifIndex"),
            ("ifName", "ifName"),
            ("ifDescr", "ifDescr"),
            ("ifAlias", "ifAlias"),
        ],
        required=False,
        initial="ifIndex",
        help_text="Method to identify ports",
    )
    poller_group = forms.ChoiceField(
        label="Poller Group",
        required=False,
        help_text="Poller group for distributed poller setup",
    )
    force_add = forms.BooleanField(
        label="Force Add",
        required=False,
        initial=False,
        help_text="Skip duplicate device and SNMP reachability checks (hostname must still be unique)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate poller groups from LibreNMS API
        self.fields["poller_group"].choices = self._get_poller_group_choices()

    def _get_poller_group_choices(self):
        """Get poller group choices from LibreNMS API."""
        from .librenms_api import LibreNMSAPI

        choices = [("0", "Default (0)")]

        try:
            api = LibreNMSAPI()
            success, poller_groups = api.get_poller_groups()

            if success and poller_groups:
                for group in poller_groups:
                    group_id = str(group.get("id", ""))
                    group_name = group.get("group_name", "")
                    group_descr = group.get("descr", "")

                    if group_id:
                        # Format: "Group Name (ID)" or "Group Name - Description (ID)"
                        if group_descr and group_descr != group_name:
                            label = f"{group_name} - {group_descr} ({group_id})"
                        else:
                            label = f"{group_name} ({group_id})"
                        choices.append((group_id, label))
        except Exception:
            # If API call fails, just use default option
            pass

        return choices


class AddToLIbreSNMPV3(forms.Form):
    """
    Form for adding devices to LibreNMS using SNMPv3 authentication.
    Provides comprehensive SNMPv3 configuration options including authentication and encryption settings.
    """

    hostname = forms.CharField(
        label="Hostname/IP",
        max_length=255,
        required=True,
    )
    snmp_version = forms.CharField(widget=forms.HiddenInput(), initial="v3")
    authlevel = forms.ChoiceField(
        label="Auth Level",
        choices=[
            ("noAuthNoPriv", "noAuthNoPriv"),
            ("authNoPriv", "authNoPriv"),
            ("authPriv", "authPriv"),
        ],
        required=True,
    )
    authname = forms.CharField(label="Auth Username", max_length=255, required=True)
    authpass = forms.CharField(
        label="Auth Password",
        max_length=255,
        required=True,
        widget=forms.PasswordInput(render_value=True),
    )
    authalgo = forms.ChoiceField(
        label="Auth Algorithm",
        choices=[
            ("SHA", "SHA"),
            ("MD5", "MD5"),
            ("SHA-224", "SHA-224"),
            ("SHA-256", "SHA-256"),
            ("SHA-384", "SHA-384"),
            ("SHA-512", "SHA-512"),
        ],
        required=True,
    )
    cryptopass = forms.CharField(
        label="Crypto Password",
        max_length=255,
        required=True,
        widget=forms.PasswordInput(render_value=True),
    )
    cryptoalgo = forms.ChoiceField(
        label="Crypto Algorithm",
        choices=[("AES", "AES"), ("DES", "DES")],
        required=True,
    )
    port = forms.IntegerField(
        label="SNMP Port",
        required=False,
        help_text="Leave blank to use default SNMP port (161)",
        widget=forms.NumberInput(attrs={"placeholder": "161"}),
    )
    transport = forms.ChoiceField(
        label="Transport",
        choices=[
            ("udp", "UDP"),
            ("tcp", "TCP"),
            ("udp6", "UDP6"),
            ("tcp6", "TCP6"),
        ],
        required=False,
        initial="udp",
    )
    port_association_mode = forms.ChoiceField(
        label="Port Association Mode",
        choices=[
            ("ifIndex", "ifIndex"),
            ("ifName", "ifName"),
            ("ifDescr", "ifDescr"),
            ("ifAlias", "ifAlias"),
        ],
        required=False,
        initial="ifIndex",
        help_text="Method to identify ports",
    )
    poller_group = forms.ChoiceField(
        label="Poller Group",
        required=False,
        help_text="Poller group for distributed poller setup",
    )
    force_add = forms.BooleanField(
        label="Force Add",
        required=False,
        initial=False,
        help_text="Skip duplicate device and SNMP reachability checks (hostname must still be unique)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate poller groups from LibreNMS API
        self.fields["poller_group"].choices = self._get_poller_group_choices()

    def _get_poller_group_choices(self):
        """Get poller group choices from LibreNMS API."""
        from .librenms_api import LibreNMSAPI

        choices = [("0", "Default (0)")]

        try:
            api = LibreNMSAPI()
            success, poller_groups = api.get_poller_groups()

            if success and poller_groups:
                for group in poller_groups:
                    group_id = str(group.get("id", ""))
                    group_name = group.get("group_name", "")
                    group_descr = group.get("descr", "")

                    if group_id:
                        # Format: "Group Name (ID)" or "Group Name - Description (ID)"
                        if group_descr and group_descr != group_name:
                            label = f"{group_name} - {group_descr} ({group_id})"
                        else:
                            label = f"{group_name} ({group_id})"
                        choices.append((group_id, label))
        except Exception:
            # If API call fails, just use default option
            pass

        return choices


class DeviceStatusFilterForm(NetBoxModelFilterSetForm):
    """
    Filter form for Device Status view - shows NetBox devices and their LibreNMS status.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Remove the saved filter field if it exists
        if "filter_id" in self.fields:
            del self.fields["filter_id"]

    site = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False)
    location = DynamicModelMultipleChoiceField(
        queryset=Location.objects.all(), required=False
    )
    rack = DynamicModelMultipleChoiceField(queryset=Rack.objects.all(), required=False)
    device_type = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False
    )
    role = DynamicModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(), required=False
    )

    model = Device


class LibreNMSImportFilterForm(forms.Form):
    """
    Filter form for LibreNMS Import view - shows LibreNMS devices for import.
    Uses a simple Django form instead of NetBox model forms.
    """

    # LibreNMS filters
    librenms_location = forms.ChoiceField(
        required=False,
        label="LibreNMS Location",
        choices=[("", "All Locations")],  # Default, will be populated in __init__
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    librenms_type = forms.ChoiceField(
        required=False,
        label="LibreNMS Type",
        choices=[
            ("", "All Types"),
            ("network", "Network"),
            ("server", "Server"),
            ("storage", "Storage"),
            ("wireless", "Wireless"),
            ("firewall", "Firewall"),
            ("power", "Power"),
            ("appliance", "Appliance"),
            ("printer", "Printer"),
            ("loadbalancer", "Load Balancer"),
            ("other", "Other"),
        ],
    )
    librenms_os = forms.CharField(
        required=False,
        label="Operating System",
        widget=forms.TextInput(attrs={"placeholder": "e.g., ios, linux, junos"}),
    )
    librenms_hostname = forms.CharField(
        required=False,
        label="LibreNMS Hostname",
        widget=forms.TextInput(attrs={"placeholder": "Partial hostname match"}),
        help_text="IP address or DNS name used to add device to LibreNMS",
    )
    librenms_sysname = forms.CharField(
        required=False,
        label="LibreNMS System Name",
        widget=forms.TextInput(attrs={"placeholder": "Exact or partial sysName match"}),
        help_text="SNMP sysName of the device (exact match only; combine with another filter for partial matching)",
    )
    show_disabled = forms.BooleanField(
        required=False,
        initial=False,
        label="Include Disabled Devices",
        help_text="Check to include disabled devices from LibreNMS",
    )
    validation_status = forms.ChoiceField(
        required=False,
        label="Import Status",
        choices=[
            ("", "All"),
            ("ready", "Ready to Import"),
            ("needs_review", "Needs Review"),
            ("cannot_import", "Cannot Import"),
            ("exists", "Already Exists"),
        ],
        help_text="Filter by import readiness status",
    )

    def __init__(self, *args, **kwargs):
        """Initialize the form and populate dynamic choices."""
        super().__init__(*args, **kwargs)
        # Populate LibreNMS location choices dynamically
        self._populate_librenms_locations()

    def _populate_librenms_locations(self):
        """Fetch and populate LibreNMS locations in the dropdown."""
        import logging

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        logger = logging.getLogger(__name__)

        try:
            # Use caching to avoid repeated API calls
            cache_key = "librenms_locations_choices"
            cached_choices = cache.get(cache_key)

            if cached_choices:
                self.fields["librenms_location"].choices = cached_choices
                return

            # Fetch locations from LibreNMS
            api = LibreNMSAPI()
            success, locations = api.get_locations()

            if success and locations:
                # Build choices list: (id, "name (id)")
                choices = [("", "All Locations")]
                for loc in locations:
                    loc_id = str(loc.get("id", ""))
                    loc_name = loc.get("location", f"Location {loc_id}")
                    choices.append((loc_id, f"{loc_name} (ID: {loc_id})"))

                # Sort by name
                choices[1:] = sorted(choices[1:], key=lambda x: x[1])

                self.fields["librenms_location"].choices = choices

                # Cache for 5 minutes
                cache.set(cache_key, choices, timeout=300)
                logger.info(f"Loaded {len(choices) - 1} LibreNMS locations")
            else:
                logger.warning(f"Failed to load LibreNMS locations: {locations}")
        except Exception as e:
            logger.exception(f"Error loading LibreNMS locations: {e}")
            # Keep default choices on error


class VirtualMachineStatusFilterForm(NetBoxModelFilterSetForm):
    """
    Form for filtering virtual machine status information in NetBox.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the form and remove the filter_id field if it exists."""
        super().__init__(*args, **kwargs)
        # Remove the saved filter field if it exists
        if "filter_id" in self.fields:
            del self.fields["filter_id"]

    virtualmachine = DynamicModelMultipleChoiceField(
        queryset=VirtualMachine.objects.all(), required=False
    )
    site = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False)
    cluster = DynamicModelMultipleChoiceField(
        queryset=Cluster.objects.all(), required=False
    )

    model = VirtualMachine


class DeviceImportConfigForm(forms.Form):
    """
    Form for configuring import of LibreNMS devices with missing prerequisites.
    Allows user to manually map LibreNMS device data to NetBox objects.
    """

    device_id = forms.IntegerField(widget=forms.HiddenInput(), required=True)
    hostname = forms.CharField(disabled=True, required=False, label="Device Hostname")
    hardware = forms.CharField(disabled=True, required=False, label="Hardware")
    librenms_location = forms.CharField(
        disabled=True, required=False, label="LibreNMS Location"
    )

    # Required mappings
    site = forms.ModelChoiceField(
        queryset=Site.objects.all(),
        required=True,
        label="NetBox Site",
        help_text="Select the NetBox site for this device",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    device_type = forms.ModelChoiceField(
        queryset=DeviceType.objects.all(),
        required=True,
        label="Device Type",
        help_text="Select the NetBox device type",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    device_role = forms.ModelChoiceField(
        queryset=DeviceRole.objects.all(),
        required=True,
        label="Device Role",
        help_text="Select the device role",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    # Optional mappings
    platform = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label="Platform",
        help_text="Select platform (optional)",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    # Sync options
    sync_interfaces = forms.BooleanField(
        initial=True,
        required=False,
        label="Sync Interfaces",
        help_text="Automatically sync interfaces from LibreNMS after import",
    )
    sync_cables = forms.BooleanField(
        initial=True,
        required=False,
        label="Sync Cables",
        help_text="Automatically sync cable connections from LibreNMS after import",
    )
    sync_ips = forms.BooleanField(
        initial=True,
        required=False,
        label="Sync IP Addresses",
        help_text="Automatically sync IP addresses from LibreNMS after import",
    )

    def __init__(self, *args, **kwargs):
        """
        Initialize form with LibreNMS device data and validation results.

        Accepts additional kwargs:
        - libre_device: LibreNMS device dictionary
        - validation: Validation result dictionary
        - suggested_site: Pre-selected site
        - suggested_device_type: Pre-selected device type
        - suggested_role: Pre-selected device role
        """
        # Extract custom kwargs
        libre_device = kwargs.pop("libre_device", {})
        validation = kwargs.pop("validation", {})
        suggested_site = kwargs.pop("suggested_site", None)
        suggested_device_type = kwargs.pop("suggested_device_type", None)
        suggested_role = kwargs.pop("suggested_role", None)

        super().__init__(*args, **kwargs)

        # Import Platform here to avoid circular imports
        from dcim.models import Platform

        self.fields["platform"].queryset = Platform.objects.all()

        # Set initial values from LibreNMS device
        if libre_device:
            self.fields["device_id"].initial = libre_device.get("device_id")
            self.fields["hostname"].initial = libre_device.get("hostname", "")
            self.fields["hardware"].initial = libre_device.get("hardware", "")
            self.fields["librenms_location"].initial = libre_device.get("location", "")

        # Set suggested values from validation
        if suggested_site:
            self.fields["site"].initial = suggested_site
        elif validation and validation.get("site", {}).get("site"):
            self.fields["site"].initial = validation["site"]["site"]

        if suggested_device_type:
            self.fields["device_type"].initial = suggested_device_type
        elif validation and validation.get("device_type", {}).get("device_type"):
            self.fields["device_type"].initial = validation["device_type"][
                "device_type"
            ]

        if suggested_role:
            self.fields["device_role"].initial = suggested_role
        elif validation and validation.get("device_role", {}).get("role"):
            self.fields["device_role"].initial = validation["device_role"]["role"]

        if validation and validation.get("platform", {}).get("platform"):
            self.fields["platform"].initial = validation["platform"]["platform"]

        # Filter device types by suggestions if available
        if validation and validation.get("device_type", {}).get("suggestions"):
            suggestions = validation["device_type"]["suggestions"]
            if suggestions:
                # Include suggested device types first, then all others
                suggested_ids = [s["device_type"].id for s in suggestions]
                self.fields["device_type"].queryset = DeviceType.objects.filter(
                    id__in=suggested_ids
                ) | DeviceType.objects.exclude(id__in=suggested_ids)

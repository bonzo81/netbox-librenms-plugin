# forms.py
import logging

from dcim.choices import InterfaceTypeChoices
from dcim.models import Device, DeviceRole, DeviceType, Location, Rack, Site
from django import forms
from django.http import QueryDict
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

logger = logging.getLogger(__name__)


def _get_librenms_server_choices():
    """
    Helper function to get server choices from plugin configuration.
    Shared between ServerConfigForm and other forms that need server selection.
    """
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


class ServerConfigForm(NetBoxModelForm):
    """
    Form for selecting the active LibreNMS server from configured servers.
    Handles server configuration changes only.
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
        self.fields["selected_server"].choices = _get_librenms_server_choices()


class ImportSettingsForm(NetBoxModelForm):
    """
    Form for configuring device import settings including naming patterns
    and virtual chassis member naming.
    """

    vc_member_name_pattern = forms.CharField(
        label="Virtual Chassis Member Naming Pattern",
        max_length=100,
        required=False,
        strip=False,  # Preserve leading/trailing whitespace
        widget=forms.TextInput(
            attrs={
                "placeholder": "-M{position}",
            }
        ),
    )

    use_sysname_default = forms.BooleanField(
        label="Use sysName",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Use SNMP sysName instead of LibreNMS hostname when importing devices",
    )

    strip_domain_default = forms.BooleanField(
        label="Strip domain",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Remove domain suffix from device names during import",
    )

    class Meta:
        model = LibreNMSSettings
        fields = [
            "vc_member_name_pattern",
            "use_sysname_default",
            "strip_domain_default",
        ]

    def clean_vc_member_name_pattern(self):
        """
        Validate VC member name pattern for valid placeholders and formatting.

        The pattern is used as a suffix appended to the master device name.
        Valid placeholders: {position}, {serial}
        At least one is required for uniqueness.
        """
        pattern = self.cleaned_data.get("vc_member_name_pattern")

        if not pattern:
            return pattern

        # Check for valid placeholder names using regex
        import re

        valid_placeholders = {"position", "serial"}
        found_placeholders = set(re.findall(r"\{(\w+)\}", pattern))
        invalid_placeholders = found_placeholders - valid_placeholders

        if invalid_placeholders:
            invalid_list = ", ".join(f"{{{p}}}" for p in sorted(invalid_placeholders))
            error_msg = f"Invalid placeholder(s): {invalid_list}. Valid options are: {{position}}, {{serial}}"
            raise forms.ValidationError(error_msg)

        # Check required: must have at least one unique identifier
        if "{position}" not in pattern and "{serial}" not in pattern:
            raise forms.ValidationError(
                "The naming pattern must include either {{position}} or {{serial}} "
                "placeholder to ensure unique member names."
            )

        # Test the pattern can be formatted without errors
        test_vars = {
            "position": 1,
            "serial": "ABC123",
        }

        try:
            test_result = pattern.format(**test_vars)

            # Check result isn't empty or just whitespace
            if not test_result.strip():
                raise forms.ValidationError(
                    "The pattern results in an empty suffix. "
                    "Please include some text content in the pattern."
                )

        except KeyError as e:
            # This should be caught by check above, but just in case
            raise forms.ValidationError(
                f"Invalid placeholder in pattern: {e}. "
                f"Valid options are: {{position}}, {{serial}}"
            )
        except (ValueError, IndexError) as e:
            raise forms.ValidationError(f"Invalid pattern syntax: {str(e)}")

        return pattern


# Keep for backward compatibility if needed elsewhere
class LibreNMSSettingsForm(ServerConfigForm):
    """
    Deprecated: Use ServerConfigForm or ImportSettingsForm instead.
    Kept for backward compatibility.
    """

    pass


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
    snmp_version = forms.CharField(widget=forms.HiddenInput(), initial="v2c")
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
        help_text="IP address or FQDN used to add device to LibreNMS",
    )
    librenms_sysname = forms.CharField(
        required=False,
        label="LibreNMS System Name",
        widget=forms.TextInput(attrs={"placeholder": "Exact or partial sysName match"}),
        help_text="SNMP sysName. (exact match only; combine with another filter for partial matching)",
    )
    librenms_hardware = forms.CharField(
        required=False,
        label="Hardware",
        widget=forms.TextInput(attrs={"placeholder": "e.g., C9300-48P, ASR-920"}),
        help_text="LibreNMS hardware model (partial match)",
    )
    show_disabled = forms.BooleanField(
        required=False,
        initial=False,
        label="Include Disabled Devices",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    enable_vc_detection = forms.BooleanField(
        required=False,
        initial=False,
        label="Include Virtual Chassis Detection",
        help_text="Run additional stack checks during the search. Will increase processing time.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    clear_cache = forms.BooleanField(
        required=False,
        initial=False,
        label="Clear cache before search",
        help_text="Discard the cache and pull fresh data from both LibreNMS and NetBox.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    exclude_existing = forms.BooleanField(
        required=False,
        initial=False,
        label="Exclude Existing Devices",
        help_text="Hide devices that already exist in NetBox",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    use_background_job = forms.BooleanField(
        required=False,
        initial=True,
        label="Run as background job",
        help_text="Recommended: Jobs are logged and can be cancelled.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs):
        """Initialize the form and populate dynamic choices."""
        # For bound forms, ensure use_background_job defaults to 'on' if not present
        # This handles the case where checkbox is checked by default but not in GET params
        # Only apply this default when no filters are applied (initial page load)
        if args and isinstance(args[0], (dict, QueryDict)):
            # Form is being bound with data (GET/POST dict or QueryDict)
            data = args[0].copy() if hasattr(args[0], "copy") else dict(args[0])
            # If use_background_job is not in the data, add it with default 'on'
            # This makes the checkbox checked by default even on first submission
            # Only do this if no filter fields are set (initial page load scenario)
            filter_fields = [
                "librenms_location",
                "librenms_type",
                "librenms_os",
                "librenms_hostname",
                "librenms_sysname",
                "librenms_hardware",
            ]
            has_filters = any(data.get(field) for field in filter_fields)

            # Apply default only on initial load (no filters, no job_id)
            if (
                "use_background_job" not in data
                and not data.get("job_id")
                and not has_filters
            ):
                data["use_background_job"] = "on"
            args = (data,) + args[1:]

        super().__init__(*args, **kwargs)
        # Populate LibreNMS location choices dynamically
        self._populate_librenms_locations()

    def clean(self):
        cleaned_data = super().clean()

        # Only enforce filter requirement when the user explicitly submits the form
        if self.data.get("apply_filters"):
            filter_fields = (
                "librenms_location",
                "librenms_type",
                "librenms_os",
                "librenms_hostname",
                "librenms_sysname",
                "librenms_hardware",
            )

            if not any(cleaned_data.get(field) for field in filter_fields):
                raise forms.ValidationError(
                    "Please select at least one LibreNMS filter before applying the search."
                )

        return cleaned_data

    def _populate_librenms_locations(self):
        """Fetch and populate LibreNMS locations in the dropdown."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

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
                # Build choices list: (id, name)
                choices = [("", "All Locations")]
                for loc in locations:
                    loc_id = str(loc.get("id", ""))
                    loc_name = loc.get("location", f"Location {loc_id}")
                    choices.append((loc_id, loc_name))

                # Sort by name
                choices[1:] = sorted(choices[1:], key=lambda x: x[1])

                self.fields["librenms_location"].choices = choices

                # Cache using configured timeout (default 300s)
                cache.set(cache_key, choices, timeout=api.cache_timeout)
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

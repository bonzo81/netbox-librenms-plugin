import re

from django.conf import settings as django_settings
from django.shortcuts import get_object_or_404, render
from netbox.views import generic

from netbox_librenms_plugin.forms import AddToLIbreSNMPV1V2, AddToLIbreSNMPV3
from netbox_librenms_plugin.import_utils import _determine_device_name
from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name
from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_librenms_device_id,
    get_librenms_sync_device,
    match_librenms_hardware_to_device_type,
    resolve_naming_preferences,
)
from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin


class BaseLibreNMSSyncView(LibreNMSPermissionMixin, LibreNMSAPIMixin, generic.ObjectListView):
    """
    Base view for LibreNMS sync information.
    """

    queryset = None  # Will be set in subclasses
    model = None  # Will be set in subclasses
    tab = None  # Will be set in subclasses
    template_name = "netbox_librenms_plugin/librenms_sync_base.html"

    def get(self, request, pk, context=None):
        """Handle GET request for the LibreNMS sync view."""
        obj = get_object_or_404(self.model, pk=pk)

        # For Virtual Chassis members, always delegate to get_librenms_sync_device() so
        # self._librenms_lookup_device and self.librenms_id are consistent with the
        # helper-based VC status computed in get_context_data().  A legacy bare-int mapping
        # on the viewed member must not shadow an explicit per-server mapping on another
        # member — get_librenms_sync_device() applies the full priority order.
        librenms_lookup_device = obj
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            sync_device = get_librenms_sync_device(obj, server_key=self.librenms_api.server_key)
            if sync_device:
                librenms_lookup_device = sync_device

        # Store for use in get_context_data (badge generation needs the same object)
        self._librenms_lookup_device = librenms_lookup_device

        # Get librenms_id using the determined lookup device
        self.librenms_id = self.librenms_api.get_librenms_id(librenms_lookup_device)

        context = self.get_context_data(request, obj)

        return render(request, self.template_name, context)

    def get_context_data(self, request, obj):
        """Get the context data for the LibreNMS sync view."""
        # Get context from parent classes (including LibreNMSAPIMixin)
        context = super().get_context_data()

        # Add our specific context
        context.update(
            {
                "object": obj,
                "tab": self.tab,
                "has_librenms_id": bool(self.librenms_id),
            }
        )

        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            # Use helper function to determine the sync device
            librenms_sync_device = get_librenms_sync_device(obj, server_key=self.librenms_api.server_key)

            # Determine sync device status
            sync_device_has_librenms_id = False
            sync_device_has_primary_ip = False

            if librenms_sync_device:
                sync_device_has_librenms_id = (
                    get_librenms_device_id(librenms_sync_device, self.librenms_api.server_key, auto_save=False)
                    is not None
                )
                sync_device_has_primary_ip = bool(librenms_sync_device.primary_ip)

            context.update(
                {
                    "is_vc_member": True,
                    "sync_device_has_primary_ip": sync_device_has_primary_ip,
                    "librenms_sync_device": librenms_sync_device,
                    "sync_device_has_librenms_id": sync_device_has_librenms_id,
                }
            )

        librenms_info = self.get_librenms_device_info(obj, request)

        interface_context = self.get_interface_context(request, obj)
        cable_context = self.get_cable_context(request, obj)
        ip_context = self.get_ip_context(request, obj)
        vlan_context = self.get_vlan_context(request, obj)
        module_context = self.get_module_context(request, obj)

        interface_name_field = get_interface_name_field(request)

        # Get platform info for display and sync
        platform_info = self._get_platform_info(librenms_info, obj)

        # Get manufacturers for platform creation modal
        from dcim.models import Manufacturer

        manufacturers = Manufacturer.objects.all().order_by("name")

        # Detect legacy bare-int librenms_id format for conversion badge
        _lookup_device = getattr(self, "_librenms_lookup_device", obj)
        _raw_cf = _lookup_device.cf.get("librenms_id") if _lookup_device else None
        librenms_id_is_legacy = (isinstance(_raw_cf, int) and not isinstance(_raw_cf, bool)) or (
            isinstance(_raw_cf, str) and _raw_cf.isdigit()
        )

        # Determine if serial match allows legacy ID conversion.
        # VMs have no serial field in NetBox; skip the gate so the Convert ID button is enabled.
        _librenms_serial = librenms_info["librenms_device_details"].get("librenms_device_serial", "-")
        _netbox_serial = getattr(_lookup_device, "serial", "") or ""
        _lookup_is_vm = _lookup_device._meta.model_name == "virtualmachine" if _lookup_device else False
        librenms_id_serial_confirmed = _lookup_is_vm or bool(
            _librenms_serial and _librenms_serial != "-" and _netbox_serial and _librenms_serial == _netbox_serial
        )

        context.update(
            {
                "interface_sync": interface_context,
                "cable_sync": cable_context,
                "ip_sync": ip_context,
                "vlan_sync": vlan_context,
                "module_sync": module_context,
                "v1v2form": AddToLIbreSNMPV1V2(prefix="v1v2"),
                "v3form": AddToLIbreSNMPV3(prefix="v3"),
                "librenms_device_id": self.librenms_id,
                "found_in_librenms": librenms_info.get("found_in_librenms"),
                "librenms_device_details": librenms_info.get("librenms_device_details"),
                "mismatched_device": librenms_info.get("mismatched_device"),
                **librenms_info["librenms_device_details"],
                "interface_name_field": interface_name_field,
                "platform_info": platform_info,
                "vc_inventory_serials": librenms_info["librenms_device_details"].get("vc_inventory_serials", []),
                "manufacturers": manufacturers,
                "all_server_mappings": self._build_all_server_mappings(_lookup_device, self.librenms_api.server_key),
                "librenms_id_is_legacy": librenms_id_is_legacy,
                "librenms_id_serial_confirmed": librenms_id_serial_confirmed,
                # Lookup device may differ from object (e.g. VC master vs member).
                # Used by the Remove server mapping form to post to the correct device.
                "lookup_device_pk": _lookup_device.pk if _lookup_device else obj.pk,
                "lookup_device_model_name": (
                    _lookup_device._meta.model_name if _lookup_device else obj._meta.model_name
                ),
                "object_model_name": obj._meta.model_name,
            }
        )

        return context

    @staticmethod
    def _build_all_server_mappings(obj, active_server_key):
        """
        Build a list of all LibreNMS server mappings for the given device.

        Each entry describes one server<->ID mapping stored in the ``librenms_id``
        custom field:

        * ``server_key``    – the key as stored in the CF dict.
        * ``display_name``  – human-readable name from PLUGINS_CONFIG, or the key.
        * ``librenms_url``  – base URL of that server (``None`` when not configured).
        * ``device_id``     – the integer device ID on that server.
        * ``device_url``    – direct URL to the device page on that server (or ``None``).
        * ``is_configured`` – True when the server key exists in current plugin config.
        * ``is_active``     – True when this is the currently active server.

        Returns ``None`` for legacy bare-int format (no per-server info to show)
        and ``None`` when the CF is absent/invalid.
        """
        cf_value = obj.custom_field_data.get("librenms_id")
        if not isinstance(cf_value, dict) or not cf_value:
            return None

        plugins_cfg = getattr(django_settings, "PLUGINS_CONFIG", {}).get("netbox_librenms_plugin", {})
        servers_config = plugins_cfg.get("servers") or {}
        if not isinstance(servers_config, dict):
            servers_config = {}

        result = []
        for sk, did in cf_value.items():
            # Validate device ID — accept int or digit-string, skip bool/None/junk.
            if isinstance(did, bool) or did is None:
                continue
            if isinstance(did, str):
                if not did.isdigit():
                    continue
                did = int(did)
            elif not isinstance(did, int):
                continue
            srv_cfg = servers_config.get(sk)
            # Legacy single-server config: "default" key with no matching servers entry —
            # fall back to root-level librenms_url/display_name in plugins_cfg.
            if srv_cfg is None and sk == "default":
                legacy_url = plugins_cfg.get("librenms_url")
                if legacy_url:
                    srv_cfg = {
                        "librenms_url": legacy_url,
                        "display_name": plugins_cfg.get("display_name") or f"Default Server ({legacy_url})",
                    }
            is_configured = srv_cfg is not None
            # Treat malformed (non-dict) server config entries as unconfigured
            if srv_cfg is not None and not isinstance(srv_cfg, dict):
                srv_cfg = None
                is_configured = False
            librenms_url = srv_cfg.get("librenms_url") if srv_cfg else None
            display_name = (srv_cfg.get("display_name") or sk) if srv_cfg else sk
            device_url = f"{librenms_url}/device/device={did}/" if librenms_url else None
            result.append(
                {
                    "server_key": sk,
                    "display_name": display_name,
                    "librenms_url": librenms_url,
                    "device_id": did,
                    "device_url": device_url,
                    "is_configured": is_configured,
                    "is_active": sk == active_server_key,
                }
            )

        # Sort: active first, then configured, then orphaned
        result.sort(key=lambda e: 0 if e["is_active"] else (1 if e["is_configured"] else 2))
        return result or None

    def get_librenms_device_info(self, obj, request=None):
        """Get the LibreNMS device information for the given object."""
        found_in_librenms = False
        mismatched_device = False
        librenms_device_details = {
            "librenms_device_url": None,
            "librenms_device_hardware": "-",
            "librenms_device_serial": "-",
            "librenms_device_os": "-",
            "librenms_device_version": "-",
            "librenms_device_features": "-",
            "librenms_device_location": "-",
            "librenms_device_hardware_match": None,
            "vc_inventory_serials": [],
        }

        if self.librenms_id:
            success, device_info = self.librenms_api.get_device_info(self.librenms_id)
            if success and device_info:
                # Get NetBox device details
                netbox_ip = str(obj.primary_ip.address.ip).lower() if obj.primary_ip else None
                netbox_name = obj.name

                # Get LibreNMS device details
                librenms_sysname = device_info.get("sysName")
                librenms_ip = device_info.get("ip")

                # Extract new fields
                hardware = device_info.get("hardware", "-")
                serial = device_info.get("serial", "-")
                os_name = device_info.get("os", "-")
                version = device_info.get("version", "-")
                features = device_info.get("features", "-")

                # Try to match hardware to NetBox DeviceType
                hardware_match = match_librenms_hardware_to_device_type(hardware)

                # Compute resolved name using naming preferences
                resolved_name = None
                if request:
                    use_sysname, strip_domain = resolve_naming_preferences(request)
                    resolved_name = _determine_device_name(
                        device_info,
                        use_sysname=use_sysname,
                        strip_domain=strip_domain,
                        device_id=self.librenms_id,
                    )

                    # For VC members, generate the expected VC member name
                    if (
                        resolved_name
                        and hasattr(obj, "virtual_chassis")
                        and obj.virtual_chassis is not None
                        and obj.vc_position is not None
                    ):
                        resolved_name = _generate_vc_member_name(
                            resolved_name,
                            obj.vc_position,
                            serial=getattr(obj, "serial", None),
                        )

                # Update device details regardless of match
                librenms_device_details.update(
                    {
                        "librenms_device_url": f"{self.librenms_api.librenms_url}/device/device={self.librenms_id}/",
                        "librenms_device_hardware": hardware,
                        "librenms_device_serial": serial,
                        "librenms_device_os": os_name,
                        "librenms_device_version": version,
                        "librenms_device_features": features,
                        "librenms_device_location": device_info.get("location", "-"),
                        "librenms_device_ip": librenms_ip,
                        "sysName": librenms_sysname,
                        "resolved_name": resolved_name or librenms_sysname,
                        "librenms_device_hostname": device_info.get("hostname", "-"),
                        "librenms_device_hardware_match": hardware_match,
                    }
                )

                # For Virtual Chassis, fetch inventory
                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    vc_serials = self._get_vc_inventory_serials(obj)
                    librenms_device_details["vc_inventory_serials"] = vc_serials

                # Device was retrieved successfully via librenms_id — trust the ID
                found_in_librenms = True

                # Normalise the NetBox name once for comparisons
                netbox_name_norm = netbox_name.lower() if netbox_name else None
                if netbox_name_norm:
                    # Strip VC member suffix like " (1)" before comparing
                    netbox_name_norm = re.sub(r"\s*\(\d+\)$", "", netbox_name_norm)

                # Also strip the VC member naming pattern from settings
                # (e.g. "-M2", " (2)", "-SW3") to recover the base device name
                netbox_name_vc_stripped = None
                if netbox_name_norm:
                    netbox_name_vc_stripped = self._strip_vc_pattern(netbox_name_norm)

                # Collect all NetBox identity values to compare against
                netbox_dns_name = (
                    obj.primary_ip.dns_name.lower() if obj.primary_ip and obj.primary_ip.dns_name else None
                )
                netbox_identities = {
                    v
                    for v in [
                        netbox_name_norm,
                        netbox_ip,
                        netbox_dns_name,
                        netbox_name_vc_stripped,
                    ]
                    if v
                }

                # Collect all LibreNMS identity values, including
                # domain-stripped short names (e.g. "sw01.example.net" → "sw01")
                librenms_hostname = device_info.get("hostname")
                librenms_values = []
                for val in [librenms_sysname, librenms_hostname, librenms_ip]:
                    if val:
                        lower_val = val.lower()
                        librenms_values.append(lower_val)
                        # Add short name (strip domain) if it looks like an FQDN
                        short = lower_val.split(".")[0]
                        if short != lower_val:
                            librenms_values.append(short)
                librenms_identities = set(librenms_values)

                # A device is considered matched when ANY NetBox identity
                # appears in the LibreNMS identities.  This covers:
                #   - NetBox name == sysName or hostname
                #   - NetBox primary IP == LibreNMS hostname (added by IP)
                #   - NetBox DNS name == sysName or hostname (FQDN match)
                if netbox_identities & librenms_identities:
                    mismatched_device = False
                else:
                    mismatched_device = True

                librenms_device_details["netbox_dns_name"] = netbox_dns_name or "-"

        return {
            "found_in_librenms": found_in_librenms,
            "librenms_device_details": librenms_device_details,
            "mismatched_device": mismatched_device,
        }

    def get_interface_context(self, request, obj):
        """
        Get the context data for interface sync.
        Subclasses should override this method.
        """
        return None

    def get_cable_context(self, request, obj):
        """
        Get the context data for cable sync.
        Subclasses should override this method if applicable.
        """
        return None

    def get_ip_context(self, request, obj):
        """
        Get the context data for IP address sync.
        Subclasses should override this method.
        """
        return None

    def get_vlan_context(self, request, obj):
        """
        Get the context data for VLAN sync.
        Subclasses should override this method.
        """
        return None

    def get_module_context(self, request, obj):
        """
        Get the context data for module sync.
        Subclasses should override this method if applicable (e.g. VMs return None).
        """
        return None

    @staticmethod
    def _strip_vc_pattern(name):
        """
        Strip the VC member naming suffix from a device name.

        Uses the vc_member_name_pattern from LibreNMSSettings to build a
        regex that removes the suffix.  For example, with the default
        pattern ``-M{position}`` and name ``switch01-m2``, this returns
        ``switch01``.

        Returns the stripped name, or None if it equals the original
        (i.e. no suffix was found).
        """
        try:
            from netbox_librenms_plugin.models import LibreNMSSettings

            settings = LibreNMSSettings.objects.first()
            pattern = (
                settings.vc_member_name_pattern
                if settings and isinstance(settings.vc_member_name_pattern, str)
                else "-M{position}"
            )
            if not isinstance(pattern, str):
                pattern = "-M{position}"

            # Turn the pattern into a regex by replacing placeholders
            # {position} → \d+   {serial} → .+
            regex_suffix = re.escape(pattern)
            regex_suffix = regex_suffix.replace(re.escape("{position}"), r"\d+")
            regex_suffix = regex_suffix.replace(re.escape("{serial}"), r".+")

            stripped = re.sub(regex_suffix + "$", "", name, flags=re.IGNORECASE)
            return stripped if stripped != name else None
        except Exception:
            return None

    def _get_vc_inventory_serials(self, obj):
        """
        Fetch inventory serials for Virtual Chassis members.

        Args:
            obj: NetBox device object (VC member)

        Returns:
            list: [
                {
                    'description': 'Chassis component description',
                    'serial': 'serial number',
                    'model': 'model name',
                    'assigned_member': Device object or None (if serial matches existing assignment)
                }
            ]
        """
        success, inventory = self.librenms_api.get_device_inventory(self.librenms_id)
        if not success:
            return []

        # Filter for chassis components
        chassis_components = [item for item in inventory if item.get("entPhysicalClass") == "chassis"]

        # Get all VC members
        vc_members = obj.virtual_chassis.members.all()

        result = []
        for component in chassis_components:
            serial = component.get("entPhysicalSerialNum", "-")
            if not serial or serial == "-":
                continue

            # Check if this serial is already assigned to a VC member
            assigned_member = None
            for member in vc_members:
                if member.serial and member.serial.strip() == serial.strip():
                    assigned_member = member
                    break

            result.append(
                {
                    "description": component.get("entPhysicalDescr", "-"),
                    "serial": serial,
                    "model": component.get("entPhysicalModelName", "-"),
                    "assigned_member": assigned_member,
                }
            )

        return result

    def _get_platform_info(self, librenms_info, obj):
        """
        Get platform information from LibreNMS.

        Platform matching is based on OS name only (not version).
        Version is displayed separately as informational data.

        Args:
            librenms_info: Dictionary with LibreNMS device info
            obj: NetBox device object

        Returns:
            dict: {
                'netbox_platform': Platform object or None,
                'librenms_os': str (OS name),
                'librenms_version': str (OS version),
                'platform_exists': bool (whether OS platform exists in NetBox),
                'platform_name': str (OS name for platform matching),
                'matching_platform': Platform object or None
            }
        """
        from dcim.models import Platform

        librenms_os = librenms_info["librenms_device_details"].get("librenms_device_os", "-")
        librenms_version = librenms_info["librenms_device_details"].get("librenms_device_version", "-")

        # Platform name is just the OS (not OS + version)
        platform_name = librenms_os if librenms_os != "-" else None

        # Check if platform exists (match by OS name only)
        platform_exists = False
        matching_platform = None
        if platform_name:
            try:
                matching_platform = Platform.objects.get(name__iexact=platform_name)
                platform_exists = True
            except Platform.DoesNotExist:
                pass

        return {
            "netbox_platform": obj.platform,
            "librenms_os": librenms_os,
            "librenms_version": librenms_version,
            "platform_exists": platform_exists,
            "platform_name": platform_name,
            "matching_platform": matching_platform,
        }

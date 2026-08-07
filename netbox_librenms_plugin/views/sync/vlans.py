from urllib.parse import quote_plus

from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from ipam.models import VLAN, VLANGroup

from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


class SyncVLANsView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """
    Handle POST requests to create/update VLANs in NetBox from LibreNMS data.
    """

    required_object_permissions = {
        "POST": [
            # The owner device is resolved through a restricted queryset (see get_object), so
            # state that read here: a missing grant is an explicit 403, not a 404 at the lookup.
            ("view", Device),
            ("view", VLANGroup),
            ("add", VLAN),
            ("change", VLAN),
        ],
    }

    def post(self, request, object_type: str, object_id: int):
        """
        Process sync request.

        Expected POST data:
        - action: 'create_vlans'
        - select: List of VLAN IDs to create
        - vlan_group_{vid}: Per-row VLAN group selection
        """
        # Check both plugin write and NetBox object permissions
        if error := self.require_all_permissions("POST"):
            return error

        # Read server_key from POST so we use the exact server the user was viewing
        self._post_server_key = request.POST.get("server_key") or self.librenms_api.server_key

        obj = self.get_object(object_type, object_id)
        action = request.POST.get("action", "")

        if action == "create_vlans":
            return self._handle_create_vlans(request, obj, object_type, object_id)
        else:
            messages.error(request, "Invalid action specified.")
            return self._redirect(object_type, object_id)

    def get_object(self, object_type: str, object_id: int):
        """Get the target object (Device or VM)."""
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        raise Http404("Invalid object type.")

    def _redirect(self, object_type: str, object_id: int):
        """Redirect back to sync page with VLAN tab active."""
        url_name = (
            "dcim:device_librenms_sync"
            if object_type == "device"
            else "plugins:netbox_librenms_plugin:vm_librenms_sync"
        )
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        url = reverse(url_name, kwargs={"pk": object_id}) + "?tab=vlans"
        if server_key:
            url += f"&server_key={quote_plus(server_key)}"
        return redirect(url)

    def _handle_create_vlans(self, request, obj, object_type, object_id):
        """
        Handle creating selected VLANs in NetBox.

        Reads per-row VLAN group selections from form fields named 'vlan_group_{vid}'.
        """
        selected_vlans = request.POST.getlist("select")

        if not selected_vlans:
            messages.error(request, "No VLANs selected for creation.")
            return self._redirect(object_type, object_id)

        # Get cached VLAN data
        cached_vlans = cache.get(self.get_cache_key(obj, "vlans", self._post_server_key))
        if not cached_vlans:
            messages.error(request, "No cached VLAN data. Please refresh VLANs first.")
            return self._redirect(object_type, object_id)

        # Build lookup of LibreNMS VLANs by VID
        librenms_vlans = {str(v["vlan_vlan"]): v for v in cached_vlans}

        created_count = 0
        updated_count = 0
        skipped_count = 0
        group_missing_count = 0
        permission_skipped_count = 0
        ambiguous_count = 0
        changeable_vlans = self.restricted_queryset(VLAN, "change")

        with transaction.atomic():
            for vid_str in selected_vlans:
                try:
                    vid = int(vid_str)
                except ValueError:
                    continue

                vlan_data = librenms_vlans.get(vid_str)
                if not vlan_data:
                    continue

                # Get per-row VLAN group selection
                group_id_str = request.POST.get(f"vlan_group_{vid}", "")
                row_vlan_group = None
                if group_id_str:
                    try:
                        row_vlan_group = self.restricted_queryset(VLANGroup).get(pk=int(group_id_str))
                    except (ValueError, VLANGroup.DoesNotExist):
                        # A group was explicitly requested but doesn't exist (stale page or
                        # tampered id). Fail closed: do NOT fall back to a global VLAN, which
                        # would persist the VLAN in the wrong scope. Skip this VID and warn.
                        messages.error(
                            request,
                            f"VLAN {vid}: the selected VLAN group no longer exists; skipped to avoid "
                            "creating it in the wrong scope.",
                        )
                        # Count separately from genuine no-ops: this VID did NOT sync (it already
                        # emitted its own error), so it must not inflate the "N unchanged" summary
                        # and imply success.
                        group_missing_count += 1
                        continue

                librenms_name = vlan_data.get("vlan_name", f"VLAN {vid}")

                lookup = {"vid": vid, "group": row_vlan_group}
                try:
                    vlan = changeable_vlans.get(**lookup)
                    created = False
                except VLAN.MultipleObjectsReturned:
                    messages.error(
                        request,
                        f"VLAN {vid}: several VLANs match this VID and scope; skipped to avoid renaming the wrong one.",
                    )
                    ambiguous_count += 1
                    continue
                except VLAN.DoesNotExist:
                    # A matching row can exist outside the caller's constrained change grant.
                    # Do not let an unrestricted get_or_create return that row for mutation.
                    if VLAN.objects.filter(**lookup).exists():
                        messages.error(
                            request,
                            f"VLAN {vid}: an existing VLAN in this scope is outside your change permission; skipped.",
                        )
                        permission_skipped_count += 1
                        continue
                    try:
                        # A concurrent creator can win after the existence check. Keep the
                        # IntegrityError inside a savepoint so the outer batch can continue.
                        with transaction.atomic():
                            vlan = VLAN.objects.create(
                                **lookup,
                                name=librenms_name,
                                status="active",
                            )
                        created = True
                    except IntegrityError:
                        # Reuse the winner only when it is inside the caller's change scope.
                        try:
                            vlan = changeable_vlans.get(**lookup)
                            created = False
                        except VLAN.MultipleObjectsReturned:
                            messages.error(
                                request,
                                f"VLAN {vid}: several VLANs match this VID and scope; skipped to avoid "
                                "renaming the wrong one.",
                            )
                            ambiguous_count += 1
                            continue
                        except VLAN.DoesNotExist:
                            messages.error(
                                request,
                                f"VLAN {vid}: an existing VLAN in this scope is outside your change permission; skipped.",
                            )
                            permission_skipped_count += 1
                            continue

                if created:
                    created_count += 1
                elif vlan.name != librenms_name:
                    vlan.name = librenms_name
                    vlan.save(update_fields=["name"])
                    updated_count += 1
                else:
                    skipped_count += 1

        # Build summary message. created/updated/unchanged are all successful sync outcomes (an
        # "unchanged" VID exists and already matches — nothing to do). Group-missing skips are NOT:
        # each already emitted its own per-VID error, must never be folded into "N unchanged", and
        # must not ride under a "VLANs synced" success when they're the only outcome.
        parts = []
        if created_count > 0:
            parts.append(f"{created_count} created")
        if updated_count > 0:
            parts.append(f"{updated_count} updated")
        if skipped_count > 0:
            parts.append(f"{skipped_count} unchanged")

        if parts:
            if group_missing_count > 0:
                parts.append(f"{group_missing_count} skipped (VLAN group missing)")
            if permission_skipped_count > 0:
                parts.append(f"{permission_skipped_count} skipped (change permission missing)")
            if ambiguous_count > 0:
                parts.append(f"{ambiguous_count} skipped (VLAN match ambiguous)")
            messages.success(request, f"VLANs synced: {', '.join(parts)}.")
        elif group_missing_count > 0 or permission_skipped_count > 0 or ambiguous_count > 0:
            # Nothing actually synced. Do not claim success for rows rejected by a scope check.
            reasons = []
            if group_missing_count > 0:
                reasons.append(f"{group_missing_count} skipped (VLAN group missing)")
            if permission_skipped_count > 0:
                reasons.append(f"{permission_skipped_count} skipped (change permission missing)")
            if ambiguous_count > 0:
                reasons.append(f"{ambiguous_count} skipped (VLAN match ambiguous)")
            messages.warning(request, f"No VLANs synced: {', '.join(reasons)}.")
        else:
            messages.warning(request, "No VLANs were created or updated.")

        return self._redirect(object_type, object_id)

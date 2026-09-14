from dataclasses import dataclass, field
from urllib.parse import quote_plus

from dcim.models import Device
from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from ipam.models import VLAN, VLANGroup
from utilities.exceptions import PermissionsViolation

from netbox_librenms_plugin.sync_cache import (
    SyncTab,
    apply_request_cache_transition,
    schedule_request_cache_mutation,
)
from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

VLAN_CONFLICT_SIGNING_SALT = "netbox_librenms_plugin.vlan_conflict"


@dataclass
class _VLANSyncOutcome:
    """Collect the observable outcomes of one VLAN synchronization request."""

    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    group_missing_count: int = 0
    permission_skipped_count: int = 0
    ambiguous_count: int = 0
    concurrent_change_count: int = 0
    invalid_vid_count: int = 0
    invalid_name_count: int = 0
    add_permission_skipped_count: int = 0
    confirmation_error_count: int = 0
    conflicts: list[dict] = field(default_factory=list)

    def success_parts(self):
        """Return summary fragments for successful synchronization outcomes."""
        parts = []
        if self.created_count:
            parts.append(f"{self.created_count} created")
        if self.updated_count:
            parts.append(f"{self.updated_count} updated")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} unchanged")
        return parts

    def skip_reasons(self):
        """Return summary fragments for rejected synchronization outcomes."""
        reasons = []
        if self.group_missing_count:
            reasons.append(f"{self.group_missing_count} skipped (VLAN group missing)")
        if self.permission_skipped_count:
            reasons.append(f"{self.permission_skipped_count} skipped (change permission missing)")
        if self.add_permission_skipped_count:
            reasons.append(f"{self.add_permission_skipped_count} skipped (add permission constraints)")
        if self.ambiguous_count:
            reasons.append(f"{self.ambiguous_count} skipped (VLAN match ambiguous)")
        if self.concurrent_change_count:
            reasons.append(f"{self.concurrent_change_count} skipped (concurrent VLAN change)")
        if self.invalid_vid_count:
            reasons.append(f"{self.invalid_vid_count} skipped (invalid VLAN VID)")
        if self.invalid_name_count:
            reasons.append(f"{self.invalid_name_count} skipped (invalid VLAN name)")
        if self.confirmation_error_count:
            reasons.append(f"{self.confirmation_error_count} skipped (confirmation no longer current)")
        return reasons


def _acquire_global_vlan_locks(vids):
    """Lock global VLAN VIDs in stable order for the current transaction."""
    for vid in sorted(set(vids)):
        acquire_advisory_transaction_lock(f"netbox-librenms-plugin:global-vlan:{vid}")


class SyncVLANsView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Create or update NetBox VLANs from a LibreNMS source snapshot."""

    required_object_permissions = {
        "POST": [
            # The owner device is resolved through a restricted queryset (see get_object), so
            # state that read here: a missing grant is an explicit 403, not a 404 at the lookup.
            ("view", Device),
            ("add", VLAN),
            ("change", VLAN),
        ],
    }

    def _required_post_permissions(self, request):
        """Require VLANGroup access only when a selected row names a group."""
        permissions = list(type(self).required_object_permissions["POST"])
        selected_vids = request.POST.getlist("select")
        for vid_str in selected_vids:
            try:
                vid = int(vid_str)
            except ValueError:
                continue
            if request.POST.get(f"vlan_group_{vid}"):
                permissions.append(("view", VLANGroup))
                break
        return permissions

    def post(self, request, object_type: str, object_id: int):
        """
        Process sync request.

        Expected POST data:
        - action: 'create_vlans'
        - select: List of VLAN IDs to create
        - vlan_group_{vid}: Per-row VLAN group selection

        Args:
            request (HttpRequest): The request that contains the VLAN synchronization data.
            object_type (str): The type of the target object.
            object_id (int): The target object's ID.

        Returns:
            HttpResponse: A permission error, confirmation disclosure, or synchronization redirect.

        Raises:
            Http404: If the object type is invalid or no permitted device matches the object ID.

        """
        self.required_object_permissions = {"POST": self._required_post_permissions(request)}

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
        url = self.get_vlan_tab_url(object_type, object_id)
        if getattr(getattr(self, "request", None), "headers", {}).get("HX-Request") == "true":
            return HttpResponse("", headers={"HX-Redirect": url})
        return redirect(url)

    def get_vlan_tab_url(self, object_type: str, object_id: int) -> str:
        """Return the URL for the VLAN synchronization tab."""
        url_name = (
            "dcim:device_librenms_sync"
            if object_type == "device"
            else "plugins:netbox_librenms_plugin:vm_librenms_sync"
        )
        server_key = getattr(self, "_post_server_key", None) or self.librenms_api.server_key
        url = reverse(url_name, kwargs={"pk": object_id}) + "?tab=vlans"
        if server_key:
            url += f"&server_key={quote_plus(server_key)}"
        return url

    @staticmethod
    def _vlan_state(vlan: VLAN) -> dict:
        """Return the mutable VLAN state protected by a confirmation intent."""
        return {
            "vid": vlan.vid,
            "group_id": vlan.group_id,
            "name": vlan.name,
        }

    def _build_conflict(self, *, vlan, proposed_name, obj, object_type, server_key):
        """Return one VLAN rename disclosure with its signed confirmation intent."""
        payload = {
            "vid": vlan.vid,
            "object_type": object_type,
            "object_pk": str(obj.pk),
            "server_key": server_key,
            "target_group_id": vlan.group_id,
            "vlan_pk": str(vlan.pk),
            "vlan_state": self._vlan_state(vlan),
            "proposed_name": proposed_name,
        }
        return {
            "vid": vlan.vid,
            "group": str(vlan.group) if vlan.group is not None else "Global",
            "group_id": vlan.group_id,
            "current_name": vlan.name,
            "proposed_name": proposed_name,
            "intent": signing.dumps(payload, salt=VLAN_CONFLICT_SIGNING_SALT, compress=True),
        }

    def _render_conflicts(self, request, conflicts, obj, object_type, object_id):
        """Render VLAN rename disclosures for an HTMX or native form submission."""
        context = {
            "conflicts": conflicts,
            "object_type": object_type,
            "object": obj,
            "server_key": self._post_server_key,
            "cancel_url": self.get_vlan_tab_url(object_type, object_id),
        }
        if request.headers.get("HX-Request") == "true":
            template_name = "netbox_librenms_plugin/htmx/vlan_conflicts.html"
        else:
            context["full_page"] = True
            template_name = "netbox_librenms_plugin/vlan_conflicts_page.html"
        return render(request, template_name, context)

    def _load_force_intents(self, request, obj, object_type, server_key):
        """Validate submitted conflict intents and return them by canonical VID."""
        intents = {}
        errors = []
        for token in request.POST.getlist("conflict_intent"):
            try:
                payload = signing.loads(token, salt=VLAN_CONFLICT_SIGNING_SALT, max_age=3600)
                if not isinstance(payload, dict) or isinstance(payload.get("vid"), bool):
                    raise signing.BadSignature("Invalid VLAN conflict intent.")
                vid = str(int(payload["vid"]))
                if (
                    payload.get("object_type") != object_type
                    or str(payload.get("object_pk")) != str(obj.pk)
                    or payload.get("server_key") != server_key
                ):
                    raise signing.BadSignature("Conflict intent belongs to another sync context.")
                if vid in intents:
                    raise signing.BadSignature("Duplicate conflict intent.")
                intents[vid] = payload
            except (KeyError, TypeError, ValueError, signing.BadSignature, signing.SignatureExpired):
                errors.append("VLAN confirmation is invalid or has expired. Refresh the VLAN data and try again.")
        return intents, errors

    def _apply_confirmed_vlan_change(
        self,
        *,
        vid,
        payload,
        proposed_name,
        row_vlan_group,
        changeable_vlans,
    ):
        """Recheck and apply one signed VLAN rename while the target row is locked."""
        target_group_id = row_vlan_group.pk if row_vlan_group is not None else None
        if payload.get("target_group_id") != target_group_id or payload.get("proposed_name") != proposed_name:
            raise ValueError("The VLAN target changed after confirmation. Refresh the VLAN data and try again.")

        vlan = changeable_vlans.select_for_update(of=("self",)).filter(pk=payload.get("vlan_pk")).first()
        if vlan is None:
            raise ValueError("The existing VLAN is no longer available in your change scope.")
        if vlan.vid != vid or vlan.group_id != target_group_id or payload.get("vlan_state") != self._vlan_state(vlan):
            raise ValueError("The existing VLAN changed after confirmation. Refresh the VLAN data and try again.")
        if VLAN.objects.filter(vid=vid, group=row_vlan_group).exclude(pk=vlan.pk).exists():
            raise ValueError("The VLAN scope is now ambiguous. Refresh the VLAN data and try again.")

        vlan.name = proposed_name
        try:
            with transaction.atomic():
                vlan.save(update_fields=["name"])
        except IntegrityError as exc:
            raise ValueError(
                "The proposed VLAN name already exists in the selected group. Refresh the VLAN data and try again."
            ) from exc
        return vlan

    @staticmethod
    def _selected_vlan_ids(request, force_intents):
        """Return the canonical VIDs selected by the initial or confirmation form."""
        if request.POST.get("force_all"):
            return list(force_intents)
        if not request.POST.get("confirm_conflicts"):
            return request.POST.getlist("select")

        confirmed = set()
        for value in request.POST.getlist("force_conflict"):
            try:
                if not isinstance(value, bool):
                    confirmed.add(str(int(value)))
            except (TypeError, ValueError):
                continue
        return [vid for vid in force_intents if vid in confirmed]

    @staticmethod
    def _selected_global_vids(request, selected_vlans, librenms_vlans, force_intents):
        """Return selected global VIDs that require advisory transaction locks."""
        global_vids = []
        for vid_str in selected_vlans:
            try:
                vid = int(vid_str)
            except ValueError:
                continue
            force_payload = force_intents.get(str(vid))
            target_group_id = (
                force_payload.get("target_group_id")
                if force_payload is not None
                else request.POST.get(f"vlan_group_{vid}", "")
            )
            if str(vid) in librenms_vlans and not target_group_id:
                global_vids.append(vid)
        return global_vids

    def _resolve_row_vlan_group(self, request, vid, force_payload, outcome):
        """Resolve one selected VLAN group without falling back to global scope."""
        group_id = (
            force_payload.get("target_group_id")
            if force_payload is not None
            else request.POST.get(f"vlan_group_{vid}", "")
        )
        if group_id is None or group_id == "":
            return None, True

        try:
            return self.restricted_queryset(VLANGroup).get(pk=int(group_id)), True
        except (ValueError, VLANGroup.DoesNotExist):
            if force_payload is not None:
                messages.error(
                    request,
                    f"VLAN {vid}: the confirmed VLAN group is no longer available. "
                    "Refresh the VLAN data and try again.",
                )
                outcome.confirmation_error_count += 1
            else:
                messages.error(
                    request,
                    f"VLAN {vid}: the selected VLAN group no longer exists; skipped to avoid "
                    "creating it in the wrong scope.",
                )
                outcome.group_missing_count += 1
            return None, False

    @staticmethod
    def _validated_vlan_name(request, vid, vlan_data, outcome):
        """Validate one LibreNMS VID and name at the NetBox model boundary."""
        name = vlan_data.get("vlan_name", f"VLAN {vid}")
        try:
            VLAN._meta.get_field("vid").clean(vid, None)
        except ValidationError:
            messages.error(request, f"VLAN {vid}: the LibreNMS VID is invalid; skipped.")
            outcome.invalid_vid_count += 1
            return None
        try:
            VLAN._meta.get_field("name").clean(name, None)
        except ValidationError:
            messages.error(request, f"VLAN {vid}: the LibreNMS name is invalid; skipped.")
            outcome.invalid_name_count += 1
            return None
        return name

    def _apply_force_payload(
        self,
        request,
        *,
        vid,
        force_payload,
        proposed_name,
        row_vlan_group,
        changeable_vlans,
        outcome,
    ):
        """Apply a confirmed rename and report whether this row was consumed."""
        if force_payload is None:
            return False
        try:
            self._apply_confirmed_vlan_change(
                vid=vid,
                payload=force_payload,
                proposed_name=proposed_name,
                row_vlan_group=row_vlan_group,
                changeable_vlans=changeable_vlans,
            )
        except ValueError as exc:
            messages.error(request, f"VLAN {vid}: {exc}")
            outcome.confirmation_error_count += 1
        else:
            outcome.updated_count += 1
        return True

    @staticmethod
    def _create_vlan(request, *, lookup, vid, name, addable_vlans, outcome):
        """Create one VLAN or resolve the row created by a concurrent request."""
        try:
            with transaction.atomic():
                vlan = VLAN.objects.create(**lookup, name=name, status="active")
                if not addable_vlans.filter(pk=vlan.pk).exists():
                    raise PermissionsViolation()
            return vlan, True
        except PermissionsViolation:
            messages.error(
                request,
                f"VLAN {vid}: the new VLAN is outside your add permission constraints; skipped.",
            )
            outcome.add_permission_skipped_count += 1
            return None, False
        except IntegrityError:
            try:
                return VLAN.objects.get(**lookup), False
            except VLAN.MultipleObjectsReturned:
                messages.error(
                    request,
                    f"VLAN {vid}: several VLANs match this VID and scope; skipped to avoid renaming the wrong one.",
                )
                outcome.ambiguous_count += 1
            except VLAN.DoesNotExist:
                messages.error(
                    request,
                    f"VLAN {vid}: the VLAN could not be resolved after a concurrent change; skipped.",
                )
                outcome.concurrent_change_count += 1
            return None, False

    def _find_or_create_vlan(self, request, *, vid, row_vlan_group, name, addable_vlans, outcome):
        """Resolve one scoped VLAN or create it under the caller's constraints."""
        lookup = {"vid": vid, "group": row_vlan_group}
        try:
            return VLAN.objects.get(**lookup), False
        except VLAN.MultipleObjectsReturned:
            messages.error(
                request,
                f"VLAN {vid}: several VLANs match this VID and scope; skipped to avoid renaming the wrong one.",
            )
            outcome.ambiguous_count += 1
            return None, False
        except VLAN.DoesNotExist:
            return self._create_vlan(
                request,
                lookup=lookup,
                vid=vid,
                name=name,
                addable_vlans=addable_vlans,
                outcome=outcome,
            )

    def _lock_changeable_vlan(self, request, *, vlan, vid, created, changeable_vlans, outcome):
        """Authorize and lock an existing VLAN before comparing its mutable state."""
        if created:
            return vlan
        try:
            changeable_vlans.get(pk=vlan.pk)
        except VLAN.DoesNotExist:
            messages.error(
                request,
                f"VLAN {vid}: an existing VLAN in this scope is outside your change permission; skipped.",
            )
            outcome.permission_skipped_count += 1
            return None

        vlan = self.relock_scoped_row(VLAN, pk=vlan.pk)
        if vlan is None:
            messages.error(
                request,
                f"VLAN {vid}: the VLAN could not be resolved after a concurrent change; skipped.",
            )
            outcome.concurrent_change_count += 1
        return vlan

    def _sync_selected_vlan(
        self,
        request,
        *,
        vid_str,
        librenms_vlans,
        force_intents,
        addable_vlans,
        changeable_vlans,
        obj,
        object_type,
        outcome,
    ):
        """Process one selected LibreNMS VLAN inside the batch transaction."""
        try:
            vid = int(vid_str)
        except ValueError:
            return
        vlan_data = librenms_vlans.get(str(vid))
        if not vlan_data:
            return

        force_payload = force_intents.get(str(vid))
        row_vlan_group, group_is_valid = self._resolve_row_vlan_group(request, vid, force_payload, outcome)
        if not group_is_valid:
            return
        name = self._validated_vlan_name(request, vid, vlan_data, outcome)
        if name is None:
            return
        if self._apply_force_payload(
            request,
            vid=vid,
            force_payload=force_payload,
            proposed_name=name,
            row_vlan_group=row_vlan_group,
            changeable_vlans=changeable_vlans,
            outcome=outcome,
        ):
            return

        vlan, created = self._find_or_create_vlan(
            request,
            vid=vid,
            row_vlan_group=row_vlan_group,
            name=name,
            addable_vlans=addable_vlans,
            outcome=outcome,
        )
        if vlan is None:
            return
        vlan = self._lock_changeable_vlan(
            request,
            vlan=vlan,
            vid=vid,
            created=created,
            changeable_vlans=changeable_vlans,
            outcome=outcome,
        )
        if vlan is None:
            return

        if created:
            outcome.created_count += 1
        elif vlan.name != name:
            outcome.conflicts.append(
                self._build_conflict(
                    vlan=vlan,
                    proposed_name=name,
                    obj=obj,
                    object_type=object_type,
                    server_key=self._post_server_key,
                )
            )
        else:
            outcome.skipped_count += 1

    @staticmethod
    def _report_outcome(request, outcome):
        """Add one batch summary message without claiming rejected rows as successes."""
        parts = outcome.success_parts()
        skip_reasons = outcome.skip_reasons()
        if parts:
            messages.success(request, f"VLANs synced: {', '.join(parts + skip_reasons)}.")
        elif skip_reasons:
            messages.warning(request, f"No VLANs synced: {', '.join(skip_reasons)}.")
        elif not outcome.conflicts:
            messages.warning(request, "No VLANs were created or updated.")

    def _handle_create_vlans(self, request, obj, object_type, object_id):
        """
        Create selected VLANs and disclose name changes that require confirmation.

        Reads per-row VLAN group selections from form fields named 'vlan_group_{vid}'.

        Args:
            request (HttpRequest): The request that contains the selected VLAN data.
            obj (Device): The device that owns the synchronization request.
            object_type (str): The type of the target object.
            object_id (int): The target object's ID.

        Returns:
            HttpResponse: A confirmation disclosure or redirect to the VLAN synchronization page.

        """
        force_intents, intent_errors = self._load_force_intents(
            request,
            obj,
            object_type,
            self._post_server_key,
        )
        selected_vlans = self._selected_vlan_ids(request, force_intents)
        if not selected_vlans:
            if intent_errors:
                for error in dict.fromkeys(intent_errors):
                    messages.error(request, error)
            else:
                messages.error(request, "No VLAN changes selected.")
            return self._redirect(object_type, object_id)

        cached_vlans = cache.get(self.get_cache_key(obj, "vlans", self._post_server_key))
        if not cached_vlans:
            messages.error(request, "No cached VLAN data. Please refresh VLANs first.")
            return self._redirect(object_type, object_id)

        librenms_vlans = {str(v["vlan_vlan"]): v for v in cached_vlans}
        outcome = _VLANSyncOutcome()
        addable_vlans = self.restricted_queryset(VLAN, "add")
        changeable_vlans = self.restricted_queryset(VLAN, "change")

        with transaction.atomic():
            global_vids = self._selected_global_vids(request, selected_vlans, librenms_vlans, force_intents)
            _acquire_global_vlan_locks(global_vids)
            for vid_str in selected_vlans:
                self._sync_selected_vlan(
                    request,
                    vid_str=vid_str,
                    librenms_vlans=librenms_vlans,
                    force_intents=force_intents,
                    addable_vlans=addable_vlans,
                    changeable_vlans=changeable_vlans,
                    obj=obj,
                    object_type=object_type,
                    outcome=outcome,
                )

        self._report_outcome(request, outcome)

        for error in dict.fromkeys(intent_errors):
            messages.error(request, error)

        if outcome.created_count or outcome.updated_count:
            schedule_request_cache_mutation(
                request,
                obj,
                SyncTab.VLANS,
                self._post_server_key,
                source_fragment_required=bool(outcome.conflicts),
            )
        if outcome.conflicts:
            return apply_request_cache_transition(
                request, self._render_conflicts(request, outcome.conflicts, obj, object_type, object_id)
            )
        return apply_request_cache_transition(request, self._redirect(object_type, object_id))

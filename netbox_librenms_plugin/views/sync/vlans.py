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
        vlan.save(update_fields=["name"])
        return vlan

    def _handle_create_vlans(self, request, obj, object_type, object_id):  # noqa: C901
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
        if request.POST.get("force_all"):
            selected_vlans = list(force_intents)
        elif request.POST.get("confirm_conflicts"):
            confirmed = set()
            for value in request.POST.getlist("force_conflict"):
                try:
                    if not isinstance(value, bool):
                        confirmed.add(str(int(value)))
                except (TypeError, ValueError):
                    continue
            selected_vlans = [vid for vid in force_intents if vid in confirmed]
        else:
            selected_vlans = request.POST.getlist("select")

        if not selected_vlans:
            if intent_errors:
                for error in dict.fromkeys(intent_errors):
                    messages.error(request, error)
            else:
                messages.error(request, "No VLAN changes selected.")
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
        concurrent_change_count = 0
        invalid_vid_count = 0
        invalid_name_count = 0
        add_permission_skipped_count = 0
        confirmation_error_count = 0
        conflicts = []
        addable_vlans = self.restricted_queryset(VLAN, "add")
        changeable_vlans = self.restricted_queryset(VLAN, "change")

        with transaction.atomic():
            # A global VLAN has no parent row to lock, and PostgreSQL treats NULL
            # group values as distinct in the VLAN uniqueness constraint. Lock all
            # selected global VIDs before lookup so concurrent batches cannot both
            # observe a missing row. Stable ordering prevents cross-batch deadlocks.
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
            _acquire_global_vlan_locks(global_vids)

            for vid_str in selected_vlans:
                try:
                    vid = int(vid_str)
                except ValueError:
                    continue

                vlan_data = librenms_vlans.get(str(vid))
                if not vlan_data:
                    continue

                # Get per-row VLAN group selection
                force_payload = force_intents.get(str(vid))
                group_id_str = (
                    force_payload.get("target_group_id")
                    if force_payload is not None
                    else request.POST.get(f"vlan_group_{vid}", "")
                )
                row_vlan_group = None
                if group_id_str is not None and group_id_str != "":
                    try:
                        row_vlan_group = self.restricted_queryset(VLANGroup).get(pk=int(group_id_str))
                    except (ValueError, VLANGroup.DoesNotExist):
                        if force_payload is not None:
                            messages.error(
                                request,
                                f"VLAN {vid}: the confirmed VLAN group is no longer available. "
                                "Refresh the VLAN data and try again.",
                            )
                            confirmation_error_count += 1
                            continue
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
                try:
                    VLAN._meta.get_field("vid").clean(vid, None)
                except ValidationError:
                    messages.error(request, f"VLAN {vid}: the LibreNMS VID is invalid; skipped.")
                    invalid_vid_count += 1
                    continue
                try:
                    VLAN._meta.get_field("name").clean(librenms_name, None)
                except ValidationError:
                    messages.error(request, f"VLAN {vid}: the LibreNMS name is invalid; skipped.")
                    invalid_name_count += 1
                    continue

                if force_payload is not None:
                    try:
                        self._apply_confirmed_vlan_change(
                            vid=vid,
                            payload=force_payload,
                            proposed_name=librenms_name,
                            row_vlan_group=row_vlan_group,
                            changeable_vlans=changeable_vlans,
                        )
                    except ValueError as exc:
                        messages.error(request, f"VLAN {vid}: {exc}")
                        confirmation_error_count += 1
                        continue
                    updated_count += 1
                    continue

                lookup = {"vid": vid, "group": row_vlan_group}
                try:
                    # Resolve the catalog match before applying the user's change scope. A
                    # constrained grant must not make duplicate rows look like one safe match.
                    vlan = VLAN.objects.get(**lookup)
                    created = False
                except VLAN.MultipleObjectsReturned:
                    messages.error(
                        request,
                        f"VLAN {vid}: several VLANs match this VID and scope; skipped to avoid renaming the wrong one.",
                    )
                    ambiguous_count += 1
                    continue
                except VLAN.DoesNotExist:
                    try:
                        # A concurrent creator can win after the initial lookup. Keep the
                        # IntegrityError inside a savepoint so the outer batch can continue.
                        with transaction.atomic():
                            vlan = VLAN.objects.create(
                                **lookup,
                                name=librenms_name,
                                status="active",
                            )
                            if not addable_vlans.filter(pk=vlan.pk).exists():
                                raise PermissionsViolation()
                        created = True
                    except PermissionsViolation:
                        messages.error(
                            request,
                            f"VLAN {vid}: the new VLAN is outside your add permission constraints; skipped.",
                        )
                        add_permission_skipped_count += 1
                        continue
                    except IntegrityError:
                        try:
                            vlan = VLAN.objects.get(**lookup)
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
                                f"VLAN {vid}: the VLAN could not be resolved after a concurrent change; skipped.",
                            )
                            concurrent_change_count += 1
                            continue

                if not created:
                    try:
                        vlan = changeable_vlans.get(pk=vlan.pk)
                    except VLAN.DoesNotExist:
                        messages.error(
                            request,
                            f"VLAN {vid}: an existing VLAN in this scope is outside your change permission; skipped.",
                        )
                        permission_skipped_count += 1
                        continue

                    # The advisory lock above covers global VIDs only, so a grouped row is still
                    # unprotected between this scope check and the save below.
                    vlan = self.relock_scoped_row(VLAN, pk=vlan.pk)
                    if vlan is None:
                        messages.error(
                            request,
                            f"VLAN {vid}: the VLAN could not be resolved after a concurrent change; skipped.",
                        )
                        concurrent_change_count += 1
                        continue

                if created:
                    created_count += 1
                elif vlan.name != librenms_name:
                    conflicts.append(
                        self._build_conflict(
                            vlan=vlan,
                            proposed_name=librenms_name,
                            obj=obj,
                            object_type=object_type,
                            server_key=self._post_server_key,
                        )
                    )
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

        skip_reasons = []
        if group_missing_count > 0:
            skip_reasons.append(f"{group_missing_count} skipped (VLAN group missing)")
        if permission_skipped_count > 0:
            skip_reasons.append(f"{permission_skipped_count} skipped (change permission missing)")
        if add_permission_skipped_count > 0:
            skip_reasons.append(f"{add_permission_skipped_count} skipped (add permission constraints)")
        if ambiguous_count > 0:
            skip_reasons.append(f"{ambiguous_count} skipped (VLAN match ambiguous)")
        if concurrent_change_count > 0:
            skip_reasons.append(f"{concurrent_change_count} skipped (concurrent VLAN change)")
        if invalid_vid_count > 0:
            skip_reasons.append(f"{invalid_vid_count} skipped (invalid VLAN VID)")
        if invalid_name_count > 0:
            skip_reasons.append(f"{invalid_name_count} skipped (invalid VLAN name)")
        if confirmation_error_count > 0:
            skip_reasons.append(f"{confirmation_error_count} skipped (confirmation no longer current)")

        if parts:
            messages.success(request, f"VLANs synced: {', '.join(parts + skip_reasons)}.")
        elif skip_reasons:
            # Nothing actually synced. Do not claim success for rows rejected by a scope check.
            messages.warning(request, f"No VLANs synced: {', '.join(skip_reasons)}.")
        elif not conflicts:
            messages.warning(request, "No VLANs were created or updated.")

        for error in dict.fromkeys(intent_errors):
            messages.error(request, error)

        if created_count or updated_count:
            schedule_request_cache_mutation(
                request,
                obj,
                SyncTab.VLANS,
                self._post_server_key,
                source_fragment_required=bool(conflicts),
            )
        if conflicts:
            return apply_request_cache_transition(
                request,
                self._render_conflicts(request, conflicts, obj, object_type, object_id),
            )
        return apply_request_cache_transition(request, self._redirect(object_type, object_id))

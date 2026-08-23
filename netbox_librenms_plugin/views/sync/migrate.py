"""
Stage 2b: per-row "Move to winner" actions on a donor device whose
``librenms_id[server_key]`` carries a ``_migrated_to`` marker.

Each view validates the marker, looks up the winner Device, runs an
atomic move under ``select_for_update`` ordering by primary key (to avoid
cross-merge deadlocks), and returns either an HTMX-friendly partial
response or a plain redirect.

Permissions: each view requires plugin write permission AND the
appropriate NetBox model permission on the object being moved.
"""

import logging
from urllib.parse import quote_plus

from dcim.models import CableTermination, Device, Interface
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import get_script_prefix, reverse
from django.views import View
from ipam.models import IPAddress

from netbox_librenms_plugin.sync_cache import (
    SyncTab,
    apply_request_cache_transition,
    claim_sync_subjects,
    schedule_request_cache_mutation,
    sync_subject_key,
)
from netbox_librenms_plugin.utils import (
    DEVICE_IP_FK_FIELDS,
    DEVICE_IP_FK_LABELS,
    coerce_librenms_id,
    get_migrated_to_marker,
    set_device_ip_fk,
    validation_error_detail,
)
from netbox_librenms_plugin.views.imports.actions import _htmx_error_response
from netbox_librenms_plugin.views.mixins import (
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    SyncSubjectClaimMixin,
    relock_scoped_row,
    resolve_configured_server_key,
    validated_referer,
)

logger = logging.getLogger(__name__)


def _parse_marker_winner_pk(device_id):
    """
    Parse a ``_migrated_to`` marker's ``device_id`` to a positive int pk, or None if invalid.

    A marker id from a tampered/legacy source may arrive as a string: accept ONLY a plain digit
    string (``str.isdecimal()``) so whitespace/sign/decimal forms (``" 5 "``, ``"+5"``, ``"1.9"``)
    fail closed — deliberately stricter than the raw ``int()`` :func:`~netbox_librenms_plugin.utils.
    coerce_librenms_id` applies to LibreNMS ids. The int/positive-value coercion itself is delegated
    to ``coerce_librenms_id`` (bool rejected, positive int only) so that rule lives in one place and
    can't drift from the rest of the plugin's id handling. Shared by :func:`_resolve_winner_for_donor`
    and :func:`_winner_unavailable_reason` so the two can't disagree on a parseable winner id.
    """
    if isinstance(device_id, str) and not device_id.isdecimal():
        return None
    return coerce_librenms_id(device_id)


def _resolve_winner_for_donor(donor, server_key="default"):
    """
    Resolve the migration winner device recorded on a donor.

    ``marker`` is the dict written by :func:`mark_librenms_migrated`.

    Args:
        donor: The donor device whose ``_migrated_to`` marker is read.
        server_key (str): The LibreNMS server key the marker is namespaced under.

    Returns:
        tuple: ``(winner, marker)`` when the marker is valid and the winner exists;
            ``(None, None)`` when no marker is present; ``(None, marker)`` when the
            marker is stale (winner deleted, unparseable ``device_id``, or
            self-pointing) so callers can distinguish "no marker" from "stale marker".
            Use :func:`_winner_unavailable_reason` to tell a deleted winner from a
            corrupt marker.
    """
    marker = get_migrated_to_marker(donor, server_key)
    if not marker:
        return None, None
    winner_pk = _parse_marker_winner_pk(marker.get("device_id"))
    if winner_pk is None:
        return None, marker
    winner = Device.objects.filter(pk=winner_pk).first()
    if winner is None:
        return None, marker
    # A self-pointing marker (winner == donor) is corrupt: it would make the move
    # operate on the same Device as both donor and winner (e.g. reconciling a device's
    # IP FKs against itself). Fail closed as a stale marker rather than resolve it.
    if winner.pk == donor.pk:
        return None, marker
    return winner, marker


def _winner_unavailable_reason(donor, marker):
    """
    Classify a present ``_migrated_to`` marker that resolved to no winner.

    Called only on the ``(None, marker)`` path of :func:`_resolve_winner_for_donor`, so a
    well-formed, non-self ``device_id`` here means the winner row was deleted; anything else
    is a corrupt marker. Mirrors the staleness checks in :func:`_resolve_winner_for_donor`.

    Returns:
        str: ``"deleted"`` (well-formed positive id, winner row gone) or ``"corrupt"``
            (bool/unparseable/non-positive ``device_id``, or self-pointing).
    """
    # A parseable positive pk (shared parser rejects bool/numeric-like/non-positive) that isn't
    # the donor itself means the winner row was deleted; anything else is a corrupt marker.
    winner_pk = _parse_marker_winner_pk(marker.get("device_id"))
    if winner_pk is None or winner_pk == donor.pk:
        return "corrupt"
    return "deleted"


def _fail_winner_unavailable(view, request, donor, marker):
    """Render the right error for a present-but-unresolvable migration marker."""
    if _winner_unavailable_reason(donor, marker) == "corrupt":
        return view._fail(
            request,
            "Donor migration marker is stale or corrupt; clear it and re-run the migration.",
            status=409,
        )
    return view._fail(request, "Winner device no longer exists.", status=410)


def _server_key_from_request(request, default_factory=None):
    """
    Extract the LibreNMS server key from the POST body (form field).

    Pass ``default_factory=lambda: self.librenms_api.server_key`` from views that
    have API access so the fallback matches the active server's namespace rather than
    the literal ``"default"``. The factory is a callable (not the resolved value) so
    the API — and the DB lookup it performs — is only touched when the POST body omits
    ``server_key``; the common path where the form supplies the key never instantiates
    the API.

    Args:
        request: The current HTTP request (its POST body is read).
        default_factory: Optional callable returning the fallback server key when the
            POST body omits ``server_key``.

    Returns:
        str: The resolved server key, or ``"default"`` when neither the POST body nor
            the factory supplies one.
    """
    sk = request.POST.get("server_key")
    if isinstance(sk, str):
        # Strip whitespace so a padded-but-valid key (" prod ") resolves instead of breaking
        # migrated-marker lookup / server-scoped redirects; a blank/whitespace key falls through.
        sk = sk.strip()
        if sk:
            return sk
    if default_factory is not None:
        resolved = default_factory()
        if isinstance(resolved, str) and resolved:
            return resolved
    return "default"


def _sync_tab_url(device_pk, tab, server_key):
    """
    Build the device's LibreNMS sync URL for a tab, preserving server_key.

    Used as the non-HTMX fallback target when a plain POST carries no usable Referer:
    without this the redirect would drop the active sync tab and server context,
    bouncing the user to the bare app root instead of the donor's "Migrated to ..."
    view they acted from.

    Args:
        device_pk: The device primary key the sync URL is built for.
        tab (str): The sync tab to land on.
        server_key (str): The active server key; appended only when it matches a
            configured server (a stale/tampered key is dropped).

    Returns:
        str: The sync URL with ``tab`` (and a validated ``server_key``) query params.
    """
    url = f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[device_pk])}?tab={tab}"
    # Re-source the key from the trusted config rather than reflecting the raw POST value: append it
    # only when it matches a configured server (a stale/tampered key resolves to None and is dropped).
    # The allowlist policy is shared with device_fields._sync_redirect via resolve_configured_server_key.
    configured_key = resolve_configured_server_key(server_key)
    if configured_key:
        url += f"&server_key={quote_plus(configured_key)}"
    return url


def _safe_referer(request, fallback=None):
    """
    Return the request's validated ``Referer``, or a safe fallback.

    ``Referer`` is a client-controlled header, so it must be validated against the
    current host before being used as a redirect target — trusting it blindly is an
    open-redirect vector.

    Args:
        request: The current HTTP request whose ``Referer`` is validated.
        fallback: An internal, server-built URL (see :func:`_sync_tab_url`) used as-is
            when the Referer is missing/untrusted. When None, the app mount path is
            used (not literal ``"/"``) so prefixed deployments land on the NetBox app
            root rather than the site root.

    Returns:
        str: The validated Referer, or *fallback* / the app mount path.
    """
    # Delegate the CWE-601 Referer check to the shared barrier so this and _get_safe_redirect_url
    # can't drift; this endpoint keeps its own fallback (a server-built sync-tab URL, else prefix).
    if referer := validated_referer(request):
        return referer
    return fallback or get_script_prefix()


def _schedule_winner_cache_mutation(request, winner, source_tab, server_key):
    """Schedule cleanup of the winner's mapped snapshots after a move."""
    schedule_request_cache_mutation(request, winner, source_tab, server_key)


def _hx_response(request, message, level=messages.SUCCESS, *, status=200, fallback_url=None):
    """
    Build the common HTMX (or redirect) response after a successful move.

    Queues a Django messages flash and emits the ``HX-Refresh`` header so the sync
    page re-renders with the row gone. For non-HTMX requests, queues the message and
    redirects to the validated Referer, falling back to *fallback_url* (the donor's
    sync tab) so the server/tab context survives a missing Referer.

    Args:
        request: The current HTTP request.
        message (str): The flash message to queue.
        level: The Django messages level (default ``messages.SUCCESS``).
        status (int): The HTTP status for the HTMX response.
        fallback_url: The non-HTMX redirect fallback (the donor's sync tab).

    Returns:
        HttpResponse: An ``HX-Refresh`` response for HTMX requests, or a redirect to
            the validated Referer / fallback otherwise.
    """
    messages.add_message(request, level, message)
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=status, headers={"HX-Refresh": "true"})
    else:
        response = redirect(_safe_referer(request, fallback_url))
    return apply_request_cache_transition(request, response)


def _reconcile_donor_device_ip_fks(donor, winner):
    """
    Keep the donor valid after a move re-homes an interface/IP onto the winner.

    NetBox requires ``Device.primary_ip4``/``primary_ip6``/``oob_ip`` to reference an
    address assigned to one of that device's *own* interfaces. Moving an interface
    (with its addresses) or an IP to the winner can leave one of those donor FKs
    pointing at an address now living on a winner-owned interface — an invalid state
    that surfaces the next time the donor is full_clean()'d. For each such dangling FK,
    transfer the designation to the winner when its slot is free, otherwise clear it on
    the donor. Only the touched FK column is saved (``update_fields``) so we don't trip
    ``full_clean()`` on pre-existing unrelated inconsistencies, mirroring
    :class:`TransferDeviceIPView`.

    Must run inside the caller's ``transaction.atomic()`` with both devices already
    locked.

    Args:
        donor: The donor device whose dangling IP FKs are reconciled.
        winner: The device that received the moved interface/IP.

    Returns:
        list: Human-readable notes describing what was reconciled (for the response
            message).
    """
    notes = []
    # Drive the loop off utils.DEVICE_IP_FK_FIELDS — the canonical list set_device_ip_fk()
    # validates against — so a fourth device IP FK added there is reconciled here too instead of
    # being silently skipped by a stale hardcoded copy. The labels are display-only.
    for field in DEVICE_IP_FK_FIELDS:
        human = DEVICE_IP_FK_LABELS.get(field, field)
        donor_ip_id = getattr(donor, f"{field}_id", None)
        # Real FK columns are plain ints; the isinstance guard also makes this a clean no-op
        # against the mock-based unit tests (a MagicMock id is not an int).
        if not isinstance(donor_ip_id, int) or isinstance(donor_ip_id, bool):
            continue
        # Lock the address row before reading its assignment so a concurrent reassignment can't
        # change assigned_object between this check and the FK save.
        ip = IPAddress.objects.select_for_update().filter(pk=donor_ip_id).first()
        if ip is None:
            continue
        assigned = ip.assigned_object
        # Only reconcile when the address now sits on a winner-owned interface (the move just
        # detached it from the donor). A non-Interface assignment (e.g. a VMInterface) has no
        # device_id and is left untouched.
        if not isinstance(assigned, Interface):
            continue
        # Locking the IPAddress row alone doesn't stabilize the owning interface's device_id:
        # this helper also reconciles pre-existing dangling FKs, so a concurrent interface move
        # could flip device_id between this check and winner.save(). Lock the owning Interface
        # and re-read device_id from the locked row so the winner can't end up owning an address
        # that has since moved away.
        locked_iface = relock_scoped_row(Interface, pk=assigned.pk)
        if locked_iface is None or locked_iface.device_id != winner.pk:
            continue
        # Refresh the cached GenericForeignKey on `ip` to the freshly locked interface:
        # set_device_ip_fk()'s internal ownership check re-reads ip.assigned_object, and the
        # GFK cache still holds the pre-lock snapshot (the FK id fields never changed, so
        # Django won't re-query). A move ONTO the winner landing between the read above and
        # the lock would otherwise pass the locked-row gate here and then be spuriously
        # rejected against the stale device_id — the freshen-after-lock pattern
        # TransferDeviceIPView already uses.
        ip.assigned_object = locked_iface
        if getattr(winner, f"{field}_id", None) is None:
            # device.primary_ip4/6 / oob_ip are UNIQUE per address, so the donor must release the
            # FK *before* the winner claims it — saving the winner first while the donor still
            # holds the same address violates the unique constraint. set_device_ip_fk() enforces
            # the "address must live on the target device's own interface" invariant the
            # update_fields save would otherwise skip (the locked-row check above is the lock-
            # stabilized first line; the helper is the shared backstop).
            set_device_ip_fk(donor, field, None)
            set_device_ip_fk(winner, field, ip)
            notes.append(f"transferred donor {human} to {winner.name}")
        else:
            set_device_ip_fk(donor, field, None)
            notes.append(f"cleared donor {human} (winner already had a {human})")
    return notes


class _BaseMoveToWinnerView(
    LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, SyncSubjectClaimMixin, View
):
    """Shared plumbing for the per-resource move endpoints."""

    # Per-request, server-built non-HTMX redirect fallback (donor sync tab +
    # server_key). Set by each post() once the donor device and server_key are
    # known; until then it stays None and _fail/_hx_response fall back to the app
    # mount path (there is no donor device to redirect to yet).
    _fallback_url = None

    def _gate(self, request):
        """
        Apply the plugin-write + object-perm gate.

        Args:
            request: The current HTTP request being authorized.

        Returns:
            A response on failure (which the caller must return verbatim), or None on
            success.
        """
        resp = self.require_all_permissions("POST")
        if resp is not None:
            return resp
        return None

    def _fail(self, request, msg, *, status=400):
        """
        Return an error response.

        For HTMX requests this returns HTTP 200 with an out-of-band swap into
        ``#django-messages`` so the toast renders through NetBox's Bootstrap pipeline;
        ``HX-Reswap: none`` prevents the primary swap target from being overwritten.
        For non-HTMX requests it adds a Django error message and redirects to the
        validated Referer or the app mount path.

        Args:
            request: The current HTTP request.
            msg (str): The error message to surface.
            status (int): The HTTP status for non-HTMX responses; ignored for HTMX
                responses, where errors are always signalled via the OOB toast.

        Returns:
            HttpResponse: The OOB-toast response (HTMX) or a redirect (non-HTMX).
        """
        if request.headers.get("HX-Request"):
            # Reuse the shared OOB-toast builder (same #django-messages container, toast classes and
            # HX-Reswap:none) so move-to-winner error toasts can't drift from the import flow's markup.
            return _htmx_error_response(msg)
        messages.error(request, msg)
        return redirect(_safe_referer(request, self._fallback_url))

    def _resolve_winner_or_fail(self, request, donor, server_key):
        """
        Resolve the migration winner for *donor*, or return an early failure response.

        Returns ``(winner, None)`` when the donor is marked migrated and its winner resolves;
        otherwise ``(None, error_response)`` — the caller returns the response verbatim. Shared by
        the three move endpoints so the "not marked" / winner-unavailable branches can't drift.

        Args:
            request: The current HTTP request.
            donor: The donor device whose migration marker is resolved.
            server_key (str): The LibreNMS server key the marker is namespaced under.

        Returns:
            tuple: ``(winner, None)`` on success, or ``(None, error_response)`` on failure.
        """
        winner, marker = _resolve_winner_for_donor(donor, server_key)
        if marker is None:
            return None, self._fail(request, "Donor device is not marked as migrated.", status=409)
        if winner is None:
            return None, _fail_winner_unavailable(self, request, donor, marker)
        # Both device rows are DERIVED — the donor from the moved interface/IP, the winner from the
        # donor's marker — and every move writes device-level IP FKs on both. The model-level gate
        # can't see them, so authorize each against the restricted change queryset before any write.
        device_pks = {donor.pk, winner.pk}
        authorized = set(
            self.restricted_queryset(Device, "change").filter(pk__in=device_pks).values_list("pk", flat=True)
        )
        if authorized != device_pks:
            return None, self._fail(
                request,
                "You do not have permission to change every device this move touches "
                "(the donor and its migration winner).",
                status=403,
            )
        return winner, None

    def _lock_donor_winner_and_reverify(self, request, donor, winner, server_key):
        """
        Lock donor+winner by pk order and re-verify the migration marker under the lock.

        Must be called inside a ``transaction.atomic()`` block. Locks both devices in pk order
        (deadlock-safe), then re-resolves the winner under the lock so a concurrent request that
        cleared or repointed ``_migrated_to`` between the unlocked resolve and acquiring these row
        locks can't move the resource to a stale winner (TOCTOU). Shared by the three move endpoints
        so this concurrency guard lives in one place.

        Args:
            request: The current HTTP request.
            donor: The donor device (re-read under the lock).
            winner: The winner device resolved before the lock (re-verified under it).
            server_key (str): The LibreNMS server key the marker is namespaced under.

        Returns:
            tuple: ``(locked_donor, locked_winner, None)`` on success, or ``(None, None,
                error_response)`` when a device was deleted or the marker changed concurrently.
        """
        ordered = sorted({donor.pk, winner.pk})
        locked = {
            d.pk: d
            for d in self.restricted_queryset(Device, "change")
            .select_for_update(of=("self",))
            .filter(pk__in=ordered)
            .order_by("pk")
        }
        donor = locked.get(donor.pk)
        winner = locked.get(winner.pk)
        if donor is None or winner is None:
            return None, None, self._fail(request, "Device was deleted concurrently.", status=410)
        relocked_winner, relocked_marker = _resolve_winner_for_donor(donor, server_key)
        if relocked_marker is None or relocked_winner is None or relocked_winner.pk != winner.pk:
            return (
                None,
                None,
                self._fail(request, "Donor migration changed concurrently; refresh and retry.", status=409),
            )
        return donor, winner, None


def _refresh_cable_termination_caches(interface):
    """
    Re-save the interface's cable terminations after a device move.

    ``CableTermination`` denormalizes ``_device``/``_rack``/``_location``/``_site``
    for cable-list filtering, and recomputes them only inside its own ``save()``
    (``cache_related_objects()``) — nothing tracks an Interface changing device
    (the same upstream gap as netbox#9788 for device site moves). Without this,
    the moved interface's cables keep filtering under the DONOR's cable lists.
    """
    ct_type = ContentType.objects.get_for_model(Interface)
    for termination in CableTermination.objects.filter(termination_type=ct_type, termination_id=interface.pk):
        termination.save()


def _donor_side_dependents(interface, donor):
    """
    Collect (and lock) donor interfaces that reference *interface* transitively.

    Interfaces left on the donor whose ``lag``/``parent``/``bridge`` points at a
    moved row would persist as cross-device relationships NetBox itself refuses
    to create — surfacing only as a validation error on their NEXT save. The
    move therefore carries the whole family. BFS order (level by level) so a
    parent is always moved before its own dependents ``full_clean()``.
    """
    dependents = []
    seen = {interface.pk}
    frontier = [interface]
    while frontier:
        layer = list(
            Interface.objects.select_for_update()
            .filter(device=donor)
            .filter(Q(lag__in=frontier) | Q(parent__in=frontier) | Q(bridge__in=frontier))
            .exclude(pk__in=seen)
            .order_by("pk")
        )
        seen.update(dep.pk for dep in layer)
        dependents.extend(layer)
        frontier = layer
    return dependents


class MoveInterfaceToWinnerView(_BaseMoveToWinnerView):
    """
    Reassign ``Interface.device`` from donor to winner.

    IP-address attachments, MAC objects, and VLAN tag config all point at
    the Interface row by FK, so they follow the move automatically; cable
    terminations follow by FK too, but their denormalized device/rack/
    location/site cache columns are explicitly refreshed (see
    :func:`_refresh_cable_termination_caches`). Donor-side interfaces whose
    ``lag``/``parent``/``bridge`` points at the moved row are carried along
    in the same transaction so no cross-device relationship is ever left
    behind.

    Fails (toast + 409) when the winner already has an interface with the
    same name as the moved row or any carried dependent — the user must
    rename or delete the colliding interface on the winner first.
    """

    # Requires change Device too: on success the move calls
    # _reconcile_donor_device_ip_fks(), which writes device-level FKs
    # (winner/donor primary_ip4/primary_ip6/oob_ip). NetBoxObjectPermissionMixin is
    # model-level only, so Device must be in the declared boundary.
    required_object_permissions = {"POST": [("change", Interface), ("change", Device)]}

    def post(self, request, pk):
        gate = self._gate(request)
        if gate is not None:
            return gate

        # Object-scoped: the gate above is model-level only, so a constrained change_interface
        # grant would otherwise move an interface it cannot see.
        interface = self.restrict_object_or_404(Interface, "change", pk=pk)
        donor = interface.device
        if donor is None:
            return self._fail(request, "Interface has no device.")

        # Fall back to active_server_key (never builds LibreNMSAPI) so a misconfigured default
        # server can't 500 a move that only needs the marker's server namespace, not the live API.
        server_key = _server_key_from_request(request, default_factory=lambda: self.active_server_key)
        self._fallback_url = _sync_tab_url(donor.pk, "interfaces", server_key)
        winner, err = self._resolve_winner_or_fail(request, donor, server_key)
        if err is not None:
            return err

        # Quick pre-check before acquiring the lock (avoids round-trip in the
        # obvious already-taken case).  The check is repeated under the lock
        # below to close the TOCTOU window.
        if Interface.objects.filter(device=winner, name=interface.name).exists():
            return self._fail(
                request,
                f"Winner device '{winner.name}' already has an interface named '{interface.name}'. "
                "Rename or remove the existing interface first.",
                status=409,
            )

        with claim_sync_subjects(sync_subject_key(donor), sync_subject_key(winner)):
            with transaction.atomic():
                donor, winner, err = self._lock_donor_winner_and_reverify(request, donor, winner, server_key)
                if err is not None:
                    return err
                # Lock the donor interface row and re-read it under the lock. A
                # concurrent rename would otherwise let a stale name slip past the
                # collision check below while the row is still moved by pk.
                interface = (
                    self.restricted_queryset(Interface, "change")
                    .select_for_update(of=("self",))
                    .filter(pk=interface.pk, device=donor)
                    .first()
                )
                if interface is None:
                    return self._fail(
                        request,
                        f"Interface is no longer attached to '{donor.name}'.",
                        status=409,
                    )
                # Re-check the collision under the lock, using the locked name.
                if Interface.objects.filter(device=winner, name=interface.name).exists():
                    return self._fail(
                        request,
                        f"Winner device '{winner.name}' already has an interface named '{interface.name}'. "
                        "Rename or remove the existing interface first.",
                        status=409,
                    )
                # Carry the moved row's donor-side family (lag members, children, bridged
                # peers) in the same transaction. Pre-check ALL their names on the winner
                # before anything is saved, so the family either moves whole or not at all.
                dependents = _donor_side_dependents(interface, donor)
                # The family is DERIVED from the moved row, not the scoped pk, and every member is
                # saved below. The move is all-or-nothing, so refuse unless the grant covers all of it.
                if dependents:
                    dependent_pks = {dep.pk for dep in dependents}
                    authorized = set(
                        self.restricted_queryset(Interface, "change")
                        .filter(pk__in=dependent_pks)
                        .values_list("pk", flat=True)
                    )
                    if authorized != dependent_pks:
                        return self._fail(
                            request,
                            f"Interface '{interface.name}' has attached interface(s) that must move with "
                            "it, and you do not have permission to change all of them.",
                            status=403,
                        )
                colliding = sorted(
                    Interface.objects.filter(device=winner, name__in=[dep.name for dep in dependents]).values_list(
                        "name", flat=True
                    )
                )
                if colliding:
                    return self._fail(
                        request,
                        f"Winner device '{winner.name}' already has interface(s) named "
                        f"{', '.join(colliding)}. They are attached to '{interface.name}' and must move "
                        "with it — rename or remove the colliding interface(s) first.",
                        status=409,
                    )
                # Move via full_clean()+save(), not a bare .update(): a plain column update
                # bypasses Interface.clean(), which is exactly what guards against the moved
                # interface keeping a parent/lag/bridge that still lives on the donor (a
                # cross-device link NetBox forbids). Surface that as a 409 so the user unlinks
                # the relationship first rather than silently creating an invalid interface.
                #
                # NetBox's ComponentModel.clean() also hard-blocks ANY device change on an
                # existing component ("Components cannot be moved to a different device"), keyed
                # on the device_id cached into self._original_device at load time. That blanket
                # block is precisely the operation this view exists to perform, so re-seed the
                # cached original to the new device to defeat ONLY that one check — every other
                # validation in clean() (the cross-device parent/lag/bridge checks above, name
                # uniqueness, …) still runs, and save() still refreshes the denormalized
                # _site/_location/_rack columns. A bare .update(device=...) would skip all of
                # that (stale denormalized location + no relationship validation).
                interface.device = winner
                # Coupling to a private NetBox internal: ComponentModel seeds
                # _original_device from device_id in __init__ and clean() raises
                # "Components cannot be moved to a different device" when it differs
                # (dcim/models/device_components.py — ComponentModel.__init__ /
                # ComponentModel.clean). If a NetBox upgrade renames or drops this
                # attribute, the move below will silently stop working or 500 — treat
                # any change to that attribute as a breaking change in upgrade notes.
                interface._original_device = winner.pk
                try:
                    interface.full_clean()
                except ValidationError as exc:
                    detail = validation_error_detail(exc)
                    return self._fail(
                        request,
                        f"Cannot move interface '{interface.name}' to '{winner.name}': {detail}",
                        status=409,
                    )
                try:
                    interface.save()
                except IntegrityError:
                    # IntegrityError already marks this atomic block rollback-only; keep the
                    # rollback explicit here to match the non-database failure handlers below.
                    transaction.set_rollback(True)
                    # A concurrent rename/create on the winner side can still trip a DB unique
                    # constraint between our name re-check and this save. Surface it as the same
                    # 409 the collision check uses rather than letting it bubble up as a 500.
                    logger.warning(
                        "IntegrityError moving interface pk=%s to winner pk=%s (concurrent winner-side change)",
                        interface.pk,
                        winner.pk,
                    )
                    return self._fail(
                        request,
                        f"Winner device '{winner.name}' already has an interface named '{interface.name}'. "
                        "Rename or remove the existing interface first.",
                        status=409,
                    )

                # Carry the donor-side family collected (and locked) above. BFS order
                # guarantees each dependent's lag/parent/bridge target has already moved,
                # so full_clean()'s cross-device checks pass for real.
                for dep in dependents:
                    dep.device = winner
                    dep._original_device = winner.pk  # same ComponentModel re-seed as above
                    try:
                        dep.full_clean()
                        dep.save()
                    except (ValidationError, IntegrityError) as exc:
                        # The master already saved in this transaction; a plain return would
                        # COMMIT the half-moved family, so roll the whole move back explicitly
                        # (IntegrityError poisons the transaction anyway; ValidationError doesn't).
                        transaction.set_rollback(True)
                        logger.warning(
                            "Family move failed for attached interface pk=%s (master pk=%s): %s",
                            dep.pk,
                            interface.pk,
                            exc,
                        )
                        return self._fail(
                            request,
                            f"Cannot move attached interface '{dep.name}' with '{interface.name}'; "
                            "the whole move was rolled back.",
                            status=409,
                        )

                # Refresh the denormalized cable-list cache columns for every moved row.
                _refresh_cable_termination_caches(interface)
                for dep in dependents:
                    _refresh_cable_termination_caches(dep)

                # The moved interface may carry an address the donor still points at via
                # primary_ip4/primary_ip6/oob_ip; reconcile those device FKs so the donor isn't
                # left referencing an address now on a winner-owned interface.
                try:
                    notes = _reconcile_donor_device_ip_fks(donor, winner)
                except ValueError as exc:
                    # A corrupt donor IP FK (e.g. a primary_ip4 referencing an IPv6 address) makes
                    # set_device_ip_fk() refuse the transfer. Fail closed — roll back the whole move
                    # rather than 500ing or leaving the donor with a dangling FK NetBox rejects.
                    transaction.set_rollback(True)
                    return self._fail(request, f"Cannot reconcile donor IP assignments: {exc}", status=409)

            message = f"Moved interface '{interface.name}' to {winner.name}."
            if dependents:
                message = (
                    f"Moved interface '{interface.name}' and {len(dependents)} attached "
                    f"interface(s) ({', '.join(dep.name for dep in dependents)}) to {winner.name}."
                )
            if notes:
                message += " " + "; ".join(notes) + "."
            schedule_request_cache_mutation(request, donor, SyncTab.INTERFACES, server_key)
            _schedule_winner_cache_mutation(request, winner, SyncTab.INTERFACES, server_key)
            response = _hx_response(request, message, fallback_url=self._fallback_url)
            return response


class MoveIPAddressToWinnerView(_BaseMoveToWinnerView):
    """
    Reassign ``IPAddress.assigned_object`` from a donor-owned target to
    the winner's equivalent.

    Behaviour by current assignment:

    * Assigned to a donor :class:`Interface` whose name exists on the
      winner → reassign to the winner's same-name interface.
    * Assigned to a donor :class:`Interface` whose name does *not* exist
      on the winner → fail (user must move the interface first).
    * Unassigned IP picked from the donor's sync page → fail (no donor
      relationship to migrate).
    """

    # Requires change Device too: on success the move calls
    # _reconcile_donor_device_ip_fks(), which writes device-level FKs
    # (winner/donor primary_ip4/primary_ip6/oob_ip). NetBoxObjectPermissionMixin is
    # model-level only, so Device must be in the declared boundary.
    required_object_permissions = {"POST": [("change", IPAddress), ("change", Device)]}

    def post(self, request, pk):
        gate = self._gate(request)
        if gate is not None:
            return gate

        # Object-scoped — see MoveInterfaceToWinnerView.post.
        ip = self.restrict_object_or_404(IPAddress, "change", pk=pk)
        assigned = ip.assigned_object
        if not isinstance(assigned, Interface):
            return self._fail(
                request,
                "IP is not assigned to a donor interface; nothing to migrate.",
                status=409,
            )
        donor = assigned.device
        if donor is None:
            return self._fail(request, "IP's interface has no device.", status=409)

        # Fall back to active_server_key (never builds LibreNMSAPI) so a misconfigured default
        # server can't 500 a move that only needs the marker's server namespace, not the live API.
        server_key = _server_key_from_request(request, default_factory=lambda: self.active_server_key)
        self._fallback_url = _sync_tab_url(donor.pk, "ipaddresses", server_key)
        winner, err = self._resolve_winner_or_fail(request, donor, server_key)
        if err is not None:
            return err

        # Quick pre-check before acquiring the lock.  Repeated under lock below.
        if not Interface.objects.filter(device=winner, name=assigned.name).exists():
            return self._fail(
                request,
                f"Winner '{winner.name}' has no interface named '{assigned.name}'. "
                "Move the interface first, then retry.",
                status=409,
            )

        with claim_sync_subjects(sync_subject_key(donor), sync_subject_key(winner)):
            with transaction.atomic():
                donor, winner, err = self._lock_donor_winner_and_reverify(request, donor, winner, server_key)
                if err is not None:
                    return err
                ip = (
                    self.restricted_queryset(IPAddress, "change")
                    .select_for_update(of=("self",))
                    .filter(pk=ip.pk)
                    .first()
                )
                if ip is None:
                    return self._fail(request, "IP address no longer exists.", status=410)
                assigned = ip.assigned_object
                if not isinstance(assigned, Interface) or assigned.device_id != donor.pk:
                    return self._fail(request, "IP is no longer assigned to the donor interface.", status=409)
                # Lock the donor interface row too and re-read it: its name is used
                # below to find the winner-side interface, so a concurrent rename
                # would otherwise send the IP to the wrong interface (TOCTOU).
                assigned = self.relock_scoped_row(Interface, pk=assigned.pk, device=donor)
                if assigned is None:
                    return self._fail(request, "IP is no longer assigned to the donor interface.", status=409)
                # Re-fetch the winner interface under the lock to close the TOCTOU window.
                winner_iface = Interface.objects.select_for_update().filter(device=winner, name=assigned.name).first()
                if winner_iface is None:
                    return self._fail(
                        request,
                        f"Winner '{winner.name}' has no interface named '{assigned.name}'. "
                        "Move the interface first, then retry.",
                        status=409,
                    )
                ip.assigned_object = winner_iface
                ip.save(update_fields=["assigned_object_type", "assigned_object_id"])

                # The moved address may itself be the donor's primary_ip4/primary_ip6/oob_ip;
                # reconcile those device FKs so the donor isn't left referencing an address now on a
                # winner-owned interface.
                try:
                    notes = _reconcile_donor_device_ip_fks(donor, winner)
                except ValueError as exc:
                    # A corrupt donor IP FK (e.g. a primary_ip4 referencing an IPv6 address) makes
                    # set_device_ip_fk() refuse the transfer. Fail closed — roll back the whole move
                    # rather than 500ing or leaving the donor with a dangling FK NetBox rejects.
                    transaction.set_rollback(True)
                    return self._fail(request, f"Cannot reconcile donor IP assignments: {exc}", status=409)

            message = f"Moved IP {ip.address} to {winner.name} interface '{winner_iface.name}'."
            if notes:
                message += " " + "; ".join(notes) + "."
            schedule_request_cache_mutation(request, donor, SyncTab.IP_ADDRESSES, server_key)
            _schedule_winner_cache_mutation(request, winner, SyncTab.IP_ADDRESSES, server_key)
            response = _hx_response(request, message, fallback_url=self._fallback_url)
            return response


class TransferDeviceIPView(_BaseMoveToWinnerView):
    """
    One-shot transfer of a donor's primary IPv4/v6 or OOB IP to the
    winner.

    Triggered with URL kwarg ``ip_kind`` ∈ ``{"primary4", "primary6",
    "oob"}``.  Refuses to overwrite a value already set on the winner —
    the user must clear it on the winner first.

    The IPAddress object itself is *not* moved off its current
    ``assigned_object`` (an interface) — only the ``Device.primary_ip4``
    / ``primary_ip6`` / ``oob_ip`` foreign key on the winner is set, and
    the donor's foreign key is cleared.
    """

    required_object_permissions = {"POST": [("change", Device)]}

    # URL ``ip_kind`` kwarg → device IP FK field name; the human label comes from the single
    # utils.DEVICE_IP_FK_LABELS map so it can't drift from the reconcile-notes wording.
    _IP_KIND_TO_FIELD = {"primary4": "primary_ip4", "primary6": "primary_ip6", "oob": "oob_ip"}

    def post(self, request, pk, ip_kind):
        gate = self._gate(request)
        if gate is not None:
            return gate

        field = self._IP_KIND_TO_FIELD.get(ip_kind)
        if field is None:
            return self._fail(request, f"Unknown ip_kind '{ip_kind}'.")
        human = DEVICE_IP_FK_LABELS[field]

        # Object-scoped — see MoveInterfaceToWinnerView.post.
        donor = self.restrict_object_or_404(Device, "change", pk=pk)
        # Fall back to active_server_key (never builds LibreNMSAPI) so a misconfigured default
        # server can't 500 a move that only needs the marker's server namespace, not the live API.
        server_key = _server_key_from_request(request, default_factory=lambda: self.active_server_key)
        self._fallback_url = _sync_tab_url(donor.pk, "ipaddresses", server_key)
        winner, err = self._resolve_winner_or_fail(request, donor, server_key)
        if err is not None:
            return err

        donor_ip = getattr(donor, field, None)
        if donor_ip is None:
            return self._fail(request, f"Donor has no {human} to transfer.", status=409)
        # Quick pre-check before acquiring the lock.  Repeated under lock below.
        if getattr(winner, field, None) is not None:
            return self._fail(
                request,
                f"Winner '{winner.name}' already has a {human}. Clear it on the winner first.",
                status=409,
            )

        with claim_sync_subjects(sync_subject_key(donor), sync_subject_key(winner)):
            with transaction.atomic():
                donor, winner, err = self._lock_donor_winner_and_reverify(request, donor, winner, server_key)
                if err is not None:
                    return err
                # Re-check under the lock so concurrent transfers don't race.
                donor_ip = getattr(donor, field, None)
                if donor_ip is None:
                    return self._fail(request, f"Donor has no {human} to transfer.", status=409)
                # Lock the IPAddress row itself before validating its assignment below: the
                # device FKs are locked but the address is not, so a concurrent reassignment
                # could otherwise change assigned_object between this check and the FK update,
                # leaving winner.primary_ip*/oob_ip pointing at an address it no longer owns.
                donor_ip = self.relock_scoped_row(IPAddress, pk=donor_ip.pk)
                if donor_ip is None:
                    return self._fail(request, f"Donor's {human} no longer exists.", status=410)
                if getattr(winner, field, None) is not None:
                    return self._fail(
                        request,
                        f"Winner '{winner.name}' already has a {human}. Clear it on the winner first.",
                        status=409,
                    )
                # The save below uses update_fields (skips full_clean), so nothing else
                # validates that the address belongs to the winner. NetBox requires
                # primary_ip/oob_ip be assigned to one of the device's OWN interfaces, so
                # refuse to point the winner at an address still attached to the donor —
                # the interface/IP must be moved to the winner first (MoveIPAddressToWinnerView).
                #
                # Locking the IPAddress only pins which interface it points at, not that
                # interface's device_id. Re-lock the owning interface so a concurrent move
                # can't change its device after this check but before the FK saves, which
                # would leave winner.primary_ip*/oob_ip on an interface it no longer owns.
                assigned = getattr(donor_ip, "assigned_object", None)
                # Only re-lock when the assignment really is a dcim Interface. GenericForeignKey
                # pks aren't unique across models, so filtering Interface by a non-Interface pk
                # (e.g. a VMInterface) could lock an unrelated Interface and let the device_id
                # check validate the wrong row. A non-Interface assignment fails closed (None).
                if isinstance(assigned, Interface):
                    # Lock the owning interface row (this transfer is device-scoped).
                    assigned = self.relock_scoped_row(Interface, pk=assigned.pk)
                    if assigned is not None:
                        # Freshen the cached GFK to the locked row: set_device_ip_fk() re-reads
                        # donor_ip.assigned_object for its ownership check, and the pre-lock snapshot
                        # would spuriously 409 a move that landed on the winner just before the lock —
                        # same freshen-after-lock as _reconcile_donor_device_ip_fks.
                        donor_ip.assigned_object = assigned
                else:
                    assigned = None
                if getattr(assigned, "device_id", None) != winner.pk:
                    return self._fail(
                        request,
                        f"{human} ({donor_ip}) is still attached to '{donor.name}'. "
                        f"Move its interface/IP to '{winner.name}' first.",
                        status=409,
                    )
                # set_device_ip_fk() saves only the touched FK column (skips full_clean(), so a
                # pre-existing inconsistency like ``face`` without ``rack`` can't block the merge)
                # while enforcing the "address must live on the winner's own interface" invariant
                # the bare update_fields save would skip. device.primary_ip4/6 / oob_ip are UNIQUE
                # per address, so the donor must release the FK (saved first, below) BEFORE the
                # winner claims it — the reverse order trips the unique constraint.
                try:
                    set_device_ip_fk(donor, field, None)
                    set_device_ip_fk(winner, field, donor_ip)
                except ValueError as exc:
                    # A corrupt donor FK (e.g. a primary_ip4 referencing an IPv6 address) makes
                    # set_device_ip_fk() refuse the claim. Roll back the donor release rather than
                    # 500ing or leaving the donor's FK cleared with the winner's left unset.
                    transaction.set_rollback(True)
                    return self._fail(request, f"Cannot transfer {human}: {exc}", status=409)

            schedule_request_cache_mutation(request, donor, SyncTab.IP_ADDRESSES, server_key)
            _schedule_winner_cache_mutation(request, winner, SyncTab.IP_ADDRESSES, server_key)
            response = _hx_response(
                request,
                f"Transferred {human} ({donor_ip}) to {winner.name}.",
                fallback_url=self._fallback_url,
            )
            return response

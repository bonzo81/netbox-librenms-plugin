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

from dcim.models import Device, Interface
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.utils.html import format_html
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from ipam.models import IPAddress

from netbox_librenms_plugin.utils import get_migrated_to_marker
from netbox_librenms_plugin.views.mixins import (
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


def _resolve_winner_for_donor(donor, server_key="default"):
    """
    Return ``(winner, marker)`` for *donor*.

    - ``(None, None)`` when no ``_migrated_to`` marker is present.
    - ``(None, marker)`` when the marker exists but the winner device has
      been deleted or the ``device_id`` in the marker is invalid/unparseable
      (so callers can distinguish "no marker" from "stale marker").
    - ``(winner, marker)`` when the marker is valid and the winner exists.

    ``marker`` is the dict written by :func:`mark_librenms_migrated`.
    """
    marker = get_migrated_to_marker(donor, server_key)
    if not marker:
        return None, None
    try:
        winner_pk = int(marker.get("device_id"))
    except (TypeError, ValueError):
        return None, marker
    winner = Device.objects.filter(pk=winner_pk).first()
    if winner is None:
        return None, marker
    return winner, marker


def _server_key_from_request(request, default_server_key=None):
    """Extract the LibreNMS server key from the POST body (form field).

    Pass ``default_server_key=self.librenms_api.server_key`` from views that
    have API access so the fallback matches the active server's namespace.
    When no default is given, ``"default"`` is used as a last-resort fallback
    (migrate views always receive the correct key via the POST body).
    """
    sk = request.POST.get("server_key") or default_server_key
    return sk if isinstance(sk, str) and sk else (default_server_key or "default")


def _safe_referer(request):
    """
    Return the request's ``Referer`` only when it points back at this
    site, otherwise ``"/"``.

    ``Referer`` is a client-controlled header, so it must be validated
    against the current host before being used as a redirect target —
    trusting it blindly is an open-redirect vector.
    """
    referer = request.META.get("HTTP_REFERER")
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return referer
    return "/"


def _hx_response(request, message, level=messages.SUCCESS, *, status=200):
    """
    Common HTMX response: queue a Django messages flash and emit the
    ``HX-Refresh`` header so the sync page re-renders with the row gone.

    For non-HTMX requests, queue the message and redirect to the
    validated Referer or '/'.
    """
    messages.add_message(request, level, message)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=status, headers={"HX-Refresh": "true"})
    return redirect(_safe_referer(request))


class _BaseMoveToWinnerView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """Shared plumbing for the per-resource move endpoints."""

    def _gate(self, request):
        """
        Plugin-write + object-perm gate.  Returns a response on failure
        (which the caller must return verbatim) or ``None`` on success.
        """
        resp = self.require_all_permissions("POST")
        if resp is not None:
            return resp
        return None

    def _fail(self, request, msg, *, status=400):
        """
        Return an error response.

        For HTMX requests: returns HTTP 200 with an out-of-band swap into
        ``#django-messages`` so the toast renders through NetBox's Bootstrap
        pipeline.  ``HX-Reswap: none`` prevents the primary swap target from
        being overwritten.  The ``status`` parameter is not used for HTMX
        responses — errors are always signalled via the OOB toast, not the
        status code.

        For non-HTMX requests: adds a Django error message and redirects to
        the validated Referer or '/'.
        """
        if request.headers.get("HX-Request"):
            toast_html = format_html(
                '<div id="django-messages"'
                ' class="toast-container position-fixed bottom-0 end-0 p-3"'
                ' hx-swap-oob="true">'
                '<div class="toast toast-dark border-0 shadow-sm" role="alert"'
                ' aria-live="assertive" aria-atomic="true" data-bs-delay="12000">'
                '<div class="toast-header text-bg-danger">'
                '<i class="mdi mdi-alert-circle me-1"></i>Error'
                '<button type="button" class="btn-close me-0 m-auto"'
                ' data-bs-dismiss="toast" aria-label="Close"></button>'
                "</div>"
                '<div class="toast-body">{}</div>'
                "</div></div>",
                msg,
            )
            resp = HttpResponse(toast_html, content_type="text/html")
            resp["HX-Reswap"] = "none"
            return resp
        messages.error(request, msg)
        return redirect(_safe_referer(request))


class MoveInterfaceToWinnerView(_BaseMoveToWinnerView):
    """
    Reassign ``Interface.device`` from donor to winner.

    Cables, IP-address attachments, MAC objects, and VLAN tag config all
    point at the Interface row by FK, so they follow the move
    automatically.

    Fails (toast + 409) when the winner already has an interface with the
    same name — the user must rename or delete the colliding interface
    on the winner first.
    """

    required_object_permissions = {"POST": [("change", Interface)]}

    def post(self, request, pk):
        gate = self._gate(request)
        if gate is not None:
            return gate

        interface = get_object_or_404(Interface, pk=pk)
        donor = interface.device
        if donor is None:
            return self._fail(request, "Interface has no device.")

        server_key = _server_key_from_request(request)
        winner, marker = _resolve_winner_for_donor(donor, server_key)
        if marker is None:
            return self._fail(request, "Donor device is not marked as migrated.", status=409)
        if winner is None:
            return self._fail(request, "Winner device no longer exists.", status=410)

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

        with transaction.atomic():
            # Lock both devices in pk order to avoid cross-merge deadlocks.
            ordered = sorted({donor.pk, winner.pk})
            locked = {d.pk: d for d in Device.objects.select_for_update().filter(pk__in=ordered).order_by("pk")}
            donor = locked.get(donor.pk)
            winner = locked.get(winner.pk)
            if donor is None or winner is None:
                return self._fail(request, "Device was deleted concurrently.", status=410)
            # Re-verify the migration marker under the lock: a concurrent request
            # could have cleared or repointed _migrated_to between the unlocked
            # resolve above and acquiring these row locks, which would otherwise
            # move the interface to the wrong (stale) winner.
            relocked_winner, relocked_marker = _resolve_winner_for_donor(donor, server_key)
            if relocked_marker is None or relocked_winner is None or relocked_winner.pk != winner.pk:
                return self._fail(request, "Donor migration changed concurrently; refresh and retry.", status=409)
            # Lock the donor interface row and re-read it under the lock. A
            # concurrent rename would otherwise let a stale name slip past the
            # collision check below while the row is still moved by pk.
            interface = Interface.objects.select_for_update().filter(pk=interface.pk, device=donor).first()
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
            # Row is locked and confirmed donor-owned, so a pk update is safe.
            Interface.objects.filter(pk=interface.pk).update(device=winner)

        return _hx_response(
            request,
            f"Moved interface '{interface.name}' to {winner.name}.",
        )


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

    required_object_permissions = {"POST": [("change", IPAddress)]}

    def post(self, request, pk):
        gate = self._gate(request)
        if gate is not None:
            return gate

        ip = get_object_or_404(IPAddress, pk=pk)
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

        server_key = _server_key_from_request(request)
        winner, marker = _resolve_winner_for_donor(donor, server_key)
        if marker is None:
            return self._fail(request, "Donor device is not marked as migrated.", status=409)
        if winner is None:
            return self._fail(request, "Winner device no longer exists.", status=410)

        # Quick pre-check before acquiring the lock.  Repeated under lock below.
        if not Interface.objects.filter(device=winner, name=assigned.name).exists():
            return self._fail(
                request,
                f"Winner '{winner.name}' has no interface named '{assigned.name}'. "
                "Move the interface first, then retry.",
                status=409,
            )

        with transaction.atomic():
            ordered = sorted({donor.pk, winner.pk})
            locked = {d.pk: d for d in Device.objects.select_for_update().filter(pk__in=ordered).order_by("pk")}
            donor = locked.get(donor.pk)
            winner = locked.get(winner.pk)
            if donor is None or winner is None:
                return self._fail(request, "Device was deleted concurrently.", status=410)
            # Re-verify the migration marker under the lock (it could have been
            # cleared or repointed since the unlocked resolve above).
            relocked_winner, relocked_marker = _resolve_winner_for_donor(donor, server_key)
            if relocked_marker is None or relocked_winner is None or relocked_winner.pk != winner.pk:
                return self._fail(request, "Donor migration changed concurrently; refresh and retry.", status=409)
            ip = IPAddress.objects.select_for_update().filter(pk=ip.pk).first()
            if ip is None:
                return self._fail(request, "IP address no longer exists.", status=410)
            assigned = ip.assigned_object
            if not isinstance(assigned, Interface) or assigned.device_id != donor.pk:
                return self._fail(request, "IP is no longer assigned to the donor interface.", status=409)
            # Lock the donor interface row too and re-read it: its name is used
            # below to find the winner-side interface, so a concurrent rename
            # would otherwise send the IP to the wrong interface (TOCTOU).
            assigned = Interface.objects.select_for_update().filter(pk=assigned.pk, device=donor).first()
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

        return _hx_response(
            request,
            f"Moved IP {ip.address} to {winner.name} interface '{winner_iface.name}'.",
        )


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

    _FIELD_MAP = {
        "primary4": ("primary_ip4", "primary IPv4"),
        "primary6": ("primary_ip6", "primary IPv6"),
        "oob": ("oob_ip", "OOB IP"),
    }

    def post(self, request, pk, ip_kind):
        gate = self._gate(request)
        if gate is not None:
            return gate

        if ip_kind not in self._FIELD_MAP:
            return self._fail(request, f"Unknown ip_kind '{ip_kind}'.")
        field, human = self._FIELD_MAP[ip_kind]

        donor = get_object_or_404(Device, pk=pk)
        server_key = _server_key_from_request(request)
        winner, marker = _resolve_winner_for_donor(donor, server_key)
        if marker is None:
            return self._fail(request, "Donor device is not marked as migrated.", status=409)
        if winner is None:
            return self._fail(request, "Winner device no longer exists.", status=410)

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

        with transaction.atomic():
            ordered = sorted({donor.pk, winner.pk})
            locked = {d.pk: d for d in Device.objects.select_for_update().filter(pk__in=ordered).order_by("pk")}
            donor = locked.get(donor.pk)
            winner = locked.get(winner.pk)
            if donor is None or winner is None:
                return self._fail(request, "Device was deleted concurrently.", status=410)
            # Re-verify the migration marker under the lock (it could have been
            # cleared or repointed since the unlocked resolve above).
            relocked_winner, relocked_marker = _resolve_winner_for_donor(donor, server_key)
            if relocked_marker is None or relocked_winner is None or relocked_winner.pk != winner.pk:
                return self._fail(request, "Donor migration changed concurrently; refresh and retry.", status=409)
            # Re-check under the lock so concurrent transfers don't race.
            donor_ip = getattr(donor, field, None)
            if donor_ip is None:
                return self._fail(request, f"Donor has no {human} to transfer.", status=409)
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
            assigned = getattr(donor_ip, "assigned_object", None)
            if getattr(assigned, "device_id", None) != winner.pk:
                return self._fail(
                    request,
                    f"{human} ({donor_ip}) is still attached to '{donor.name}'. "
                    f"Move its interface/IP to '{winner.name}' first.",
                    status=409,
                )
            setattr(winner, field, donor_ip)
            setattr(donor, field, None)
            # Save only the touched FK column to avoid full_clean() rejecting
            # the merge over pre-existing inconsistencies on either device
            # (e.g. ``face`` set without ``rack``).
            winner.save(update_fields=[field])
            donor.save(update_fields=[field])

        return _hx_response(
            request,
            f"Transferred {human} ({donor_ip}) to {winner.name}.",
        )

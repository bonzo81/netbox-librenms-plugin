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
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from ipam.models import IPAddress

from netbox_librenms_plugin.utils import get_migrated_to_marker
from netbox_librenms_plugin.views.mixins import (
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)


def _resolve_winner_for_donor(donor, server_key="default"):
    """
    Return ``(winner, marker)`` for *donor*, or ``(None, None)`` when no
    valid ``_migrated_to`` marker is present or the winner has been
    deleted.

    ``marker`` is the dict written by :func:`mark_librenms_migrated`.
    """
    marker = get_migrated_to_marker(donor, server_key)
    if not marker:
        return None, None
    winner = Device.objects.filter(pk=marker["device_id"]).first()
    if winner is None:
        return None, marker
    return winner, marker


def _server_key_from_request(request, default="default"):
    """Extract the LibreNMS server key from the POST body (form field)."""
    sk = request.POST.get("server_key") or default
    return sk if isinstance(sk, str) and sk else default


def _hx_response(request, message, level=messages.SUCCESS, *, status=200):
    """
    Common HTMX response: queue a Django messages flash and emit the
    ``HX-Refresh`` header so the sync page re-renders with the row gone.

    For non-HTMX requests, queue the message and redirect to the donor's
    librenms-sync URL.
    """
    messages.add_message(request, level, message)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=status, headers={"HX-Refresh": "true"})
    return redirect(request.META.get("HTTP_REFERER") or "/")


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
            list(Device.objects.select_for_update().filter(pk__in=ordered).order_by("pk"))
            interface.device = winner
            interface.save()

        return _hx_response(
            request,
            f"Moved interface '{interface.name}' to {winner.name}.",
        )

    def _fail(self, request, msg, *, status=400):
        if request.headers.get("HX-Request"):
            return JsonResponse({"error": msg}, status=status)
        messages.error(request, msg)
        return redirect(request.META.get("HTTP_REFERER") or "/")


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

        winner_iface = Interface.objects.filter(device=winner, name=assigned.name).first()
        if winner_iface is None:
            return self._fail(
                request,
                f"Winner '{winner.name}' has no interface named '{assigned.name}'. "
                "Move the interface first, then retry.",
                status=409,
            )

        with transaction.atomic():
            ordered = sorted({donor.pk, winner.pk})
            list(Device.objects.select_for_update().filter(pk__in=ordered).order_by("pk"))
            ip.assigned_object = winner_iface
            ip.save()

        return _hx_response(
            request,
            f"Moved IP {ip.address} to {winner.name} interface '{winner_iface.name}'.",
        )

    def _fail(self, request, msg, *, status=400):
        if request.headers.get("HX-Request"):
            return JsonResponse({"error": msg}, status=status)
        messages.error(request, msg)
        return redirect(request.META.get("HTTP_REFERER") or "/")


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
        if getattr(winner, field, None) is not None:
            return self._fail(
                request,
                f"Winner '{winner.name}' already has a {human}. Clear it on the winner first.",
                status=409,
            )

        with transaction.atomic():
            ordered = sorted({donor.pk, winner.pk})
            list(Device.objects.select_for_update().filter(pk__in=ordered).order_by("pk"))
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

    def _fail(self, request, msg, *, status=400):
        if request.headers.get("HX-Request"):
            return JsonResponse({"error": msg}, status=status)
        messages.error(request, msg)
        return redirect(request.META.get("HTTP_REFERER") or "/")

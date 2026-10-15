"""
Breakout cables: one profiled cable on a channelized parent interface, mirrored onto its channels.

NetBox 4.7 refuses a cable on a channel interface. The cable terminates on the parent, and its
breakout profile gives B connector N to channel N. NetBox numbers the B connectors by their list
order, so a far end can be stored only when every lower channel already has one.
"""

from dataclasses import dataclass

from dcim.models import Interface

from netbox_librenms_plugin.constants import OOB_INVENTORY_SOURCE, SERIAL_INVENTORY_SOURCE
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id
from netbox_librenms_plugin.utils import cable_has_librenms_tag

CHANNELS_SUPPORTED = hasattr(Interface, "channel_id")


def breakout_parent(interface):
    """Return the channelized parent of *interface* when it is a channel, else None."""
    if not CHANNELS_SUPPORTED or not isinstance(interface, Interface):
        return None
    if interface.channel_id is None or interface.parent_id is None:
        return None
    parent = interface.parent
    return parent if parent.channels else None


def breakout_profile(channels):
    """Return the NetBox breakout cable profile for *channels* channels, or None when NetBox has none."""
    from dcim.choices import CableProfileChoices

    profile = f"breakout-1c{channels}p-{channels}c1p"
    return profile if profile in CableProfileChoices.values() else None


def _cable_ends(cable):
    """Return the terminations of *cable* as ``(end, connector, termination)``, in connector order."""
    terminations = sorted(cable.terminations.all(), key=lambda term: (term.cable_end, term.connector or 0, term.pk))
    return [(term.cable_end, term.connector, term.termination) for term in terminations]


def _parent_end(ends, parent):
    """Return the cable end that *parent* terminates, or None."""
    return next((end for end, _connector, termination in ends if termination == parent), None)


def breakout_far_end(channel, parent, cable):
    """Return the far end at connector ``channel_id`` of the parent's profiled *cable*, or None."""
    if cable is None or not getattr(cable, "profile", ""):
        return None
    ends = _cable_ends(cable)
    near = _parent_end(ends, parent)
    if near is None:
        return None
    far = "B" if near == "A" else "A"
    return next(
        (term for end, connector, term in ends if end == far and connector == channel.channel_id),
        None,
    )


def decide_channel_far_end(local, remote):
    """Decide a row whose far end is a channel and whose local end is not, or return None for another row."""
    if (parent := breakout_parent(remote)) is None:
        return None
    if breakout_far_end(remote, parent, parent.cable) == local:
        return BreakoutDecision("matching")
    return _refuse("the far end is a channel, so sync the cable from the device of the channel")


def lower_channel_far_end_ids(channel, rows):
    """
    Return the far-end interface pk of each lower channel of *channel*, read from enriched cable rows.

    A channel that no row resolves, or that two rows resolve to different far ends, has no entry.

    Args:
        channel (Interface): The channel interface of the row being synced.
        rows (list[dict]): The enriched cable rows of the same snapshot.

    Returns:
        dict[int, int]: ``{channel_id: remote interface pk}``.

    """
    siblings = dict(
        Interface.objects.filter(parent_id=channel.parent_id, channel_id__lt=channel.channel_id).values_list(
            "pk", "channel_id"
        )
    )
    found = {}
    for row in rows:
        if row.get("_source") in (OOB_INVENTORY_SOURCE, SERIAL_INVENTORY_SOURCE):
            continue
        channel_id = siblings.get(coerce_librenms_id(row.get("netbox_local_interface_id")))
        remote_id = coerce_librenms_id(row.get("netbox_remote_interface_id"))
        if channel_id is not None and remote_id is not None:
            found.setdefault(channel_id, set()).add(remote_id)
    return {channel_id: next(iter(ids)) for channel_id, ids in found.items() if len(ids) == 1}


@dataclass(frozen=True)
class BreakoutDecision:
    """What a cable sync on one channel does: ``matching``, ``create``, ``extend`` or ``refuse``."""

    action: str
    reason: str = ""
    profile: str = ""
    # The full B side that the write stores, and the interfaces in it that the write cables.
    b_terminations: tuple = ()
    new_far_ends: tuple = ()


def _refuse(reason):
    return BreakoutDecision("refuse", reason=reason)


def _far_end_refusal(far_end, label):
    """Return why *far_end* cannot take a new termination, or None."""
    if far_end.channel_id is not None:
        return f"{label} is a channel"
    if far_end.cable_id is not None:
        return f"{label} already has a cable"
    return None


def decide_breakout_cable(channel, parent, cable, remote, lower_far_ends, provenance_tag):
    """
    Decide the cable sync of one channel row against the cable on its parent.

    Args:
        channel (Interface): The local channel interface.
        parent (Interface): Its channelized parent.
        cable (Cable | None): The cable on the parent.
        remote (Interface): The far end that LibreNMS reports for the channel.
        lower_far_ends (dict[int, Interface]): The known far ends of the lower channels, by channel ID.
        provenance_tag (Tag | None): The plugin's cable tag.

    Returns:
        BreakoutDecision: The decision, with the reason of a refusal.

    """
    profile = breakout_profile(parent.channels)
    if profile is None:
        return _refuse(f"NetBox has no breakout cable profile for {parent.channels} channels")
    if remote.channel_id is not None:
        return _refuse("the far end is a channel")
    if cable is None:
        return _decide_new_cable(channel, remote, lower_far_ends, profile)
    if breakout_far_end(channel, parent, cable) == remote:
        return BreakoutDecision("matching", profile=profile)
    return _decide_extension(channel, parent, cable, remote, profile, provenance_tag)


def _decide_new_cable(channel, remote, lower_far_ends, profile):
    """Create the cable with the far ends of channels 1 to ``channel_id``, when each one is known and free."""
    lower = []
    for lower_id in range(1, channel.channel_id):
        far_end = lower_far_ends.get(lower_id)
        if far_end is None:
            return _refuse(f"channel ID {lower_id} has no known far end, and NetBox numbers the far ends in order")
        if refusal := _far_end_refusal(far_end, f"the far end of channel ID {lower_id}"):
            return _refuse(refusal)
        lower.append(far_end)
    if refusal := _far_end_refusal(remote, "the far end"):
        return _refuse(refusal)
    b_terminations = (*lower, remote)
    if len(set(b_terminations)) != len(b_terminations):
        return _refuse("two channels have the same far end")
    return BreakoutDecision("create", profile=profile, b_terminations=b_terminations, new_far_ends=b_terminations)


def _decide_extension(channel, parent, cable, remote, profile, provenance_tag):
    """Append the far end to the plugin's breakout cable, when it takes the next B connector."""
    if not cable_has_librenms_tag(cable, provenance_tag):
        return _refuse(f"the cable on {parent.name} does not have the LibreNMS cable tag")
    if getattr(cable, "profile", "") != profile:
        return _refuse(f"the cable on {parent.name} has no {profile} profile")
    ends = _cable_ends(cable)
    if _parent_end(ends, parent) != "A":
        return _refuse(f"{parent.name} is not on the A side of its cable")
    b_side = [term for end, _connector, term in ends if end == "B"]
    channel_id = channel.channel_id
    if len(b_side) >= channel_id:
        return _refuse(f"connector {channel_id} of the cable on {parent.name} ends on another interface")
    if len(b_side) < channel_id - 1:
        missing = len(b_side) + 1
        return _refuse(f"channel ID {missing} has no far end on the cable on {parent.name} yet, so sync it first")
    if refusal := _far_end_refusal(remote, "the far end"):
        return _refuse(refusal)
    return BreakoutDecision("extend", profile=profile, b_terminations=(*b_side, remote), new_far_ends=(remote,))

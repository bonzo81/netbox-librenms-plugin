"""
Breakout cables: one profiled cable on a channelized parent interface, mirrored onto its channels.

NetBox 4.7 refuses a cable on a channel interface. The cable terminates on the parent, and its
breakout profile gives B connector N to channel N. Each channel row writes only its own far end.
"""

from dataclasses import dataclass

from dcim.models import Interface

from netbox_librenms_plugin.utils import cable_has_librenms_tag

CHANNELS_SUPPORTED = hasattr(Interface, "channel_id")


def is_channel(interface):
    """Return whether *interface* is a channel interface (NetBox 4.7 and later)."""
    return CHANNELS_SUPPORTED and isinstance(interface, Interface) and interface.channel_id is not None


def breakout_profile(channels):
    """Return the NetBox breakout cable profile for *channels* channels, or None when NetBox has none."""
    from dcim.choices import CableProfileChoices

    profile = f"breakout-1c{channels}p-{channels}c1p"
    return profile if profile in CableProfileChoices.values() else None


def _cable_ends(cable):
    """Return the terminations of *cable* as ``(end, connector, termination)``, in connector order."""
    terminations = sorted(cable.terminations.all(), key=lambda term: (term.cable_end, term.connector or 0, term.pk))
    return [(term.cable_end, term.connector, term.termination) for term in terminations]


def _readable_b_side(cable, parent):
    """Return the B side of *cable* by connector, or None when it is not the layout this sync writes."""
    if parent is None or cable is None or cable.profile != breakout_profile(parent.channels):
        return None
    ends = _cable_ends(cable)
    # Only this layout maps channel N to B connector N: other profiles shuffle the positions.
    if [(connector, term) for end, connector, term in ends if end == "A"] != [(1, parent)]:
        return None
    return {connector: term for end, connector, term in ends if end == "B"}


def breakout_far_end(channel, parent, cable):
    """Return the far end at connector ``channel_id`` of the parent's breakout *cable*, or None."""
    b_side = _readable_b_side(cable, parent)
    return None if b_side is None else b_side.get(channel.channel_id)


def channel_write_endpoints(interface):
    """Return the other interfaces that a cable write on *interface* writes: a channel's parent."""
    return [interface.parent] if is_channel(interface) and interface.parent_id is not None else []


def holds_a_cable(interface):
    """Return whether *interface* is cabled; NetBox mirrors the parent's cable onto every channel."""
    if is_channel(interface):
        parent = interface.parent
        return breakout_far_end(interface, parent, None if parent is None else parent.cable) is not None
    return interface.cable_id is not None


@dataclass(frozen=True)
class BreakoutDecision:
    """What a cable sync on one channel does: ``matching``, ``create``, ``extend`` or ``refuse``."""

    action: str
    reason: str = ""
    profile: str = ""
    # The full B side that the write stores.
    b_terminations: tuple = ()


def _refuse(reason):
    return BreakoutDecision("refuse", reason=reason)


def _sync_first(channel_id, parent):
    return _refuse(
        f"channel ID {channel_id} has no far end on {parent.name} yet, so sync channel ID {channel_id} first"
    )


def _far_end_refusal(remote):
    """Return why *remote* cannot take a new termination, or None."""
    if remote.cable_id is not None:
        return _refuse("the far end already has a cable")
    return None


def decide_channel_far_end(local, remote):
    """Decide a row whose far end is a channel and whose local end is not, or return None for another row."""
    if not is_channel(remote):
        return None
    parent = remote.parent
    if parent is not None and breakout_far_end(remote, parent, parent.cable) == local:
        return BreakoutDecision("matching")
    return _refuse("the far end is a channel, so sync the cable from the device of the channel")


def decide_breakout_cable(channel, parent, cable, remote, provenance_tag):
    """
    Decide the cable sync of one channel row against the cable on its parent.

    Channel 1 creates the cable. Channel N appends its far end only when connectors 1 to N - 1 hold one.

    Args:
        channel (Interface): The local channel interface.
        parent (Interface | None): Its parent interface.
        cable (Cable | None): The cable on the parent.
        remote (Interface): The far end that LibreNMS reports for the channel.
        provenance_tag (Tag | None): The plugin's cable tag.

    Returns:
        BreakoutDecision: The decision, with the reason of a refusal.

    """
    if parent is None or not parent.channels:
        return _refuse(f"{channel.name} is a channel without a channelized parent interface")
    profile = breakout_profile(parent.channels)
    if profile is None:
        return _refuse(f"NetBox has no breakout cable profile for {parent.channels} channels")
    if is_channel(remote):
        return _refuse("the far end is a channel")
    if cable is None:
        if refusal := _far_end_refusal(remote):
            return refusal
        if channel.channel_id != 1:
            return _sync_first(1, parent)
        return BreakoutDecision("create", profile=profile, b_terminations=(remote,))
    if (b_side := _readable_b_side(cable, parent)) is None:
        return _refuse(
            f"the cable on {parent.name} uses profile {cable.profile or 'none'}, which this sync does not read"
        )
    if b_side.get(channel.channel_id) == remote:
        return BreakoutDecision("matching", profile=profile)
    return _decide_extension(channel, parent, cable, b_side, remote, profile, provenance_tag)


def _decide_extension(channel, parent, cable, b_side, remote, profile, provenance_tag):
    """Append the far end to the plugin's breakout cable, when connectors 1 to ``channel_id - 1`` hold one."""
    if not cable_has_librenms_tag(cable, provenance_tag):
        return _refuse(f"the cable on {parent.name} does not have the LibreNMS cable tag")
    channel_id = channel.channel_id
    if channel_id in b_side:
        return _refuse(f"connector {channel_id} of the cable on {parent.name} ends on another interface")
    if refusal := _far_end_refusal(remote):
        return refusal
    if missing := [lower for lower in range(1, channel_id) if lower not in b_side]:
        return _sync_first(missing[0], parent)
    if len(b_side) != channel_id - 1:
        return _refuse(f"the cable on {parent.name} has a far end after connector {channel_id}")
    b_terminations = (*(b_side[connector] for connector in range(1, channel_id)), remote)
    return BreakoutDecision("extend", profile=profile, b_terminations=b_terminations)

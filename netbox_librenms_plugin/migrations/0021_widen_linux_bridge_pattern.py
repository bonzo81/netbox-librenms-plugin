from django.db import migrations

BRIDGE_OS = "linux"
# The 0019 seed. Anchored on a trailing number and nothing after it, so it rejects br-lan, br-int,
# virbr0, docker0, pnet0 and Proxmox's VLAN-aware vmbr0v5.
OLD_PATTERN = r"^(vmbr|br|bridge)\d+$"
# Each alternative still names a whole interface, so a bridge member cannot match: natmac stays a
# member of nat0, and virbr0-nic stays a member of virbr0. A pair whose two sides both match is
# not claimed at all, so a pattern that swallows the member silently drops the relationship.
NEW_PATTERN = r"^(?:vmbr\d+(?:v\d+)?|br\d+|br-[\w.-]+|bridge\d+|virbr\d+|docker\d+|pnet\d+|nat\d+)$"


def _normalized(value):
    """Normalize an OS name in the same way as the model constraint."""
    return (value or "").strip().lower()


def _linux_row(apps, schema_editor):
    """Return the seeded Linux rule, or None."""
    alias = schema_editor.connection.alias
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    rows = PortStackLagPattern.objects.using(alias)
    return alias, next((row for row in rows.all() if _normalized(row.librenms_os) == BRIDGE_OS), None)


def widen_bridge_pattern(apps, schema_editor):
    """Replace the shipped Linux bridge pattern, leaving an operator's own value alone."""
    alias, existing = _linux_row(apps, schema_editor)
    if existing is None or existing.bridge_name_pattern != OLD_PATTERN:
        return
    existing.bridge_name_pattern = NEW_PATTERN
    existing.save(using=alias, update_fields=["bridge_name_pattern"])


def restore_bridge_pattern(apps, schema_editor):
    """Put 0019's pattern back, leaving a row an operator has since repointed."""
    alias, existing = _linux_row(apps, schema_editor)
    if existing is None or existing.bridge_name_pattern != NEW_PATTERN:
        return
    existing.bridge_name_pattern = OLD_PATTERN
    existing.save(using=alias, update_fields=["bridge_name_pattern"])


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0020_seed_lag_interface_type_mapping"),
        ("netbox_librenms_plugin", "0019_portstacklagpattern_bridge_name_pattern"),
    ]

    operations = [
        migrations.RunPython(widen_bridge_pattern, restore_bridge_pattern),
    ]

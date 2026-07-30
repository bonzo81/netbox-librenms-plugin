"""Canonicalize existing Device serials and index exact serial lookups."""

from django.db import migrations

_BATCH_SIZE = 1000
_SERIAL_INDEX = "nblp_dcim_device_serial_idx"


def normalize_device_serials(apps, schema_editor):
    """Strip surrounding whitespace from every existing NetBox Device serial."""
    Device = apps.get_model("dcim", "Device")
    db_alias = schema_editor.connection.alias
    pending = []

    for device in Device.objects.using(db_alias).only("pk", "serial").iterator(chunk_size=_BATCH_SIZE):
        raw_serial = device.serial
        normalized = raw_serial.strip() if raw_serial is not None else ""
        if normalized == raw_serial:
            continue
        device.serial = normalized
        pending.append(device)
        if len(pending) == _BATCH_SIZE:
            Device.objects.using(db_alias).bulk_update(pending, ["serial"], batch_size=_BATCH_SIZE)
            pending.clear()

    if pending:
        Device.objects.using(db_alias).bulk_update(pending, ["serial"], batch_size=_BATCH_SIZE)


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. Keep the data rewrite
    # atomic via RunPython.atomic while leaving the index operation outside it.
    atomic = False

    dependencies = [
        ("dcim", "0001_squashed"),
        ("netbox_librenms_plugin", "0011_renormalize_device_type_mappings"),
    ]

    operations = [
        migrations.RunPython(
            normalize_device_serials,
            migrations.RunPython.noop,
            atomic=True,
        ),
        # Device is owned by NetBox's dcim app, so the plugin cannot declare this index
        # through its model state. NetBox does not otherwise index Device.serial.
        migrations.RunSQL(
            sql=(f"CREATE INDEX CONCURRENTLY {_SERIAL_INDEX} ON dcim_device (serial)"),
            reverse_sql=f"DROP INDEX CONCURRENTLY IF EXISTS {_SERIAL_INDEX}",
        ),
    ]

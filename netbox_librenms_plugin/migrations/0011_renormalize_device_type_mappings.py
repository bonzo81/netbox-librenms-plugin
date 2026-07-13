"""Re-key existing DeviceTypeMapping rows through the device_type NormalizationRule scope.

The hardware→DeviceType lookup (``utils.match_librenms_hardware_to_device_type``) now normalizes
the LibreNMS hardware string via the ``device_type`` NormalizationRule scope *before* querying
``librenms_hardware__iexact``, and newly created mappings are stored normalized. Mappings created
before that change hold the un-normalized value, so once any ``device_type`` rule exists they no
longer match the normalized lookup. This data migration applies the same normalization the runtime
uses to existing rows so they keep matching after upgrade.
"""

import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)


def renormalize_device_type_mappings(apps, schema_editor):
    """Apply the device_type normalization to each existing DeviceTypeMapping's hardware key."""
    DeviceTypeMapping = apps.get_model("netbox_librenms_plugin", "DeviceTypeMapping")

    # Use the runtime normalizer so re-keyed values mirror the lookup path exactly. Guard the
    # import/normalization: if anything is unavailable, leave rows untouched rather than corrupt
    # them (a fresh install has no rows to migrate anyway).
    try:
        from netbox_librenms_plugin.utils import apply_normalization_rules, preload_normalization_rules
    except Exception:  # pragma: no cover - defensive
        return

    # Preload the device_type rule chain ONCE. Without this, each per-row
    # apply_normalization_rules() re-queries NormalizationRule (an avoidable N+1 that scales with
    # the number of existing mappings); passing the preloaded dict makes the whole migration issue
    # a constant number of rule queries. The mappings are un-scoped (manufacturer=None), matching
    # the per-row calls below which pass no manufacturer.
    preloaded_rules = preload_normalization_rules(scope="device_type")

    for mapping in DeviceTypeMapping.objects.all().iterator():
        raw = mapping.librenms_hardware or ""
        rekeyed = None
        # One savepoint per ROW, wrapping every DB touch — the NormalizationRule queries
        # inside apply_normalization_rules, the clash .exists(), AND the save. Under the
        # migration's default atomic transaction, a DB-level failure in ANY of them poisons
        # the whole transaction on PostgreSQL, so a bare try/except could not actually
        # continue — the next row's query would error with "current transaction is
        # aborted". Rolling back only this row's savepoint keeps the loop going, mirroring
        # the "leave it for manual fixup" path. (The row iterator's cursor predates the
        # savepoint, so a rollback doesn't invalidate it.)
        try:
            with transaction.atomic():
                normalized = (
                    (apply_normalization_rules(value=raw, scope="device_type", preloaded_rules=preloaded_rules) or "")
                    .strip()
                    .lower()
                )

                # Stored values are already lowercased (the model lowercases on save); skip when
                # the normalization is a no-op so we don't issue pointless writes or fire signals.
                if not normalized or normalized == raw.strip().lower():
                    continue

                # The lookup is case-insensitive (__iexact). If another row already holds the
                # normalized key, re-keying this one would trip the unique constraint — and
                # signals that two raw mappings collapse to the same normalized value. Leave the
                # original and warn so the operator can resolve the duplicate by hand.
                clash = (
                    DeviceTypeMapping.objects.exclude(pk=mapping.pk)
                    .filter(librenms_hardware__iexact=normalized)
                    .exists()
                )
                if clash:
                    logger.warning(
                        "renormalize_device_type_mappings: %r normalizes to %r which already maps to a "
                        "device type; leaving the original and skipping to avoid a unique-constraint clash "
                        "— resolve the duplicate DeviceTypeMapping manually.",
                        raw,
                        normalized,
                    )
                    continue

                mapping.librenms_hardware = normalized
                mapping.save(update_fields=["librenms_hardware"])
                rekeyed = normalized
        except Exception:
            logger.exception(
                "renormalize_device_type_mappings: failed to re-key %r; leaving the original value.",
                raw,
            )
            continue
        if rekeyed is not None:
            logger.info("renormalize_device_type_mappings: re-keyed %r → %r", raw, rekeyed)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0010_inventory_and_mapping_models"),
    ]

    operations = [
        # Irreversible: the original (un-normalized) hardware strings cannot be reconstructed.
        migrations.RunPython(renormalize_device_type_mappings, migrations.RunPython.noop),
    ]

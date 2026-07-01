from django.db import migrations, models
from django.db.models.functions import Lower


def normalize_librenms_os_case(apps, schema_editor):
    """Canonicalize librenms_os and fail clearly on real duplicates before the CI-unique.

    UniqueConstraint(Lower("librenms_os")) is validated against existing rows the instant it is
    added, so a database the old case-sensitive unique let accumulate both "ios" and "IOS" would
    fail this migration with an opaque IntegrityError at deploy time. Detect genuine
    case-insensitive collisions first and abort with an actionable message (they need a human
    merge — the migration can't know which pattern wins); then rewrite the surviving rows to the
    same canonical form clean() already writes on every save, so a full_clean-bypassing insert
    (bulk_create / raw SQL / loaddata) can't leave a noncanonical value behind the constraint.

    Normalize with ``.strip().lower()`` — exactly what ``clean()`` applies — NOT a bare ``Lower()``:
    rows a bypassing path left as ``" IOS "`` and ``"ios"`` are the same pattern to ``clean()`` but
    differ under ``Lower()`` alone, so a Lower()-only collision check would miss them and a
    Lower()-only rewrite would leave the surrounding whitespace behind the new constraint.
    """
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    seen_pk_by_value = {}
    collisions = set()
    normalized_by_pk = {}
    for pattern in PortStackLagPattern.objects.all():
        normalized = (pattern.librenms_os or "").strip().lower()
        if not normalized:
            raise RuntimeError(
                "Cannot add the case-insensitive PortStackLagPattern.librenms_os uniqueness: a row "
                "has a blank librenms_os after normalization; fix it by hand first."
            )
        if seen_pk_by_value.get(normalized, pattern.pk) != pattern.pk:
            collisions.add(normalized)
        seen_pk_by_value.setdefault(normalized, pattern.pk)
        normalized_by_pk[pattern.pk] = normalized
    if collisions:
        raise RuntimeError(
            "Cannot add the case-insensitive PortStackLagPattern.librenms_os uniqueness: these "
            "values already have case-variant duplicates that must be merged by hand first: "
            + ", ".join(sorted(collisions))
        )
    for pattern in PortStackLagPattern.objects.all():
        normalized = normalized_by_pk[pattern.pk]
        if pattern.librenms_os != normalized:
            pattern.librenms_os = normalized
            pattern.save(update_fields=["librenms_os"])


class Migration(migrations.Migration):
    # Depend only on 0011 (which already pins extras to 0122 for the supported NetBox 4.2+
    # range). makemigrations tried to bump the extras dependency to a 4.3-era tip, but this
    # migration adds nothing that needs a newer field, so keep it minimal — same rationale as
    # the dependency note in 0011_portstacklagpattern.
    dependencies = [
        ("netbox_librenms_plugin", "0011_portstacklagpattern"),
    ]

    operations = [
        # Drop the case-sensitive column unique: at the DB level it let "ios" and "IOS" coexist
        # even though compiled_patterns_for_os reads librenms_os case-insensitively
        # (librenms_os__iexact), making the per-OS LAG-pattern fallback ambiguous. help_text is
        # carried verbatim from the model so makemigrations sees no residual field drift.
        migrations.AlterField(
            model_name="portstacklagpattern",
            name="librenms_os",
            field=models.CharField(
                help_text="LibreNMS OS identifier (e.g. 'ios', 'timos', 'junos')",
                max_length=50,
            ),
        ),
        # Pre-clean before the constraint is validated: canonicalize any mixed-case rows an old
        # full_clean-bypassing path may have left and abort with a clear message if genuine
        # case-variant duplicates exist, instead of letting AddConstraint fail with an opaque
        # IntegrityError at deploy time. noop reverse (lowercasing isn't reversible).
        migrations.RunPython(normalize_librenms_os_case, migrations.RunPython.noop),
        # Replace it with a functional unique on Lower(librenms_os) so the DB enforces uniqueness
        # the SAME case-insensitive way the lookup reads it. clean() already lowercases on every
        # save; this closes the gap for any path that bypasses full_clean (bulk_create / raw SQL
        # / loaddata).
        migrations.AddConstraint(
            model_name="portstacklagpattern",
            constraint=models.UniqueConstraint(
                Lower("librenms_os"),
                name="unique_portstacklagpattern_librenms_os_ci",
            ),
        ),
    ]

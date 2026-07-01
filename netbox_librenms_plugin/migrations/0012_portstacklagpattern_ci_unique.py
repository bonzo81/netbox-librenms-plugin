from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower


def normalize_librenms_os_case(apps, schema_editor):
    """Canonicalize librenms_os casing and fail clearly on real duplicates before the CI-unique.

    UniqueConstraint(Lower("librenms_os")) is validated against existing rows the instant it is
    added, so a database the old case-sensitive unique let accumulate both "ios" and "IOS" would
    fail this migration with an opaque IntegrityError at deploy time. Detect genuine
    case-insensitive collisions first and abort with an actionable message (they need a human
    merge — the migration can't know which pattern wins); then lowercase the surviving rows to the
    same canonical form clean() already writes on every save, so a full_clean-bypassing insert
    (bulk_create / raw SQL / loaddata) can't leave a mixed-case value behind the constraint.
    """
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    collisions = sorted(
        row["os_ci"]
        for row in (
            PortStackLagPattern.objects.annotate(os_ci=Lower("librenms_os"))
            .values("os_ci")
            .annotate(n=Count("pk"))
            .filter(n__gt=1)
        )
    )
    if collisions:
        raise RuntimeError(
            "Cannot add the case-insensitive PortStackLagPattern.librenms_os uniqueness: these "
            "values already have case-variant duplicates that must be merged by hand first: " + ", ".join(collisions)
        )
    for pattern in PortStackLagPattern.objects.all():
        lowered = (pattern.librenms_os or "").lower()
        if pattern.librenms_os != lowered:
            pattern.librenms_os = lowered
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

from django.db import migrations, models
from django.db.models.functions import Lower


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

"""
List the Devices and Modules whose stored serial changes under the current serial rules.

The plugin stores the trimmed LibreNMS text and keys it when it compares. Rows that an older
release wrote hold rule output instead (Modules, and Virtual Chassis members for a while). Such a
row keeps matching while its rule is unchanged. A rule that matches its own output, a doubled
vendor mark, or a deleted rule can make it stop matching. The report lists each row whose key
differs from its stored text, so an operator can check it. It changes nothing.
"""

from django.core.management.base import BaseCommand

from netbox_librenms_plugin.serial_identity import report_rows


class Command(BaseCommand):
    """Report the stored serials that the current serial rules change."""

    help = "List Devices and Modules whose stored serial changes under the current serial NormalizationRules."

    def handle(self, *args, **options):
        """Print one line per row, then the count."""
        count = 0
        for row, stored, key, reason in report_rows():
            count += 1
            kind = row._meta.model_name
            name = getattr(row, "name", None) or (
                f"{row.device.name} / {row.module_bay.name}" if kind == "module" else ""
            )
            outcome = f"keys to {key!r}" if key else reason
            self.stdout.write(f"{kind} {row.pk} ({name}): stored {stored!r} {outcome}")
        self.stdout.write(f"{count} row(s) change under the current serial rules.")

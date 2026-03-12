"""Coverage tests for netbox_librenms_plugin.forms — has_option_only empty-data guard."""

from unittest.mock import patch


class TestLibreNMSFilterFormBackgroundJobDefault:
    """Tests for use_background_job default injection in LibreNMSImportFilterForm.__init__.

    The form checks args[0] (positional) for the data dict, matching how Django
    binds forms from request.GET/POST. Tests pass data positionally to match.
    """

    def _make_form(self, data):
        """Instantiate LibreNMSImportFilterForm with mocked server settings.
        Pass data as positional arg to match how Django provides request.GET.
        """
        with (
            patch("netbox_librenms_plugin.forms.LibreNMSImportFilterForm._populate_librenms_locations"),
        ):
            from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

            # Pass data positionally — the form's __init__ checks args[0], not kwargs
            return LibreNMSImportFilterForm(data)

    def test_empty_data_sets_use_background_job_on(self):
        """LibreNMSImportFilterForm({}) should set use_background_job='on' (initial GET)."""
        form = self._make_form({})
        assert form.data.get("use_background_job") == "on"

    def test_option_only_data_does_not_auto_set_background_job(self):
        """Submitting only option-only fields should NOT auto-set use_background_job."""
        # show_disabled is an option-only field — not a real filter field
        # has_option_only = bool({show_disabled}) and not non_option_fields and not has_filters
        # = True and True and True = True → condition fails → use_background_job NOT injected
        form = self._make_form({"show_disabled": "on"})
        assert form.data.get("use_background_job") is None

    def test_filter_data_does_not_auto_set_background_job(self):
        """When real filter fields are submitted, use_background_job is not auto-injected."""
        form = self._make_form({"librenms_hostname": "switch01"})
        assert form.data.get("use_background_job") is None

    def test_use_background_job_preserved_when_already_set(self):
        """If use_background_job is already in data, it should not be overridden."""
        form = self._make_form({"use_background_job": "off"})
        assert form.data.get("use_background_job") == "off"

    def test_no_positional_args_no_injection(self):
        """Unbound form (no positional args) should not inject use_background_job."""
        with (
            patch("netbox_librenms_plugin.forms.LibreNMSImportFilterForm._populate_librenms_locations"),
        ):
            from netbox_librenms_plugin.forms import LibreNMSImportFilterForm

            form = LibreNMSImportFilterForm()
        assert form.data.get("use_background_job") is None

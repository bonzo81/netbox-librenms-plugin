"""
Tests for the LibreNMS location parse-pattern feature.

Covers the placeholder/regex parser, the settings-aware wrapper, and the
ImportSettingsForm validation. Follows the repo convention of mocked DB access
(no @pytest.mark.django_db).
"""

from unittest.mock import MagicMock, patch


# =============================================================================
# TestParseLibrenmsLocation
# =============================================================================


class TestParseLibrenmsLocation:
    """Tests for parse_librenms_location()."""

    def test_empty_inputs_return_all_none(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        assert parse_librenms_location("", "{site}") == {
            "region": None,
            "site": None,
            "location": None,
            "rack": None,
            "tenant": None,
        }
        assert parse_librenms_location("NYC", "") == {
            "region": None,
            "site": None,
            "location": None,
            "rack": None,
            "tenant": None,
        }

    def test_single_placeholder(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("NYC", "{site}")
        assert result["site"] == "NYC"
        assert result["rack"] is None

    def test_two_placeholders_with_separator(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("NYC - R1", "{site} - {rack}")
        assert result["site"] == "NYC"
        assert result["rack"] == "R1"

    def test_three_placeholders(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("East/NYC/HallA", "{region}/{site}/{location}")
        assert result["region"] == "East"
        assert result["site"] == "NYC"
        assert result["location"] == "HallA"

    def test_values_are_stripped(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("NYC ,  Hall A", "{site} , {location}")
        assert result["site"] == "NYC"
        assert result["location"] == "Hall A"

    def test_no_match_returns_all_none(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("NoSeparatorHere", "{site} - {rack}")
        assert all(v is None for v in result.values())

    def test_regex_mode_with_named_groups(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("NYC-R1", r"(?P<site>[^-]+)-(?P<rack>.+)", is_regex=True)
        assert result["site"] == "NYC"
        assert result["rack"] == "R1"

    def test_regex_mode_search_anywhere(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("prefix site=NYC end", r"site=(?P<site>\w+)", is_regex=True)
        assert result["site"] == "NYC"

    def test_invalid_regex_returns_all_none(self):
        from netbox_librenms_plugin.utils import parse_librenms_location

        result = parse_librenms_location("NYC", "(?P<site>[", is_regex=True)
        assert all(v is None for v in result.values())


# =============================================================================
# TestPlaceholderToRegex
# =============================================================================


class TestPlaceholderToRegex:
    """Tests for the _placeholder_pattern_to_regex() helper."""

    def test_escapes_literals(self):
        from netbox_librenms_plugin.utils import _placeholder_pattern_to_regex

        regex = _placeholder_pattern_to_regex("{site}.{rack}")
        # The literal dot must be escaped, not a regex wildcard
        assert r"\." in regex
        assert regex.startswith("^") and regex.endswith("$")

    def test_named_groups_present(self):
        from netbox_librenms_plugin.utils import _placeholder_pattern_to_regex

        regex = _placeholder_pattern_to_regex("{site} - {rack}")
        assert "(?P<site>" in regex
        assert "(?P<rack>" in regex


# =============================================================================
# TestParseLocationForImport
# =============================================================================


class TestParseLocationForImport:
    """Tests for the settings-aware parse_location_for_import() wrapper."""

    def test_no_pattern_uses_whole_string_for_site_and_location(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "get_location_parse_settings", return_value=("", False)):
            result = utils.parse_location_for_import("Some Location")
        assert result["site"] == "Some Location"
        assert result["location"] == "Some Location"
        assert result["rack"] is None

    def test_no_pattern_empty_string(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "get_location_parse_settings", return_value=("", False)):
            result = utils.parse_location_for_import("")
        assert result["site"] is None
        assert result["location"] is None

    def test_pattern_applied(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "get_location_parse_settings", return_value=("{site} - {rack}", False)):
            result = utils.parse_location_for_import("NYC - R1")
        assert result["site"] == "NYC"
        assert result["rack"] == "R1"

    def test_regex_pattern_applied(self):
        from netbox_librenms_plugin import utils

        with patch.object(
            utils,
            "get_location_parse_settings",
            return_value=(r"(?P<site>[^-]+)-(?P<rack>.+)", True),
        ):
            result = utils.parse_location_for_import("NYC-R1")
        assert result["site"] == "NYC"
        assert result["rack"] == "R1"

    def test_pattern_set_but_location_does_not_match_falls_back_to_whole_string(self):
        # A global parse pattern is best-effort: locations that don't fit the
        # pattern should still resolve a site from the whole string.
        from netbox_librenms_plugin import utils

        with patch.object(utils, "get_location_parse_settings", return_value=("{site} - {rack}", False)):
            result = utils.parse_location_for_import("PlainSiteName")
        assert result["site"] == "PlainSiteName"
        assert result["location"] == "PlainSiteName"
        assert result["rack"] is None

    def test_dict_location_is_normalised_to_name(self):
        # LibreNMS 26.5.0 returns location as a relationship object.
        from netbox_librenms_plugin import utils

        with patch.object(utils, "get_location_parse_settings", return_value=("", False)):
            result = utils.parse_location_for_import({"id": 1, "location": "NYC", "lat": 1.0, "lng": 2.0})
        assert result["site"] == "NYC"
        assert result["location"] == "NYC"


# =============================================================================
# TestGetLocationParseSettings
# =============================================================================


class TestGetLocationParseSettings:
    """Tests for get_location_parse_settings()."""

    def test_returns_defaults_when_no_settings_row(self):
        from netbox_librenms_plugin import utils

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.first.return_value = None
        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LibreNMSSettings=fake_model)},
        ):
            assert utils.get_location_parse_settings() == ("", False)

    def test_returns_configured_values(self):
        from netbox_librenms_plugin import utils

        settings = MagicMock(location_parse_pattern="{site}", location_parse_is_regex=True)
        fake_model = MagicMock()
        fake_model.objects.filter.return_value.first.return_value = settings
        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LibreNMSSettings=fake_model)},
        ):
            assert utils.get_location_parse_settings() == ("{site}", True)


# =============================================================================
# TestImportSettingsFormLocationValidation
# =============================================================================


class TestImportSettingsFormLocationValidation:
    """Tests for ImportSettingsForm.clean() location pattern validation."""

    def _run_clean(self, pattern, is_regex):
        from netbox_librenms_plugin.forms import ImportSettingsForm

        form = ImportSettingsForm.__new__(ImportSettingsForm)
        form.cleaned_data = {
            "location_parse_pattern": pattern,
            "location_parse_is_regex": is_regex,
        }
        form._errors = {}
        errors = {}

        def fake_add_error(field, msg):
            errors.setdefault(field, []).append(msg)

        form.add_error = fake_add_error
        with patch("netbox.models.NetBoxModel.clean", create=True):
            with patch.object(type(form).__mro__[1], "clean", lambda self: None, create=True):
                form.clean()
        return errors

    def test_blank_pattern_is_valid(self):
        errors = self._run_clean("", False)
        assert errors == {}

    def test_valid_placeholder_pattern(self):
        errors = self._run_clean("{site} - {rack}", False)
        assert errors == {}

    def test_invalid_placeholder_rejected(self):
        errors = self._run_clean("{site} - {bogus}", False)
        assert "location_parse_pattern" in errors

    def test_placeholder_with_no_tokens_rejected(self):
        errors = self._run_clean("no tokens here", False)
        assert "location_parse_pattern" in errors

    def test_malformed_unclosed_placeholder_rejected(self):
        # e.g. "{site}, {location, {rack}" — the middle placeholder is missing
        # its closing brace and must be flagged rather than silently ignored.
        errors = self._run_clean("{site}, {location, {rack}", False)
        assert "location_parse_pattern" in errors

    def test_valid_regex_pattern(self):
        errors = self._run_clean(r"(?P<site>[^-]+)-(?P<rack>.+)", True)
        assert errors == {}

    def test_regex_with_invalid_group_rejected(self):
        errors = self._run_clean(r"(?P<bogus>.+)", True)
        assert "location_parse_pattern" in errors

    def test_regex_with_no_named_group_rejected(self):
        errors = self._run_clean(r"\w+", True)
        assert "location_parse_pattern" in errors

    def test_malformed_regex_rejected(self):
        errors = self._run_clean(r"(?P<site>[", True)
        assert "location_parse_pattern" in errors

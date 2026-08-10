"""Unit tests for the shared utils helpers extracted to de-duplicate view logic.

* ``is_valid_ports_payload`` — the ports-payload shape gate used by the interface and module
  refresh paths (host, OOB and cached snapshots).
* ``resolve_server_mapping_display_id`` — the per-server display-id resolver (host id, else the
  nested OOB controller id) shared by the device-sync page and the import-validation modal.

Both are pure functions, so these exercise the real implementations directly with no test doubles.
"""

import pytest

from netbox_librenms_plugin.utils import (
    cached_row_matches,
    is_valid_ports_payload,
    resolve_server_mapping_display_id,
    row_identity_matches,
)


class TestCachedRowMatches:
    """The single- and bulk-import cache reads share this acceptance rule (can't drift)."""

    def test_matching_id_is_served(self):
        assert cached_row_matches({"device_id": 12, "hostname": "a"}, 12) is True

    def test_string_and_int_ids_normalize_equal(self):
        assert cached_row_matches({"device_id": "12"}, 12) is True

    def test_contradicting_id_is_rejected(self):
        # A mis-keyed/stale entry (another device's row under this key) must not be served.
        assert cached_row_matches({"device_id": 99}, 12) is False

    def test_row_without_device_id_stays_trusted(self):
        assert cached_row_matches({"hostname": "a"}, 12) is True

    def test_none_row_never_matches(self):
        assert cached_row_matches(None, 12) is False

    def test_non_dict_row_is_rejected(self):
        assert cached_row_matches(["not-a-dict"], 12) is False

    def test_two_invalid_ids_do_not_match(self):
        # Both ids un-coercible must not compare equal as None == None; fail closed.
        assert cached_row_matches({"device_id": "abc"}, "xyz") is False

    def test_invalid_requested_id_rejects_even_a_trusted_row(self):
        # An un-coercible requested id can't be identity-checked at all, so nothing matches it.
        assert cached_row_matches({"hostname": "a"}, "xyz") is False


class TestRowIdentityMatches:
    """The strict identity rule for the collision pre-check and the single-row fetch fallback."""

    def test_matching_dict_row(self):
        assert row_identity_matches({"device_id": 5, "hostname": "a"}, 5) is True

    def test_string_and_int_ids_normalize_equal(self):
        assert row_identity_matches({"device_id": "5"}, 5) is True

    def test_contradicting_id_fails(self):
        assert row_identity_matches({"device_id": 6}, 5) is False

    def test_row_without_device_id_fails(self):
        # Unlike cached_row_matches, a fetched row MUST carry its own id — fail closed.
        assert row_identity_matches({"hostname": "a"}, 5) is False

    def test_non_dict_payload_fails(self):
        assert row_identity_matches(["not-a-dict"], 5) is False

    def test_none_payload_fails(self):
        assert row_identity_matches(None, 5) is False

    def test_two_invalid_ids_do_not_match(self):
        # None == None must not read as a verified identity.
        assert row_identity_matches({"device_id": "abc"}, "abc") is False


class TestIsValidPortsPayload:
    @pytest.mark.parametrize(
        "payload",
        [
            {"ports": [{"port_id": 1}, {"port_id": 2}]},
            {"ports": [{}]},
            {"ports": []},  # empty but well-formed
            {"ports": [], "count": 0},  # extra keys are fine
        ],
    )
    def test_valid_shapes(self, payload):
        assert is_valid_ports_payload(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            "ports",
            42,
            {},  # missing "ports" -> .get returns None, not a list
            {"ports": None},
            {"ports": {}},  # dict, not list
            {"ports": "x"},
            {"ports": [{}, 5]},  # a non-dict row
            {"ports": [None]},
        ],
    )
    def test_invalid_shapes(self, payload):
        assert is_valid_ports_payload(payload) is False


class TestResolveServerMappingDisplayId:
    def test_scalar_valid_int(self):
        assert resolve_server_mapping_display_id(42) == (42, False)

    def test_scalar_valid_digit_string(self):
        assert resolve_server_mapping_display_id("42") == (42, False)

    @pytest.mark.parametrize("entry", [0, -1, "0", "abc", None, True, False])
    def test_scalar_invalid(self, entry):
        assert resolve_server_mapping_display_id(entry) == (None, False)

    def test_dict_host_id_wins(self):
        assert resolve_server_mapping_display_id({"id": 10}) == (10, False)

    def test_dict_host_id_wins_over_oob(self):
        # A valid host id is preferred; the OOB fallback is not consulted.
        assert resolve_server_mapping_display_id({"id": 10, "oob": {"id": 7}}) == (10, False)

    def test_dict_falls_back_to_oob_when_host_absent(self):
        assert resolve_server_mapping_display_id({"oob": {"id": 7}}) == (7, True)

    def test_dict_falls_back_to_oob_when_host_invalid(self):
        # Host id present but corrupt (0) -> still surface the real OOB-only linkage.
        assert resolve_server_mapping_display_id({"id": 0, "oob": {"id": 7}}) == (7, True)

    def test_dict_neither_host_nor_oob(self):
        assert resolve_server_mapping_display_id({"_migrated_to": "prod"}) == (None, False)

    def test_dict_host_and_oob_both_invalid(self):
        assert resolve_server_mapping_display_id({"id": 0, "oob": {"id": -1}}) == (None, False)

    def test_dict_oob_not_a_dict(self):
        assert resolve_server_mapping_display_id({"id": 0, "oob": 5}) == (None, False)


class TestRenderVcMemberOptions:
    """The shared VC-member <option> builder used by the interface/cable/module tables."""

    class _Member:
        def __init__(self, pk, name):
            self.id = pk
            self.name = name

    def test_marks_the_selected_member(self):
        from netbox_librenms_plugin.utils import render_vc_member_options

        html = render_vc_member_options([self._Member(1, "sw1"), self._Member(2, "sw2")], 2)
        assert '<option value="1">sw1</option>' in html
        assert '<option value="2" selected>sw2</option>' in html

    def test_string_selected_id_matches_int_member_id(self):
        # The module table's cached selected id can be a string; it must still match.
        from netbox_librenms_plugin.utils import render_vc_member_options

        html = render_vc_member_options([self._Member(3, "sw3")], "3")
        assert '<option value="3" selected>sw3</option>' in html

    def test_member_names_are_escaped(self):
        from django.utils.safestring import SafeString

        from netbox_librenms_plugin.utils import render_vc_member_options

        html = render_vc_member_options([self._Member(1, '<img src=x onerror=alert(1)>"')], None)
        assert "<img" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;&quot;" in html
        # SafeString so format_html() callers embed it without double-escaping.
        assert isinstance(html, SafeString)


class TestOobBadgeHtml:
    """The shared OOB-source badge used by the interface/module/cable tables and cable verify."""

    def test_oob_row_gets_the_badge(self):
        from django.utils.safestring import SafeString

        from netbox_librenms_plugin.constants import OOB_BADGE_HTML
        from netbox_librenms_plugin.utils import oob_badge_html

        html = oob_badge_html({"_source": "oob"})
        assert html == OOB_BADGE_HTML
        # SafeString so format_html() callers embed the trusted markup un-escaped.
        assert isinstance(html, SafeString)

    def test_leading_space_prefixes_the_badge(self):
        from django.utils.safestring import SafeString

        from netbox_librenms_plugin.constants import OOB_BADGE_HTML
        from netbox_librenms_plugin.utils import oob_badge_html

        html = oob_badge_html({"_source": "oob"}, leading_space=True)
        assert html == " " + OOB_BADGE_HTML
        assert isinstance(html, SafeString)

    @pytest.mark.parametrize("record", [{}, {"_source": "main"}, {"_source": None}, {"_source": "OOB"}])
    def test_non_oob_rows_get_no_badge(self, record):
        from netbox_librenms_plugin.utils import oob_badge_html

        assert oob_badge_html(record) == ""
        assert oob_badge_html(record, leading_space=True) == ""

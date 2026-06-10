"""Tests for multi-server librenms_id helpers.

Covers get_librenms_device_id, set_librenms_device_id, find_by_librenms_id,
and migrate_legacy_librenms_id.
"""

from unittest.mock import MagicMock


def _qs_returning(rows):
    """Build a mock queryset whose ``[:2]`` slice yields ``rows``.

    ``find_by_librenms_id`` calls ``list(model.objects.filter(q)[:2])`` per side, so the
    test double must support slice indexing rather than ``.first()``.
    """
    qs = MagicMock()
    qs.__getitem__.return_value = rows
    return qs


class TestGetLibreNMSDeviceId:
    """Tests for get_librenms_device_id()."""

    def test_returns_none_when_cf_missing(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {}
        result = get_librenms_device_id(obj, "default")
        assert result is None

    def test_returns_int_for_legacy_bare_integer(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": 42}
        result = get_librenms_device_id(obj, "default")
        assert result == 42

    def test_legacy_bare_int_returned_for_any_server_key(self):
        """Legacy bare integers are returned as a universal fallback for any server_key.

        Devices imported before multi-server support store a bare integer in
        librenms_id.  These must remain discoverable regardless of which server is
        active, so the bare-int is returned as-is for any server_key.
        """
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": 99}
        assert get_librenms_device_id(obj, "default") == 99
        assert get_librenms_device_id(obj, "production") == 99
        assert get_librenms_device_id(obj, "secondary") == 99

    def test_returns_value_for_matching_server_key(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"production": 7, "secondary": 12}}
        assert get_librenms_device_id(obj, "production") == 7

    def test_returns_none_for_missing_server_key_in_dict(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"production": 7}}
        result = get_librenms_device_id(obj, "secondary")
        assert result is None

    def test_returns_none_for_unexpected_type(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "not-an-int-or-dict"}
        result = get_librenms_device_id(obj, "default")
        assert result is None

    def test_legacy_string_int_returned_for_any_server_key(self):
        """A bare string integer ('42') is coerced and returned for any server_key."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "42"}
        assert get_librenms_device_id(obj, "default") == 42
        assert get_librenms_device_id(obj, "production") == 42

    def test_returns_none_for_bare_boolean(self):
        """bool is a subclass of int; bare True/False must not be treated as a valid ID."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": True}
        assert get_librenms_device_id(obj, "default") is None

        obj.cf = {"librenms_id": False}
        assert get_librenms_device_id(obj, "default") is None

    def test_returns_none_for_boolean_inside_dict(self):
        """Boolean values inside the JSON dict must be rejected."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": True}}
        assert get_librenms_device_id(obj, "default") is None

    def test_default_server_key_is_default(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": 5}}
        assert get_librenms_device_id(obj) == 5


class TestFindByLibreNMSId:
    """Tests for find_by_librenms_id()."""

    def test_queries_server_key_and_legacy_integer(self):
        """find_by_librenms_id() issues a Q that covers both the JSON server-key branch
        and the legacy bare-int branch in a single filter() call.

        We inspect the Q object's children directly because the two branches must
        coexist — matching only one would silently miss devices stored in the other
        format.
        """
        from unittest.mock import MagicMock
        from django.db.models import Q
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = []

        find_by_librenms_id(mock_model, 42, "default")

        # Host and OOB predicate sets are queried separately (so a host match and a
        # different OOB match can be detected and rejected). Their union must still cover
        # the server-key JSON branch, the __id/__oob__id dict forms, and the legacy
        # bare-int/string branches.
        assert mock_model.objects.filter.call_count == 2
        all_children = set()
        for call in mock_model.objects.filter.call_args_list:
            q_arg = call[0][0]
            assert isinstance(q_arg, Q)
            assert q_arg.connector == "OR"
            all_children |= set(q_arg.children)
        assert all_children == {
            ("custom_field_data__librenms_id__default", 42),
            ("custom_field_data__librenms_id__default", "42"),
            ("custom_field_data__librenms_id__default__id", 42),
            ("custom_field_data__librenms_id__default__id", "42"),
            ("custom_field_data__librenms_id__default__oob__id", 42),
            ("custom_field_data__librenms_id__default__oob__id", "42"),
            ("custom_field_data__librenms_id", 42),
            ("custom_field_data__librenms_id", "42"),
        }

    def test_returns_first_matching_object(self):
        from netbox_librenms_plugin.utils import find_by_librenms_id

        expected = MagicMock(pk=7)
        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = [expected]

        result = find_by_librenms_id(mock_model, 42, "default")
        assert result is expected

    def test_returns_none_when_not_found(self):
        from unittest.mock import MagicMock
        from django.db.models import Q
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = []

        result = find_by_librenms_id(mock_model, 999, "production")
        assert result is None

        # Any server_key must include legacy bare-int/string fallback conditions
        # so that devices imported before multi-server support are still found.
        # (Predicates are split across the host and OOB queries — aggregate them.)
        all_children = set()
        for call in mock_model.objects.filter.call_args_list:
            q_arg = call[0][0]
            assert isinstance(q_arg, Q)
            all_children |= set(q_arg.children)
        assert all_children == {
            ("custom_field_data__librenms_id__production", 999),
            ("custom_field_data__librenms_id__production", "999"),
            ("custom_field_data__librenms_id__production__id", 999),
            ("custom_field_data__librenms_id__production__id", "999"),
            ("custom_field_data__librenms_id__production__oob__id", 999),
            ("custom_field_data__librenms_id__production__oob__id", "999"),
            ("custom_field_data__librenms_id", 999),
            ("custom_field_data__librenms_id", "999"),
        }

    def test_default_server_key_is_default(self):
        """
        find_by_librenms_id() uses "default" as the server key when no key is passed.

        We inspect the Q predicate's children to confirm the key embedded in the
        JSON path is exactly "default", not some other fallback value.
        """
        from unittest.mock import MagicMock
        from django.db.models import Q
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = []

        find_by_librenms_id(mock_model, 42)

        # The JSON-path branch must use "default" as the server key; exact tuple check
        # across the host + OOB queries prevents duplicate or missing branches.
        assert mock_model.objects.filter.call_count == 2
        all_children = set()
        for call in mock_model.objects.filter.call_args_list:
            q_arg = call[0][0]
            assert isinstance(q_arg, Q)
            assert q_arg.connector == "OR"
            all_children |= set(q_arg.children)
        assert all_children == {
            ("custom_field_data__librenms_id__default", 42),
            ("custom_field_data__librenms_id__default", "42"),
            ("custom_field_data__librenms_id__default__id", 42),
            ("custom_field_data__librenms_id__default__id", "42"),
            ("custom_field_data__librenms_id__default__oob__id", 42),
            ("custom_field_data__librenms_id__default__oob__id", "42"),
            ("custom_field_data__librenms_id", 42),
            ("custom_field_data__librenms_id", "42"),
        }

    def test_fail_closed_when_host_and_oob_match_different_rows(self):
        """If the host query matches one object and the OOB query a *different* one, the
        result is ambiguous — return None rather than binding to whichever sorts first."""
        from unittest.mock import MagicMock
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock(__name__="Device")  # __name__ used in the fail-closed log
        host_obj = MagicMock(pk=1)
        oob_obj = MagicMock(pk=2)
        # filter() is called twice (host_q, then oob_q); each result is sliced [:2].
        mock_model.objects.filter.side_effect = [
            _qs_returning([host_obj]),
            _qs_returning([oob_obj]),
        ]

        assert find_by_librenms_id(mock_model, 42, "default") is None

    def test_same_row_for_host_and_oob_is_returned(self):
        """When both queries resolve to the same row, it is returned (not ambiguous)."""
        from unittest.mock import MagicMock
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        same = MagicMock(pk=5)
        mock_model.objects.filter.side_effect = [_qs_returning([same]), _qs_returning([same])]

        assert find_by_librenms_id(mock_model, 42, "default") is same

    def test_host_match_wins_when_no_oob_match(self):
        """Host identity is returned when only the host query matches."""
        from unittest.mock import MagicMock
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        host_obj = MagicMock(pk=1)
        mock_model.objects.filter.side_effect = [_qs_returning([host_obj]), _qs_returning([])]

        assert find_by_librenms_id(mock_model, 42, "default") is host_obj

    def test_fail_closed_on_duplicate_host_matches(self):
        """Two distinct rows sharing the same host librenms_id is a data-integrity
        violation — refuse to bind to either rather than picking whichever sorts first."""
        from unittest.mock import MagicMock
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock(__name__="Device")
        # Host query returns two rows; OOB query is irrelevant (host ambiguity fails first).
        mock_model.objects.filter.side_effect = [
            _qs_returning([MagicMock(pk=1), MagicMock(pk=2)]),
            _qs_returning([]),
        ]

        assert find_by_librenms_id(mock_model, 42, "default") is None

    def test_fail_closed_on_duplicate_oob_matches(self):
        """Two distinct rows sharing the same OOB librenms_id is likewise rejected."""
        from unittest.mock import MagicMock
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock(__name__="Device")
        mock_model.objects.filter.side_effect = [
            _qs_returning([]),
            _qs_returning([MagicMock(pk=3), MagicMock(pk=4)]),
        ]

        assert find_by_librenms_id(mock_model, 42, "default") is None


class TestMigrateLegacyLibreNMSId:
    """Tests for migrate_legacy_librenms_id()."""

    def test_returns_true_when_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is True

    def test_migrates_integer_to_dict_format(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}
        migrate_legacy_librenms_id(obj, "production")
        assert obj.custom_field_data["librenms_id"] == {"production": 42}

    def test_returns_false_when_already_dict(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 42}}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False

    def test_returns_false_when_value_is_none(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": None}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False

    def test_returns_false_for_boolean_value(self):
        """bool is a subclass of int; True/False must not be migrated."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": True}
        assert migrate_legacy_librenms_id(obj, "default") is False
        assert obj.custom_field_data["librenms_id"] is True  # unchanged

    def test_does_not_call_save(self):
        """migrate_legacy_librenms_id must NOT call obj.save() — caller is responsible."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 7}
        migrate_legacy_librenms_id(obj, "default")
        obj.save.assert_not_called()

    def test_preserves_value_in_migrated_dict(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 99}
        migrate_legacy_librenms_id(obj, "secondary")
        assert obj.custom_field_data["librenms_id"]["secondary"] == 99


class TestLibreNMSIdRoundtrip:
    """get_librenms_device_id should see the value set by set_librenms_device_id."""

    def test_set_then_get_returns_same_value(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        obj.cf = obj.custom_field_data  # make cf a live view of custom_field_data

        set_librenms_device_id(obj, 42, "production")
        result = get_librenms_device_id(obj, "production")
        assert result == 42

    def test_set_multiple_servers_get_correct_each(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        obj.cf = obj.custom_field_data

        set_librenms_device_id(obj, 10, "primary")
        set_librenms_device_id(obj, 20, "secondary")

        assert get_librenms_device_id(obj, "primary") == 10
        assert get_librenms_device_id(obj, "secondary") == 20

    def test_migrate_then_get_returns_value(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 55}
        obj.cf = obj.custom_field_data

        migrate_legacy_librenms_id(obj, "default")
        result = get_librenms_device_id(obj, "default")
        assert result == 55


class TestSetLibreNMSDeviceId:
    """Tests for set_librenms_device_id in utils.py."""

    def test_stores_int_for_valid_device_id(self):
        """Valid integer device_id is stored under server_key."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": None}
        set_librenms_device_id(obj, 42, server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 42}

    def test_invalid_device_id_not_stored(self):
        """Non-integer device_id is rejected and nothing is written."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        set_librenms_device_id(obj, "not-an-int", server_key="primary")
        assert "librenms_id" not in obj.custom_field_data

    def test_invalid_device_id_does_not_overwrite_existing(self):
        """Existing valid value is preserved when new device_id is invalid."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 10}}
        set_librenms_device_id(obj, None, server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 10}

    def test_legacy_bare_int_blocks_write(self):
        """Legacy bare-integer value blocks the write (no silent migration)."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 7}
        set_librenms_device_id(obj, 99, server_key="secondary")
        # Write must be skipped; user must use the migration workflow.
        assert obj.custom_field_data["librenms_id"] == 7

    def test_adds_new_server_key_to_existing_dict(self):
        """Adding a new server key preserves existing keys."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 5}}
        set_librenms_device_id(obj, 20, server_key="secondary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 5, "secondary": 20}

    def test_string_integer_is_coerced(self):
        """String '42' is coerced to int 42."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        set_librenms_device_id(obj, "42", server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 42}

    def test_unexpected_cf_type_reset_to_empty(self):
        """If custom_field_data has unexpected type for librenms_id, it is reset."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": "unexpected-string"}
        set_librenms_device_id(obj, 5, server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 5}


class TestOOBHelpers:
    """Tests for get_librenms_oob, set_librenms_oob, clear_librenms_oob,
    and the dict-with-id changes to get/set_librenms_device_id and find_by_librenms_id.
    """

    # ── get_librenms_device_id: dict-with-id form ─────────────────────────────

    def test_get_id_from_dict_with_id_form(self):
        """get_librenms_device_id extracts 'id' from {"server": {"id": N}} form."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"primary": {"id": 42}}}
        assert get_librenms_device_id(obj, "primary") == 42

    def test_get_id_when_oob_also_present(self):
        """get_librenms_device_id returns the main id, ignoring the oob sub-object."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"primary": {"id": 42, "oob": {"id": 17, "type": "drac"}}}}
        assert get_librenms_device_id(obj, "primary") == 42

    def test_get_returns_none_for_dict_without_id_key(self):
        """dict entry without 'id' key returns None."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"primary": {"oob": {"id": 17}}}}
        assert get_librenms_device_id(obj, "primary") is None

    def test_get_normalises_string_id_inside_dict_with_id_form(self):
        """String 'id' inside dict-with-id form is coerced to int."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"primary": {"id": "42"}}}
        obj.custom_field_data = {"librenms_id": {"primary": {"id": "42"}}}
        result = get_librenms_device_id(obj, "primary", auto_save=False)
        assert result == 42

    # ── set_librenms_device_id: oob preservation ─────────────────────────────

    def test_set_preserves_oob_when_entry_has_oob(self):
        """Updating main id preserves existing oob sub-object."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {
            "librenms_id": {"primary": {"id": 42, "oob": {"id": 17, "type": "drac", "ip": "10.0.0.5"}}}
        }
        set_librenms_device_id(obj, 99, server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {
            "primary": {"id": 99, "oob": {"id": 17, "type": "drac", "ip": "10.0.0.5"}}
        }

    def test_set_bare_int_when_no_oob_present(self):
        """When no oob in existing entry, set_librenms_device_id stores bare int (no regression)."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 42}}
        set_librenms_device_id(obj, 99, server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 99}

    # ── find_by_librenms_id: dict-with-id and oob id lookups ─────────────────

    def test_find_by_matches_main_id_in_dict_with_id_form(self):
        """find_by_librenms_id Q includes __id sub-key lookup."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = []

        find_by_librenms_id(mock_model, 42, "primary")

        # Host and OOB predicate sets are now queried separately, so aggregate the
        # children across every filter() call rather than inspecting only the last.
        all_children = set()
        for call in mock_model.objects.filter.call_args_list:
            all_children |= set(call[0][0].children)
        assert ("custom_field_data__librenms_id__primary__id", 42) in all_children
        assert ("custom_field_data__librenms_id__primary__id", "42") in all_children

    def test_find_by_matches_oob_id(self):
        """find_by_librenms_id Q includes __oob__id sub-key lookup."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = []

        find_by_librenms_id(mock_model, 17, "primary")

        call_args = mock_model.objects.filter.call_args
        q_arg = call_args[0][0]
        assert ("custom_field_data__librenms_id__primary__oob__id", 17) in q_arg.children
        assert ("custom_field_data__librenms_id__primary__oob__id", "17") in q_arg.children

    def test_find_by_does_not_return_unrelated_id(self):
        """find_by_librenms_id returns None when no model matches."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.__getitem__.return_value = []

        result = find_by_librenms_id(mock_model, 999, "primary")
        assert result is None

    # ── get_librenms_oob ──────────────────────────────────────────────────────

    def test_get_oob_returns_none_for_legacy_bare_int(self):
        from netbox_librenms_plugin.utils import get_librenms_oob

        obj = MagicMock()
        obj.cf = {"librenms_id": 42}
        assert get_librenms_oob(obj, "primary") is None

    def test_get_oob_returns_none_for_bare_int_entry(self):
        """When server-key entry is a bare int (no oob), returns None."""
        from netbox_librenms_plugin.utils import get_librenms_oob

        obj = MagicMock()
        obj.cf = {"librenms_id": {"primary": 42}}
        assert get_librenms_oob(obj, "primary") is None

    def test_get_oob_returns_oob_dict_when_present(self):
        from netbox_librenms_plugin.utils import get_librenms_oob

        oob_data = {"id": 17, "type": "drac", "version": "5.10", "ip": "10.0.0.5"}
        obj = MagicMock()
        obj.cf = {"librenms_id": {"primary": {"id": 42, "oob": oob_data}}}
        result = get_librenms_oob(obj, "primary")
        assert result == oob_data

    # ── set_librenms_oob ──────────────────────────────────────────────────────

    def test_set_oob_round_trip(self):
        """set_librenms_oob stores only id + type; ip/version are not persisted.

        Mutable LibreNMS state (the controller's IP and firmware version) is
        deliberately NOT denormalised into the librenms_id custom field — it is
        read live from LibreNMS via the stored id when needed.
        """
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 42}}
        obj.cf = obj.custom_field_data

        set_librenms_oob(obj, 17, "primary", oob_type="drac")
        result = get_librenms_oob(obj, "primary")

        assert result == {"id": 17, "type": "drac"}

    def test_set_oob_promotes_bare_int_entry(self):
        """set_librenms_oob promotes a bare-int entry to dict form, preserving the main id."""
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 42}}
        obj.cf = obj.custom_field_data

        set_librenms_oob(obj, 17, "primary", oob_type="idrac")
        assert get_librenms_device_id(obj, "primary") == 42

    def test_set_oob_rejects_unknown_type(self):
        """set_librenms_oob raises ValueError for a type that doesn't match OOB_TYPE_PATTERN."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 42}}

        import pytest

        with pytest.raises(ValueError, match="does not match any known OOB type"):
            set_librenms_oob(obj, 17, "primary", oob_type="ubuntu")

    def test_set_oob_fails_closed_on_corrupt_host_string(self):
        """A non-empty, unparseable stored host id must raise rather than be silently
        collapsed to {} (which would drop the host link). Mirrors merge_librenms_links."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": "not-an-id"}}

        import pytest

        with pytest.raises(ValueError, match="not a valid id"):
            set_librenms_oob(obj, 17, "primary", oob_type="idrac")

    def test_set_oob_fails_closed_on_corrupt_dict_host_id(self):
        """A dict-form entry with a non-empty unparseable host id (e.g. {"id": "abc"})
        must raise, not get OOB metadata attached over corrupt host state."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": {"id": "abc"}}}

        import pytest

        with pytest.raises(ValueError, match="not a valid id"):
            set_librenms_oob(obj, 17, "primary", oob_type="idrac")

    def test_set_oob_lenient_on_dict_without_host_id(self):
        """A dict entry with no host id (absent/None) stays lenient — OOB is attached."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": {"id": None}}}

        set_librenms_oob(obj, 17, "primary", oob_type="idrac")  # must not raise
        assert obj.custom_field_data["librenms_id"]["primary"]["oob"] == {"id": 17, "type": "idrac"}

    def test_set_oob_lenient_on_empty_host_string(self):
        """An empty/whitespace host string is treated leniently (→ fresh dict), not an
        error — consistent with merge_librenms_links's empty-string handling."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": "   "}}

        set_librenms_oob(obj, 17, "primary", oob_type="idrac")  # must not raise
        assert obj.custom_field_data["librenms_id"]["primary"]["oob"] == {"id": 17, "type": "idrac"}

    def test_set_oob_accepts_generic_oob_sentinel(self):
        """set_librenms_oob must accept "oob" as a generic fallback type.

        The detection layer in device_operations.py uses "oob" as a sentinel when
        neither the LibreNMS OS/hardware fields nor the device names contain any
        specific OOB keyword (idrac/ilo/ipmi/bmc/drac). This is the common case for
        devices like switch consoles or PDUs. Rejecting "oob" caused HTTP 400 on
        AddAsOOBView / PromoteToHostView for every such device.
        """
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 99}}
        obj.cf = obj.custom_field_data

        set_librenms_oob(obj, 55, "default", oob_type="oob")
        result = get_librenms_oob(obj, "default")

        assert result is not None
        assert result["id"] == 55
        assert result["type"] == "oob"

    def test_set_oob_generic_sentinel_case_insensitive(self):
        """The "oob" sentinel is accepted case-insensitively (OOB, Oob, etc.)."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 99}}

        # Should not raise
        set_librenms_oob(obj, 55, "default", oob_type="OOB")
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"

    def test_set_oob_does_not_call_save(self):
        """set_librenms_oob must NOT call obj.save() — caller is responsible."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 42}}

        set_librenms_oob(obj, 17, "primary", oob_type="ilo")
        obj.save.assert_not_called()

    # ── clear_librenms_oob ────────────────────────────────────────────────────

    def test_clear_oob_removes_oob_sub_key(self):
        from netbox_librenms_plugin.utils import clear_librenms_oob, get_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": {"id": 42, "oob": {"id": 17, "type": "drac"}}}}
        obj.cf = obj.custom_field_data

        clear_librenms_oob(obj, "primary")
        assert get_librenms_oob(obj, "primary") is None
        # Main id should still be accessible via dict-with-id form
        assert obj.custom_field_data["librenms_id"]["primary"] == {"id": 42}

    def test_clear_oob_is_noop_when_no_oob(self):
        """clear_librenms_oob is a no-op when oob key is not present."""
        from netbox_librenms_plugin.utils import clear_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": {"id": 42}}}

        clear_librenms_oob(obj, "primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": {"id": 42}}

    def test_clear_oob_does_not_call_save(self):
        """clear_librenms_oob must NOT call obj.save() — caller is responsible."""
        from netbox_librenms_plugin.utils import clear_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": {"id": 42, "oob": {"id": 17, "type": "bmc"}}}}

        clear_librenms_oob(obj, "primary")
        obj.save.assert_not_called()


class TestMergeLibreNMSLinks:
    """Tests for merge_librenms_links() — winner-wins conflict policy."""

    def _make_dev(self, name, librenms_id_dict):
        d = MagicMock()
        d.name = name
        d.custom_field_data = {"librenms_id": librenms_id_dict} if librenms_id_dict is not None else {}
        return d

    def test_winner_inherits_id_when_winner_has_no_id(self):
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 99
        assert summary["host_id_from_donor"] == 99
        assert summary["donor_id_demoted_to_oob"] is None

    def test_winner_inherits_string_id_coerced_to_int(self):
        """inherit-id branch must coerce donor_id to int, matching the demote branch."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {}})
        # Simulate a custom field value that arrived as a JSON string (e.g. "99").
        donor = self._make_dev("router-spare", {"default": {"id": "99"}})
        summary = merge_librenms_links(winner, donor, "default")

        stored = winner.custom_field_data["librenms_id"]["default"]["id"]
        assert stored == 99
        assert isinstance(stored, int)
        assert summary["host_id_from_donor"] == 99
        assert isinstance(summary["host_id_from_donor"], int)

    def test_donor_id_demoted_to_oob_when_winner_has_id_and_donor_name_matches_oob_pattern(self):
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 42
        assert winner.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 99
        assert winner.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "idrac"
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}

    def test_donor_real_oob_preferred_over_demoting_donor_id(self):
        """Donor shaped {"id": X, "oob": {...}} with the winner already holding a host id:
        the donor's REAL oob must be inherited, not the donor's host id demoted into oob —
        otherwise the actual OOB controller is silently lost during merge."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99, "oob": {"id": 77, "type": "ilo"}}})
        summary = merge_librenms_links(winner, donor, "default")

        # Winner keeps its own host id and inherits the donor's REAL oob (77/ilo) — the
        # donor's host id (99) is NOT demoted into oob.
        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 42
        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ilo"}
        assert summary.get("oob_from_donor") == {"id": 77, "type": "ilo"}
        assert summary.get("donor_id_demoted_to_oob") is None

    def test_donor_id_demoted_to_oob_generic_when_no_pattern_in_name(self):
        """Donor id is always demoted; type falls back to 'oob' when no keyword in name."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-03-spare", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 42
        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 99, "type": "oob"}
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "oob"}

    def test_winner_inherits_donor_oob_when_winner_has_none(self):
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ipmi"}
        assert summary["oob_from_donor"] == {"id": 77, "type": "ipmi"}

    def test_malformed_donor_oob_id_fails_closed_on_inherit(self):
        """A corrupt donor oob link ({"oob": {"id": "abc"}}) must not be inherited verbatim;
        the inherit branch coerces the host id and raises on a non-empty invalid value."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": "abc", "type": "ipmi"}}})
        with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
            merge_librenms_links(winner, donor, "default")

    def test_donor_oob_id_coerced_to_int_on_inherit(self):
        """A numeric-string donor oob id is normalized to int when inherited."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": "77", "type": "ipmi"}}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ipmi"}
        assert summary["oob_from_donor"] == {"id": 77, "type": "ipmi"}

    def test_blank_donor_oob_id_is_lenient_and_dropped(self):
        """A blank/whitespace donor oob id ({"oob": {"id": "   "}}) must be treated as 'no oob
        id' (lenient) — the same as a blank host id and an absent oob id — not raise like a
        non-blank corrupt one. The blank id is dropped so the winner inherits the oob (type)
        without carrying a bogus id string."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-x", {"default": {"oob": {"id": "   ", "type": "drac"}}})
        summary = merge_librenms_links(winner, donor, "default")

        inherited = winner.custom_field_data["librenms_id"]["default"]["oob"]
        assert inherited == {"type": "drac"}  # blank id dropped, type preserved
        assert "id" not in inherited
        assert summary["oob_from_donor"] == {"type": "drac"}

    def test_winner_oob_never_overwritten(self):
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": {"id": 11, "type": "drac"}}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 11, "type": "drac"}
        assert summary["oob_from_donor"] is None

    def test_legacy_bare_int_raises(self):
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = MagicMock()
        winner.custom_field_data = {"librenms_id": 42}
        donor = self._make_dev("idrac-x", {"default": {"id": 99}})
        with pytest.raises(ValueError):
            merge_librenms_links(winner, donor, "default")

    def test_malformed_donor_id_raises_clear_error_in_inherit_branch(self):
        """coerce_librenms_id raises ValueError with a clear message for non-numeric donor ids."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {}})
        donor = self._make_dev("router-spare", {"default": {"id": "not-a-number"}})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            merge_librenms_links(winner, donor, "default")

    def test_malformed_per_server_string_id_fails_closed(self):
        """A bare per-server string entry ({server_key: 'abc'}) that can't be parsed must
        raise, not silently collapse to {} (which would drop/swap link state)."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        # Bad winner string id.
        winner = self._make_dev("eve-ng-02", {"default": "abc"})
        donor = self._make_dev("router-spare", {"default": {"id": 99}})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            merge_librenms_links(winner, donor, "default")

        # Bad donor string id.
        winner = self._make_dev("eve-ng-02", {"default": {}})
        donor = self._make_dev("router-spare", {"default": "xyz"})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            merge_librenms_links(winner, donor, "default")

    def test_empty_per_server_string_id_is_lenient(self):
        """An empty/whitespace string is treated as 'no id', not a hard error."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": "  "})
        donor = self._make_dev("router-spare", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")
        # Winner had no usable id → inherits donor's host id.
        assert summary["host_id_from_donor"] == 99

    def test_blank_dict_form_id_is_lenient(self):
        """A blank/whitespace dict-form id ({"id": "   "}) must be treated as 'no id'
        (lenient), the same as a blank top-level string — not raise like a non-blank
        corrupt id ('abc'). Equivalent stored states must behave the same."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": "   "}})
        donor = self._make_dev("router-spare", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")
        # Winner's blank id is "no id" → it inherits the donor's host id rather than raising.
        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 99
        assert summary["host_id_from_donor"] == 99

    def test_malformed_donor_id_raises_clear_error_in_demote_branch(self):
        """Same clear error when demoting donor id into winner's oob slot."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": "bad"}})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            merge_librenms_links(winner, donor, "default")


class TestMarkLibreNMSMigrated:
    """Tests for mark_librenms_migrated()."""

    def test_clears_id_and_oob_and_writes_marker(self):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        donor = MagicMock()
        donor.custom_field_data = {"librenms_id": {"default": {"id": 99, "oob": {"id": 11, "type": "drac"}}}}
        mark_librenms_migrated(donor, winner_pk=42, server_key="default", at="2025-01-01T00:00:00Z")

        entry = donor.custom_field_data["librenms_id"]["default"]
        assert "id" not in entry
        assert "oob" not in entry
        assert entry["_migrated_to"] == {
            "device_id": 42,
            "server_key": "default",
            "at": "2025-01-01T00:00:00Z",
        }

    def test_default_timestamp_is_iso_z(self):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        donor = MagicMock()
        donor.custom_field_data = {"librenms_id": {"default": {"id": 99}}}
        mark_librenms_migrated(donor, winner_pk=42, server_key="default")

        ts = donor.custom_field_data["librenms_id"]["default"]["_migrated_to"]["at"]
        # Contract: an ISO-8601 UTC string ending in "Z" (tolerate fractional seconds).
        assert ts.endswith("Z")
        from datetime import datetime

        datetime.fromisoformat(ts.replace("Z", "+00:00"))  # must parse without raising

    def test_rejects_bool_and_non_positive_winner_pk(self):
        import pytest

        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for bad in (True, 0, -1):
            donor = MagicMock()
            donor.custom_field_data = {"librenms_id": {"default": {"id": 99}}}
            with pytest.raises(ValueError):
                mark_librenms_migrated(donor, winner_pk=bad, server_key="default")

    def test_after_marker_find_by_librenms_id_no_longer_matches(self):
        """Donor with only _migrated_to should not be returned by find_by_librenms_id."""
        from netbox_librenms_plugin.utils import find_by_librenms_id, mark_librenms_migrated

        donor = MagicMock()
        donor.custom_field_data = {"librenms_id": {"default": {"id": 99}}}
        donor.cf = donor.custom_field_data
        mark_librenms_migrated(donor, winner_pk=42, server_key="default")

        # cf.librenms_id[default] now only has _migrated_to — no id, no oob.
        # find_by_librenms_id walks cf via the model query, but logic-wise: simulate
        # by directly inspecting the entry.
        entry = donor.cf["librenms_id"]["default"]
        assert entry.get("id") is None
        assert entry.get("oob") is None
        # Mock model.objects.filter: should return empty queryset for either id or oob lookup
        mock_model = MagicMock()
        mock_model.objects.filter.return_value.__getitem__.return_value = []
        assert find_by_librenms_id(mock_model, 99, "default") is None

        # Prove the function actually built the lookup (not a vacuous pass): the
        # combined Q must target the id/oob predicates and must NEVER match on the
        # _migrated_to marker, so a migrated-only donor can't be returned.
        # Host and OOB predicates are queried separately now — aggregate across calls.
        lookups = []
        for call in mock_model.objects.filter.call_args_list:
            lookups += [child[0] for child in call[0][0].children]
        assert any(lk == "custom_field_data__librenms_id__default__id" for lk in lookups)
        assert any(lk == "custom_field_data__librenms_id__default__oob__id" for lk in lookups)
        assert all("_migrated_to" not in lk for lk in lookups)

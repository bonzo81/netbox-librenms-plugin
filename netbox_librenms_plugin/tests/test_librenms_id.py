"""
Tests for multi-server librenms_id helpers.

Covers get_librenms_device_id, set_librenms_device_id, find_by_librenms_id,
and migrate_legacy_librenms_id.
"""

from unittest.mock import MagicMock


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
        """
        Legacy bare integers are returned as a universal fallback for any server_key.

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


class TestGetLibreNMSDeviceIdAutoSave:
    """Tests for auto_save behaviour of get_librenms_device_id()."""

    def test_auto_save_true_mutates_bare_string(self):
        """When auto_save=True (default), a bare string is normalised in-place and saved."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "42"}
        obj.custom_field_data = {"librenms_id": "42"}
        result = get_librenms_device_id(obj, "default", auto_save=True)
        assert result == 42
        assert obj.custom_field_data["librenms_id"] == 42
        obj.save.assert_called_once()

    def test_auto_save_false_does_not_mutate_bare_string(self):
        """When auto_save=False, bare string is returned as int but obj is not mutated."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "42"}
        obj.custom_field_data = {"librenms_id": "42"}
        result = get_librenms_device_id(obj, "default", auto_save=False)
        assert result == 42
        assert obj.custom_field_data["librenms_id"] == "42"
        obj.save.assert_not_called()

    def test_auto_save_false_does_not_mutate_string_in_dict(self):
        """When auto_save=False, string inside dict is returned as int but dict is not mutated."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"prod": "7"}}
        obj.custom_field_data = {"librenms_id": {"prod": "7"}}
        result = get_librenms_device_id(obj, "prod", auto_save=False)
        assert result == 7
        assert obj.custom_field_data["librenms_id"]["prod"] == "7"
        obj.save.assert_not_called()

    def test_auto_save_true_mutates_string_in_dict(self):
        """When auto_save=True, string inside dict is normalised and saved."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"prod": "7"}}
        obj.custom_field_data = {"librenms_id": {"prod": "7"}}
        result = get_librenms_device_id(obj, "prod", auto_save=True)
        assert result == 7
        assert obj.custom_field_data["librenms_id"]["prod"] == 7
        obj.save.assert_called_once()


class TestFindByLibreNMSId:
    """Tests for find_by_librenms_id()."""

    def test_rejects_boolean_true(self):
        """find_by_librenms_id(model, True) returns None without querying."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        result = find_by_librenms_id(mock_model, True, "default")
        assert result is None
        mock_model.objects.filter.assert_not_called()

    def test_rejects_boolean_false(self):
        """find_by_librenms_id(model, False) returns None without querying."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        result = find_by_librenms_id(mock_model, False, "default")
        assert result is None
        mock_model.objects.filter.assert_not_called()

    def test_queries_server_key_and_legacy_integer(self):
        """
        find_by_librenms_id() issues a Q that covers both the JSON server-key branch
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
        mock_qs.first.return_value = None

        find_by_librenms_id(mock_model, 42, "default")

        mock_model.objects.filter.assert_called_once()
        # Verify the Q predicate covers both the server-key JSON branch and legacy bare-int/string branches
        call_args = mock_model.objects.filter.call_args
        q_arg = call_args[0][0]
        assert isinstance(q_arg, Q)
        assert q_arg.connector == "OR"
        # The combined Q should contain four children: JSON key (int), JSON key (str), bare-int, bare-string
        q_str = str(q_arg)
        assert "librenms_id__default" in q_str
        assert "custom_field_data__librenms_id__default" in q_str
        assert "custom_field_data__librenms_id" in q_str
        assert "42" in q_str

    def test_returns_first_matching_object(self):
        from netbox_librenms_plugin.utils import find_by_librenms_id

        expected = MagicMock()
        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = expected

        result = find_by_librenms_id(mock_model, 42, "default")
        assert result is expected

    def test_returns_none_when_not_found(self):
        from unittest.mock import MagicMock
        from django.db.models import Q
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = None

        result = find_by_librenms_id(mock_model, 999, "production")
        assert result is None

        # Any server_key must include legacy bare-int/string fallback conditions
        # so that devices imported before multi-server support are still found.
        call_args = mock_model.objects.filter.call_args
        q_arg = call_args[0][0]
        assert isinstance(q_arg, Q)
        q_str = str(q_arg)
        assert "custom_field_data__librenms_id__production" in q_str
        assert "custom_field_data__librenms_id" in q_str

    def test_default_server_key_is_default(self):
        from netbox_librenms_plugin.utils import find_by_librenms_id
        from django.db.models import Q

        mock_model = MagicMock()
        mock_qs = MagicMock()
        mock_model.objects.filter.return_value = mock_qs
        mock_qs.first.return_value = None

        find_by_librenms_id(mock_model, 42)

        # Verify the Q predicate uses "default" server key — not an arbitrary key
        mock_model.objects.filter.assert_called_once()
        call_args = mock_model.objects.filter.call_args
        q_arg = call_args[0][0]
        assert isinstance(q_arg, Q)
        assert q_arg.connector == "OR"
        q_str = str(q_arg)
        assert "custom_field_data__librenms_id__default" in q_str


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

    def test_migrates_string_digit_legacy_id(self):
        """A string-digit like "42" should be migrated to {server_key: 42} (int)."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": "42"}
        result = migrate_legacy_librenms_id(obj, "production")
        assert result is True
        assert obj.custom_field_data["librenms_id"] == {"production": 42}
        assert isinstance(obj.custom_field_data["librenms_id"]["production"], int)
        obj.save.assert_not_called()

    def test_returns_false_for_non_digit_string(self):
        """A non-digit string should not be migrated."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": "not-a-number"}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False
        assert obj.custom_field_data["librenms_id"] == "not-a-number"

    def test_returns_false_for_plus_prefix_string(self):
        """'+42' is not strictly digit-only; must not be migrated."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": "+42"}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False
        assert obj.custom_field_data["librenms_id"] == "+42"
        obj.save.assert_not_called()

    def test_returns_false_for_space_padded_string(self):
        """' 42 ' is not strictly digit-only; must not be migrated."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": " 42 "}
        result = migrate_legacy_librenms_id(obj, "default")
        assert result is False
        assert obj.custom_field_data["librenms_id"] == " 42 "
        obj.save.assert_not_called()

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

    def test_legacy_bare_string_int_blocks_write(self):
        """Legacy bare numeric string value blocks the write (no silent migration)."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": "7"}
        set_librenms_device_id(obj, 99, server_key="secondary")
        # Write must be skipped; user must use the migration workflow.
        assert obj.custom_field_data["librenms_id"] == "7"

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

    def test_boolean_true_rejected(self):
        """Boolean True is not accepted as a valid device_id (bool is subclass of int)."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {}
        set_librenms_device_id(obj, True, server_key="primary")
        assert "librenms_id" not in obj.custom_field_data

    def test_boolean_false_rejected(self):
        """Boolean False is not accepted as a valid device_id."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 10}}
        set_librenms_device_id(obj, False, server_key="primary")
        # Existing mapping must be preserved
        assert obj.custom_field_data["librenms_id"] == {"primary": 10}

    def test_unexpected_cf_type_reset_to_empty(self):
        """If custom_field_data has unexpected type for librenms_id, it is reset."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": "unexpected-string"}
        set_librenms_device_id(obj, 5, server_key="primary")
        assert obj.custom_field_data["librenms_id"] == {"primary": 5}

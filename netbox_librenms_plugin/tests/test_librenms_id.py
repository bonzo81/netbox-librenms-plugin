"""Tests for multi-server librenms_id helpers.

Covers get_librenms_device_id, set_librenms_device_id, find_by_librenms_id,
and migrate_legacy_librenms_id.

These run against real NetBox Device rows (``@pytest.mark.django_db``): the
helpers read/write the real ``librenms_id`` JSON custom field via ``device.cf`` /
``device.custom_field_data``, and ``find_by_librenms_id`` issues real ORM queries
against the JSON field. A MagicMock object would let ``.cf`` (a computed property
distinct from ``custom_field_data``) or the JSON-path query silently diverge from
production; real rows + DB reloads catch that. The few genuinely query-free guards
(rejecting a float/dict before touching the ORM) keep a mock model so the
"no DB hit on bad input" contract stays assertable.
"""

from types import SimpleNamespace
import itertools
from unittest.mock import MagicMock

import pytest

from netbox_librenms_plugin.tests.conftest import make_device

_UNSET = object()
_counter = itertools.count(1)


def _dev(librenms_value=_UNSET, *, name=None):
    """Create a real Device, optionally seeding its ``librenms_id`` custom field."""
    dev = make_device(name or f"libreid-dev-{next(_counter)}")
    if librenms_value is not _UNSET:
        dev.custom_field_data["librenms_id"] = librenms_value
        dev.save()
    return dev


@pytest.mark.django_db
class TestGetLibreNMSDeviceId:
    """Tests for get_librenms_device_id() against the real ``device.cf`` accessor."""

    def test_returns_none_when_cf_missing(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev(), "default") is None

    def test_returns_int_for_legacy_bare_integer(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev(42), "default") == 42

    def test_legacy_bare_int_returned_for_any_server_key(self):
        """Legacy bare integers are returned as a universal fallback for any server_key."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        dev = _dev(99)
        assert get_librenms_device_id(dev, "default") == 99
        assert get_librenms_device_id(dev, "production") == 99
        assert get_librenms_device_id(dev, "secondary") == 99

    def test_returns_value_for_matching_server_key(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev({"production": 7, "secondary": 12}), "production") == 7

    def test_returns_none_for_missing_server_key_in_dict(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev({"production": 7}), "secondary") is None

    def test_returns_none_for_unexpected_type(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev("not-an-int-or-dict"), "default") is None

    def test_legacy_string_int_returned_for_any_server_key_and_persists(self):
        """A bare string integer ('42') is coerced, returned for any server_key, and the auto-save path normalises it back to an int in the DB (real save, verified by reload)."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import get_librenms_device_id

        dev = _dev("42")
        assert get_librenms_device_id(dev, "default") == 42
        assert get_librenms_device_id(dev, "production") == 42
        # auto_save=True normalised the bare string to an int and persisted it.
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 42

    def test_returns_none_for_bare_boolean(self):
        """bool is a subclass of int; bare True/False must not be treated as a valid ID."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev(True), "default") is None
        assert get_librenms_device_id(_dev(False), "default") is None

    def test_returns_none_for_boolean_inside_dict(self):
        """Boolean values inside the JSON dict must be rejected."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev({"default": True}), "default") is None

    def test_default_server_key_is_default(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev({"default": 5})) == 5


@pytest.mark.django_db
class TestFindByLibreNMSId:
    """Tests for find_by_librenms_id() against real Device rows and JSON-field queries."""

    def test_finds_each_storage_shape(self):
        """Every supported storage shape (namespaced scalar, string scalar, dict-with-id, oob sub-id, legacy bare int/string) is actually resolvable by a real query."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        scalar = _dev({"default": 42})
        assert find_by_librenms_id(Device, 42, "default") == scalar
        scalar.delete()

        string_scalar = _dev({"default": "42"})
        assert find_by_librenms_id(Device, 42, "default") == string_scalar
        string_scalar.delete()

        dict_id = _dev({"default": {"id": 42}})
        assert find_by_librenms_id(Device, 42, "default") == dict_id
        dict_id.delete()

        oob_id = _dev({"default": {"id": 1, "oob": {"id": 42}}})
        assert find_by_librenms_id(Device, 42, "default") == oob_id
        oob_id.delete()

        legacy_int = _dev(42)
        assert find_by_librenms_id(Device, 42, "anyserver") == legacy_int
        legacy_int.delete()

        legacy_str = _dev("42")
        assert find_by_librenms_id(Device, 42, "anyserver") == legacy_str

    def test_librenms_id_q_resolves_each_storage_shape(self):
        """cables_view._librenms_id_q (sharing build_librenms_id_qs with find_by_librenms_id) must resolve the same storage shapes — including the OOB sub-id — so the two can't drift apart."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        for shape in (
            {"default": 42},
            {"default": "42"},
            {"default": {"id": 42}},
            {"default": {"id": 1, "oob": {"id": 42}}},
        ):
            dev = _dev(shape)
            assert Device.objects.filter(_librenms_id_q("default", 42)).first() == dev, shape
            dev.delete()

        # Legacy bare int/str resolve under any server key.
        for legacy in (42, "42"):
            dev = _dev(legacy)
            assert Device.objects.filter(_librenms_id_q("anyserver", 42)).first() == dev, legacy
            dev.delete()

        # include_oob=False (resolving a device by its OWN identity) must NOT match an OOB sub-id.
        oob_only = _dev({"default": {"id": 1, "oob": {"id": 42}}})
        assert Device.objects.filter(_librenms_id_q("default", 42, include_oob=False)).first() is None
        oob_only.delete()

    def test_returns_matching_object(self):
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = _dev({"default": 42})
        assert find_by_librenms_id(Device, 42, "default") == dev

    def test_returns_none_when_not_found(self):
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        _dev({"production": 7})  # a row exists, but not for id 999
        assert find_by_librenms_id(Device, 999, "production") is None

    def test_single_match_uses_one_query(self, django_assert_num_queries):
        """The common case (0 or 1 match) must use a single combined query, not separate host + OOB queries — find_by_librenms_id runs per-port during sync."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = _dev({"default": {"id": 42}})
        with django_assert_num_queries(1):
            result = find_by_librenms_id(Device, 42, "default")
        assert result == dev

    def test_fail_closed_when_host_and_oob_match_different_rows(self):
        """Host query matches one row, OOB query a *different* one → ambiguous → raise."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError, find_by_librenms_id

        _dev({"default": 42})  # host id 42
        _dev({"default": {"id": 99, "oob": {"id": 42}}})  # OOB id 42 on a different device
        with pytest.raises(AmbiguousLibreNMSIdError):
            find_by_librenms_id(Device, 42, "default")

    def test_same_row_for_host_and_oob_is_returned(self):
        """When both queries resolve to the same row, it is returned (not ambiguous)."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = _dev({"default": {"id": 42, "oob": {"id": 42}}})
        assert find_by_librenms_id(Device, 42, "default") == dev

    def test_host_match_wins_when_no_oob_match(self):
        """Host identity is returned when only the host query matches."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = _dev({"default": 42})
        assert find_by_librenms_id(Device, 42, "default") == dev

    def test_fail_closed_on_duplicate_host_matches(self):
        """Two distinct rows sharing the same host librenms_id → raise."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError, find_by_librenms_id

        _dev({"default": 42})
        _dev({"default": 42})
        with pytest.raises(AmbiguousLibreNMSIdError):
            find_by_librenms_id(Device, 42, "default")

    def test_fail_closed_on_duplicate_oob_matches(self):
        """Two distinct rows sharing the same OOB librenms_id → raise."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError, find_by_librenms_id

        # Host ids differ (1 and 2) so only the OOB query is ambiguous.
        _dev({"default": {"id": 1, "oob": {"id": 42}}})
        _dev({"default": {"id": 2, "oob": {"id": 42}}})
        with pytest.raises(AmbiguousLibreNMSIdError):
            find_by_librenms_id(Device, 42, "default")

    def test_float_input_rejected_without_querying(self):
        """A positive float bypasses the int-only coerce contract → reject before the ORM."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        assert find_by_librenms_id(mock_model, 42.0, "default") is None
        mock_model.objects.filter.assert_not_called()

    def test_non_scalar_input_rejected_without_querying(self):
        """Arbitrary non-int/str objects (e.g. a dict) must fail closed before the lookup."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        mock_model = MagicMock()
        assert find_by_librenms_id(mock_model, {"id": 42}, "default") is None
        mock_model.objects.filter.assert_not_called()


@pytest.mark.django_db
class TestMigrateLegacyLibreNMSId:
    """Tests for migrate_legacy_librenms_id() — mutates custom_field_data, never saves."""

    def test_returns_true_when_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        assert migrate_legacy_librenms_id(_dev(42), "default") is True

    def test_migrates_integer_to_dict_format(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        dev = _dev(42)
        migrate_legacy_librenms_id(dev, "production")
        assert dev.custom_field_data["librenms_id"] == {"production": 42}

    def test_returns_false_when_already_dict(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        assert migrate_legacy_librenms_id(_dev({"default": 42}), "default") is False

    def test_returns_false_when_value_is_none(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        assert migrate_legacy_librenms_id(_dev(None), "default") is False

    def test_returns_false_for_boolean_value(self):
        """bool is a subclass of int; True/False must not be migrated."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        dev = _dev(True)
        assert migrate_legacy_librenms_id(dev, "default") is False
        assert dev.custom_field_data["librenms_id"] is True  # unchanged

    def test_does_not_save(self):
        """migrate_legacy_librenms_id must NOT persist — caller is responsible."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        dev = _dev(7)
        migrate_legacy_librenms_id(dev, "default")
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 7

    def test_preserves_value_in_migrated_dict(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        dev = _dev(99)
        migrate_legacy_librenms_id(dev, "secondary")
        assert dev.custom_field_data["librenms_id"]["secondary"] == 99


@pytest.mark.django_db
class TestLibreNMSIdRoundtrip:
    """get_librenms_device_id should see the value set by set_librenms_device_id."""

    def test_set_then_get_returns_same_value(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        dev = _dev()
        set_librenms_device_id(dev, 42, "production")
        assert get_librenms_device_id(dev, "production") == 42

    def test_set_multiple_servers_get_correct_each(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        dev = _dev()
        set_librenms_device_id(dev, 10, "primary")
        set_librenms_device_id(dev, 20, "secondary")
        assert get_librenms_device_id(dev, "primary") == 10
        assert get_librenms_device_id(dev, "secondary") == 20

    def test_migrate_then_get_returns_value(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id, migrate_legacy_librenms_id

        dev = _dev(55)
        migrate_legacy_librenms_id(dev, "default")
        assert get_librenms_device_id(dev, "default") == 55


@pytest.mark.django_db
class TestSetLibreNMSDeviceId:
    """Tests for set_librenms_device_id in utils.py."""

    def test_stores_int_for_valid_device_id(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev(None)
        set_librenms_device_id(dev, 42, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 42}

    def test_invalid_device_id_not_stored(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev()
        set_librenms_device_id(dev, "not-an-int", server_key="primary")
        assert dev.custom_field_data.get("librenms_id") in (None, {})

    def test_invalid_device_id_does_not_overwrite_existing(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev({"primary": 10})
        set_librenms_device_id(dev, None, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 10}

    def test_legacy_bare_int_blocks_write(self):
        """Legacy bare-integer value blocks the write (no silent migration)."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev(7)
        set_librenms_device_id(dev, 99, server_key="secondary")
        assert dev.custom_field_data["librenms_id"] == 7

    def test_adds_new_server_key_to_existing_dict(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev({"primary": 5})
        set_librenms_device_id(dev, 20, server_key="secondary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 5, "secondary": 20}

    def test_string_integer_is_coerced(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev()
        set_librenms_device_id(dev, "42", server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 42}

    def test_unexpected_cf_type_reset_to_empty(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev("unexpected-string")
        set_librenms_device_id(dev, 5, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 5}


class TestIsLegacyLibreNMSIdPositivity:
    """is_legacy_librenms_id must treat only a *positive* bare int / int-string as a legacy link."""

    def test_positive_int_and_string_are_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        assert is_legacy_librenms_id(42) is True
        assert is_legacy_librenms_id("42") is True
        # int() coercion accepts surrounding whitespace / a leading +, so these stay legacy.
        assert is_legacy_librenms_id(" 42 ") is True
        assert is_legacy_librenms_id("+42") is True

    def test_zero_and_negative_are_not_legacy(self):
        """A LibreNMS device id is a positive PK; 0 / negative is not a real link and must not migrate."""
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        for value in (0, -1, "0", " 0 ", "-1", "-42", "+0"):
            assert is_legacy_librenms_id(value) is False, value

    def test_non_numeric_and_dict_and_bool_are_not_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        for value in (None, True, False, "abc", "", {"default": 42}):
            assert is_legacy_librenms_id(value) is False, value


class TestMigrateLegacyRejectsNonPositive:
    """migrate_legacy_librenms_id must never canonicalise a non-positive id into the JSON form."""

    def test_zero_is_not_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = SimpleNamespace(custom_field_data={"librenms_id": 0})
        assert migrate_legacy_librenms_id(obj, "default") is False
        assert obj.custom_field_data["librenms_id"] == 0  # left untouched, not {"default": 0}

    def test_negative_is_not_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = SimpleNamespace(custom_field_data={"librenms_id": "-5"})
        assert migrate_legacy_librenms_id(obj, "default") is False
        assert obj.custom_field_data["librenms_id"] == "-5"


@pytest.mark.django_db
class TestOOBHelpers:
    """Tests for get_librenms_oob, set_librenms_oob, clear_librenms_oob, and the dict-with-id behaviour of get/set_librenms_device_id and find_by_librenms_id."""

    # ── get_librenms_device_id: dict-with-id form ─────────────────────────────

    def test_get_id_from_dict_with_id_form(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev({"primary": {"id": 42}}), "primary") == 42

    def test_get_id_when_oob_also_present(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "drac"}}})
        assert get_librenms_device_id(dev, "primary") == 42

    def test_get_returns_none_for_dict_without_id_key(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        assert get_librenms_device_id(_dev({"primary": {"oob": {"id": 17}}}), "primary") is None

    def test_get_normalises_string_id_inside_dict_with_id_form(self):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        dev = _dev({"primary": {"id": "42"}})
        assert get_librenms_device_id(dev, "primary", auto_save=False) == 42

    # ── set_librenms_device_id: oob preservation ─────────────────────────────

    def test_set_preserves_oob_when_entry_has_oob(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "drac", "ip": "10.0.0.5"}}})
        set_librenms_device_id(dev, 99, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {
            "primary": {"id": 99, "oob": {"id": 17, "type": "drac", "ip": "10.0.0.5"}}
        }

    def test_set_bare_int_when_no_oob_present(self):
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev({"primary": 42})
        set_librenms_device_id(dev, 99, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 99}

    # ── find_by_librenms_id: dict-with-id and oob id lookups ─────────────────

    def test_find_by_matches_main_id_in_dict_with_id_form(self):
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = _dev({"primary": {"id": 42}})
        assert find_by_librenms_id(Device, 42, "primary") == dev

    def test_find_by_matches_oob_id(self):
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        dev = _dev({"primary": {"id": 1, "oob": {"id": 17}}})
        assert find_by_librenms_id(Device, 17, "primary") == dev

    def test_find_by_does_not_return_unrelated_id(self):
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        _dev({"primary": {"id": 1}})
        assert find_by_librenms_id(Device, 999, "primary") is None

    # ── get_librenms_oob ──────────────────────────────────────────────────────

    def test_get_oob_returns_none_for_legacy_bare_int(self):
        from netbox_librenms_plugin.utils import get_librenms_oob

        assert get_librenms_oob(_dev(42), "primary") is None

    def test_get_oob_returns_none_for_bare_int_entry(self):
        from netbox_librenms_plugin.utils import get_librenms_oob

        assert get_librenms_oob(_dev({"primary": 42}), "primary") is None

    def test_get_oob_returns_oob_dict_when_present(self):
        from netbox_librenms_plugin.utils import get_librenms_oob

        oob_data = {"id": 17, "type": "drac", "version": "5.10", "ip": "10.0.0.5"}
        dev = _dev({"primary": {"id": 42, "oob": oob_data}})
        assert get_librenms_oob(dev, "primary") == oob_data

    # ── set_librenms_oob ──────────────────────────────────────────────────────

    def test_set_oob_round_trip(self):
        """set_librenms_oob stores only id + type; ip/version are not persisted."""
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        dev = _dev({"primary": 42})
        set_librenms_oob(dev, 17, "primary", oob_type="drac")
        assert get_librenms_oob(dev, "primary") == {"id": 17, "type": "drac"}

    def test_set_oob_promotes_bare_int_entry(self):
        """set_librenms_oob promotes a bare-int entry to dict form, preserving the main id."""
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_oob

        dev = _dev({"primary": 42})
        set_librenms_oob(dev, 17, "primary", oob_type="idrac")
        assert get_librenms_device_id(dev, "primary") == 42

    def test_set_oob_fails_closed_on_non_positive_int_host_id(self):
        """A stored bare-int host id of 0 or negative is corrupt → raise."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        for bad in (0, -5):
            dev = _dev({"primary": bad})
            with pytest.raises(ValueError, match="not a valid id"):
                set_librenms_oob(dev, 17, "primary", oob_type="idrac")

    def test_set_oob_rejects_unknown_type(self):
        """set_librenms_oob raises ValueError for a type that doesn't match OOB_TYPE_PATTERN."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"primary": 42})
        with pytest.raises(ValueError, match="does not match any known OOB type"):
            set_librenms_oob(dev, 17, "primary", oob_type="ubuntu")

    def test_set_oob_fails_closed_on_corrupt_host_string(self):
        """A non-empty, unparseable stored host id must raise rather than be collapsed to {}."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"primary": "not-an-id"})
        with pytest.raises(ValueError, match="not a valid id"):
            set_librenms_oob(dev, 17, "primary", oob_type="idrac")

    def test_set_oob_fails_closed_on_corrupt_dict_host_id(self):
        """A dict-form entry with a non-empty unparseable host id (e.g. {"id": "abc"}) must raise."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"primary": {"id": "abc"}})
        with pytest.raises(ValueError, match="not a valid id"):
            set_librenms_oob(dev, 17, "primary", oob_type="idrac")

    def test_set_oob_lenient_on_dict_without_host_id(self):
        """A dict entry with no host id (absent/None) stays lenient — OOB is attached."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"primary": {"id": None}})
        set_librenms_oob(dev, 17, "primary", oob_type="idrac")  # must not raise
        assert dev.custom_field_data["librenms_id"]["primary"]["oob"] == {"id": 17, "type": "idrac"}

    def test_set_oob_lenient_on_empty_host_string(self):
        """An empty/whitespace host string is treated leniently (→ fresh dict), not an error."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"primary": "   "})
        set_librenms_oob(dev, 17, "primary", oob_type="idrac")  # must not raise

    def test_set_oob_accepts_generic_oob_sentinel(self):
        """set_librenms_oob must accept "oob" as a generic fallback type."""
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        dev = _dev({"default": 99})
        set_librenms_oob(dev, 55, "default", oob_type="oob")
        result = get_librenms_oob(dev, "default")
        assert result is not None
        assert result["id"] == 55
        assert result["type"] == "oob"

    def test_set_oob_generic_sentinel_case_insensitive(self):
        """The "oob" sentinel is accepted case-insensitively (OOB, Oob, etc.)."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"default": 99})
        set_librenms_oob(dev, 55, "default", oob_type="OOB")  # should not raise
        assert dev.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"

    def test_set_oob_does_not_save(self):
        """set_librenms_oob must NOT persist — caller is responsible (verified by reload)."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import set_librenms_oob

        dev = _dev({"primary": 42})
        set_librenms_oob(dev, 17, "primary", oob_type="ilo")
        # DB row still holds the bare-int entry; the OOB promotion lives only in memory.
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"primary": 42}

    # ── clear_librenms_oob ────────────────────────────────────────────────────

    def test_clear_oob_removes_oob_sub_key(self):
        from netbox_librenms_plugin.utils import clear_librenms_oob, get_librenms_oob

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "drac"}}})
        clear_librenms_oob(dev, "primary")
        assert get_librenms_oob(dev, "primary") is None
        assert dev.custom_field_data["librenms_id"]["primary"] == {"id": 42}

    def test_clear_oob_is_noop_when_no_oob(self):
        from netbox_librenms_plugin.utils import clear_librenms_oob

        dev = _dev({"primary": {"id": 42}})
        clear_librenms_oob(dev, "primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": {"id": 42}}

    def test_clear_oob_does_not_save(self):
        """clear_librenms_oob must NOT persist — caller is responsible (verified by reload)."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import clear_librenms_oob

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "bmc"}}})
        clear_librenms_oob(dev, "primary")
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {
            "primary": {"id": 42, "oob": {"id": 17, "type": "bmc"}}
        }

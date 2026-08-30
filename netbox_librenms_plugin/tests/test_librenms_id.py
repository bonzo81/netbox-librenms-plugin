"""Tests for multi-server librenms_id helpers.

Covers get_librenms_device_id, set_librenms_device_id, find_by_librenms_id,
and migrate_legacy_librenms_id.

These run against real NetBox Device rows (``@pytest.mark.django_db``): the
helpers read/write the real ``librenms_id`` JSON custom field via ``device.cf`` /
``device.custom_field_data``, and ``find_by_librenms_id`` issues real ORM queries
against the JSON field. A dynamically fabricated object would let ``.cf`` (a computed property
distinct from ``custom_field_data``) or the JSON-path query silently diverge from
production; real rows + DB reloads catch that. The few genuinely query-free guards
(rejecting a float/dict before touching the ORM) use a model whose manager access
raises, so the "no DB hit on bad input" contract stays assertable.
"""

import itertools

import pytest

from netbox_librenms_plugin.tests.conftest import make_device

_UNSET = object()
_counter = itertools.count(1)


class _QueryForbiddenModel:
    @property
    def objects(self):
        raise AssertionError("invalid IDs must be rejected before ORM manager access")


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

    @pytest.mark.parametrize(
        ("shape", "server_key"),
        [
            ({"default": "0042"}, "default"),
            ({"default": {"id": " +42 "}}, "default"),
            ({"default": {"id": 1, "oob": {"id": "0042"}}}, "default"),
            (" 0042 ", "anyserver"),
        ],
    )
    def test_integer_lookup_finds_every_accepted_numeric_string(self, shape, server_key):
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id

        device = _dev(shape)

        assert find_by_librenms_id(Device, 42, server_key) == device

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

    def test_build_librenms_id_qs_fails_closed_on_invalid_value(self):
        """A malformed value must build match-nothing predicates so it can't resolve a corrupt legacy row."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import build_librenms_id_qs

        # A device carrying a corrupt legacy bare-string id. Unfixed, build_librenms_id_qs("abc")
        # emits Q(custom_field_data__librenms_id="abc") and this row is matched; the central
        # coerce_librenms_id() guard now returns match-nothing predicates instead.
        corrupt = _dev("abc")
        host_q, oob_q = build_librenms_id_qs("prod", "abc")
        assert Device.objects.filter(host_q).first() is None
        assert Device.objects.filter(oob_q).first() is None
        corrupt.delete()

        # bool / zero / negative also fail closed (return match-nothing) rather than building a lookup.
        for bad in (True, 0, -5, None):
            hq, oq = build_librenms_id_qs("prod", bad)
            assert Device.objects.filter(hq).first() is None
            assert Device.objects.filter(oq).first() is None

    def test_build_librenms_id_qs_fails_closed_on_invalid_server_key(self):
        """A malformed server key must not raise or resolve corrupt custom-field data."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import build_librenms_id_qs

        corrupt = _dev({"invalid__server": 42})
        host_q, oob_q = build_librenms_id_qs("invalid__server", 42)

        assert Device.objects.filter(host_q).first() is None
        assert Device.objects.filter(oob_q).first() is None
        corrupt.delete()

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

        assert find_by_librenms_id(_QueryForbiddenModel(), 42.0, "default") is None

    def test_non_scalar_input_rejected_without_querying(self):
        """Arbitrary non-int/str objects (e.g. a dict) must fail closed before the lookup."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        assert find_by_librenms_id(_QueryForbiddenModel(), {"id": 42}, "default") is None


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


class TestIsLegacyLibreNMSId:
    """Unit coverage for the shared is_legacy_librenms_id predicate (no DB)."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (42, True),  # legacy bare int
            (0, False),  # 0 is not a valid (positive) legacy id
            (-5, False),  # negative is not a valid legacy id
            ("42", True),  # legacy numeric string
            ("0", False),  # parses to 0 -> not a valid legacy id
            (True, False),  # bool is never a valid id (isinstance(True, int) is True)
            (False, False),
            ("abc", False),  # corrupt string, not a legacy id
            ("", False),
            (None, False),
            (1.5, False),  # float is not the legacy format
            ({"default": 5}, False),  # multi-server dict
            ({"default": {"id": 5}}, False),
            ([], False),
            ("007", True),  # zero-padded numeric string is still legacy
            ("-3", False),  # negative numeric string parses to a value < 0 -> not a valid legacy id
            ("4.2", False),  # float-form string is not the legacy int format
            (4.2, False),  # float is not the legacy format
            ({}, False),  # empty dict is the (empty) modern form, not legacy
        ],
    )
    def test_classifies_legacy_values(self, value, expected):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        assert is_legacy_librenms_id(value) is expected

    def test_defined_exactly_once_in_utils(self):
        """Guard is_legacy_librenms_id has exactly one module-level def (a duplicate silently shadows the other)."""
        import ast
        import inspect

        from netbox_librenms_plugin import utils

        defs = [
            node
            for node in ast.parse(inspect.getsource(utils)).body
            if isinstance(node, ast.FunctionDef) and node.name == "is_legacy_librenms_id"
        ]
        assert len(defs) == 1, f"is_legacy_librenms_id defined {len(defs)}x — a duplicate shadows the other"


@pytest.mark.django_db
class TestSetLibreNMSDeviceId:
    """Tests for set_librenms_device_id in utils.py."""

    def test_legacy_bare_int_cf_skips_write(self):
        """A legacy bare-int cf value is left untouched (no silent migration) — the refactored guard still fails closed."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev(42)  # legacy bare-int format
        set_librenms_device_id(dev, 99, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == 42

    def test_legacy_numeric_string_cf_skips_write(self):
        """A legacy numeric-string cf value is also left untouched by the shared predicate."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = _dev("42")
        set_librenms_device_id(dev, 99, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == "42"

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


@pytest.mark.django_db
class TestLibreNMSIdAcceptedFormsContract:
    """Pin which stored librenms_id forms the reader and the lookup each accept.

    Two different definitions of "a numeric id" are in use, and they are not the same:

    * the reader ``get_librenms_device_id`` and the classifier ``is_legacy_librenms_id``
      parse a top-level string with a bare ``int()``;
    * ``coerce_librenms_id`` and the ``build_librenms_id_qs`` predicates behind
      ``find_by_librenms_id`` require ASCII digits.

    Their overlap is the deliberate legacy tolerance: surrounding ASCII whitespace, a
    leading ``+``, leading zeros. Anything the reader accepts BEYOND that overlap resolves
    for the reader while no query can find the row, so a guard built on
    ``find_by_librenms_id`` cannot see it.

    These tests CHARACTERISE that split rather than endorse it. Narrowing the reader was
    tried and reverted: it only makes sense together with narrowing the classifier, and
    once both are narrow ``set_librenms_device_id`` treats the value as corrupt and resets
    the field, destroying a mapping the reader still resolves. Any change to either side
    must break these tests and be a deliberate decision.
    """

    # Stored form -> id it resolves to. Accepted by BOTH sides.
    TOLERATED = [(42, 42), ("42", 42), (" 42 ", 42), ("+42", 42), ("007", 7)]
    # Stored form -> id the READER resolves. The lookup cannot match any of these.
    READER_ONLY = [("4_2", 42), ("\u0661\u0662", 12), ("\uff11\uff12", 12), ("\u00a042", 42)]

    @pytest.mark.parametrize("stored,resolved", TOLERATED, ids=lambda v: repr(v))
    def test_tolerated_forms_resolve_and_are_findable(self, stored, resolved):
        """The legacy tolerance set: the reader resolves it and a real query finds the row."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import coerce_librenms_id, find_by_librenms_id, get_librenms_device_id

        device = _dev(stored)

        assert get_librenms_device_id(device, "default", auto_save=False) == resolved
        assert coerce_librenms_id(stored) == resolved
        assert find_by_librenms_id(Device, resolved, "default") == device

    @pytest.mark.parametrize("stored,resolved", READER_ONLY, ids=lambda v: repr(v))
    def test_reader_only_forms_resolve_but_no_query_finds_them(self, stored, resolved):
        """Forms only a bare int() accepts: the reader resolves them, the lookup is blind to them."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import coerce_librenms_id, find_by_librenms_id, get_librenms_device_id

        device = _dev(stored)

        # The reader binds the row to an id ...
        assert get_librenms_device_id(device, "default", auto_save=False) == resolved
        # ... that no predicate can match, so every lookup-based guard is blind to it.
        assert coerce_librenms_id(stored) is None
        assert find_by_librenms_id(Device, resolved, "default") is None

    @pytest.mark.parametrize("stored,resolved", READER_ONLY, ids=lambda v: repr(v))
    def test_a_reader_pass_with_auto_save_heals_the_row(self, stored, resolved):
        """The split closes itself: one read with auto_save rewrites the value as a plain int.

        This is why the split is tolerable in practice. A row only stays invisible to the
        lookup while nothing has read it through the normalising path.
        """
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id, get_librenms_device_id

        device = _dev(stored)
        assert find_by_librenms_id(Device, resolved, "default") is None

        assert get_librenms_device_id(device, "default") == resolved

        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == resolved
        assert find_by_librenms_id(Device, resolved, "default") == device

    @pytest.mark.parametrize("stored,_resolved", TOLERATED + READER_ONLY, ids=lambda v: repr(v))
    def test_the_classifier_tracks_the_reader_not_the_lookup(self, stored, _resolved):
        """is_legacy_librenms_id must agree with the reader, because set_librenms_device_id gates on it.

        Whenever the reader resolves a bare value, the classifier has to call it legacy, or
        set_librenms_device_id falls through to its corrupt-string branch and resets the
        custom field to an empty dict.
        """
        from netbox_librenms_plugin.utils import get_librenms_device_id, is_legacy_librenms_id

        reader_resolves = get_librenms_device_id(_dev(stored), "default", auto_save=False) is not None

        assert is_legacy_librenms_id(stored) is reader_resolves


@pytest.mark.django_db
class TestMigrateLegacyRejectsNonPositive:
    """migrate_legacy_librenms_id must never canonicalise a non-positive id into the JSON form."""

    def test_zero_is_not_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = _dev(0)
        assert migrate_legacy_librenms_id(obj, "default") is False
        assert obj.custom_field_data["librenms_id"] == 0  # left untouched, not {"default": 0}

    def test_negative_is_not_migrated(self):
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = _dev("-5")
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


@pytest.mark.django_db
class TestMergeLibreNMSLinks:
    """Tests for merge_librenms_links() — winner-wins conflict policy."""

    def _make_dev(self, name, librenms_id_dict):
        return _dev(librenms_id_dict, name=f"{name}-{next(_counter)}")

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

    def test_distinct_donor_host_and_oob_with_only_one_winner_slot_fails_closed(self):
        """Two distinct donor links cannot be compressed into the winner's one free OOB slot."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99, "oob": {"id": 77, "type": "ilo"}}})
        with pytest.raises(ValueError, match="two distinct LibreNMS links"):
            merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 42}

    def test_donor_id_demoted_to_oob_generic_when_no_pattern_in_name(self):
        """Donor id is always demoted; type falls back to 'oob' when no keyword in name."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-03-spare", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 42
        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 99, "type": "oob"}
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "oob"}

    def test_blank_only_donor_oob_does_not_persist_empty_oob_slot(self):
        """A donor oob carrying only a blank id (no other metadata) must NOT leave an empty {} oob.

        The blank id is dropped (validated up-front); with no other metadata the inherited oob
        collapses to {} and must not be written — a persisted empty dict reads as an occupied slot.
        """
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("host-win", {"default": {}})
        donor = self._make_dev("host-don", {"default": {"oob": {"id": "  "}}})  # blank id, nothing else
        summary = merge_librenms_links(winner, donor, "default")

        entry = winner.custom_field_data["librenms_id"]["default"]
        assert "oob" not in entry, f"empty oob slot persisted: {entry}"
        assert summary["oob_from_donor"] is None

    def test_empty_oob_inheritance_does_not_block_a_later_demote(self):
        """The real harm: a persisted empty {} oob would block a subsequent donor-id demotion.

        Merge a blank-only donor oob into a winner that holds a host id, then merge a second donor
        whose host id should demote into the (still-free) oob slot. Before the fix the first merge
        wrote oob={}, which the second merge read as occupied → the second donor id was lost.
        """
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        merge_librenms_links(winner, self._make_dev("blank-oob", {"default": {"oob": {"id": " "}}}), "default")
        # The blank-only oob left the slot free, not occupied by {}.
        assert "oob" not in winner.custom_field_data["librenms_id"]["default"]

        summary = merge_librenms_links(winner, self._make_dev("idrac-jhw6nc4", {"default": {"id": 99}}), "default")
        assert winner.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 99
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}

    def test_demoted_oob_type_prefers_vendor_token_over_generic(self):
        # A donor name carrying a generic 'oob' token BEFORE the vendor token (e.g.
        # 'leaf01-oob-idrac9') must demote with the vendor type ('idrac'), matching the import-path
        # normalize_oob_type — not the raw first-match search that would pick the generic 'oob'.
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("leaf01-oob-idrac9", {"default": {"id": 99}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 99, "type": "idrac"}
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}

    def test_winner_inherits_donor_oob_when_winner_has_none(self):
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ipmi"}
        assert summary["oob_from_donor"] == {"id": 77, "type": "ipmi"}

    def test_malformed_donor_oob_id_fails_closed_on_inherit(self):
        """A corrupt donor oob link ({"oob": {"id": "abc"}}) must not be inherited verbatim; the inherit branch coerces the host id and raises on a non-empty invalid value."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": "abc", "type": "ipmi"}}})
        with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
            merge_librenms_links(winner, donor, "default")

    def test_non_dict_donor_oob_shape_fails_closed(self):
        """A corrupt non-dict donor oob (e.g. a list) is corrupted state, not 'no OOB link' — fail closed rather than silently drop it during merge."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"id": 7, "oob": ["not", "a", "dict"]}})
        with pytest.raises(ValueError, match="unsupported librenms_id.*oob shape"):
            merge_librenms_links(winner, donor, "default")

    def test_non_dict_winner_oob_shape_fails_closed(self):
        """A corrupt non-dict winner oob (e.g. a string) must fail closed, not be silently overwritten by donor data."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": "garbage"}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        with pytest.raises(ValueError, match="unsupported librenms_id.*oob shape"):
            merge_librenms_links(winner, donor, "default")

    def test_malformed_winner_oob_id_fails_closed(self):
        """A winner oob with a non-blank unparseable id ({"oob": {"id": "abc"}} / {"id": 0}) only passes the shape check, so it would look 'occupied' and skip inheriting the donor's real controller — losing it once the donor is marked migrated. It must fail closed instead."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        for bad_id in ("abc", 0):
            winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": {"id": bad_id, "type": "ipmi"}}})
            donor = self._make_dev("idrac-jhw6nc4", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
            with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
                merge_librenms_links(winner, donor, "default")

    def test_blank_winner_oob_id_is_lenient(self):
        """A blank/whitespace winner oob id must NOT fail closed (matches the lenient host-id handling) — the merge proceeds without raising."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        # Winner holds a host id and a blank-id oob slot; the donor carries only the SAME host id
        # (a duplicate mapping, not an orphan) so the blank-oob leniency is exercised without
        # tripping the "donor host id has nowhere to move" guard that a distinct donor id would.
        winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": {"id": "  ", "type": "ipmi"}}})
        donor = self._make_dev("eve-ng-02-dup", {"default": {"id": 42}})
        # Must not raise; the blank winner oob id is treated leniently as "no id".
        merge_librenms_links(winner, donor, "default")

    def test_distinct_donor_host_id_with_winner_holding_both_slots_fails_closed(self):
        """A distinct donor host id with the winner holding both its host and oob slots fails closed."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 100, "oob": {"id": 50, "type": "idrac"}}})
        donor = self._make_dev("router-spare", {"default": {"id": 200}})
        with pytest.raises(ValueError, match="already holds both a LibreNMS host id and an OOB link"):
            merge_librenms_links(winner, donor, "default")
        # The donor's link must be left untouched (nothing captured, no partial mutation of winner).
        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 100, "oob": {"id": 50, "type": "idrac"}}

    def test_duplicate_donor_host_id_with_winner_holding_both_slots_is_allowed(self):
        """A donor host id equal to the winner's is a duplicate mapping, not an orphan, so it is allowed."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 100, "oob": {"id": 50, "type": "idrac"}}})
        donor = self._make_dev("eve-ng-02-dup", {"default": {"id": 100}})
        summary = merge_librenms_links(winner, donor, "default")
        # Winner is unchanged (same host id, keeps its own oob); nothing was demoted or dropped.
        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 100, "oob": {"id": 50, "type": "idrac"}}
        assert summary["donor_id_demoted_to_oob"] is None

    def test_donor_oob_id_coerced_to_int_on_inherit(self):
        """A numeric-string donor oob id is normalized to int when inherited."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": "77", "type": "ipmi"}}})
        summary = merge_librenms_links(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ipmi"}
        assert summary["oob_from_donor"] == {"id": 77, "type": "ipmi"}

    def test_blank_donor_oob_id_is_lenient_and_dropped(self):
        """A blank/whitespace donor oob id ({"oob": {"id": " "}}) must be treated as 'no oob id' (lenient) — the same as a blank host id and an absent oob id — not raise like a non-blank corrupt one."""
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-x", {"default": {"oob": {"id": "   ", "type": "drac"}}})
        summary = merge_librenms_links(winner, donor, "default")

        inherited = winner.custom_field_data["librenms_id"]["default"]["oob"]
        assert inherited == {"type": "drac"}  # blank id dropped, type preserved
        assert "id" not in inherited
        assert summary["oob_from_donor"] == {"type": "drac"}

    def test_metadata_only_donor_oob_does_not_drop_donor_host_id(self):
        # A donor with a host id AND a metadata-only oob (a type but no usable id) must still
        # demote its host id into the winner's empty oob slot — the metadata-only oob is not a
        # real controller link. Treating the truthy-but-idless oob as "occupied" used to skip
        # demotion and inherit the useless metadata, silently losing the donor host id once the
        # donor was marked migrated.
        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 50}})
        donor = self._make_dev("idrac-host", {"default": {"id": 99, "oob": {"type": "idrac"}}})
        summary = merge_librenms_links(winner, donor, "default")

        oob = winner.custom_field_data["librenms_id"]["default"]["oob"]
        assert oob == {"id": 99, "type": "idrac"}  # host id preserved + type metadata folded in
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}
        assert summary["oob_from_donor"] is None  # not the useless metadata-only inherit path
        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 50

    def test_donor_host_id_with_corrupt_oob_id_still_fails_closed(self):
        # A donor host id paired with a non-blank unparseable oob id must still fail closed: the
        # up-front oob-id validation runs before demotion, so a corrupt link can't be silently
        # demoted/dropped just because the donor also carries a host id.
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        winner = self._make_dev("eve-ng-02", {"default": {"id": 50}})
        donor = self._make_dev("idrac-host", {"default": {"id": 99, "oob": {"id": "abc", "type": "idrac"}}})
        with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
            merge_librenms_links(winner, donor, "default")

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

        winner = self._make_dev("legacy-winner", 42)
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
        """A bare per-server string entry ({server_key: 'abc'}) that can't be parsed must raise, not silently collapse to {} (which would drop/swap link state)."""
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
        """A blank/whitespace dict-form id ({"id": " "}) must be treated as 'no id' (lenient), the same as a blank top-level string — not raise like a non-blank corrupt id ('abc')."""
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

    def test_falsy_corrupt_top_level_librenms_id_fails_closed(self):
        """A top-level librenms_id of False/0 must raise, not collapse to {} via `or {}` and merge as 'no mapping'."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        for bad in (False, 0):
            # Corrupt winner.
            winner = self._make_dev("eve-ng-02", bad)
            donor = self._make_dev("router-spare", {"default": {"id": 99}})
            with pytest.raises(ValueError, match="legacy bare-integer or corrupt"):
                merge_librenms_links(winner, donor, "default")

            # Corrupt donor.
            winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
            donor = self._make_dev("router-spare", bad)
            with pytest.raises(ValueError, match="legacy bare-integer or corrupt"):
                merge_librenms_links(winner, donor, "default")

    def test_unsupported_winner_entry_shape_fails_closed(self):
        """A non-None winner entry of an unsupported type (bool/float/list) must raise, not collapse to {} (which would let the winner inherit the donor's id)."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        for bad in (True, 1.5, [99], (1, 2)):
            winner = self._make_dev("eve-ng-02", {"default": bad})
            donor = self._make_dev("router-spare", {"default": {"id": 99}})
            with pytest.raises(ValueError, match="unsupported librenms_id"):
                merge_librenms_links(winner, donor, "default")

    def test_unsupported_donor_entry_shape_fails_closed(self):
        """A non-None donor entry of an unsupported type must raise rather than silently becoming {} (dropping the donor's link during merge)."""
        import pytest

        from netbox_librenms_plugin.utils import merge_librenms_links

        for bad in (True, 1.5, [99], (1, 2)):
            winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
            donor = self._make_dev("router-spare", {"default": bad})
            with pytest.raises(ValueError, match="unsupported librenms_id"):
                merge_librenms_links(winner, donor, "default")


@pytest.mark.django_db
class TestMarkLibreNMSMigrated:
    """Tests for mark_librenms_migrated()."""

    def test_clears_id_and_oob_and_writes_marker(self):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        donor = _dev({"default": {"id": 99, "oob": {"id": 11, "type": "drac"}}})
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

        donor = _dev({"default": {"id": 99}})
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
            donor = _dev({"default": {"id": 99}})
            with pytest.raises(ValueError):
                mark_librenms_migrated(donor, winner_pk=bad, server_key="default")

    def test_fails_closed_on_legacy_or_corrupt_top_level_librenms_id(self):
        """A legacy bare-int/bare-string or corrupt top-level librenms_id must raise, not collapse.

        Collapsing it to {} and stamping the marker would drop the donor's still-resolvable
        mapping (data loss). The donor's value must be left intact so it stays recoverable.
        """
        import pytest

        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for legacy in (42, "42", True, [1, 2]):
            donor = _dev(legacy)
            with pytest.raises(ValueError):
                mark_librenms_migrated(donor, winner_pk=99, server_key="default")
            # Untouched: no marker stamped, original value preserved for the caller to migrate.
            assert donor.custom_field_data["librenms_id"] == legacy

    def test_fails_closed_on_corrupt_per_server_entry(self):
        """A corrupt per-server entry (bool/list/float/unparseable string) must raise, not collapse.

        The top-level guard rejects a corrupt librenms_id, but a per-server value such as
        ``{"default": True}`` / ``{"default": ["bad"]}`` was previously collapsed to ``{}`` and
        stamped migrated — hiding the malformed donor state behind ``_migrated_to``. Mirror the
        per-entry validation from merge_librenms_links() and fail closed instead.
        """
        import pytest

        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for corrupt in (True, ["bad"], 3.5, "notanid"):
            donor = _dev({"default": corrupt})
            with pytest.raises(ValueError):
                mark_librenms_migrated(donor, winner_pk=99, server_key="default")
            # The raise happens before any mutation: no marker stamped, entry untouched.
            assert donor.custom_field_data["librenms_id"] == {"default": corrupt}

    def test_blank_or_numeric_string_per_server_entry_does_not_raise(self):
        """A blank string is "no link" (collapse to {}); a numeric string is a valid id — neither raises."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        # Blank string → no recoverable link → collapses to {} and stamps the marker (no raise).
        donor = _dev({"default": ""})
        mark_librenms_migrated(donor, winner_pk=99, server_key="default", at="2025-01-01T00:00:00Z")
        assert donor.custom_field_data["librenms_id"]["default"]["_migrated_to"]["device_id"] == 99

        # Numeric string → a real id → also valid, marker stamped, id cleared.
        donor2 = _dev({"default": "77"})
        mark_librenms_migrated(donor2, winner_pk=99, server_key="default", at="2025-01-01T00:00:00Z")
        entry = donor2.custom_field_data["librenms_id"]["default"]
        assert "id" not in entry
        assert entry["_migrated_to"]["device_id"] == 99

    def test_fails_closed_on_corrupt_nested_oob(self):
        """A dict entry with a non-dict oob, or an oob with a non-blank unparseable id, must raise."""
        import pytest

        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for corrupt_oob in ("garbage", ["bad"], 7, {"id": "abc"}):
            donor = _dev({"default": {"oob": corrupt_oob}})
            with pytest.raises(ValueError):
                mark_librenms_migrated(donor, winner_pk=99, server_key="default")
            # The raise happens before any mutation: no marker stamped, oob preserved to migrate first.
            assert donor.custom_field_data["librenms_id"]["default"] == {"oob": corrupt_oob}

    def test_valid_or_blank_nested_oob_does_not_raise(self):
        """A well-formed oob (numeric/blank id, or empty dict) is popped and the marker is stamped."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for ok_oob in ({"id": 55}, {"id": "55"}, {"id": ""}, {}):
            donor = _dev({"default": {"oob": ok_oob}})
            mark_librenms_migrated(donor, winner_pk=99, server_key="default", at="2025-01-01T00:00:00Z")
            entry = donor.custom_field_data["librenms_id"]["default"]
            assert "oob" not in entry
            assert entry["_migrated_to"]["device_id"] == 99

    def test_fails_closed_on_unparseable_dict_host_id(self):
        """A dict entry whose own id is non-blank but unparseable must raise, not be popped + marked.

        The dict branch validated the nested oob but not entry["id"], so {"id": "abc"} / {"id": 0} /
        {"id": True} was silently popped and stamped _migrated_to — erasing the corrupt-but-
        recoverable host mapping instead of forcing the caller to migrate it first.
        """
        import pytest

        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for corrupt_id in ("abc", 0, True):
            donor = _dev({"default": {"id": corrupt_id}})
            with pytest.raises(ValueError):
                mark_librenms_migrated(donor, winner_pk=99, server_key="default")
            # The raise happens before any mutation: no marker stamped, id preserved to migrate first.
            assert donor.custom_field_data["librenms_id"]["default"] == {"id": corrupt_id}

    def test_valid_or_blank_dict_host_id_does_not_raise(self):
        """A dict entry with a numeric/blank/absent id is popped and the marker stamped (no raise)."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        for ok_id in ({"id": 55}, {"id": "55"}, {"id": ""}, {"id": None}, {}):
            donor = _dev({"default": dict(ok_id)})
            mark_librenms_migrated(donor, winner_pk=99, server_key="default", at="2025-01-01T00:00:00Z")
            entry = donor.custom_field_data["librenms_id"]["default"]
            assert "id" not in entry
            assert entry["_migrated_to"]["device_id"] == 99

    @pytest.mark.django_db
    def test_after_marker_find_by_librenms_id_no_longer_matches(self):
        """A donor whose librenms_id entry holds only the _migrated_to marker must NOT be returned by find_by_librenms_id, queried against the REAL Device model."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import find_by_librenms_id, mark_librenms_migrated

        donor = _dev({"default": {"id": 99}})
        mark_librenms_migrated(donor, winner_pk=99, server_key="default")
        donor.save()
        donor.refresh_from_db()

        # The entry now holds only _migrated_to — no id, no oob.
        entry = donor.cf["librenms_id"]["default"]
        assert entry.get("id") is None
        assert entry.get("oob") is None

        # The real model query must not return the migrated-only donor for id 99.
        assert find_by_librenms_id(Device, 99, "default") is None


class TestNormalizeMergeEntry:
    """_normalize_merge_entry: the shared fail-closed shape validation for the merge winner/donor entries."""

    @staticmethod
    def _norm(entry, *, copy=True, owner="winner"):
        from netbox_librenms_plugin.utils import _normalize_merge_entry

        return _normalize_merge_entry(entry, owner_label=owner, owner_name="X", server_key="default", copy_dict=copy)

    def test_coerces_scalars_and_blank_to_no_link(self):
        assert self._norm(42) == {"id": 42}
        assert self._norm("42") == {"id": 42}
        assert self._norm("") == {}  # blank string is a genuine "no active link"
        assert self._norm(None) == {}

    def test_fails_closed_on_corrupt_shapes(self):
        import pytest

        with pytest.raises(ValueError, match="unparseable"):
            self._norm("abc")  # non-blank, non-numeric string
        with pytest.raises(ValueError, match="unsupported"):
            self._norm([1])  # list
        with pytest.raises(ValueError, match="unsupported"):
            self._norm(True)  # bool is never a valid id

    def test_dict_copy_flag_controls_isolation(self):
        src = {"id": 5, "oob": {"id": 7}}
        # Winner entry is copied (it is mutated downstream): mutating the result must not touch src.
        copied = self._norm(src, copy=True)
        copied["id"] = 99
        assert src["id"] == 5
        # Donor entry is read-only: returned as-is (same object).
        assert self._norm(src, copy=False, owner="donor") is src

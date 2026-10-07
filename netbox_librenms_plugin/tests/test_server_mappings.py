"""Tests for the object server mapping module: its readers, its builders and their persistence.

The readers run against real NetBox rows (``@pytest.mark.django_db``): a snapshot decodes the
stored ``librenms_id`` custom field, and the lookups issue real JSON-field queries. A fabricated
object would let the stored shape or the JSON-path query drift from production. The builder
classes below cover ``assign_own``, ``convert_legacy``, the OOB builders, merge and the migration
marker; a test puts a built change on its object with ``apply_mapping_change``.
"""

import itertools
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    ChangeOutcome,
    ContainerStatus,
    MappingRole,
    PreferenceStatus,
    assign_own,
    attach_oob,
    clear_oob,
    convert_legacy,
    find_mapping,
    find_port_owner,
    port_holders,
    identity_q,
    mapped_device_servers,
    mark_migrated,
    merge_links,
    name_match_may_be_port,
    read_mapping,
    read_mappings,
    resolve_device_port,
)
from netbox_librenms_plugin.tests.conftest import (
    apply_mapping_change,
    make_device,
    make_interface,
    make_virtual_chassis_members,
    make_vm,
    transactional_db_with_all_apps,
)

_UNSET = object()
_counter = itertools.count(1)
BOTH_ROLES = (MappingRole.OWN, MappingRole.OOB)


def _dev(librenms_value=_UNSET, *, name=None):
    """Create a real Device, optionally seeding its ``librenms_id`` custom field."""
    dev = make_device(name or f"libreid-dev-{next(_counter)}")
    if librenms_value is not _UNSET:
        dev.custom_field_data["librenms_id"] = librenms_value
        dev.save()
    return dev


def _find(identity, server="default", *, roles=BOTH_ROLES, queryset=None):
    from dcim.models import Device

    return find_mapping(
        Device.objects.all() if queryset is None else queryset, server=server, identity=identity, roles=roles
    )


def _bind(interface, value):
    interface.custom_field_data["librenms_id"] = value
    interface.save()
    return interface


def _decoded(value):
    """Return the snapshot of a stored value, read from an unsaved Device (no query)."""
    from dcim.models import Device

    return read_mapping(Device(custom_field_data={"librenms_id": value}))


def _merge_into_winner(winner, donor, server):
    """Build the merge, put the winner's side on *winner*, and return the summary."""
    merge = merge_links(winner, donor, server)
    apply_mapping_change(winner, merge.winner)
    return merge.summary


@pytest.mark.django_db
class TestOwnIdentityRead:
    """The own identity a snapshot resolves per server, on real Device rows."""

    def test_returns_none_when_cf_missing(self):
        assert read_mapping(_dev()).own_id("default") is None

    def test_returns_int_for_legacy_bare_integer(self):
        assert read_mapping(_dev(42)).own_id("default") == 42

    def test_legacy_bare_int_returned_for_any_server_key(self):
        """Legacy bare integers are returned as a universal fallback for any server key."""
        mapping = read_mapping(_dev(99))
        assert mapping.own_id("default") == 99
        assert mapping.own_id("production") == 99
        assert mapping.own_id("secondary") == 99

    def test_returns_value_for_matching_server_key(self):
        assert read_mapping(_dev({"production": 7, "secondary": 12})).own_id("production") == 7

    def test_returns_none_for_missing_server_key_in_dict(self):
        assert read_mapping(_dev({"production": 7})).own_id("secondary") is None

    def test_returns_none_for_unexpected_type(self):
        assert read_mapping(_dev("not-an-int-or-dict")).own_id("default") is None

    def test_legacy_string_int_resolves_for_any_server_key_without_persisting(self):
        """A bare string integer is coerced for every server, and the read leaves the stored string alone."""
        from dcim.models import Device

        dev = _dev("42")
        assert read_mapping(dev).own_id("default") == 42
        assert read_mapping(dev).own_id("production") == 42
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == "42"

    def test_returns_none_for_bare_boolean(self):
        """A bool is an int subclass, so bare True/False must not count as a valid ID."""
        assert read_mapping(_dev(True)).own_id("default") is None
        assert read_mapping(_dev(False)).own_id("default") is None

    def test_returns_none_for_boolean_inside_dict(self):
        """Boolean values inside the JSON dict must be rejected."""
        assert read_mapping(_dev({"default": True})).own_id("default") is None

    def test_an_invalid_server_key_is_refused(self):
        with pytest.raises(ValueError, match="'__'"):
            read_mapping(_dev({"default": 5})).own_id("a__b")


@pytest.mark.django_db
class TestReadPurity:
    """A read is a snapshot: no SQL on a loaded object, no write, no lock, no cache."""

    def test_a_read_of_a_loaded_object_runs_no_sql_and_changes_nothing(self, django_assert_num_queries):
        from core.models import ObjectChange
        from dcim.models import Device

        stored = {
            "default": {"id": "42", "oob": {"id": "7", "type": "idrac"}},
            "retired": {"_migrated_to": {"device_id": 3, "server_key": "retired", "at": "2026-01-01T00:00:00Z"}},
            "_preferred_server": "default",
        }
        loaded = Device.objects.get(pk=_dev(stored).pk)
        last_updated = loaded.last_updated
        change_count = ObjectChange.objects.count()

        with django_assert_num_queries(0):
            mapping = read_mapping(loaded)
            assert mapping.own_id("default") == 42
            assert mapping.oob_id("default") == 7
            assert mapping.migrated_to("retired").device_id == 3
            assert name_match_may_be_port(loaded, server="default", port_id=42)

        reloaded = Device.objects.get(pk=loaded.pk)
        assert reloaded.custom_field_data["librenms_id"] == stored
        assert reloaded.last_updated == last_updated
        assert ObjectChange.objects.count() == change_count

    def test_a_new_snapshot_sees_an_unsaved_change_and_an_earlier_one_does_not(self):
        dev = _dev({"default": {"id": 42, "oob": {"id": 7, "type": "idrac"}}})
        earlier = read_mapping(dev)

        dev.custom_field_data["librenms_id"]["default"]["oob"]["type"] = "ilo"
        dev.custom_field_data["librenms_id"]["default"]["id"] = 43
        later = read_mapping(dev)

        assert (earlier.own_id("default"), earlier.server("default").oob_type) == (42, "idrac")
        assert (later.own_id("default"), later.server("default").oob_type) == (43, "ilo")

    def test_a_snapshot_is_immutable(self):
        mapping = read_mapping(_dev({"default": 42}))

        with pytest.raises(FrozenInstanceError):
            mapping.container = ContainerStatus.ABSENT
        with pytest.raises(FrozenInstanceError):
            mapping.server("default").own_id = 43

    def test_only_mapped_models_can_be_read(self):
        from dcim.models import Site

        with pytest.raises(TypeError):
            read_mapping(SimpleNamespace(custom_field_data={"librenms_id": 42}))
        with pytest.raises(TypeError):
            identity_q(Site, server="default", identities=(42,), roles=BOTH_ROLES)


@pytest.mark.django_db
class TestMappingDecoding:
    """Decoding of every stored form, on real rows."""

    @pytest.mark.parametrize(
        ("stored", "container", "readable_id", "queryable_id", "recorded"),
        [
            (None, ContainerStatus.ABSENT, None, None, False),
            ({}, ContainerStatus.SCOPED, None, None, False),
            ({"_preferred_server": "a"}, ContainerStatus.SCOPED, None, None, True),
            (42, ContainerStatus.LEGACY, 42, 42, True),
            ("42", ContainerStatus.LEGACY, 42, 42, True),
            (" +42 ", ContainerStatus.LEGACY, 42, 42, True),
            ("4_2", ContainerStatus.LEGACY, 42, None, True),
            ("١٢", ContainerStatus.LEGACY, 12, None, True),
            (0, ContainerStatus.INVALID, None, None, False),
            (-1, ContainerStatus.INVALID, None, None, True),
            ("²", ContainerStatus.INVALID, None, None, True),
            (True, ContainerStatus.INVALID, None, None, True),
            ("", ContainerStatus.INVALID, None, None, False),
            ("abc", ContainerStatus.INVALID, None, None, True),
            ([], ContainerStatus.INVALID, None, None, False),
        ],
        ids=repr,
    )
    def test_container_and_both_legacy_meanings(self, stored, container, readable_id, queryable_id, recorded):
        mapping = read_mapping(_dev(stored))

        assert mapping.container is container
        assert mapping.legacy.readable_id == readable_id
        assert mapping.legacy.queryable_id == queryable_id
        assert mapping.legacy.is_legacy is (readable_id is not None)
        assert mapping.has_recorded_state is recorded
        assert mapping.own_id("any-server") == (readable_id if container is ContainerStatus.LEGACY else None)

    def test_servers_keep_stored_order_and_skip_invalid_keys_and_metadata(self):
        mapping = read_mapping(_dev({"b": 1, "a__x": 2, "a": {"id": "3"}, "_preferred_server": "b"}))

        assert [(entry.server, entry.own_id) for entry in mapping.servers] == [("b", 1), ("a", 3)]
        assert mapping.server("a__x") is None

    def test_own_and_oob_identities_stay_separate(self):
        mapping = read_mapping(
            _dev({"host": {"id": 42, "oob": {"id": 7, "type": "idrac"}}, "oob-only": {"oob": {"id": "8"}}})
        )

        host, oob_only = mapping.server("host"), mapping.server("oob-only")
        assert (host.own_id, host.oob_id, host.oob_type, host.display_id, host.is_oob_only) == (
            42,
            7,
            "idrac",
            42,
            False,
        )
        assert (oob_only.own_id, oob_only.oob_id, oob_only.display_id, oob_only.is_oob_only) == (None, 8, 8, True)
        assert mapping.own_id("oob-only") is None

    @pytest.mark.parametrize(
        ("entry", "has_oob", "oob_id"),
        [
            ({"id": 42, "oob": {"id": 7, "type": "idrac"}}, True, 7),
            ({"id": 42, "oob": {"type": "idrac"}}, True, None),
            ({"id": 42, "oob": {"id": "abc"}}, True, None),
            ({"id": 42, "oob": {}}, False, None),
            ({"id": 42, "oob": "idrac"}, False, None),
            ({"id": 42}, False, None),
            (42, False, None),
        ],
        ids=repr,
    )
    def test_oob_occupancy_counts_a_metadata_only_entry(self, entry, has_oob, oob_id):
        mapping = read_mapping(_dev({"default": entry}))

        assert mapping.has_oob("default") is has_oob
        assert mapping.oob_id("default") == oob_id

    @pytest.mark.parametrize(
        ("stored", "status", "server"),
        [
            ({"a": 1}, PreferenceStatus.ABSENT, None),
            ({"a": 1, "_preferred_server": "a"}, PreferenceStatus.NAMED, "a"),
            ({"a": 1, "_preferred_server": "gone"}, PreferenceStatus.NAMED, "gone"),
            ({"a": 1, "_preferred_server": "   "}, PreferenceStatus.MALFORMED, None),
            ({"a": 1, "_preferred_server": 123}, PreferenceStatus.MALFORMED, None),
            (42, PreferenceStatus.ABSENT, None),
        ],
        ids=repr,
    )
    def test_preference_states(self, stored, status, server):
        preference = read_mapping(_dev(stored)).preference

        assert (preference.status, preference.server) == (status, server)

    @pytest.mark.parametrize(
        ("entry", "recorded", "effective_pk"),
        [
            ({"_migrated_to": {"device_id": 5, "server_key": "default", "at": "t"}}, True, 5),
            ({"id": 9, "_migrated_to": {"device_id": 5, "server_key": "default"}}, True, None),
            ({"oob": {"id": 9}, "_migrated_to": {"device_id": 5, "server_key": "default"}}, True, None),
            ({"oob": {"type": "idrac"}, "_migrated_to": {"device_id": 5, "server_key": "default"}}, True, 5),
            ({"_migrated_to": {"device_id": 5, "server_key": "other"}}, True, None),
            ({"_migrated_to": {"device_id": "5", "server_key": "default"}}, True, None),
            ({"_migrated_to": {"device_id": True, "server_key": "default"}}, True, None),
            ({"_migrated_to": {"device_id": 0, "server_key": "default"}}, True, None),
            ({"_migrated_to": "garbage"}, False, None),
            ({"id": 9}, False, None),
        ],
        ids=repr,
    )
    def test_recorded_and_effective_migration_markers(self, entry, recorded, effective_pk):
        mapping = read_mapping(_dev({"default": entry}))
        migration = mapping.server("default").migration
        target = mapping.migrated_to("default")

        assert migration.recorded is recorded
        assert (target.device_id if target else None) == effective_pk
        assert migration.effective == target

    def test_an_effective_marker_carries_its_timestamp(self):
        target = read_mapping(
            _dev({"default": {"_migrated_to": {"device_id": 5, "server_key": "default", "at": "2026-09-29T10:00:00Z"}}})
        ).migrated_to("default")

        assert (target.device_id, target.server_key, target.at) == (5, "default", "2026-09-29T10:00:00Z")

    @pytest.mark.parametrize(
        ("stored", "port_id", "expected"),
        [
            (None, 42, True),
            ({"other": 7}, 42, True),
            ({"default": 42}, 42, True),
            ({"default": {"id": "42"}}, 42, True),
            ({"default": 7}, 42, False),
            ({"default": "abc"}, 42, False),
            ({"default": {"oob": {"id": 42}}}, 42, False),
            (42, 42, True),
            (42, 7, False),
            ("abc", 42, False),
            (None, "abc", True),
            ({"default": 7}, None, True),
        ],
        ids=repr,
    )
    def test_name_fallback_needs_an_absent_or_matching_binding(self, stored, port_id, expected):
        interface = _bind(make_interface(_dev(), "eth0"), stored)

        assert name_match_may_be_port(interface, server="default", port_id=port_id) is expected

    def test_name_fallback_refuses_an_invalid_server_key(self):
        interface = make_interface(_dev(), "eth0")

        assert name_match_may_be_port(interface, server="", port_id=42) is False

    def test_the_raw_decoder_agrees_with_the_object_reader(self):
        stored = {"default": {"id": "42", "oob": {"type": "idrac"}}, "_preferred_server": 1}

        assert _decoded(stored) == read_mapping(_dev(stored))


@pytest.mark.django_db
class TestFindMapping:
    """find_mapping against real Device rows and JSON-field queries."""

    @pytest.mark.parametrize(
        ("shape", "server_key"),
        [
            ({"default": 42}, "default"),
            ({"default": "42"}, "default"),
            ({"default": {"id": 42}}, "default"),
            ({"default": {"id": 1, "oob": {"id": 42}}}, "default"),
            (42, "anyserver"),
            ("42", "anyserver"),
        ],
        ids=repr,
    )
    def test_finds_each_storage_shape(self, shape, server_key):
        """Every supported storage shape is resolvable by a real query."""
        device = _dev(shape)
        assert _find(42, server_key) == device

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
        device = _dev(shape)
        assert _find(42, server_key) == device

    def test_returns_none_when_not_found(self):
        _dev({"production": 7})  # a row exists, but not for id 999
        assert _find(999, "production") is None

    def test_single_match_uses_one_query(self, django_assert_num_queries):
        """The common case (0 or 1 match) uses one combined query, because it runs per port during sync."""
        dev = _dev({"default": {"id": 42}})
        with django_assert_num_queries(1):
            result = _find(42)
        assert result == dev

    def test_fail_closed_when_host_and_oob_match_different_rows(self):
        _dev({"default": 42})
        _dev({"default": {"id": 99, "oob": {"id": 42}}})
        with pytest.raises(AmbiguousLibreNMSIdError, match="host pk=.* but a different OOB pk="):
            _find(42)

    def test_same_row_for_host_and_oob_is_returned(self):
        dev = _dev({"default": {"id": 42, "oob": {"id": 42}}})
        assert _find(42) == dev

    def test_fail_closed_on_duplicate_host_matches(self):
        _dev({"default": 42})
        _dev({"default": 42})
        with pytest.raises(AmbiguousLibreNMSIdError, match="multiple Device host records"):
            _find(42)

    def test_fail_closed_on_duplicate_oob_matches(self):
        _dev({"default": {"id": 1, "oob": {"id": 42}}})
        _dev({"default": {"id": 2, "oob": {"id": 42}}})
        with pytest.raises(AmbiguousLibreNMSIdError, match="multiple Device OOB records"):
            _find(42)

    def test_the_own_role_ignores_an_oob_reference(self):
        owner = _dev({"default": {"id": 42}})
        referrer = _dev({"default": {"id": 1, "oob": {"id": 42}}})

        assert _find(42, roles=(MappingRole.OWN,)) == owner
        assert _find(42, roles=(MappingRole.OOB,)) == referrer
        with pytest.raises(AmbiguousLibreNMSIdError):
            _find(42)

    def test_the_callers_queryset_scopes_the_lookup(self):
        from dcim.models import Device

        owner = _dev({"default": 42})
        other = _dev({"default": 43})

        assert _find(42, queryset=Device.objects.filter(pk=other.pk)) is None
        assert _find(42, queryset=Device.objects.filter(pk=owner.pk)) == owner

    def test_a_locking_queryset_locks_the_matched_row(self):
        from dcim.models import Device

        owner = _dev({"default": 42})
        with transaction.atomic(), CaptureQueriesContext(connection) as queries:
            assert _find(42, queryset=Device.objects.select_for_update()) == owner

        assert all("FOR UPDATE" in query["sql"] for query in queries.captured_queries)

    @pytest.mark.parametrize("invalid", [None, True, 0, -1, "", "  ", "abc", 42.0, {"id": 42}, [42]], ids=repr)
    def test_an_invalid_identity_is_refused_without_a_query(self, invalid, django_assert_num_queries):
        with django_assert_num_queries(0):
            assert _find(invalid) is None

    @pytest.mark.parametrize(
        ("identity", "found"),
        [
            (42, True),
            ("42", True),
            (" 42 ", True),
            ("+042", True),
            ("١٢", False),
            ("4_2", False),
            ("0" * 18 + "42", False),
            (True, False),
            (4.2, False),
            ("0", False),
            ("-1", False),
        ],
        ids=repr,
    )
    def test_the_lookup_accepts_only_the_ascii_id_rule(self, identity, found, django_assert_num_queries):
        """find_mapping and find_port_owner accept exactly the identities coerce_librenms_id accepts."""
        device = _dev({"default": 42})
        _dev({"default": 12})
        interface = _bind(make_interface(device, "eth0"), {"default": 42})
        _bind(make_interface(device, "eth1"), {"default": 12})

        with django_assert_num_queries(3 if found else 0):
            assert _find(identity) == (device if found else None)
            assert find_port_owner(identity, server="default") == (interface if found else None)

    def test_a_lookup_names_at_least_one_role(self):
        with pytest.raises(ValueError, match="role"):
            _find(42, roles=())


@pytest.mark.django_db
class TestIdentityPredicate:
    """identity_q builds the one set of JSON predicates every lookup uses."""

    def test_an_invalid_identity_matches_nothing(self):
        from dcim.models import Device

        _dev("abc")  # a corrupt legacy row that a literal predicate would match
        for bad in ("abc", True, 0, -5, None):
            assert not Device.objects.filter(
                identity_q(Device, server="prod", identities=(bad,), roles=BOTH_ROLES)
            ).exists()

    def test_an_invalid_server_key_matches_nothing(self):
        from dcim.models import Device

        _dev({"invalid__server": 42})
        _dev({"invalid": {"server": 42}})

        assert not Device.objects.filter(
            identity_q(Device, server="invalid__server", identities=(42,), roles=BOTH_ROLES)
        ).exists()

    def test_several_identities_and_the_roles_compose(self):
        from dcim.models import Device

        first = _dev({"default": 41})
        second = _dev({"default": {"id": 1, "oob": {"id": 42}}})
        _dev({"default": 43})

        own = Device.objects.filter(identity_q(Device, server="default", identities=(41, 42), roles=(MappingRole.OWN,)))
        both = Device.objects.filter(identity_q(Device, server="default", identities=(41, 42), roles=BOTH_ROLES))

        assert list(own) == [first]
        assert set(both) == {first, second}
        assert not Device.objects.filter(identity_q(Device, server="default", identities=(), roles=BOTH_ROLES)).exists()


@pytest.mark.django_db
class TestPortLookups:
    """Port-space lookups across Interface and VMInterface."""

    def test_a_port_owner_is_found_on_either_interface_model(self):
        from virtualization.models import VMInterface

        vm_interface = _bind(
            VMInterface.objects.create(virtual_machine=make_vm("port-owner-vm"), name="eth0"), {"p": 5}
        )

        assert find_port_owner(5, server="p") == vm_interface
        assert find_port_owner(6, server="p") is None

    def test_port_holders_agree_with_find_port_owner(self):
        from virtualization.models import VMInterface

        single = _bind(make_interface(_dev(), "eth0"), {"p": 5})
        oob_holder = _bind(
            VMInterface.objects.create(virtual_machine=make_vm("held-port-vm"), name="eth0"),
            {"p": {"id": 1, "oob": {"id": "6"}}},
        )
        _bind(make_interface(_dev(), "eth1"), {"p": 7})
        _bind(make_interface(_dev(), "eth2"), {"p": 7})
        _bind(make_interface(_dev(), "eth3"), {"q": 8})

        assert port_holders([5, "6", 7, 8, 9, "abc"], server="p") == {
            5: ("dcim.interface", single.pk),
            6: ("virtualization.vminterface", oob_holder.pk),
            7: None,
        }
        assert find_port_owner(6, server="p") == oob_holder
        with pytest.raises(AmbiguousLibreNMSIdError):
            find_port_owner(7, server="p")
        assert port_holders([5], server="") == {}

    def test_a_port_held_on_both_models_is_ambiguous(self):
        from virtualization.models import VMInterface

        _bind(make_interface(_dev(), "eth0"), {"p": 5})
        _bind(VMInterface.objects.create(virtual_machine=make_vm("port-both-vm"), name="eth0"), {"p": 5})

        with pytest.raises(AmbiguousLibreNMSIdError, match="both an Interface and a VMInterface"):
            find_port_owner(5, server="p")

    def test_a_device_port_resolves_by_id_first_then_by_name(self):
        device = _dev()
        bound = _bind(make_interface(device, "xe-0/0/1"), {"p": 5})
        named = make_interface(device, "eth0")
        _bind(make_interface(_dev(), "eth0"), {"p": 6})  # another device's binding stays out of scope

        assert resolve_device_port(device, server="p", port_id=5, name_candidates=["eth0"]) == bound
        assert resolve_device_port(device, server="p", port_id=6, name_candidates=["eth0"]) == named
        assert resolve_device_port(device, server="p", port_id=None, name_candidates=["missing"]) is None

    def test_a_name_never_wins_over_a_binding_to_another_port(self):
        device = _dev()
        stale = _bind(make_interface(device, "eth0"), {"p": 7})
        elsewhere = _bind(make_interface(device, "eth1"), {"q": 7})
        _bind(make_interface(device, "eth2"), 7)

        assert resolve_device_port(device, server="p", port_id=6, name_candidates=["eth0"]) is None
        assert resolve_device_port(device, server="p", port_id=None, name_candidates=["eth0"]) == stale
        assert resolve_device_port(device, server="p", port_id=6, name_candidates=["eth1"]) == elsewhere
        assert resolve_device_port(device, server="p", port_id=6, name_candidates=["eth2"]) is None

    def test_an_ambiguous_port_id_does_not_fall_through_to_a_name(self):
        device = _dev()
        _bind(make_interface(device, "a"), {"p": 5})
        _bind(make_interface(device, "b"), {"p": 5})
        make_interface(device, "eth0")

        assert resolve_device_port(device, server="p", port_id=5, name_candidates=["eth0"]) is None

    def test_an_oob_reference_is_not_a_device_port_binding(self):
        device = _dev()
        _bind(make_interface(device, "a"), {"p": {"oob": {"id": 5}}})
        named = make_interface(device, "eth0")

        assert resolve_device_port(device, server="p", port_id=5, name_candidates=["eth0"]) == named


@pytest.mark.django_db
class TestMappedDeviceServers:
    """Server enumeration across a subject and its virtual chassis."""

    def test_counts_own_oob_and_marker_entries_but_not_metadata(self):
        device = _dev(
            {
                "own": 1,
                "oob": {"oob": {"id": 2}},
                "moved": {"_migrated_to": {"device_id": 9, "server_key": "moved"}},
                "broken": "abc",
                "_preferred_server": "own",
            }
        )

        assert mapped_device_servers(device) == ("moved", "oob", "own")

    def test_a_legacy_value_counts_only_for_the_active_server(self):
        device = _dev("42")

        assert mapped_device_servers(device) == ()
        assert mapped_device_servers(device, active_server="primary") == ("primary",)
        assert mapped_device_servers(device, active_server="a__b") == ()

    def test_every_chassis_member_contributes(self):
        _chassis, (first, second) = make_virtual_chassis_members("mapped-servers", count=2)
        first.custom_field_data["librenms_id"] = {"a": 1}
        first.save()
        second.custom_field_data["librenms_id"] = "7"
        second.save()

        assert mapped_device_servers(first) == ("a",)
        assert mapped_device_servers(first, active_server="b") == ("a", "b")


@pytest.mark.django_db
class TestBulkRead:
    """read_mappings keeps the caller's queryset and reads every mapping in one query."""

    def test_scope_order_and_one_query(self, django_assert_num_queries):
        from dcim.models import Interface
        from django.db.models.signals import post_init

        device = _dev()
        for index, name in enumerate(("c", "a", "b"), start=1):
            _bind(make_interface(device, name), {"p": index})
        make_interface(_dev(), "z")  # outside the caller's scope
        built = []

        def count_instance(**_kwargs):
            built.append(1)

        queryset = Interface.objects.filter(device=device).order_by("-name")
        post_init.connect(count_instance, sender=Interface, weak=False)
        try:
            with django_assert_num_queries(1):
                records = read_mappings(queryset, fields=("name", "device_id"))
                rows = [(record.values, record.mapping.own_id("p")) for record in records]
        finally:
            post_init.disconnect(count_instance, sender=Interface)

        assert rows == [
            ({"name": "c", "device_id": device.pk}, 1),
            ({"name": "b", "device_id": device.pk}, 3),
            ({"name": "a", "device_id": device.pk}, 2),
        ]
        assert built == []

    def test_the_caller_names_no_storage_field(self):
        from dcim.models import Interface

        with pytest.raises(ValueError, match="mapping storage"):
            read_mappings(Interface.objects.all(), fields=("name", "custom_field_data"))


@pytest.mark.django_db
class TestSeedMappingHelper:
    """The test helper ``seed_mapping(save=True)`` always persists the mapping."""

    def test_a_seed_already_on_the_object_is_still_saved(self):
        device = _dev()
        seed_mapping(device, own=42, save=False)

        seed_mapping(device, own=42)

        assert read_mapping(type(device).objects.get(pk=device.pk)).own_id("default") == 42


def _locked(obj):
    return type(obj).objects.select_for_update().get(pk=obj.pk)


def _record_writes(writes):
    def write(row, fields):
        writes.append(fields)
        row.save()
        return row

    return write


@pytest.mark.django_db
class TestPersistMapping:
    """``persist_mapping`` claims again, checks the owners and the baseline, and then calls the save once."""

    def test_a_held_claim_refuses_at_once_and_writes_nothing(self):
        from netbox_librenms_plugin.server_mappings import IdentityBusy, assign_own, persist_mapping
        from netbox_librenms_plugin.tests.claim_race_helpers import held_device_claim

        device = _dev()
        writes = []
        with held_device_claim("default", 7101), transaction.atomic():
            row = _locked(device)
            with pytest.raises(IdentityBusy):
                persist_mapping(row, assign_own(row, "default", 7101), write=_record_writes(writes))

        assert writes == []
        device.refresh_from_db()
        assert read_mapping(device).own_id("default") is None

    def test_an_owner_of_the_other_model_is_refused(self):
        from netbox_librenms_plugin.server_mappings import IdentityOwned, assign_own, persist_mapping

        device = _dev()
        owner = make_vm(f"persist-owner-{next(_counter)}")
        owner.custom_field_data["librenms_id"] = {"default": 7102}
        owner.save()
        writes = []
        with transaction.atomic():
            row = _locked(device)
            with pytest.raises(IdentityOwned) as refused:
                persist_mapping(row, assign_own(row, "default", 7102), write=_record_writes(writes))

        assert refused.value.owner == owner
        assert writes == []

    def test_the_save_runs_once_with_the_mapping_and_the_writers_fields(self):
        from netbox_librenms_plugin.server_mappings import assign_own, persist_mapping

        device = _dev({"other": 7103})
        writes = []
        with transaction.atomic():
            row = _locked(device)
            row.serial = "PERSIST-SERIAL"
            persist_mapping(row, assign_own(row, "default", 7104), write=_record_writes(writes))

        assert len(writes) == 1
        device.refresh_from_db()
        assert device.serial == "PERSIST-SERIAL"
        assert [(entry.server, entry.own_id) for entry in read_mapping(device).servers] == [
            ("other", 7103),
            ("default", 7104),
        ]

    def test_a_mapping_that_changed_after_the_build_is_refused(self):
        from netbox_librenms_plugin.server_mappings import MappingChanged, assign_own, persist_mapping

        device = _dev({"other": 7105})
        stale = assign_own(device, "default", 7106)
        type(device).objects.filter(pk=device.pk).update(custom_field_data={"librenms_id": {"other": 7107}})
        writes = []
        with transaction.atomic(), pytest.raises(MappingChanged):
            persist_mapping(_locked(device), stale, write=_record_writes(writes))

        assert writes == []

    def test_another_custom_field_is_not_a_mapping_change(self):
        from netbox_librenms_plugin.server_mappings import assign_own, persist_mapping

        device = _dev({"other": 7108})
        change = assign_own(device, "default", 7109)
        stored = {**device.custom_field_data, "unrelated_field": "edited"}
        type(device).objects.filter(pk=device.pk).update(custom_field_data=stored)
        with transaction.atomic():
            persist_mapping(_locked(device), change, write=_record_writes([]))

        device.refresh_from_db()
        assert device.custom_field_data["unrelated_field"] == "edited"
        assert read_mapping(device).own_id("default") == 7109

    def test_a_change_persists_only_on_its_own_row(self):
        from netbox_librenms_plugin.server_mappings import assign_own, persist_mapping

        change = assign_own(_dev(), "default", 7110)
        with transaction.atomic(), pytest.raises(ValueError, match="belongs to"):
            persist_mapping(_locked(_dev()), change, write=_record_writes([]))


@transactional_db_with_all_apps()
@pytest.mark.parametrize("competitor", ["committed", "holding"])
def test_a_change_kept_across_a_savepoint_rollback_is_claimed_and_checked_again(competitor):
    """The rollback releases the first claim, so persistence claims again and reads the owners again."""
    import json

    from netbox_librenms_plugin.server_mappings import (
        IdentityBusy,
        IdentityOwned,
        assign_own,
        lock_librenms_id_assignment,
        persist_mapping,
    )
    from netbox_librenms_plugin.tests.claim_race_helpers import device_claim_key
    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection

    device = make_device(f"retained-change-{competitor}")
    vm = make_vm(f"retained-change-competitor-{competitor}")
    bound = json.dumps({"librenms_id": {"default": 7201}})

    class _RolledBack(Exception):
        pass

    with second_connection() as other, transaction.atomic():
        try:
            with transaction.atomic():
                lock_librenms_id_assignment(7201, "default")
                change = assign_own(_locked(device), "default", 7201)
                raise _RolledBack
        except _RolledBack:
            pass
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [device_claim_key("default", 7201)])
            assert cursor.fetchone()[0] is True
            cursor.execute(
                "UPDATE virtualization_virtualmachine SET custom_field_data = %s WHERE id = %s", [bound, vm.pk]
            )
        if competitor == "committed":
            other.commit()
        expected = IdentityOwned if competitor == "committed" else IdentityBusy
        with pytest.raises(expected):
            persist_mapping(_locked(device), change, write=_record_writes([]))

    device.refresh_from_db()
    assert read_mapping(device).own_id("default") is None


MOVED_OUT_OF_UTILS = frozenset(
    {
        "AmbiguousLibreNMSIdError",
        "coerce_librenms_id",
        "normalize_librenms_port_id",
        "read_mapping",
        "find_mapping",
        "identity_q",
        "decode_stored_mapping",
        "readable_legacy_id",
    }
)
# The write side moved whole into server_mappings; utils keeps no name of it, not even an import.
WRITE_SIDE_NAMES = frozenset(
    {
        "set_librenms_device_id",
        "add_librenms_server_mapping",
        "lock_librenms_id_assignment",
        "set_librenms_oob",
        "clear_librenms_oob",
        "migrate_legacy_librenms_id",
        "merge_librenms_links",
        "mark_librenms_migrated",
        "claim_librenms_port_binding",
        "LibreNMSPortBindingConflict",
        "LibreNMSPortBindingBusy",
        "get_librenms_sync_device",
    }
)


def test_utils_holds_no_write_side_name():
    from netbox_librenms_plugin import utils

    assert sorted(name for name in WRITE_SIDE_NAMES if hasattr(utils, name)) == []


def test_no_module_imports_a_mapping_or_id_name_from_utils():
    """Utils only uses these names, so an import of one from utils would make utils a re-export."""
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "utils":
                offenders += [
                    f"{path.name}:{node.lineno} {alias.name}"
                    for alias in node.names
                    if alias.name in MOVED_OUT_OF_UTILS | WRITE_SIDE_NAMES
                ]

    assert offenders == []


@pytest.mark.django_db
class TestConvertLegacy:
    """Tests for convert_legacy(): it returns a change and never mutates or saves."""

    def test_returns_true_when_migrated(self):

        assert convert_legacy(_dev(42), "default").changed is True

    def test_migrates_integer_to_dict_format(self):

        dev = _dev(42)
        apply_mapping_change(dev, convert_legacy(dev, "production"))
        assert dev.custom_field_data["librenms_id"] == {"production": 42}

    def test_returns_false_when_already_dict(self):

        assert convert_legacy(_dev({"default": 42}), "default").changed is False

    def test_returns_false_when_value_is_none(self):

        assert convert_legacy(_dev(None), "default").changed is False

    def test_returns_false_for_boolean_value(self):
        """A bool is an int subclass, so True/False must not be migrated."""

        dev = _dev(True)
        assert convert_legacy(dev, "default").changed is False
        assert dev.custom_field_data["librenms_id"] is True  # unchanged

    def test_does_not_save(self):
        """convert_legacy must NOT persist: persistence is the writer's."""
        from dcim.models import Device

        dev = _dev(7)
        apply_mapping_change(dev, convert_legacy(dev, "default"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == 7

    def test_preserves_value_in_migrated_dict(self):

        dev = _dev(99)
        apply_mapping_change(dev, convert_legacy(dev, "secondary"))
        assert dev.custom_field_data["librenms_id"]["secondary"] == 99


@pytest.mark.django_db
class TestLibreNMSIdRoundtrip:
    """The reader sees the value that an applied builder change sets."""

    def test_set_then_get_returns_same_value(self):
        from netbox_librenms_plugin.tests.conftest import seed_own_mapping

        dev = _dev()
        seed_own_mapping(dev, 42, "production")
        assert read_mapping(dev).own_id("production") == 42

    def test_set_multiple_servers_get_correct_each(self):
        from netbox_librenms_plugin.tests.conftest import seed_own_mapping

        dev = _dev()
        seed_own_mapping(dev, 10, "primary")
        seed_own_mapping(dev, 20, "secondary")
        assert read_mapping(dev).own_id("primary") == 10
        assert read_mapping(dev).own_id("secondary") == 20

    def test_migrate_then_get_returns_value(self):

        dev = _dev(55)
        apply_mapping_change(dev, convert_legacy(dev, "default"))
        assert read_mapping(dev).own_id("default") == 55


class TestLegacyClassification:
    """Unit coverage for the wide legacy rule that readers and writers share (no DB)."""

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
        assert _decoded(value).legacy.is_legacy is expected

    def test_defined_exactly_once(self):
        """Guard the wide rule has exactly one module-level def (a duplicate silently shadows the other)."""
        import ast
        import inspect

        from netbox_librenms_plugin import server_mappings

        defs = [
            node
            for node in ast.parse(inspect.getsource(server_mappings)).body
            if isinstance(node, ast.FunctionDef) and node.name == "_readable_legacy_id"
        ]
        assert len(defs) == 1, f"_readable_legacy_id defined {len(defs)}x — a duplicate shadows the other"


@pytest.mark.django_db
class TestAssignOwn:
    """Tests for assign_own(): the general setter returns a change and its outcome."""

    @pytest.mark.parametrize("stored", [42, "42"])
    def test_a_legacy_value_is_skipped_not_migrated(self, stored):
        """A legacy bare-int or numeric-string value is left untouched (no silent migration)."""
        change = assign_own(_dev(stored), "primary", 99)

        assert change.outcome is ChangeOutcome.SKIPPED_LEGACY
        assert change.changed is False
        assert change.after == change.before

    def test_stores_int_for_valid_device_id(self):
        dev = _dev(None)
        apply_mapping_change(dev, assign_own(dev, "primary", 42))
        assert dev.custom_field_data["librenms_id"] == {"primary": 42}

    @pytest.mark.parametrize(("stored", "identity"), [(None, "not-an-int"), ({"primary": 10}, None), (None, True)])
    def test_an_invalid_id_is_skipped_and_claims_nothing(self, stored, identity):
        change = assign_own(_dev(stored), "primary", identity)

        assert change.outcome is ChangeOutcome.SKIPPED_INVALID_ID
        assert change.changed is False
        assert change._claims == ()

    def test_adds_new_server_key_to_existing_dict(self):
        dev = _dev({"primary": 5})
        apply_mapping_change(dev, assign_own(dev, "secondary", 20))
        assert dev.custom_field_data["librenms_id"] == {"primary": 5, "secondary": 20}

    def test_string_integer_is_coerced(self):
        change = assign_own(_dev(), "primary", "42")
        assert change.outcome is ChangeOutcome.APPLIED
        assert change.after.own_id("primary") == 42

    def test_unexpected_cf_type_reset_to_empty(self):
        dev = _dev("unexpected-string")
        apply_mapping_change(dev, assign_own(dev, "primary", 5))
        assert dev.custom_field_data["librenms_id"] == {"primary": 5}

    def test_the_same_id_is_unchanged(self):
        change = assign_own(_dev({"primary": 5}), "primary", 5)
        assert change.outcome is ChangeOutcome.UNCHANGED
        assert change.changed is False

    def test_a_builder_runs_no_query_and_does_not_mutate(self, django_assert_num_queries):
        dev = _dev({"primary": 5})
        with django_assert_num_queries(0):
            change = assign_own(dev, "primary", 6)
        assert change.changed is True
        assert dev.custom_field_data["librenms_id"] == {"primary": 5}


class TestLegacyClassificationPositivity:
    """The wide rule treats only a *positive* bare int / int-string as a legacy link."""

    def test_positive_int_and_string_are_legacy(self):
        # int() coercion accepts surrounding whitespace / a leading +, so these stay legacy.
        for value in (42, "42", " 42 ", "+42"):
            assert _decoded(value).legacy.is_legacy is True, value

    def test_zero_and_negative_are_not_legacy(self):
        """A LibreNMS device id is a positive PK; 0 / negative is not a real link and must not migrate."""
        for value in (0, -1, "0", " 0 ", "-1", "-42", "+0"):
            assert _decoded(value).legacy.is_legacy is False, value

    def test_non_numeric_and_dict_and_bool_are_not_legacy(self):
        for value in (None, True, False, "abc", "", {"default": 42}):
            assert _decoded(value).legacy.is_legacy is False, value


@pytest.mark.django_db
class TestLibreNMSIdAcceptedFormsContract:
    """Pin which stored librenms_id forms the reader and the lookup each accept.

    Two different definitions of "a numeric id" are in use, and they are not the same:

    * the snapshot's ``legacy.readable_id`` (the wide rule) parses a top-level string with a
      bare ``int()``;
    * ``legacy.queryable_id``, ``coerce_librenms_id`` and the ``identity_q`` predicates behind
      ``find_mapping`` require ASCII digits.

    Their overlap is the deliberate legacy tolerance: surrounding ASCII whitespace, a
    leading ``+``, leading zeros. Anything the reader accepts BEYOND that overlap resolves
    for the reader while no query can find the row, so a guard built on
    ``find_mapping`` cannot see it.

    These tests CHARACTERISE that split rather than endorse it. Narrowing the reader was
    tried and reverted: it only makes sense together with narrowing the classifier, and
    once both are narrow ``assign_own`` treats the value as corrupt and resets
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
        from netbox_librenms_plugin.librenms_ids import coerce_librenms_id

        device = _dev(stored)

        assert read_mapping(device).own_id("default") == resolved
        assert coerce_librenms_id(stored) == resolved
        assert _find(resolved) == device

    @pytest.mark.parametrize("stored,resolved", READER_ONLY, ids=lambda v: repr(v))
    def test_reader_only_forms_resolve_but_no_query_finds_them(self, stored, resolved):
        """Forms only a bare int() accepts: the reader resolves them, the lookup is blind to them."""
        from netbox_librenms_plugin.librenms_ids import coerce_librenms_id

        device = _dev(stored)

        # The reader binds the row to an id ...
        assert read_mapping(device).own_id("default") == resolved
        # ... that no predicate can match, so every lookup-based guard is blind to it.
        assert coerce_librenms_id(stored) is None
        assert _find(resolved) is None

    @pytest.mark.parametrize("stored,resolved", READER_ONLY, ids=lambda v: repr(v))
    def test_a_read_leaves_a_reader_only_form_invisible_to_the_lookup(self, stored, resolved):
        """A read never rewrites the stored value, so only an explicit conversion makes the row findable."""
        device = _dev(stored)

        assert read_mapping(device).legacy.readable_id == resolved
        assert read_mapping(device).legacy.queryable_id is None
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] == stored
        assert _find(resolved) is None

    @pytest.mark.parametrize("stored,_resolved", TOLERATED + READER_ONLY, ids=lambda v: repr(v))
    def test_the_setter_keeps_every_value_the_reader_resolves(self, stored, _resolved):
        """assign_own refuses a legacy value, so it never resets a mapping the reader still resolves."""
        from netbox_librenms_plugin.tests.conftest import seed_own_mapping

        device = _dev(stored)
        seed_own_mapping(device, 99, "primary")

        assert device.custom_field_data["librenms_id"] == stored


@pytest.mark.django_db
class TestConvertLegacyRejectsNonPositive:
    """convert_legacy must never canonicalise a non-positive id into the JSON form."""

    def test_zero_is_not_migrated(self):

        obj = _dev(0)
        assert convert_legacy(obj, "default").changed is False
        assert obj.custom_field_data["librenms_id"] == 0  # left untouched, not {"default": 0}

    def test_negative_is_not_migrated(self):

        obj = _dev("-5")
        assert convert_legacy(obj, "default").changed is False
        assert obj.custom_field_data["librenms_id"] == "-5"


@pytest.mark.django_db
class TestOOBHelpers:
    """Tests for the OOB snapshot facts, attach_oob, clear_oob, and the dict-with-id form in the reader, assign_own and find_mapping."""

    # ── get_librenms_device_id: dict-with-id form ─────────────────────────────

    def test_get_id_from_dict_with_id_form(self):
        assert read_mapping(_dev({"primary": {"id": 42}})).own_id("primary") == 42

    def test_get_id_when_oob_also_present(self):
        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "drac"}}})
        assert read_mapping(dev).own_id("primary") == 42

    def test_get_returns_none_for_dict_without_id_key(self):
        assert read_mapping(_dev({"primary": {"oob": {"id": 17}}})).own_id("primary") is None

    def test_get_normalises_string_id_inside_dict_with_id_form(self):
        dev = _dev({"primary": {"id": "42"}})
        assert read_mapping(dev).own_id("primary") == 42

    # ── assign_own: oob preservation ─────────────────────────────────────────

    def test_set_preserves_oob_when_entry_has_oob(self):
        from netbox_librenms_plugin.tests.conftest import seed_own_mapping

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "drac", "ip": "10.0.0.5"}}})
        seed_own_mapping(dev, 99, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {
            "primary": {"id": 99, "oob": {"id": 17, "type": "drac", "ip": "10.0.0.5"}}
        }

    def test_set_bare_int_when_no_oob_present(self):
        from netbox_librenms_plugin.tests.conftest import seed_own_mapping

        dev = _dev({"primary": 42})
        seed_own_mapping(dev, 99, server_key="primary")
        assert dev.custom_field_data["librenms_id"] == {"primary": 99}

    # ── find_by_librenms_id: dict-with-id and oob id lookups ─────────────────

    def test_find_by_matches_main_id_in_dict_with_id_form(self):

        dev = _dev({"primary": {"id": 42}})
        assert _find(42, "primary") == dev

    def test_find_by_matches_oob_id(self):

        dev = _dev({"primary": {"id": 1, "oob": {"id": 17}}})
        assert _find(17, "primary") == dev

    def test_find_by_does_not_return_unrelated_id(self):

        _dev({"primary": {"id": 1}})
        assert _find(999, "primary") is None

    # ── OOB snapshot facts ────────────────────────────────────────────────────

    def test_a_legacy_bare_int_has_no_oob(self):
        assert not read_mapping(_dev(42)).has_oob("primary")

    def test_a_bare_int_entry_has_no_oob(self):
        assert not read_mapping(_dev({"primary": 42})).has_oob("primary")

    def test_an_oob_entry_reads_its_id_and_type(self):
        oob_data = {"id": 17, "type": "drac", "version": "5.10", "ip": "10.0.0.5"}
        dev = _dev({"primary": {"id": 42, "oob": oob_data}})
        entry = read_mapping(dev).server("primary")
        assert (entry.oob_recorded, entry.oob_id, entry.oob_type) == (True, 17, "drac")
        assert dev.custom_field_data["librenms_id"]["primary"]["oob"] == oob_data

    # ── attach_oob ────────────────────────────────────────────────────────────

    def test_set_oob_round_trip(self):
        """attach_oob stores only id + type; ip/version are not persisted."""

        dev = _dev({"primary": 42})
        apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="drac"))
        assert dev.custom_field_data["librenms_id"]["primary"]["oob"] == {"id": 17, "type": "drac"}
        assert read_mapping(dev).oob_id("primary") == 17

    def test_set_oob_promotes_bare_int_entry(self):
        """attach_oob promotes a bare-int entry to dict form, preserving the main id."""

        dev = _dev({"primary": 42})
        apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="idrac"))
        assert read_mapping(dev).own_id("primary") == 42

    def test_set_oob_fails_closed_on_non_positive_int_host_id(self):
        """A stored bare-int host id of 0 or negative is corrupt → raise."""

        for bad in (0, -5):
            dev = _dev({"primary": bad})
            with pytest.raises(ValueError, match="not a valid id"):
                apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="idrac"))

    def test_set_oob_rejects_unknown_type(self):
        """attach_oob raises ValueError for a type that doesn't match OOB_TYPE_PATTERN."""

        dev = _dev({"primary": 42})
        with pytest.raises(ValueError, match="does not match any known OOB type"):
            apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="ubuntu"))

    def test_set_oob_fails_closed_on_corrupt_host_string(self):
        """A non-empty, unparseable stored host id must raise rather than be collapsed to {}."""

        dev = _dev({"primary": "not-an-id"})
        with pytest.raises(ValueError, match="not a valid id"):
            apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="idrac"))

    def test_set_oob_fails_closed_on_corrupt_dict_host_id(self):
        """A dict-form entry with a non-empty unparseable host id (e.g. {"id": "abc"}) must raise."""

        dev = _dev({"primary": {"id": "abc"}})
        with pytest.raises(ValueError, match="not a valid id"):
            apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="idrac"))

    def test_set_oob_lenient_on_dict_without_host_id(self):
        """A dict entry with no host id (absent/None) stays lenient — OOB is attached."""

        dev = _dev({"primary": {"id": None}})
        apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="idrac"))  # must not raise
        assert dev.custom_field_data["librenms_id"]["primary"]["oob"] == {"id": 17, "type": "idrac"}

    def test_set_oob_lenient_on_empty_host_string(self):
        """An empty/whitespace host string is treated leniently (→ fresh dict), not an error."""

        dev = _dev({"primary": "   "})
        apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="idrac"))  # must not raise

    def test_set_oob_accepts_generic_oob_sentinel(self):
        """attach_oob must accept "oob" as a generic fallback type."""

        dev = _dev({"default": 99})
        apply_mapping_change(dev, attach_oob(dev, "default", 55, oob_type="oob"))
        entry = read_mapping(dev).server("default")
        assert entry.oob_recorded
        assert entry.oob_id == 55
        assert entry.oob_type == "oob"

    def test_set_oob_generic_sentinel_case_insensitive(self):
        """The "oob" sentinel is accepted case-insensitively (OOB, Oob, etc.)."""

        dev = _dev({"default": 99})
        apply_mapping_change(dev, attach_oob(dev, "default", 55, oob_type="OOB"))  # should not raise
        assert dev.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"

    def test_set_oob_does_not_save(self):
        """attach_oob must NOT persist: persistence is the writer's (verified by reload)."""
        from dcim.models import Device

        dev = _dev({"primary": 42})
        apply_mapping_change(dev, attach_oob(dev, "primary", 17, oob_type="ilo"))
        # DB row still holds the bare-int entry; the OOB promotion lives only in memory.
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"primary": 42}

    # ── clear_oob ─────────────────────────────────────────────────────────────

    def test_clear_oob_removes_oob_sub_key(self):

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "drac"}}})
        apply_mapping_change(dev, clear_oob(dev, "primary"))
        assert not read_mapping(dev).has_oob("primary")
        assert dev.custom_field_data["librenms_id"]["primary"] == {"id": 42}

    def test_clear_oob_is_noop_when_no_oob(self):

        dev = _dev({"primary": {"id": 42}})
        apply_mapping_change(dev, clear_oob(dev, "primary"))
        assert dev.custom_field_data["librenms_id"] == {"primary": {"id": 42}}

    def test_clear_oob_does_not_save(self):
        """clear_oob must NOT persist: persistence is the writer's (verified by reload)."""
        from dcim.models import Device

        dev = _dev({"primary": {"id": 42, "oob": {"id": 17, "type": "bmc"}}})
        apply_mapping_change(dev, clear_oob(dev, "primary"))
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {
            "primary": {"id": 42, "oob": {"id": 17, "type": "bmc"}}
        }


@pytest.mark.django_db
class TestMergeLinks:
    """Tests for merge_links() — winner-wins conflict policy."""

    def _make_dev(self, name, librenms_id_dict):
        return _dev(librenms_id_dict, name=f"{name}-{next(_counter)}")

    def test_winner_inherits_id_when_winner_has_no_id(self):

        winner = self._make_dev("eve-ng-02", {"default": {}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 99
        assert summary["host_id_from_donor"] == 99
        assert summary["donor_id_demoted_to_oob"] is None

    def test_winner_inherits_string_id_coerced_to_int(self):
        """inherit-id branch must coerce donor_id to int, matching the demote branch."""

        winner = self._make_dev("eve-ng-02", {"default": {}})
        # Simulate a custom field value that arrived as a JSON string (e.g. "99").
        donor = self._make_dev("router-spare", {"default": {"id": "99"}})
        summary = _merge_into_winner(winner, donor, "default")

        stored = winner.custom_field_data["librenms_id"]["default"]["id"]
        assert stored == 99
        assert isinstance(stored, int)
        assert summary["host_id_from_donor"] == 99
        assert isinstance(summary["host_id_from_donor"], int)

    def test_donor_id_demoted_to_oob_when_winner_has_id_and_donor_name_matches_oob_pattern(self):

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 42
        assert winner.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 99
        assert winner.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "idrac"
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}

    def test_distinct_donor_host_and_oob_with_only_one_winner_slot_fails_closed(self):
        """Two distinct donor links cannot be compressed into the winner's one free OOB slot."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": 99, "oob": {"id": 77, "type": "ilo"}}})
        with pytest.raises(ValueError, match="two distinct LibreNMS links"):
            _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 42}

    def test_donor_id_demoted_to_oob_generic_when_no_pattern_in_name(self):
        """Donor id is always demoted; type falls back to 'oob' when no keyword in name."""

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-03-spare", {"default": {"id": 99}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 42
        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 99, "type": "oob"}
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "oob"}

    def test_blank_only_donor_oob_does_not_persist_empty_oob_slot(self):
        """A donor oob carrying only a blank id (no other metadata) must NOT leave an empty {} oob.

        The blank id is dropped (validated up-front); with no other metadata the inherited oob
        collapses to {} and must not be written — a persisted empty dict reads as an occupied slot.
        """

        winner = self._make_dev("host-win", {"default": {}})
        donor = self._make_dev("host-don", {"default": {"oob": {"id": "  "}}})  # blank id, nothing else
        summary = _merge_into_winner(winner, donor, "default")

        entry = winner.custom_field_data["librenms_id"]["default"]
        assert "oob" not in entry, f"empty oob slot persisted: {entry}"
        assert summary["oob_from_donor"] is None

    def test_empty_oob_inheritance_does_not_block_a_later_demote(self):
        """The real harm: a persisted empty {} oob would block a subsequent donor-id demotion.

        Merge a blank-only donor oob into a winner that holds a host id, then merge a second donor
        whose host id should demote into the (still-free) oob slot. Before the fix the first merge
        wrote oob={}, which the second merge read as occupied → the second donor id was lost.
        """

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        _merge_into_winner(winner, self._make_dev("blank-oob", {"default": {"oob": {"id": " "}}}), "default")
        # The blank-only oob left the slot free, not occupied by {}.
        assert "oob" not in winner.custom_field_data["librenms_id"]["default"]

        summary = _merge_into_winner(winner, self._make_dev("idrac-jhw6nc4", {"default": {"id": 99}}), "default")
        assert winner.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 99
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}

    def test_demoted_oob_type_prefers_vendor_token_over_generic(self):
        # A donor name carrying a generic 'oob' token BEFORE the vendor token (e.g.
        # 'leaf01-oob-idrac9') must demote with the vendor type ('idrac'), matching the import-path
        # normalize_oob_type — not the raw first-match search that would pick the generic 'oob'.

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("leaf01-oob-idrac9", {"default": {"id": 99}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 99, "type": "idrac"}
        assert summary["donor_id_demoted_to_oob"] == {"id": 99, "type": "idrac"}

    def test_winner_inherits_donor_oob_when_winner_has_none(self):

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ipmi"}
        assert summary["oob_from_donor"] == {"id": 77, "type": "ipmi"}

    def test_malformed_donor_oob_id_fails_closed_on_inherit(self):
        """A corrupt donor oob link ({"oob": {"id": "abc"}}) must not be inherited verbatim; the inherit branch coerces the host id and raises on a non-empty invalid value."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": "abc", "type": "ipmi"}}})
        with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
            _merge_into_winner(winner, donor, "default")

    def test_non_dict_donor_oob_shape_fails_closed(self):
        """A corrupt non-dict donor oob (e.g. a list) is corrupted state, not 'no OOB link' — fail closed rather than silently drop it during merge."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"id": 7, "oob": ["not", "a", "dict"]}})
        with pytest.raises(ValueError, match="unsupported librenms_id.*oob shape"):
            _merge_into_winner(winner, donor, "default")

    def test_non_dict_winner_oob_shape_fails_closed(self):
        """A corrupt non-dict winner oob (e.g. a string) must fail closed, not be silently overwritten by donor data."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": "garbage"}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        with pytest.raises(ValueError, match="unsupported librenms_id.*oob shape"):
            _merge_into_winner(winner, donor, "default")

    def test_malformed_winner_oob_id_fails_closed(self):
        """A winner oob with a non-blank unparseable id ({"oob": {"id": "abc"}} / {"id": 0}) only passes the shape check, so it would look 'occupied' and skip inheriting the donor's real controller — losing it once the donor is marked migrated. It must fail closed instead."""
        import pytest

        for bad_id in ("abc", 0):
            winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": {"id": bad_id, "type": "ipmi"}}})
            donor = self._make_dev("idrac-jhw6nc4", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
            with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
                _merge_into_winner(winner, donor, "default")

    def test_blank_winner_oob_id_is_lenient(self):
        """A blank/whitespace winner oob id must NOT fail closed (matches the lenient host-id handling) — the merge proceeds without raising."""

        # Winner holds a host id and a blank-id oob slot; the donor carries only the SAME host id
        # (a duplicate mapping, not an orphan) so the blank-oob leniency is exercised without
        # tripping the "donor host id has nowhere to move" guard that a distinct donor id would.
        winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": {"id": "  ", "type": "ipmi"}}})
        donor = self._make_dev("eve-ng-02-dup", {"default": {"id": 42}})
        # Must not raise; the blank winner oob id is treated leniently as "no id".
        _merge_into_winner(winner, donor, "default")

    def test_distinct_donor_host_id_with_winner_holding_both_slots_fails_closed(self):
        """A distinct donor host id with the winner holding both its host and oob slots fails closed."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {"id": 100, "oob": {"id": 50, "type": "idrac"}}})
        donor = self._make_dev("router-spare", {"default": {"id": 200}})
        with pytest.raises(ValueError, match="already holds both a LibreNMS host id and an OOB link"):
            _merge_into_winner(winner, donor, "default")
        # The donor's link must be left untouched (nothing captured, no partial mutation of winner).
        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 100, "oob": {"id": 50, "type": "idrac"}}

    def test_duplicate_donor_host_id_with_winner_holding_both_slots_is_allowed(self):
        """A donor host id equal to the winner's is a duplicate mapping, not an orphan, so it is allowed."""

        winner = self._make_dev("eve-ng-02", {"default": {"id": 100, "oob": {"id": 50, "type": "idrac"}}})
        donor = self._make_dev("eve-ng-02-dup", {"default": {"id": 100}})
        summary = _merge_into_winner(winner, donor, "default")
        # Winner is unchanged (same host id, keeps its own oob); nothing was demoted or dropped.
        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 100, "oob": {"id": 50, "type": "idrac"}}
        assert summary["donor_id_demoted_to_oob"] is None

    def test_donor_oob_id_coerced_to_int_on_inherit(self):
        """A numeric-string donor oob id is normalized to int when inherited."""

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": "77", "type": "ipmi"}}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 77, "type": "ipmi"}
        assert summary["oob_from_donor"] == {"id": 77, "type": "ipmi"}

    def test_blank_donor_oob_id_is_lenient_and_dropped(self):
        """A blank/whitespace donor oob id ({"oob": {"id": " "}}) must be treated as 'no oob id' (lenient) — the same as a blank host id and an absent oob id — not raise like a non-blank corrupt one."""

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-x", {"default": {"oob": {"id": "   ", "type": "drac"}}})
        summary = _merge_into_winner(winner, donor, "default")

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

        winner = self._make_dev("eve-ng-02", {"default": {"id": 50}})
        donor = self._make_dev("idrac-host", {"default": {"id": 99, "oob": {"type": "idrac"}}})
        summary = _merge_into_winner(winner, donor, "default")

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

        winner = self._make_dev("eve-ng-02", {"default": {"id": 50}})
        donor = self._make_dev("idrac-host", {"default": {"id": 99, "oob": {"id": "abc", "type": "idrac"}}})
        with pytest.raises(ValueError, match="unparseable librenms_id.*oob id"):
            _merge_into_winner(winner, donor, "default")

    def test_winner_oob_never_overwritten(self):

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42, "oob": {"id": 11, "type": "drac"}}})
        donor = self._make_dev("eve-ng-02-old", {"default": {"oob": {"id": 77, "type": "ipmi"}}})
        summary = _merge_into_winner(winner, donor, "default")

        assert winner.custom_field_data["librenms_id"]["default"]["oob"] == {"id": 11, "type": "drac"}
        assert summary["oob_from_donor"] is None

    def test_legacy_bare_int_raises(self):
        import pytest

        winner = self._make_dev("legacy-winner", 42)
        donor = self._make_dev("idrac-x", {"default": {"id": 99}})
        with pytest.raises(ValueError):
            _merge_into_winner(winner, donor, "default")

    def test_malformed_donor_id_raises_clear_error_in_inherit_branch(self):
        """coerce_librenms_id raises ValueError with a clear message for non-numeric donor ids."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {}})
        donor = self._make_dev("router-spare", {"default": {"id": "not-a-number"}})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            _merge_into_winner(winner, donor, "default")

    def test_malformed_per_server_string_id_fails_closed(self):
        """A bare per-server string entry ({server_key: 'abc'}) that can't be parsed must raise, not silently collapse to {} (which would drop/swap link state)."""
        import pytest

        # Bad winner string id.
        winner = self._make_dev("eve-ng-02", {"default": "abc"})
        donor = self._make_dev("router-spare", {"default": {"id": 99}})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            _merge_into_winner(winner, donor, "default")

        # Bad donor string id.
        winner = self._make_dev("eve-ng-02", {"default": {}})
        donor = self._make_dev("router-spare", {"default": "xyz"})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            _merge_into_winner(winner, donor, "default")

    def test_empty_per_server_string_id_is_lenient(self):
        """An empty/whitespace string is treated as 'no id', not a hard error."""

        winner = self._make_dev("eve-ng-02", {"default": "  "})
        donor = self._make_dev("router-spare", {"default": {"id": 99}})
        summary = _merge_into_winner(winner, donor, "default")
        # Winner had no usable id → inherits donor's host id.
        assert summary["host_id_from_donor"] == 99

    def test_blank_dict_form_id_is_lenient(self):
        """A blank/whitespace dict-form id ({"id": " "}) must be treated as 'no id' (lenient), the same as a blank top-level string — not raise like a non-blank corrupt id ('abc')."""

        winner = self._make_dev("eve-ng-02", {"default": {"id": "   "}})
        donor = self._make_dev("router-spare", {"default": {"id": 99}})
        summary = _merge_into_winner(winner, donor, "default")
        # Winner's blank id is "no id" → it inherits the donor's host id rather than raising.
        assert winner.custom_field_data["librenms_id"]["default"]["id"] == 99
        assert summary["host_id_from_donor"] == 99

    def test_malformed_donor_id_raises_clear_error_in_demote_branch(self):
        """Same clear error when demoting donor id into winner's oob slot."""
        import pytest

        winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
        donor = self._make_dev("idrac-jhw6nc4", {"default": {"id": "bad"}})
        with pytest.raises(ValueError, match="unparseable librenms_id"):
            _merge_into_winner(winner, donor, "default")

    def test_falsy_corrupt_top_level_librenms_id_fails_closed(self):
        """A top-level librenms_id of False/0 must raise, not collapse to {} via `or {}` and merge as 'no mapping'."""
        import pytest

        for bad in (False, 0):
            # Corrupt winner.
            winner = self._make_dev("eve-ng-02", bad)
            donor = self._make_dev("router-spare", {"default": {"id": 99}})
            with pytest.raises(ValueError, match="legacy bare-integer or corrupt"):
                _merge_into_winner(winner, donor, "default")

            # Corrupt donor.
            winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
            donor = self._make_dev("router-spare", bad)
            with pytest.raises(ValueError, match="legacy bare-integer or corrupt"):
                _merge_into_winner(winner, donor, "default")

    def test_unsupported_winner_entry_shape_fails_closed(self):
        """A non-None winner entry of an unsupported type (bool/float/list) must raise, not collapse to {} (which would let the winner inherit the donor's id)."""
        import pytest

        for bad in (True, 1.5, [99], (1, 2)):
            winner = self._make_dev("eve-ng-02", {"default": bad})
            donor = self._make_dev("router-spare", {"default": {"id": 99}})
            with pytest.raises(ValueError, match="unsupported librenms_id"):
                _merge_into_winner(winner, donor, "default")

    def test_unsupported_donor_entry_shape_fails_closed(self):
        """A non-None donor entry of an unsupported type must raise rather than silently becoming {} (dropping the donor's link during merge)."""
        import pytest

        for bad in (True, 1.5, [99], (1, 2)):
            winner = self._make_dev("eve-ng-02", {"default": {"id": 42}})
            donor = self._make_dev("router-spare", {"default": bad})
            with pytest.raises(ValueError, match="unsupported librenms_id"):
                _merge_into_winner(winner, donor, "default")


@pytest.mark.django_db
class TestMarkMigrated:
    """Tests for mark_migrated()."""

    def test_clears_id_and_oob_and_writes_marker(self):

        donor = _dev({"default": {"id": 99, "oob": {"id": 11, "type": "drac"}}})
        apply_mapping_change(donor, mark_migrated(donor, 42, "default", at="2025-01-01T00:00:00Z"))

        entry = donor.custom_field_data["librenms_id"]["default"]
        assert "id" not in entry
        assert "oob" not in entry
        assert entry["_migrated_to"] == {
            "device_id": 42,
            "server_key": "default",
            "at": "2025-01-01T00:00:00Z",
        }

    def test_default_timestamp_is_iso_z(self):

        donor = _dev({"default": {"id": 99}})
        apply_mapping_change(donor, mark_migrated(donor, 42, "default"))

        ts = donor.custom_field_data["librenms_id"]["default"]["_migrated_to"]["at"]
        # Contract: an ISO-8601 UTC string ending in "Z" (tolerate fractional seconds).
        assert ts.endswith("Z")
        from datetime import datetime

        datetime.fromisoformat(ts.replace("Z", "+00:00"))  # must parse without raising

    def test_rejects_bool_and_non_positive_winner_pk(self):
        import pytest

        for bad in (True, 0, -1):
            donor = _dev({"default": {"id": 99}})
            with pytest.raises(ValueError):
                apply_mapping_change(donor, mark_migrated(donor, bad, "default"))

    def test_fails_closed_on_legacy_or_corrupt_top_level_librenms_id(self):
        """A legacy bare-int/bare-string or corrupt top-level librenms_id must raise, not collapse.

        Collapsing it to {} and stamping the marker would drop the donor's still-resolvable
        mapping (data loss). The donor's value must be left intact so it stays recoverable.
        """
        import pytest

        for legacy in (42, "42", True, [1, 2]):
            donor = _dev(legacy)
            with pytest.raises(ValueError):
                apply_mapping_change(donor, mark_migrated(donor, 99, "default"))
            # Untouched: no marker stamped, original value preserved for the caller to migrate.
            assert donor.custom_field_data["librenms_id"] == legacy

    def test_fails_closed_on_corrupt_per_server_entry(self):
        """A corrupt per-server entry (bool/list/float/unparseable string) must raise, not collapse.

        The top-level guard rejects a corrupt librenms_id, but a per-server value such as
        ``{"default": True}`` / ``{"default": ["bad"]}`` was previously collapsed to ``{}`` and
        stamped migrated — hiding the malformed donor state behind ``_migrated_to``. Mirror the
        per-entry validation from _merge_into_winner() and fail closed instead.
        """
        import pytest

        for corrupt in (True, ["bad"], 3.5, "notanid"):
            donor = _dev({"default": corrupt})
            with pytest.raises(ValueError):
                apply_mapping_change(donor, mark_migrated(donor, 99, "default"))
            # The raise happens before any mutation: no marker stamped, entry untouched.
            assert donor.custom_field_data["librenms_id"] == {"default": corrupt}

    def test_blank_or_numeric_string_per_server_entry_does_not_raise(self):
        """A blank string is "no link" (collapse to {}); a numeric string is a valid id — neither raises."""

        # Blank string → no recoverable link → collapses to {} and stamps the marker (no raise).
        donor = _dev({"default": ""})
        apply_mapping_change(donor, mark_migrated(donor, 99, "default", at="2025-01-01T00:00:00Z"))
        assert donor.custom_field_data["librenms_id"]["default"]["_migrated_to"]["device_id"] == 99

        # Numeric string → a real id → also valid, marker stamped, id cleared.
        donor2 = _dev({"default": "77"})
        apply_mapping_change(donor2, mark_migrated(donor2, 99, "default", at="2025-01-01T00:00:00Z"))
        entry = donor2.custom_field_data["librenms_id"]["default"]
        assert "id" not in entry
        assert entry["_migrated_to"]["device_id"] == 99

    def test_fails_closed_on_corrupt_nested_oob(self):
        """A dict entry with a non-dict oob, or an oob with a non-blank unparseable id, must raise."""
        import pytest

        for corrupt_oob in ("garbage", ["bad"], 7, {"id": "abc"}):
            donor = _dev({"default": {"oob": corrupt_oob}})
            with pytest.raises(ValueError):
                apply_mapping_change(donor, mark_migrated(donor, 99, "default"))
            # The raise happens before any mutation: no marker stamped, oob preserved to migrate first.
            assert donor.custom_field_data["librenms_id"]["default"] == {"oob": corrupt_oob}

    def test_valid_or_blank_nested_oob_does_not_raise(self):
        """A well-formed oob (numeric/blank id, or empty dict) is popped and the marker is stamped."""

        for ok_oob in ({"id": 55}, {"id": "55"}, {"id": ""}, {}):
            donor = _dev({"default": {"oob": ok_oob}})
            apply_mapping_change(donor, mark_migrated(donor, 99, "default", at="2025-01-01T00:00:00Z"))
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

        for corrupt_id in ("abc", 0, True):
            donor = _dev({"default": {"id": corrupt_id}})
            with pytest.raises(ValueError):
                apply_mapping_change(donor, mark_migrated(donor, 99, "default"))
            # The raise happens before any mutation: no marker stamped, id preserved to migrate first.
            assert donor.custom_field_data["librenms_id"]["default"] == {"id": corrupt_id}

    def test_valid_or_blank_dict_host_id_does_not_raise(self):
        """A dict entry with a numeric/blank/absent id is popped and the marker stamped (no raise)."""

        for ok_id in ({"id": 55}, {"id": "55"}, {"id": ""}, {"id": None}, {}):
            donor = _dev({"default": dict(ok_id)})
            apply_mapping_change(donor, mark_migrated(donor, 99, "default", at="2025-01-01T00:00:00Z"))
            entry = donor.custom_field_data["librenms_id"]["default"]
            assert "id" not in entry
            assert entry["_migrated_to"]["device_id"] == 99

    @pytest.mark.django_db
    def test_after_marker_find_by_librenms_id_no_longer_matches(self):
        """A donor whose librenms_id entry holds only the _migrated_to marker must NOT be returned by find_by_librenms_id, queried against the REAL Device model."""

        donor = _dev({"default": {"id": 99}})
        apply_mapping_change(donor, mark_migrated(donor, 99, "default"))
        donor.save()
        donor.refresh_from_db()

        # The entry now holds only _migrated_to — no id, no oob.
        entry = donor.cf["librenms_id"]["default"]
        assert entry.get("id") is None
        assert entry.get("oob") is None

        # The real model query must not return the migrated-only donor for id 99.
        assert _find(99) is None


class TestNormalizeMergeEntry:
    """_normalize_merge_entry: the shared fail-closed shape validation for the merge winner/donor entries."""

    @staticmethod
    def _norm(entry, *, copy=True, owner="winner"):
        from netbox_librenms_plugin.server_mappings import _normalize_merge_entry

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

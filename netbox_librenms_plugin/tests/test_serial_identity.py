"""Hardware serial identity: the rule language, key admission, lookups, grouping, storage and writers."""

import re

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from netbox_librenms_plugin.serial_identity import (
    REFUSING,
    TRIM_CHARACTERS,
    Comparison,
    ResolutionKind,
    SerialRules,
    compare,
    contains_pattern,
    display,
    freshness_token,
    group,
    is_identity,
    naming_token,
    resolve_device,
    resolve_devices,
    resolve_module,
    stored_form,
    stored_form_differs,
)
from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_virtual_chassis,
)

pytestmark = pytest.mark.django_db

SEEDED = r"^S/N\s+(.+)$"


def _rules_only(*rules):
    """Replace every serial rule with *rules*: ``(pattern, replacement, manufacturer)`` tuples."""
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
    return [
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_SERIAL, match_pattern=pattern, replacement=replacement, manufacturer=owner
        )
        for pattern, replacement, owner in rules
    ]


def _manufacturer(tag):
    from dcim.models import Manufacturer

    return Manufacturer.objects.create(name=f"Serial maker {tag}", slug=f"serial-maker-{tag}")


def _device_of(manufacturer, name, serial):
    from dcim.models import DeviceType

    device = make_device(name, serial=serial)
    device.device_type = DeviceType.objects.create(
        manufacturer=manufacturer, model=f"Serial model {name}", slug=f"serial-model-{name}"
    )
    device.save()
    return device


class TestSerialRuleLanguage:
    """A serial rule extracts one capture group of a full match; other scopes keep substitution."""

    @pytest.mark.parametrize(
        ("pattern", "replacement"),
        [
            (SEEDED, r"X\1"),
            (SEEDED, r"\1\1"),
            (SEEDED, "literal"),
            (SEEDED, r"\g<0>"),
            (SEEDED, r"\2"),
            (r"^S/N\s+(?P<serial>.+)$", r"\g<missing>"),
            (SEEDED, r"\1 "),
            (SEEDED, ""),
        ],
    )
    def test_clean_refuses_a_replacement_that_is_not_one_defined_group(self, pattern, replacement):
        from netbox_librenms_plugin.models import NormalizationRule

        with pytest.raises(ValidationError) as refused:
            NormalizationRule(scope="serial", match_pattern=pattern, replacement=replacement).full_clean()

        assert "replacement" in refused.value.message_dict

    @pytest.mark.parametrize(
        ("pattern", "replacement"),
        [(SEEDED, r"\1"), (SEEDED, r"\g<1>"), (r"^S/N\s+(?P<serial>.+)$", r"\g<serial>")],
    )
    def test_clean_accepts_one_group_reference(self, pattern, replacement):
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule(scope="serial", match_pattern=pattern, replacement=replacement).full_clean()

    def test_another_scope_keeps_a_substitution_template(self):
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule(scope="module_type", match_pattern=r"^(.+)-AA$", replacement=r"X\1").full_clean()

    def test_a_group_reference_reads_the_group_and_never_expands_a_template(self):
        r"""With 100 groups, the template \100 is an octal escape; \g<100> reads group 100."""
        from netbox_librenms_plugin.models import NormalizationRule

        pattern = "(.)" * 99 + "([A-Z0-9]+)"
        value = "A" * 99 + "ZZZ123"
        with pytest.raises(ValidationError):
            NormalizationRule(scope="serial", match_pattern=pattern, replacement=r"\100").full_clean()
        assert re.sub(pattern, r"\100", value) == "@"
        _rules_only((pattern, r"\g<100>", None))

        assert naming_token(value, SerialRules.load().context(None)) == "ZZZ123"

    @pytest.mark.parametrize(
        ("pattern", "value"),
        [(r"^(A)?(BCDEF)$", "BCDEF"), (r"^(A)|(BCDEF)$", "BCDEF"), (r"^BCDEF()$", "BCDEF")],
        ids=["optional", "alternation", "empty"],
    )
    def test_a_group_that_takes_no_part_or_is_empty_ends_the_chain_without_an_identity(self, pattern, value):
        _rules_only((pattern, r"\1", None), (r"^(.+)$", r"\1", None))
        context = SerialRules.load().context(None)

        assert not is_identity(value, context)
        assert compare(value, value, context) is Comparison.ABSENT

    def test_a_rule_that_does_not_fullmatch_leaves_the_value(self):
        _rules_only((r"S/N\s+(.+)", r"\1", None))
        context = SerialRules.load().context(None)

        # A search would find "S/N ABC" inside the value; a full match does not.
        assert naming_token("X S/N ABC", context) == "X S/N ABC"
        assert naming_token("S/N ABC", context) == "ABC"

    def test_manufacturer_rules_run_before_the_global_rules(self):
        maker = _manufacturer("order")
        _rules_only((r"^G-(.+)$", r"\1", None), (r"^M-(.+)$", r"\1", maker))
        rules = SerialRules.load()

        assert naming_token("M-G-ABC", rules.context(maker.pk)) == "ABC"
        assert naming_token("M-G-ABC", rules.context(None)) == "M-G-ABC"

    def test_a_stored_rule_that_does_not_conform_fails_every_decision_of_its_chain(self):
        from netbox_librenms_plugin.models import NormalizationRule, RuleConfigurationError

        (rule,) = _rules_only((SEEDED, r"\1", None))
        # A QuerySet.update() skips clean(), so the stored rule can still be non-conforming.
        NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"X\1")
        make_device("non-conforming-rule-holder", serial="ABC-123")

        with pytest.raises(RuleConfigurationError) as broken:
            resolve_device("ABC-123")
        assert broken.value.rule_pk == rule.pk
        with pytest.raises(RuleConfigurationError):
            SerialRules.load().context(None)

    def test_a_serial_rule_is_never_applied_as_a_substitution(self):
        from netbox_librenms_plugin.models import NormalizationRule

        (rule,) = _rules_only((SEEDED, r"\1", None))

        with pytest.raises(TypeError):
            NormalizationRule.objects.get(pk=rule.pk).apply("S/N ABC")


class TestKeyAdmission:
    """A placeholder, before or after the rules, and a key without three letters or digits are no identity."""

    @pytest.mark.parametrize(
        ("value", "identity"),
        [
            ("A++", False),
            ("S/N-12", False),
            ("AB-1", False),
            ("12", False),
            (0, False),
            ("ABC", True),
            ("ABC++", True),
            ("abc", True),
            ("A1B2C3", True),
            ("123", True),
            (12345, True),
            ("", False),
            ("  ", False),
            (None, False),
            ("n/a", False),
            (" Unknown ", False),
            ("S/N unknown", False),
            ("S/N ABC", True),
            ("S/N AB", False),
        ],
    )
    def test_the_admission_rule(self, value, identity):
        _rules_only((SEEDED, r"\1", None))

        assert is_identity(value, SerialRules.load().context(None)) is identity

    @pytest.mark.parametrize("stored", ["unknown", "AB", "S/N N/A", "1"])
    def test_a_value_without_an_identity_binds_no_device_and_costs_no_lookup(self, stored):
        _rules_only((SEEDED, r"\1", None))
        make_device(f"no-identity-{len(stored)}-{stored.replace('/', '')}", serial=stored)
        rules = SerialRules.load()
        rules.candidates()

        with CaptureQueriesContext(connection) as queries:
            assert resolve_device(stored, rules=rules).kind is ResolutionKind.ABSENT
        assert len(queries) == 0

    def test_a_module_placeholder_binds_no_module(self):
        device = make_device_with_module_bays("module-placeholder", ["Slot 1"])
        install_module(device, "Slot 1", "PLACEHOLDER-CARD", serial="BUILTIN")

        assert resolve_module("BUILTIN").kind is ResolutionKind.ABSENT

    def test_a_rule_whose_output_is_a_placeholder_leaves_no_identity(self):
        _rules_only((r"^VENDOR-(N/A)$", r"\1", None))
        make_device("rule-output-placeholder", serial="VENDOR-N/A")

        assert resolve_device("VENDOR-N/A").kind is ResolutionKind.ABSENT

    def test_every_admitted_contains_pattern_holds_a_three_character_run(self):
        """The admission rule guarantees a trigram: assert the run in the escaped pattern, not with show_trgm()."""
        for key in ["ABC", "ABC++", "S/N-ABC", "a%b_c\\ABC", "1-2-345"]:
            pattern = contains_pattern(key)
            assert re.search(r"[A-Za-z0-9]{3}", pattern), pattern


class TestLookup:
    """One query finds every row whose key equals the incoming key under the row's own context."""

    @pytest.mark.parametrize(("stored", "incoming"), [("S/N ABC-LOOK", "ABC-LOOK"), ("ABC-LOOK", "S/N ABC-LOOK")])
    def test_a_decorated_serial_and_its_bare_form_find_each_other(self, stored, incoming):
        _rules_only((SEEDED, r"\1", None))
        device = make_device("lookup-both-ways", serial=stored)

        found = resolve_device(incoming)

        assert found.kind is ResolutionKind.UNIQUE
        assert found.object == device

    def test_a_manufacturer_rule_confirms_only_rows_of_that_manufacturer(self):
        scoped = _manufacturer("scoped")
        other = _manufacturer("other")
        _rules_only((r"^X(.+)$", r"\1", scoped))
        with_rule = _device_of(scoped, "lookup-scoped", "XABC-1")
        _device_of(other, "lookup-unscoped", "XDEF-1")

        assert resolve_device("ABC-1").object == with_rule
        assert resolve_device("DEF-1").kind is ResolutionKind.NO_MATCH

    def test_a_chassis_member_uses_the_rules_of_its_masters_manufacturer(self):
        master_maker = _manufacturer("master")
        member_maker = _manufacturer("member")
        _rules_only((r"^X(.+)$", r"\1", master_maker))
        master = _device_of(master_maker, "lookup-master", "MASTER-SN1")
        member = _device_of(member_maker, "lookup-member", "XMEMBER-SN2")
        chassis = make_virtual_chassis("lookup-chassis", master, member)
        chassis.master = master
        chassis.save()

        assert resolve_device("MEMBER-SN2").object == member

    def test_a_master_outside_its_chassis_makes_a_probed_match_unavailable(self):
        from dcim.models import Device, VirtualChassis

        master = make_device("lookup-stray-master", serial="STRAY-SN1")
        member = make_device("lookup-stray-member", serial="STRAY-SN2")
        chassis = make_virtual_chassis("lookup-stray-chassis", member)
        other = VirtualChassis.objects.create(name="lookup-stray-other")
        Device.objects.filter(pk=master.pk).update(virtual_chassis=other, vc_position=1)
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)

        found = resolve_device("STRAY-SN2")

        assert found.kind is ResolutionKind.UNAVAILABLE
        assert found.object is None

    @pytest.mark.parametrize(("stored", "incoming"), [("ABCXDEF", "ABC_DEF"), ("ABC-DEF", "ABC%DEF")])
    def test_like_wildcards_in_a_key_stay_literal(self, stored, incoming):
        make_device(f"lookup-wildcard-{incoming.replace('%', 'p')}", serial=stored)

        assert resolve_device(incoming).kind is ResolutionKind.NO_MATCH

    def test_a_backslash_in_a_serial_matches_literally(self):
        device = make_device("lookup-backslash", serial="ABC\\DEF")

        assert resolve_device("ABC\\DEF").object == device
        assert resolve_device("ABCXDEF").kind is ResolutionKind.NO_MATCH

    def test_unicode_padding_that_the_index_keeps_is_still_found(self):
        device = make_device("lookup-nbsp", serial=" NBSP-SERIAL ")

        assert resolve_device("NBSP-SERIAL").object == device

    def test_an_external_queryset_update_is_seen_by_the_next_lookup(self):
        from dcim.models import Device

        _rules_only((SEEDED, r"\1", None))
        device = make_device("lookup-external-edit", serial="OLD-EXTERNAL")
        Device.objects.filter(pk=device.pk).update(serial="S/N NEW-EXTERNAL")

        assert resolve_device("NEW-EXTERNAL").object == device
        assert resolve_device("OLD-EXTERNAL").kind is ResolutionKind.NO_MATCH

    def test_one_query_reads_the_candidates_of_a_whole_batch(self):
        _rules_only((SEEDED, r"\1", None))
        serials = [f"BATCH-SN{number:03}" for number in range(20)]
        for number, serial in enumerate(serials[:10]):
            make_device(f"lookup-batch-{number}", serial=f"S/N {serial}")
        rules = SerialRules.load()
        rules.candidates()

        with CaptureQueriesContext(connection) as queries:
            found = resolve_devices(serials, rules=rules)

        assert len(queries) == 1
        assert [found[serial].kind for serial in serials] == [ResolutionKind.UNIQUE] * 10 + [
            ResolutionKind.NO_MATCH
        ] * 10

    def test_a_module_lookup_counts_only_the_devices_it_names(self):
        first = make_device_with_module_bays("module-scope-a", ["Slot 1"])
        second = make_device_with_module_bays("module-scope-b", ["Slot 1"])
        on_first = install_module(first, "Slot 1", "SCOPE-CARD", serial="SCOPE-SN1")
        install_module(second, "Slot 1", "SCOPE-CARD", serial="S/N SCOPE-SN1")
        _rules_only((SEEDED, r"\1", None))

        assert resolve_module("SCOPE-SN1", device_ids=[first.pk]).object == on_first
        assert resolve_module("SCOPE-SN1").kind is ResolutionKind.AMBIGUOUS

    def test_the_query_and_every_trigram_index_use_one_trimmed_expression(self):
        """The ORM sends the expression that the indexes store; the catalog is read, never a query plan."""
        expected = f"btrim((serial)::text, '{TRIM_CHARACTERS}'::text)"
        sent = []

        def capture(execute, sql, params, many, context):
            sent.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            resolve_device("EXPRESSION-SN1")
        (lookup,) = [sql for sql in sent if "LIKE ANY" in sql]
        trim_literal = "'" + TRIM_CHARACTERS + "'"
        assert lookup.count(f'BTRIM("dcim_device"."serial", {trim_literal})') == 2

        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL quote_all_identifiers = off")
            cursor.execute(
                """
                SELECT index_class.relname, access_method.amname, operator_class.opcname,
                       pg_get_expr(index_state.indexprs, index_state.indrelid), index_state.indisvalid
                FROM pg_index AS index_state
                JOIN pg_class AS index_class ON index_class.oid = index_state.indexrelid
                JOIN pg_am AS access_method ON access_method.oid = index_class.relam
                JOIN pg_opclass AS operator_class ON operator_class.oid = index_state.indclass[0]
                WHERE index_class.relname = ANY(%s)
                ORDER BY index_class.relname
                """,
                [
                    [
                        "nblp_dcim_device_serial_trgm_idx",
                        "nblp_dcim_module_serial_trgm_idx",
                        "nblp_dcim_device_serial_trim_idx",
                    ]
                ],
            )
            indexes = cursor.fetchall()

        assert indexes == [
            ("nblp_dcim_device_serial_trgm_idx", "gin", "gin_trgm_ops", expected, True),
            ("nblp_dcim_device_serial_trim_idx", "btree", "text_ops", expected, True),
            ("nblp_dcim_module_serial_trgm_idx", "gin", "gin_trgm_ops", expected, True),
        ]

    def test_the_migration_trims_the_characters_that_the_owner_trims(self):
        import importlib

        module = importlib.import_module("netbox_librenms_plugin.migrations.0030_serial_trigram_indexes")

        assert module._SERIAL_TRIM_CHARACTERS == TRIM_CHARACTERS


class TestAmbiguity:
    """Two or more equivalent rows are Ambiguous, also when the user cannot see one of them."""

    def test_two_forms_of_one_serial_are_ambiguous_from_either_side(self):
        _rules_only((SEEDED, r"\1", None))
        decorated = make_device("ambiguous-decorated", serial="S/N TWIN-SN1")
        bare = make_device("ambiguous-bare", serial="TWIN-SN1")

        for incoming in ("TWIN-SN1", "S/N TWIN-SN1"):
            found = resolve_device(incoming)
            assert found.kind is ResolutionKind.AMBIGUOUS
            assert found.object is None
            assert {device.pk for device in found.matches} == {decorated.pk, bare.pk}

    def test_excluding_one_of_two_rows_leaves_a_unique_match(self):
        first = make_device("ambiguous-exclude-a", serial="EXCL-SN1")
        second = make_device("ambiguous-exclude-b", serial=" EXCL-SN1 ")

        assert resolve_device("EXCL-SN1", exclude_pks=[first.pk]).object == second

    def test_a_read_that_stops_at_two_refuses_an_exclusion_it_cannot_count(self):
        from netbox_librenms_plugin.serial_identity import resolve_modules
        from netbox_librenms_plugin.tests.conftest import install_module, make_device_with_module_bays

        holders = [
            install_module(
                make_device_with_module_bays(f"spare-{tag}", ["Slot 1"]), "Slot 1", "SPARE-CARD", serial="SPARE-SN1"
            )
            for tag in "abc"
        ]

        with pytest.raises(RuntimeError):
            resolve_modules(["SPARE-SN1"])["SPARE-SN1"].excluding([holders[0].pk])
        found = resolve_modules(["SPARE-SN1"], spare=1)["SPARE-SN1"].excluding([holders[0].pk])
        assert found.kind is ResolutionKind.AMBIGUOUS

    def test_import_validation_blocks_a_duplicate_that_the_user_cannot_see(self):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.import_utils.disclosure import scope_validation_disclosures
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        _rules_only((SEEDED, r"\1", None))
        visible = make_device("hidden-dup-visible", serial="S/N HIDDEN-SN1")
        make_device("hidden-dup-hidden", serial="HIDDEN-SN1")
        user = grant(make_user_with_perms("hidden-dup-user", []), "view", Device, constraints={"pk": visible.pk})
        validation = validate_device_for_import(
            {"device_id": 7701, "hostname": "hidden-dup-row", "sysName": "hidden-dup-row", "serial": "HIDDEN-SN1"},
            include_vc_detection=False,
        )

        scope_validation_disclosures([validation], user)

        assert validation["existing_device"] is None
        assert validation["serial_duplicate"] is True
        assert validation["can_import"] is False


class TestGroupingAndPresentation:
    def test_a_group_of_two_equivalent_observations_is_kept_whole(self):
        _rules_only((SEEDED, r"\1", None))
        groups = group(["S/N GRP-SN1", "GRP-SN1", "GRP-SN2", "n/a"], SerialRules.load().context(None))

        assert sorted(len(items) for items in groups) == [1, 2]
        assert groups.find("GRP-SN1") == ("S/N GRP-SN1", "GRP-SN1")
        assert groups.absent == ["n/a"]
        assert groups.find("unknown") == ()

    def test_the_stored_form_is_the_trimmed_text_and_refuses_an_overlong_serial(self):
        from dcim.models import Device, Module

        assert stored_form("  S/N RAW-SN1 ", Device) == "S/N RAW-SN1"
        assert stored_form(" n/a ", Module) == ""
        assert stored_form(0, Device) == "0"
        with pytest.raises(ValidationError) as refused:
            stored_form("S" * 51, Device)
        assert "serial" in refused.value.message_dict

    def test_a_legacy_stored_form_is_same_hardware_in_another_form(self):
        _rules_only((SEEDED, r"\1", None))
        context = SerialRules.load().context(None)

        assert stored_form_differs("LEGACY-SN1", "S/N LEGACY-SN1", context)
        assert not stored_form_differs("S/N LEGACY-SN1", " S/N LEGACY-SN1 ", context)
        assert not stored_form_differs("OTHER-SN1", "S/N LEGACY-SN1", context)
        assert display(" S/N LEGACY-SN1 ") == "S/N LEGACY-SN1"

    def test_a_rule_edit_changes_the_freshness_token(self):
        (rule,) = _rules_only((SEEDED, r"\1", None))
        before = freshness_token("S/N FRESH-SN1", SerialRules.load().context(None))
        rule.match_pattern = r"^S/N:?\s+(.+)$"
        rule.save()

        assert freshness_token("S/N FRESH-SN1", SerialRules.load().context(None)) != before
        assert freshness_token("S/N FRESH-SN2", SerialRules.load().context(None)) != freshness_token(
            "S/N FRESH-SN1", SerialRules.load().context(None)
        )


class TestWriters:
    """Every refusing outcome blocks a write; an absent incoming serial never clears a stored one."""

    def test_the_refusing_outcomes(self):
        assert REFUSING == {ResolutionKind.UNIQUE, ResolutionKind.AMBIGUOUS, ResolutionKind.UNAVAILABLE}

    def test_a_member_of_a_new_chassis_is_skipped_when_a_device_holds_its_decorated_serial(self):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        _rules_only((SEEDED, r"\1", None))
        existing = make_device("member-dedupe-existing", serial="S/N MEMBER-DEDUPE")
        master = make_device("member-dedupe-master", serial="MASTER-DEDUPE")
        members = [{"serial": "MASTER-DEDUPE", "position": 1}, {"serial": "MEMBER-DEDUPE", "position": 2}]

        chassis = create_virtual_chassis_with_members(master, members, {"device_id": 7702}, master_member=members[0])

        assert list(chassis.members.values_list("pk", flat=True)) == [master.pk]
        assert list(Device.objects.filter(serial__contains="MEMBER-DEDUPE").values_list("pk", flat=True)) == [
            existing.pk
        ]

    def test_a_chassis_master_type_change_checks_the_serial_of_every_member(self):
        """A member keeps the master's context: a new master type decides the member serials too."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.serial_identity import retype_conflict, retype_scope
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

        old_maker, new_maker = _manufacturer("retype-old"), _manufacturer("retype-new")
        _rules_only((SEEDED, r"\1", None), (r"^X(AB)C$", r"\1", old_maker))
        master = _device_of(old_maker, "retype-master", "MASTER-SN1")
        member = make_device("retype-member", serial="XABC")
        chassis = make_virtual_chassis("retype-chassis", master, member)
        VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
        master.refresh_from_db()
        _device_of(new_maker, "retype-holder", "XABC")
        same_rules_type = _device_of(old_maker, "retype-same-maker", "OTHER-SN1").device_type
        new_type = _device_of(new_maker, "retype-new-type", "OTHER-SN2").device_type
        rules = SerialRules.load()

        scope = retype_scope(master)

        assert sorted(serial for _pk, serial in scope.devices) == ["MASTER-SN1", "XABC"]
        assert retype_conflict(master, new_type, rules=rules, scope=scope) is True
        assert retype_conflict(master, same_rules_type, rules=rules, scope=scope) is False
        assert retype_scope(member).devices == ()

    def test_a_member_is_skipped_when_another_device_holds_its_key_under_the_master_rules(self):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_maker = _manufacturer("key-member")
        _rules_only((SEEDED, r"\1", None), (r"^X(.+)$", r"\1", master_maker))
        master = _device_of(master_maker, "key-member-master", "MASTER-KEY1")
        make_device("key-member-holder", serial="ABC")
        members = [{"serial": "MASTER-KEY1", "position": 1}, {"serial": "XABC", "position": 2}]

        chassis = create_virtual_chassis_with_members(master, members, {"device_id": 7808}, master_member=members[0])

        assert list(chassis.members.values_list("pk", flat=True)) == [master.pk]
        assert not Device.objects.filter(serial="XABC").exists()

    def test_a_chassis_is_not_created_when_the_master_type_changed_after_the_master_decision(self):
        """Under maker A the root "ABC" is FPC "XABC"; a type change to maker B, which has no rule, leaves no master."""
        from dcim.models import Device, VirtualChassis

        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            create_virtual_chassis_with_members,
            identify_vc_master,
        )

        maker_a, maker_b = _manufacturer("master-decision-a"), _manufacturer("master-decision-b")
        _rules_only((SEEDED, r"\1", None), (r"^X(.+)$", r"\1", maker_a))
        master = _device_of(maker_a, "master-decision", "ABC")
        members = [
            {"serial": "XABC", "position": 0, "root_serial": "ABC"},
            {"serial": "XDEF", "position": 1, "root_serial": "ABC"},
        ]
        master_member = identify_vc_master(members, master.serial, SerialRules.load().for_device(master))
        assert master_member is members[0]
        Device.objects.filter(pk=master.pk).update(
            device_type=_device_of(maker_b, "master-decision-b", "B-SN1").device_type
        )

        with pytest.raises(ValueError):
            create_virtual_chassis_with_members(master, members, {"device_id": 7809}, master_member=master_member)

        assert not VirtualChassis.objects.filter(members__pk=master.pk).exists()
        assert not Device.objects.filter(serial="XDEF").exists()

    def test_an_import_refuses_a_serial_that_a_device_committed_after_the_validation(self, settings):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server

        configure_default_librenms_server(settings)
        _rules_only((SEEDED, r"\1", None))
        libre_device = {
            "device_id": 7703,
            "hostname": "import-race-new",
            "sysName": "import-race-new",
            "serial": "IMPORT-RACE",
        }
        validation = validate_device_for_import(libre_device, include_vc_detection=False)
        assert validation["existing_device"] is None
        # Another import commits the same hardware in its decorated form after this validation.
        holder = make_device("import-race-holder", serial="S/N IMPORT-RACE")

        result = import_single_device(
            7703,
            server_key="default",
            validation=validation,
            manual_mappings={
                "site_id": holder.site_id,
                "device_type_id": holder.device_type_id,
                "device_role_id": holder.role_id,
            },
            libre_device=libre_device,
        )

        assert result["success"] is False
        assert "IMPORT-RACE" in result["error"]
        assert "import-race-holder" not in result["error"]


@pytest.fixture
def serial_librenms(settings, monkeypatch):
    """A real loopback LibreNMS under the server key ``default``."""
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers
    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        configure_librenms_servers(
            settings, {"default": {"librenms_url": server.url, "api_token": "serial-token", "cache_timeout": 0}}
        )
        yield server


def _validate(payload):
    from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

    return validate_device_for_import(payload, api=None, server_key="default", include_vc_detection=False)


class TestEquivalentFormsAcrossDecisions:
    """S1: a stored "S/N X" and an incoming "X" name one device in every decision that looks a serial up."""

    def test_the_drift_check_of_a_linked_device_names_the_equivalent_holder(self):
        _rules_only((SEEDED, r"\1", None))
        make_device("drift-linked", serial="OLD-DRIFT-SN", librenms_cf={"default": {"id": 7801}})
        holder = make_device("drift-holder", serial="S/N DRIFT-SN1")

        result = _validate({"device_id": 7801, "hostname": "drift-linked", "serial": "DRIFT-SN1"})

        assert result["serial_action"] == "conflict"
        assert result["serial_conflict"]["pk"] == holder.pk

    def test_the_merge_peer_is_found_in_its_other_form(self):
        _rules_only((SEEDED, r"\1", None))
        host = make_device("merge-forms-host", serial="MERGE-SN1", librenms_cf={"default": {"id": 42}})
        oob = make_device("merge-forms-oob", serial="S/N MERGE-SN1", librenms_cf={"default": {"id": 99}})

        result = _validate({"device_id": 7804, "hostname": host.name, "sysName": host.name, "serial": "MERGE-SN1"})

        assert result["serial_action"] == "merge_netbox_devices"
        assert result["merge_candidates"]["oob_named"]["pk"] == oob.pk

    def test_the_cached_refresh_binds_the_device_that_stores_the_decorated_form(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        _rules_only((SEEDED, r"\1", None))
        device = make_device("refresh-forms", serial="S/N REFRESH-SN1")
        validation = {"existing_device": None, "import_as_vm": False, "can_import": True, "issues": []}

        _refresh_existing_device(
            validation, libre_device={"device_id": 7805, "hostname": "unmatched-refresh", "serial": "REFRESH-SN1"}
        )

        assert validation["existing_device"] == device
        assert validation["existing_match_type"] == "serial"

    def test_a_serial_update_refuses_an_equivalent_serial_that_another_device_holds(self, client, serial_librenms):
        from dcim.models import Device
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

        _rules_only((SEEDED, r"\1", None))
        target = seed_mapping(make_device("assign-forms-target", serial="ASSIGN-OLD1"), "default", own=7802)
        make_device("assign-forms-holder", serial="S/N ASSIGN-SN1")
        serial_librenms.device_info_response(device_id=7802, hostname=target.name, serial="ASSIGN-SN1")
        client.force_login(make_superuser("assign-forms-user"))

        client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": target.pk}),
            {"server_key": "default"},
        )

        assert Device.objects.get(pk=target.pk).serial == "ASSIGN-OLD1"

    def test_a_serial_update_refuses_a_serial_whose_key_under_the_target_rules_another_device_holds(
        self, client, serial_librenms
    ):
        """The target's maker keys "XABC" to "ABC", which another maker's device stores plainly."""
        from dcim.models import Device
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

        target_maker = _manufacturer("key-write-target")
        _rules_only((SEEDED, r"\1", None), (r"^X(.+)$", r"\1", target_maker))
        target = seed_mapping(_device_of(target_maker, "key-write-target", ""), "default", own=7806)
        make_device("key-write-holder", serial="ABC")
        serial_librenms.device_info_response(device_id=7806, hostname=target.name, serial="XABC")
        client.force_login(make_superuser("key-write-user"))

        client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": target.pk}),
            {"server_key": "default"},
        )

        assert Device.objects.get(pk=target.pk).serial == ""

    def test_an_import_refuses_a_serial_whose_key_under_the_new_type_rules_another_device_holds(self, settings):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        configure_default_librenms_server(settings)
        target_maker = _manufacturer("key-import")
        _rules_only((SEEDED, r"\1", None), (r"^X(.+)$", r"\1", target_maker))
        new_type = _device_of(target_maker, "key-import-type-donor", "DONOR-SN1").device_type
        holder = make_device("key-import-holder", serial="ABC")
        libre_device = {"device_id": 7807, "hostname": "key-import-new", "sysName": "key-import-new", "serial": "XABC"}

        validation = validate_device_for_import(libre_device, include_vc_detection=False)
        assert validation["existing_device"] is None

        result = import_single_device(
            7807,
            server_key="default",
            validation=validation,
            manual_mappings={
                "site_id": holder.site_id,
                "device_type_id": new_type.pk,
                "device_role_id": holder.role_id,
            },
            libre_device=libre_device,
        )

        assert result["success"] is False
        assert "key-import-holder" not in result["error"]

    def test_a_serial_update_heals_a_legacy_stored_form(self, client, serial_librenms):
        from dcim.models import Device
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

        _rules_only((SEEDED, r"\1", None))
        target = seed_mapping(make_device("heal-forms-target", serial="HEAL-SN1"), "default", own=7803)
        serial_librenms.device_info_response(device_id=7803, hostname=target.name, serial="S/N HEAL-SN1")
        client.force_login(make_superuser("heal-forms-user"))

        client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": target.pk}),
            {"server_key": "default"},
        )

        assert Device.objects.get(pk=target.pk).serial == "S/N HEAL-SN1"


_REPLACED_HELPERS = frozenset(
    {
        "normalize_serial",
        "normalize_stack_serial",
        "normalize_inventory_serial",
        "vc_serial_key",
        "serials_match",
        "chassis_serial_key",
        "devices_with_serial",
        "find_devices_by_serial",
        "candidate_vc_serial_keys",
        "members_by_serial_key",
        "serial_rule_sets",
    }
)


@pytest.fixture
def caching_librenms(settings, monkeypatch, request):
    """A real loopback LibreNMS whose search results the plugin caches, under a server key of this test only."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers
    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server_key = re.sub(r"[^a-z0-9]+", "-", f"serial-cache-{request.node.name}".lower())
    with librenms_mock_server() as server:
        configure_librenms_servers(
            settings, {server_key: {"librenms_url": server.url, "api_token": "cache-token", "cache_timeout": 300}}
        )
        yield server, LibreNMSAPI(server_key=server_key)


def _cached_search(caching_librenms, serial):
    """Search LibreNMS for one device through the import search, which caches its validation."""
    from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

    server, api = caching_librenms
    row = {"device_id": 7810, "hostname": "cache-incoming", "sysName": "cache-incoming", "serial": serial}
    server.register("/api/v0/devices", {"status": "ok", "devices": [{**row, "disabled": 0, "hardware": "", "os": ""}]})
    server.vc_inventory_callable(7810, [], {})
    (found,) = process_device_filters(
        api, {"hostname": "cache-incoming"}, vc_detection_enabled=False, clear_cache=False, show_disabled=True
    )
    return found["_validation"]


class TestCachedSearchResults:
    """S5: a cached search result shows the serial match that the current rules and rows give."""

    def test_a_rule_deletion_drops_a_cached_serial_match(self, caching_librenms):
        _rules_only((SEEDED, r"\1", None))
        holder = make_device("cache-rule-holder", serial="S/N CACHE-SN1")
        assert _cached_search(caching_librenms, "CACHE-SN1")["existing_device"] == holder

        _rules_only()

        assert _cached_search(caching_librenms, "CACHE-SN1")["existing_device"] is None

    def test_a_rule_deletion_recomputes_the_serial_drift_of_a_cached_hostname_match(self, caching_librenms):
        _rules_only((SEEDED, r"\1", None))
        make_device("cache-incoming", serial="S/N CACHE-SN3")
        assert _cached_search(caching_librenms, "CACHE-SN3")["serial_action"] is None

        _rules_only()

        assert _cached_search(caching_librenms, "CACHE-SN3")["serial_action"] == "update_serial"

    @pytest.mark.parametrize("change", ["equivalent-duplicate", "holder-serial-changed"])
    def test_a_row_change_drops_a_cached_serial_match(self, caching_librenms, change):
        from dcim.models import Device

        _rules_only((SEEDED, r"\1", None))
        holder = make_device(f"cache-row-holder-{change}", serial="S/N CACHE-SN2")
        assert _cached_search(caching_librenms, "CACHE-SN2")["existing_device"] == holder

        if change == "equivalent-duplicate":
            make_device(f"cache-row-twin-{change}", serial="CACHE-SN2")
        else:
            Device.objects.filter(pk=holder.pk).update(serial="REPLACED-SN9")
        validation = _cached_search(caching_librenms, "CACHE-SN2")

        assert validation["existing_device"] is None
        if change == "equivalent-duplicate":
            assert validation["can_import"] is False


class TestCompletedSearchJobResults:
    """S5: a completed background search shows the serial match that the current rows give."""

    @pytest.mark.parametrize("change", ["equivalent-duplicate", "holder-serial-changed"])
    def test_a_row_change_drops_the_serial_match_of_a_completed_job(self, caching_librenms, change):
        from uuid import uuid4

        from core.models import Job
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.imports.list import LibreNMSImportView

        _rules_only((SEEDED, r"\1", None))
        holder = make_device(f"job-holder-{change}", serial="S/N JOB-SN1")
        assert _cached_search(caching_librenms, "JOB-SN1")["existing_device"] == holder
        user = make_superuser(f"job-serial-user-{change}")
        job = Job.objects.create(
            name="Completed import search",
            user=user,
            job_id=uuid4(),
            status="completed",
            data={
                "device_ids": [7810],
                "filters": {"hostname": "cache-incoming"},
                "server_key": caching_librenms[1].server_key,
                "vc_detection_enabled": False,
            },
        )
        if change == "equivalent-duplicate":
            make_device(f"job-twin-{change}", serial="JOB-SN1")
        else:
            Device.objects.filter(pk=holder.pk).update(serial="REPLACED-SN9")

        (row,) = LibreNMSImportView()._load_job_results(job.pk, user)

        assert row["_validation"]["existing_device"] is None


def _serial_bypasses(source):
    """Return ``(line, form)`` for each replaced-helper reference and each lookup on the serial column."""
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            found += [(node.lineno, alias.name) for alias in node.names if alias.name in _REPLACED_HELPERS]
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in _REPLACED_HELPERS:
                found.append((node.lineno, name))
        if isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if callee in {"filter", "exclude", "get", "Q"}:
                found += [
                    (node.lineno, keyword.arg)
                    for keyword in node.keywords
                    if keyword.arg and (keyword.arg == "serial" or keyword.arg.startswith("serial__"))
                ]
    return found


def test_the_guard_scan_finds_each_bypass_form():
    """The control: without it, a scan that finds nothing would pass the package check by accident."""
    source = (
        "from netbox_librenms_plugin.utils import normalize_serial\n"
        "virtual_chassis.vc_serial_key()\n"
        "Device.objects.filter(serial=value)\n"
        "Module.objects.exclude(device=device, serial__in=values)\n"
        "Q(serial__iexact=value)\n"
        "Module(device=device, serial=value)\n"
    )

    assert sorted(line for line, _form in _serial_bypasses(source)) == [1, 2, 3, 4, 5]


def test_no_module_outside_the_owner_uses_a_replaced_helper_or_the_serial_column():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1]
    offenders = {
        str(path.relative_to(package)): found
        for path in package.rglob("*.py")
        if not {"tests", "migrations"} & set(path.relative_to(package).parts)
        and path.name != "serial_identity.py"
        and (found := _serial_bypasses(path.read_text()))
    }

    assert offenders == {}

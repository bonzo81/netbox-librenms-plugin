"""Interface rules: the InterfaceTypeMapping model, its constraints, and the one matcher that reads it."""

import importlib
from itertools import combinations

import pytest
from dcim.models import Platform
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory

from netbox_librenms_plugin.interface_rules import (
    InterfaceRuleMatcher,
    RuleConfigurationError,
    RuleDecisionKind,
    interface_rules_for_request,
)
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import convert_speed_to_kbps, validate_regex_field

pytestmark = pytest.mark.django_db

SET = InterfaceTypeMapping.ACTION_SET_TYPE
IGNORE = InterfaceTypeMapping.ACTION_IGNORE
RULE_EXISTS = "A rule with the same platform, LibreNMS type, name pattern and speed already exists."


def _platform(slug):
    return Platform.objects.create(name=slug.upper(), slug=slug)


def _rule(**fields):
    return InterfaceTypeMapping.objects.create(**fields)


def _port(name="Te1/1", descr=None, if_type="ethernetCsmacd", speed_bps=10_000_000_000):
    return {"ifName": name, "ifDescr": name if descr is None else descr, "ifType": if_type, "ifSpeed": speed_bps}


def _decide(port, platform=None):
    return InterfaceRuleMatcher.load().decide(port, platform_id=platform.pk if platform else None)


def _pks(decision):
    return [rule.pk for rule in decision.rules]


# ---------------------------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------------------------


class TestRuleValidation:
    def test_an_existing_style_rule_saves_as_a_global_set_type_rule(self):
        rule = _rule(librenms_type="ethernetCsmacd", librenms_speed=1_000_000, netbox_type="1000base-t")
        rule.refresh_from_db()
        assert (rule.action, rule.platform_id, rule.name_pattern) == (SET, None, "")

    def test_the_type_is_stripped_and_the_pattern_is_kept_verbatim(self):
        rule = _rule(librenms_type="  ethernetCsmacd\n", name_pattern=" ^Te ", netbox_type="10gbase-x-sfpp")
        rule.refresh_from_db()
        assert rule.librenms_type == "ethernetCsmacd"
        assert rule.name_pattern == " ^Te "

    def test_a_pattern_that_does_not_compile_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _rule(name_pattern="(unclosed", netbox_type="other")
        assert "Invalid regex" in exc.value.message_dict["name_pattern"][0]
        assert not InterfaceTypeMapping.objects.filter(name_pattern="(unclosed").exists()

    def test_a_pattern_with_a_repeat_count_too_large_to_compile_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _rule(name_pattern="a{4294967295}", netbox_type="other")
        assert "Invalid regex" in exc.value.message_dict["name_pattern"][0]

    @pytest.mark.parametrize("pattern", ["(unclosed", "a{4294967295}"])
    def test_the_shared_regex_validator_turns_every_compile_failure_into_a_field_error(self, pattern):
        with pytest.raises(ValidationError) as exc:
            validate_regex_field(pattern, "some_pattern")
        assert list(exc.value.message_dict) == ["some_pattern"]

    def test_a_rule_needs_at_least_one_selector(self):
        with pytest.raises(ValidationError) as exc:
            _rule(netbox_type="other")
        assert "Give at least one of platform, LibreNMS type or name pattern." in exc.value.messages

    @pytest.mark.parametrize("netbox_type", [None, ""])
    def test_set_type_needs_a_netbox_type(self, netbox_type):
        with pytest.raises(ValidationError) as exc:
            _rule(action=SET, name_pattern="^Te", netbox_type=netbox_type)
        assert exc.value.message_dict["netbox_type"] == ["A Set type rule needs a NetBox type."]

    def test_ignore_rejects_a_netbox_type(self):
        with pytest.raises(ValidationError) as exc:
            _rule(action=IGNORE, name_pattern="^Vlan", netbox_type="virtual")
        assert "netbox_type" in exc.value.message_dict

    def test_ignore_stores_a_blank_type_as_null(self):
        rule = _rule(action=IGNORE, name_pattern="^Vlan", netbox_type="")
        rule.refresh_from_db()
        assert rule.netbox_type is None

    def test_ignore_rejects_a_speed(self):
        with pytest.raises(ValidationError) as exc:
            _rule(action=IGNORE, librenms_type="propVirtual", librenms_speed=1000)
        assert exc.value.message_dict["librenms_speed"] == ["An Ignore rule takes no speed."]

    def test_a_speed_needs_a_type(self):
        with pytest.raises(ValidationError) as exc:
            _rule(name_pattern="^Te", librenms_speed=1000, netbox_type="other")
        assert exc.value.message_dict["librenms_speed"] == ["A speed needs a LibreNMS type."]

    def test_deleting_the_platform_deletes_its_rules(self):
        platform = _platform("ios")
        rule = _rule(action=IGNORE, platform=platform, name_pattern="^Vlan")
        platform.delete()
        assert not InterfaceTypeMapping.objects.filter(pk=rule.pk).exists()


# The four partitions of the (platform, type, pattern, speed) key: platform null/set x speed null/set.
_PARTITIONS = [(False, False), (False, True), (True, False), (True, True)]


class TestRuleUniqueness:
    @pytest.mark.parametrize(("scoped", "with_speed"), _PARTITIONS)
    def test_a_second_rule_with_the_same_selectors_is_rejected_whatever_its_action(self, scoped, with_speed):
        platform = _platform("ios") if scoped else None
        speed = 1_000_000 if with_speed else None
        _rule(
            platform=platform,
            librenms_type="ethernetCsmacd",
            name_pattern="^Te",
            librenms_speed=speed,
            netbox_type="other",
        )
        duplicate = {"platform": platform, "librenms_type": "ethernetCsmacd", "name_pattern": "^Te"}
        if with_speed:
            duplicate.update(librenms_speed=speed, netbox_type="1000base-t")
        else:
            duplicate["action"] = IGNORE
        with pytest.raises(ValidationError) as exc:
            _rule(**duplicate)
        assert RULE_EXISTS in exc.value.messages

    @pytest.mark.parametrize(("scoped", "with_speed"), _PARTITIONS)
    def test_the_database_rejects_a_duplicate_that_skips_validation(self, scoped, with_speed):
        platform = _platform("ios") if scoped else None
        fields = {
            "platform": platform,
            "librenms_type": "ethernetCsmacd",
            "name_pattern": "^Te",
            "librenms_speed": 1_000_000 if with_speed else None,
            "netbox_type": "other",
        }
        InterfaceTypeMapping.objects.bulk_create([InterfaceTypeMapping(**fields)])
        with pytest.raises(IntegrityError), transaction.atomic():
            InterfaceTypeMapping.objects.bulk_create([InterfaceTypeMapping(**fields)])

    def test_rules_that_differ_in_one_selector_coexist(self):
        ios, junos = _platform("ios"), _platform("junos")
        base = {"librenms_type": "ethernetCsmacd", "name_pattern": "^Te", "netbox_type": "other"}
        _rule(**base)
        _rule(**base, platform=ios)
        _rule(**base, platform=junos)
        _rule(**base, librenms_speed=1_000_000)
        _rule(**base, platform=ios, librenms_speed=1_000_000)
        _rule(**{**base, "name_pattern": "^Gi"})
        _rule(**{**base, "librenms_type": "propVirtual"})
        assert InterfaceTypeMapping.objects.filter(name_pattern__in=["^Te", "^Gi"]).count() == 7


class TestRuleCheckConstraints:
    @pytest.mark.parametrize(
        "fields",
        [
            {"action": SET, "name_pattern": "^Te", "netbox_type": None},
            {"action": SET, "name_pattern": "^Te", "netbox_type": ""},
            {"action": IGNORE, "name_pattern": "^Te", "netbox_type": "other"},
            {"action": "delete", "name_pattern": "^Te", "netbox_type": "other"},
            {"action": "delete", "name_pattern": "^Te", "netbox_type": None},
            {"action": SET, "netbox_type": "other"},
            {"action": IGNORE, "netbox_type": None},
        ],
        ids=[
            "set-null-type",
            "set-empty-type",
            "ignore-with-type",
            "unknown-action-with-type",
            "unknown-action-without-type",
            "set-no-selector",
            "ignore-no-selector",
        ],
    )
    def test_the_database_rejects_an_invalid_rule_that_skips_validation(self, fields):
        with pytest.raises(IntegrityError), transaction.atomic():
            InterfaceTypeMapping.objects.bulk_create([InterfaceTypeMapping(**fields)])

    def test_the_database_rejects_an_update_that_clears_the_only_selector(self):
        rule = _rule(name_pattern="^Te", netbox_type="other")
        with pytest.raises(IntegrityError), transaction.atomic():
            InterfaceTypeMapping.objects.filter(pk=rule.pk).update(name_pattern="")


class TestRuleExport:
    def test_str_and_yaml_name_the_platform_by_slug(self):
        import yaml

        rule = _rule(action=IGNORE, platform=_platform("ios-xe"), name_pattern="^Vlan", description="SVIs")
        assert str(rule) == "platform ios-xe, name /^Vlan/ -> ignore"
        assert yaml.safe_load(rule.to_yaml()) == {
            "action": IGNORE,
            "platform": "ios-xe",
            "name_pattern": "^Vlan",
            "librenms_type": "",
            "librenms_speed": None,
            "netbox_type": None,
            "description": "SVIs",
        }

    def test_str_of_a_legacy_rule(self):
        rule = _rule(librenms_type="ethernetCsmacd", librenms_speed=1_000_000, netbox_type="1000base-t")
        assert str(rule) == "ethernetCsmacd, >= 1000000 Kbps -> 1000base-t"


class TestMigration:
    def test_the_migrated_seed_row_is_a_global_set_type_rule(self):
        seed = importlib.import_module("netbox_librenms_plugin.migrations.0020_seed_lag_interface_type_mapping")
        row = InterfaceTypeMapping.objects.get(librenms_type=seed.SEEDED_MAPPING["librenms_type"], librenms_speed=None)
        assert (row.action, row.platform_id, row.name_pattern) == (SET, None, "")
        decision = _decide(_port("ae0", if_type="ieee8023adLag"))
        assert (decision.kind, decision.netbox_type) == (RuleDecisionKind.SET_TYPE, "lag")

    def test_the_migration_cannot_be_reversed(self):
        executor = MigrationExecutor(connection)
        with pytest.raises(IrreversibleError):
            executor.migrate([("netbox_librenms_plugin", "0021_widen_linux_bridge_pattern")])
        executor.loader.build_graph()
        assert ("netbox_librenms_plugin", "0022_interface_rules") in executor.loader.applied_migrations


# ---------------------------------------------------------------------------------------------
# Matcher: legacy parity
# ---------------------------------------------------------------------------------------------


def _legacy_selection(mappings, speed):
    """The deleted ``utils.select_interface_type_mapping``, kept verbatim as the parity oracle."""
    wildcard = None
    best = None
    for mapping in mappings:
        if mapping.librenms_speed is None:
            if wildcard is None:
                wildcard = mapping
        elif speed is not None and mapping.librenms_speed <= speed:
            if best is None or mapping.librenms_speed > best.librenms_speed:
                best = mapping
    return best or wildcard


_THRESHOLDS = {0: "100base-tx", 1_000_000: "1000base-t", 10_000_000: "10gbase-x-sfpp", 25_000_000: "25gbase-x-sfp28"}
_THRESHOLD_SETS = [subset for size in range(len(_THRESHOLDS) + 1) for subset in combinations(sorted(_THRESHOLDS), size)]
_PORT_SPEEDS_BPS = [None, 0, 999, 1000, 999_999_000, 1_000_000_000, 5_000_000_000, 10_000_000_000, 40_000_000_000]


@pytest.mark.parametrize("with_wildcard", [True, False], ids=["wildcard", "no-wildcard"])
@pytest.mark.parametrize("thresholds", _THRESHOLD_SETS, ids=lambda subset: "-".join(map(str, subset)) or "none")
def test_existing_type_and_speed_rows_decide_as_the_legacy_selector(with_wildcard, thresholds):
    """Acceptance 4: every legacy (type, speed) row set gives the legacy result for every port."""
    platform = _platform("ios")
    if with_wildcard:
        _rule(librenms_type="ethernetCsmacd", netbox_type="other")
    for threshold in thresholds:
        _rule(librenms_type="ethernetCsmacd", librenms_speed=threshold, netbox_type=_THRESHOLDS[threshold])
    rows = list(InterfaceTypeMapping.objects.filter(librenms_type="ethernetCsmacd"))
    matcher = InterfaceRuleMatcher.load()
    for if_type in ("ethernetCsmacd", "propVirtual", None):
        for speed_bps in _PORT_SPEEDS_BPS:
            port = _port(if_type=if_type, speed_bps=speed_bps)
            legacy = _legacy_selection(rows if if_type == "ethernetCsmacd" else [], convert_speed_to_kbps(speed_bps))
            expected = legacy.netbox_type if legacy else None
            for platform_id in (None, platform.pk):
                decision = matcher.decide(port, platform_id=platform_id)
                assert decision.netbox_type == expected, (if_type, speed_bps, platform_id)
                if legacy:
                    assert (decision.kind, _pks(decision)) == (RuleDecisionKind.SET_TYPE, [legacy.pk])
                else:
                    assert (decision.kind, decision.rules) == (RuleDecisionKind.UNMAPPED, ())


# ---------------------------------------------------------------------------------------------
# Matcher: selectors, rank, Ignore and ambiguity
# ---------------------------------------------------------------------------------------------


class TestPlatformScope:
    def test_a_platform_rule_applies_only_to_its_platform(self):
        ios, junos = _platform("ios"), _platform("junos")
        ignore = _rule(action=IGNORE, platform=ios, name_pattern="^Vlan")
        port = _port("Vlan10", if_type="propVirtual")
        assert (_decide(port, ios).kind, _pks(_decide(port, ios))) == (RuleDecisionKind.IGNORE, [ignore.pk])
        assert _decide(port, junos).kind is RuleDecisionKind.UNMAPPED
        assert _decide(port, None).kind is RuleDecisionKind.UNMAPPED

    def test_a_platform_name_rule_beats_a_global_type_rule_only_on_its_platform(self):
        """Acceptance 3 (matcher side): platform P, name ^Te sets the type of Te1/1 on P."""
        ios, junos = _platform("ios"), _platform("junos")
        _rule(librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        _rule(platform=ios, name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        assert _decide(_port("Te1/1"), ios).netbox_type == "10gbase-x-sfpp"
        assert _decide(_port("Te1/1"), junos).netbox_type == "1000base-t"
        assert _decide(_port("Gi1/1"), ios).netbox_type == "1000base-t"

    def test_platform_outranks_pattern(self):
        ios = _platform("ios")
        _rule(name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        platform_rule = _rule(platform=ios, librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        decision = _decide(_port("Te1/1"), ios)
        assert (decision.kind, _pks(decision)) == (RuleDecisionKind.SET_TYPE, [platform_rule.pk])

    def test_pattern_outranks_type_and_type_outranks_any(self):
        ios = _platform("ios")
        _rule(platform=ios, netbox_type="other")
        _rule(platform=ios, librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        _rule(platform=ios, name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        assert _decide(_port("Te1/1"), ios).netbox_type == "10gbase-x-sfpp"
        assert _decide(_port("Gi1/1"), ios).netbox_type == "1000base-t"
        assert _decide(_port("lo0", if_type="softwareLoopback"), ios).netbox_type == "other"


class TestNameMatching:
    @pytest.mark.parametrize(
        ("name", "descr"),
        [("Te1/1", "uplink"), ("uplink", "Te1/1"), (None, "Te1/1"), (7, "Te1/1")],
        ids=["ifName", "ifDescr", "ifName-missing", "ifName-not-a-string"],
    )
    def test_the_pattern_matches_either_raw_name(self, name, descr):
        _rule(name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        assert _decide(_port(name, descr=descr)).netbox_type == "10gbase-x-sfpp"

    @pytest.mark.parametrize(("name", "descr"), [(None, None), (7, b"Te1/1"), ("uplink", "Gi1/1")])
    def test_a_port_with_no_matching_string_name_does_not_match(self, name, descr):
        _rule(name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        port = {"ifName": name, "ifDescr": descr, "ifType": "ethernetCsmacd", "ifSpeed": None}
        assert _decide(port).kind is RuleDecisionKind.UNMAPPED

    def test_the_pattern_is_searched_and_case_sensitive(self):
        _rule(name_pattern="1/1$", netbox_type="10gbase-x-sfpp")
        assert _decide(_port("Te1/1")).kind is RuleDecisionKind.SET_TYPE
        _rule(name_pattern="^te", netbox_type="other")
        assert _decide(_port("Te2/1")).kind is RuleDecisionKind.UNMAPPED

    def test_the_type_selector_is_exact(self):
        _rule(librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        assert _decide(_port(if_type="EthernetCsmacd")).kind is RuleDecisionKind.UNMAPPED


class TestIgnoreAndAmbiguity:
    def test_ignore_wins_over_any_set_rank_and_lists_every_match(self):
        """Acceptance 1 (matcher side) and settled rule 5: Ignore always wins."""
        ios = _platform("ios")
        first = _rule(action=IGNORE, platform=ios, name_pattern="^Vlan")
        _rule(platform=ios, librenms_type="propVirtual", name_pattern="^Vlan10$", netbox_type="virtual")
        second = _rule(action=IGNORE, name_pattern="10$")
        decision = _decide(_port("Vlan10", if_type="propVirtual"), ios)
        assert (decision.kind, decision.netbox_type, _pks(decision)) == (
            RuleDecisionKind.IGNORE,
            None,
            [first.pk, second.pk],
        )

    @pytest.mark.parametrize("reverse", [False, True], ids=["insertion-order", "reverse-order"])
    @pytest.mark.parametrize("same_type", [False, True], ids=["different-types", "equal-types"])
    def test_two_rules_on_the_top_rank_are_ambiguous(self, reverse, same_type):
        """Acceptance 5 (matcher side): ties are never broken by id or order, even when outputs agree."""
        ios = _platform("ios")
        specs = [
            {"platform": ios, "name_pattern": "^Te", "netbox_type": "10gbase-x-sfpp"},
            {"platform": ios, "name_pattern": "1/1$", "netbox_type": "10gbase-x-sfpp" if same_type else "10gbase-t"},
        ]
        _rule(platform=ios, librenms_type="ethernetCsmacd", netbox_type="1000base-t")
        rules = [_rule(**spec) for spec in (reversed(specs) if reverse else specs)]
        decision = _decide(_port("Te1/1"), ios)
        assert (decision.kind, decision.netbox_type) == (RuleDecisionKind.AMBIGUOUS, None)
        assert _pks(decision) == sorted(rule.pk for rule in rules)

    def test_a_tie_below_the_top_rank_is_not_ambiguous(self):
        ios = _platform("ios")
        _rule(name_pattern="^Te", netbox_type="10gbase-t")
        _rule(name_pattern="1/1$", netbox_type="10gbase-x-sfpp")
        winner = _rule(platform=ios, name_pattern="^Te", netbox_type="10gbase-x-xfp")
        decision = _decide(_port("Te1/1"), ios)
        assert (decision.kind, _pks(decision)) == (RuleDecisionKind.SET_TYPE, [winner.pk])


class TestMayIgnore:
    def test_may_ignore_follows_the_ignore_rules_platforms(self):
        ios, junos = _platform("ios"), _platform("junos")
        _rule(platform=ios, name_pattern="^Te", netbox_type="other")
        matcher = InterfaceRuleMatcher.load()
        assert not any(matcher.may_ignore(platform_id) for platform_id in (None, ios.pk, junos.pk))

        _rule(action=IGNORE, platform=ios, name_pattern="^Vlan")
        matcher = InterfaceRuleMatcher.load()
        assert matcher.may_ignore(ios.pk)
        assert not matcher.may_ignore(junos.pk)
        assert not matcher.may_ignore(None)

        _rule(action=IGNORE, librenms_type="softwareLoopback")
        matcher = InterfaceRuleMatcher.load()
        assert all(matcher.may_ignore(platform_id) for platform_id in (None, ios.pk, junos.pk))


class TestLoading:
    def test_one_query_to_load_and_none_to_decide(self, django_assert_num_queries):
        ios = _platform("ios")
        _rule(action=IGNORE, platform=ios, name_pattern="^Vlan")
        _rule(platform=ios, name_pattern="^Te", netbox_type="10gbase-x-sfpp")
        _rule(librenms_type="ethernetCsmacd", librenms_speed=1_000_000, netbox_type="1000base-t")
        with django_assert_num_queries(1):
            matcher = InterfaceRuleMatcher.load()
        with django_assert_num_queries(0):
            decisions = [matcher.decide(_port(name), platform_id=ios.pk) for name in ("Vlan10", "Te1/1", "Gi1/1")]
            labels = [rule.label for decision in decisions for rule in decision.rules]
        assert [decision.netbox_type for decision in decisions] == [None, "10gbase-x-sfpp", "1000base-t"]
        assert labels == [
            "platform ios, name /^Vlan/ -> ignore",
            "platform ios, name /^Te/ -> 10gbase-x-sfpp",
            "ethernetCsmacd, >= 1000000 Kbps -> 1000base-t",
        ]

    def test_the_request_keeps_one_matcher(self, django_assert_num_queries):
        request = RequestFactory().get("/")
        with django_assert_num_queries(1):
            first = interface_rules_for_request(request)
            second = interface_rules_for_request(request)
        assert first is second
        assert interface_rules_for_request(RequestFactory().get("/")) is not first

    @pytest.mark.parametrize("stored", ["(unclosed", "a{4294967295}"])
    def test_a_stored_pattern_that_does_not_compile_fails_the_load(self, stored):
        rule = _rule(name_pattern="^Te", netbox_type="other")
        InterfaceTypeMapping.objects.filter(pk=rule.pk).update(name_pattern=stored)
        with pytest.raises(RuleConfigurationError, match=f"Interface rule {rule.pk} "):
            InterfaceRuleMatcher.load()

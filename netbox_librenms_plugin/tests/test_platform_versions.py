"""Platform version tracking: the assessment and application rules, against a real database."""

import pytest

from netbox_librenms_plugin.platform_versions import (
    NewOSPlatform,
    OutcomeStatus,
    PlatformFacts,
    PlatformOperation,
    PlatformReason,
    PlatformState,
    apply_platform,
    assess_platform,
    create_os_platform,
    import_platform,
    map_os_platform,
    platform_version_tracking_enabled,
    version_platform_slug,
)
from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    make_device,
    make_superuser,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms


def _platform(name, *, parent=None, manufacturer=None, slug=None):
    from dcim.models import Platform

    return Platform.objects.create(
        name=name, slug=slug or version_platform_slug(name), parent=parent, manufacturer=manufacturer
    )


def _on(device, platform):
    device.platform = platform
    device.save()
    return device


def _facts(os="junos", version="21.2R3-S4.8"):
    return PlatformFacts.from_librenms(os, version)


def _assess(device, facts, *, tracking=True, actor=None):
    return assess_platform(device, facts, tracking=tracking, actor=actor)


@pytest.mark.django_db
class TestTrackingSetting:
    def test_tracking_is_off_by_default_and_follows_the_settings_row(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.filter(pk=1).delete()
        assert platform_version_tracking_enabled() is False
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"platform_version_tracking": True})
        assert platform_version_tracking_enabled() is True


@pytest.mark.django_db
class TestAssessmentWithoutTracking:
    @pytest.mark.parametrize("tracking, version", [(False, "21.2R3-S4.8"), (True, "-"), (True, ""), (True, None)])
    def test_the_os_platform_and_every_descendant_are_current(self, tracking, version):
        os_platform = _platform("junos")
        child = _platform("junos 20.1", parent=os_platform)
        grandchild = _platform("junos 20.1 lab", parent=child)
        facts = _facts(version=version)

        for platform in (os_platform, child, grandchild):
            assessment = _assess(
                _on(make_device(f"desc-{platform.slug}-{tracking}"), platform), facts, tracking=tracking
            )
            assert assessment.state is PlatformState.CURRENT, platform
            assert assessment.reason is PlatformReason.ON_TARGET
            assert assessment.operation is None

    def test_an_unrelated_platform_proposes_the_os_platform(self):
        os_platform = _platform("junos")
        device = _on(make_device("unrelated"), _platform("eos"))

        assessment = _assess(device, _facts(), tracking=False)

        assert assessment.state is PlatformState.PROPOSED
        assert assessment.operation is PlatformOperation.ASSIGN
        assert assessment.target == os_platform

    def test_no_os_is_current_and_offers_nothing(self):
        assessment = _assess(make_device("no-os"), _facts(os="-"))

        assert assessment.state is PlatformState.CURRENT
        assert assessment.reason is PlatformReason.NO_OS
        assert assessment.operation is None


@pytest.mark.django_db
class TestOSPlatformRule:
    def test_name_match_is_case_insensitive(self):
        os_platform = _platform("JunOS")

        assert _assess(make_device("case"), _facts(os="junos"), tracking=False).target == os_platform

    def test_a_platform_mapping_finds_the_os_platform_and_its_version_platform(self):
        from netbox_librenms_plugin.models import PlatformMapping

        os_platform = _platform("Juniper Junos")
        version_platform = _platform("Juniper Junos 21.2R3-S4.8", parent=os_platform)
        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=os_platform)

        assessment = _assess(make_device("mapped"), _facts())

        assert assessment.os_platform == os_platform
        assert assessment.target == version_platform
        assert assessment.operation is PlatformOperation.ASSIGN

    def test_duplicate_names_are_blocked_unless_a_mapping_breaks_the_tie(self):
        from dcim.models import Manufacturer

        from netbox_librenms_plugin.models import PlatformMapping

        first, second = (
            _platform("junos", manufacturer=Manufacturer.objects.create(name=f"OS mfr {n}", slug=f"os-mfr-{n}"))
            for n in (1, 2)
        )
        device = make_device("duplicate-os")

        blocked = _assess(device, _facts(), tracking=False)
        assert blocked.state is PlatformState.BLOCKED
        assert blocked.reason is PlatformReason.OS_PLATFORM_AMBIGUOUS
        assert set(blocked.named) == {first, second}

        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=second)
        assert _assess(device, _facts(), tracking=False).target == second

    def test_a_missing_os_platform_offers_its_creation(self):
        assessment = _assess(make_device("no-os-platform"), _facts())

        assert assessment.state is PlatformState.PROPOSED
        assert assessment.reason is PlatformReason.OS_PLATFORM_MISSING
        assert assessment.operation is PlatformOperation.CREATE_OS_PLATFORM

    def test_a_missing_os_platform_is_blocked_for_an_actor_who_cannot_add_the_version_platform(self):
        from dcim.models import Device

        actor = make_user_with_perms("no-add-os", [("change", Device)])

        assessment = _assess(make_device("no-os-platform-actor"), _facts(), actor=actor)

        assert assessment.state is PlatformState.BLOCKED
        assert assessment.reason is PlatformReason.ADD_PLATFORM_DENIED


@pytest.mark.django_db
class TestVersionPlatformIdentity:
    def test_a_child_named_exactly_the_version_is_the_version_platform(self):
        os_platform = _platform("junos")
        child = _platform("21.2R3-S4.8", parent=os_platform)

        assessment = _assess(_on(make_device("exact"), os_platform), _facts())

        assert assessment.state is PlatformState.PROPOSED
        assert assessment.target == child
        assert assessment.operation is PlatformOperation.ASSIGN

    def test_a_child_whose_name_ends_with_the_version_is_adopted_case_insensitively(self):
        os_platform = _platform("iosxr")
        hand_made = _platform("Cisco IOS-XR 24.4.2-SMU", parent=os_platform)

        assessment = _assess(make_device("suffix"), _facts(os="iosxr", version="24.4.2-smu"))

        assert assessment.target == hand_made

    def test_a_suffix_without_the_separating_space_is_not_the_version(self):
        os_platform = _platform("junos")
        _platform("junos 21.2", parent=os_platform)

        assessment = _assess(make_device("no-space"), _facts(version="1.2"))

        assert assessment.operation is PlatformOperation.CREATE_VERSION_PLATFORM
        assert assessment.target_name == "junos 1.2"

    def test_the_version_platform_survives_a_rename_of_the_os_platform(self):
        os_platform = _platform("Juniper Junos")
        child = _platform("Juniper Junos 21.2R3-S4.8", parent=os_platform)
        os_platform.name = "junos"
        os_platform.save()

        device = _on(make_device("renamed"), child)

        assert _assess(device, _facts()).state is PlatformState.CURRENT

    def test_two_matching_children_are_blocked_as_ambiguous(self):
        os_platform = _platform("junos")
        first = _platform("21.2R3-S4.8", parent=os_platform)
        second = _platform("Juniper 21.2R3-S4.8", parent=os_platform)

        assessment = _assess(make_device("ambiguous"), _facts())

        assert assessment.state is PlatformState.BLOCKED
        assert assessment.reason is PlatformReason.VERSION_PLATFORM_AMBIGUOUS
        assert set(assessment.named) == {first, second}

    def test_a_grandchild_is_not_the_version_platform(self):
        os_platform = _platform("junos")
        branch = _platform("junos lab", parent=os_platform)
        _platform("lab 21.2R3-S4.8", parent=branch)

        assessment = _assess(make_device("grandchild"), _facts())

        assert assessment.operation is PlatformOperation.CREATE_VERSION_PLATFORM


@pytest.mark.django_db
class TestVersionPlatformCreationRule:
    def test_a_missing_version_platform_has_the_derived_name(self):
        from netbox_librenms_plugin.models import PlatformMapping

        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform("Juniper Junos"))

        assessment = _assess(make_device("derive"), _facts())

        assert assessment.state is PlatformState.PROPOSED
        assert assessment.reason is PlatformReason.VERSION_PLATFORM_MISSING
        assert assessment.target_name == "Juniper Junos 21.2R3-S4.8"
        assert assessment.operation is PlatformOperation.CREATE_VERSION_PLATFORM

    def test_the_derived_name_taken_outside_the_os_platform_is_blocked_and_named(self):
        _platform("junos")
        holder = _platform("JUNOS 21.2R3-S4.8")

        assessment = _assess(make_device("taken"), _facts())

        assert assessment.state is PlatformState.BLOCKED
        assert assessment.reason is PlatformReason.NAME_TAKEN
        assert assessment.named == (holder,)

    def test_an_actor_who_cannot_add_platforms_is_blocked(self):
        from dcim.models import Device

        _platform("junos")
        actor = make_user_with_perms("no-add-version", [("change", Device)])

        assessment = _assess(make_device("denied"), _facts(), actor=actor)

        assert assessment.state is PlatformState.BLOCKED
        assert assessment.reason is PlatformReason.ADD_PLATFORM_DENIED
        assert assessment.target_name == "junos 21.2R3-S4.8"

    def test_slugs_keep_versions_with_moved_dots_apart(self):
        assert version_platform_slug("Junos 21.2.1") == "junos-21-2-1"
        assert version_platform_slug("Junos 2.12.1") == "junos-2-12-1"


@pytest.mark.django_db
class TestApplication:
    def test_the_version_platform_is_created_under_the_os_platform_and_assigned(self):
        from dcim.models import Manufacturer

        manufacturer = Manufacturer.objects.get_or_create(name="TestMfr", slug="test-mfr")[0]
        os_platform = _platform("Juniper Junos", manufacturer=manufacturer)
        from netbox_librenms_plugin.models import PlatformMapping

        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=os_platform)
        device = _on(make_device("apply-create"), os_platform)

        outcome = apply_platform(type(device), device.pk, _facts(), tracking=True, actor=make_superuser("apply-su"))

        device.refresh_from_db()
        created = device.platform
        assert outcome.status is OutcomeStatus.APPLIED
        assert outcome.created == (created,)
        assert outcome.previous == os_platform
        assert (created.name, created.slug) == ("Juniper Junos 21.2R3-S4.8", "juniper-junos-21-2r3-s4-8")
        assert created.parent == os_platform
        assert created.manufacturer == manufacturer

    def test_a_current_subject_is_unchanged(self):
        os_platform = _platform("junos")
        device = _on(make_device("apply-current"), _platform("junos 21.2R3-S4.8", parent=os_platform))

        outcome = apply_platform(type(device), device.pk, _facts(), tracking=True, actor=make_superuser("cur-su"))

        assert outcome.status is OutcomeStatus.UNCHANGED

    def test_an_actor_who_cannot_add_platforms_changes_nothing(self):
        from dcim.models import Device, Platform

        os_platform = _platform("junos")
        device = _on(make_device("apply-denied"), os_platform)
        actor = make_user_with_perms("apply-no-add", [("change", Device)])

        outcome = apply_platform(Device, device.pk, _facts(), tracking=True, actor=actor)

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.BLOCKED
        assert outcome.reason is PlatformReason.ADD_PLATFORM_DENIED
        assert device.platform == os_platform
        assert not Platform.objects.filter(parent=os_platform).exists()

    def test_a_constrained_add_grant_that_excludes_the_new_platform_rolls_it_back(self):
        from dcim.models import Device, Platform

        os_platform = _platform("junos")
        device = make_device("apply-constrained")
        actor = make_user_with_perms("apply-constrained", [("change", Device)])
        actor = grant(actor, "add", Platform, constraints={"name": "some other platform"})

        outcome = apply_platform(Device, device.pk, _facts(), tracking=True, actor=actor)

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.BLOCKED
        assert outcome.reason is PlatformReason.ADD_PLATFORM_DENIED
        assert device.platform is None
        assert not Platform.objects.filter(parent=os_platform).exists()

    def test_an_actor_who_cannot_change_the_subject_is_refused(self):
        from dcim.models import Device, Platform

        _platform("junos")
        device = make_device("apply-no-change")
        actor = make_user_with_perms("apply-no-change", [("add", Platform)])

        outcome = apply_platform(Device, device.pk, _facts(), tracking=True, actor=actor)

        assert outcome.status is OutcomeStatus.BLOCKED
        assert outcome.reason is PlatformReason.SUBJECT_UNAVAILABLE

    def test_a_named_os_platform_and_its_version_platform_are_created_together(self):
        from dcim.models import Device

        device = make_device("apply-named")

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(),
            tracking=True,
            actor=make_superuser("named-su"),
            new_os_platform=NewOSPlatform("Juniper Junos", None, reported_version="21.2R3-S4.8"),
        )

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.APPLIED
        assert [platform.name for platform in outcome.created] == ["Juniper Junos", "Juniper Junos 21.2R3-S4.8"]
        assert device.platform.name == "Juniper Junos 21.2R3-S4.8"
        assert device.platform.parent.name == "Juniper Junos"

    def test_a_blocked_version_platform_rolls_back_the_named_os_platform(self):
        from dcim.models import Device, Platform

        _platform("Juniper Junos 21.2R3-S4.8")
        device = make_device("apply-named-blocked")

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(),
            tracking=True,
            actor=make_superuser("named-blocked-su"),
            new_os_platform=NewOSPlatform("Juniper Junos", None, reported_version="21.2R3-S4.8"),
        )

        assert outcome.reason is PlatformReason.NAME_TAKEN
        assert not Platform.objects.filter(name="Juniper Junos").exists()


@transactional_db_with_all_apps()
class TestConcurrentCreate:
    def test_a_concurrent_create_of_the_same_version_platform_is_reused(self):
        """Another session commits the version platform first; this create meets a real unique violation."""
        from dcim.models import Device, Platform
        from django.db import connection, connections

        os_platform = _platform("junos")
        device = make_device("concurrent")
        actor = make_superuser("concurrent-su")
        winners = []

        def create_winner():
            try:
                winners.append(
                    Platform.objects.create(
                        name="junos 21.2R3-S4.8", slug="junos-21-2r3-s4-8-winner", parent=os_platform
                    )
                )
            finally:
                connections.close_all()

        def insert_after_the_winner(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO "dcim_platform"') and not winners:
                with bounded_executor(1) as executor:
                    executor.submit(create_winner).result(timeout=10)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(insert_after_the_winner):
            outcome = apply_platform(Device, device.pk, _facts(), tracking=True, actor=actor)

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.APPLIED
        assert outcome.created == ()
        assert device.platform == winners[0]
        assert Platform.objects.filter(parent=os_platform).count() == 1


@pytest.mark.django_db
class TestImportPlatform:
    def test_an_actor_who_may_add_platforms_gets_the_new_version_platform(self):
        os_platform = _platform("junos")

        result = import_platform(_facts(), tracking=True, actor=make_superuser("import-su"))

        assert result.platform.parent == os_platform
        assert result.created == result.platform
        assert result.reason is None

    def test_an_actor_who_cannot_add_platforms_gets_the_os_platform_and_the_reason(self):
        from dcim.models import Device, Platform

        os_platform = _platform("junos")
        actor = make_user_with_perms("import-no-add", [("add", Device)])

        result = import_platform(_facts(), tracking=True, actor=actor)

        assert result.platform == os_platform
        assert result.reason is PlatformReason.ADD_PLATFORM_DENIED
        assert result.assessment.target_name == "junos 21.2R3-S4.8"
        assert not Platform.objects.filter(parent=os_platform).exists()

    def test_tracking_off_assigns_the_os_platform(self):
        os_platform = _platform("junos")

        result = import_platform(_facts(), tracking=False, actor=None)

        assert (result.platform, result.reason) == (os_platform, None)

    def test_a_missing_os_platform_assigns_nothing(self):
        result = import_platform(_facts(), tracking=True, actor=make_superuser("import-missing-su"))

        assert result.platform is None
        assert result.assessment.reason is PlatformReason.OS_PLATFORM_MISSING


def _platform_viewer_without_platforms(username):
    """A user who may view and change devices but may not view any platform."""
    from dcim.models import Device

    return make_user_with_perms(username, [("view", Device), ("change", Device)])


@pytest.mark.django_db
class TestDisplayedPlatformsFollowTheViewerScope:
    def test_the_name_clash_holder_is_named_only_to_a_viewer_who_may_view_it(self):
        from netbox_librenms_plugin.platform_versions import HIDDEN_PLATFORM, reason_text

        _platform("junos")
        _platform("junos 21.2R3-S4.8")
        assessment = _assess(make_device("label-clash"), _facts())
        hidden_viewer = _platform_viewer_without_platforms("label-clash-viewer")

        shown = reason_text(assessment.reason, assessment, make_superuser("label-clash-su"))
        hidden = reason_text(assessment.reason, assessment, hidden_viewer)
        saved = reason_text(assessment.reason, assessment, None)

        assert "'junos 21.2R3-S4.8'" in shown
        for text in (hidden, saved):
            assert "junos 21.2R3-S4.8" not in text, text
            assert "'junos'" not in text, text
            assert HIDDEN_PLATFORM in text, text

    def test_ambiguous_version_platforms_are_not_named_to_a_viewer_who_cannot_view_them(self):
        from netbox_librenms_plugin.platform_versions import reason_text

        os_platform = _platform("junos")
        _platform("21.2R3-S4.8", parent=os_platform)
        _platform("Lab 21.2R3-S4.8", parent=os_platform)
        assessment = _assess(make_device("label-ambiguous"), _facts())

        text = reason_text(assessment.reason, assessment, _platform_viewer_without_platforms("label-ambiguous-viewer"))

        assert "Lab 21.2R3-S4.8" not in text
        assert "'junos'" not in text

    def test_a_new_version_platform_under_a_hidden_os_platform_is_named_by_its_version_only(self):
        from netbox_librenms_plugin.platform_versions import target_label

        _platform("Secret Junos", slug="secret-junos")
        from netbox_librenms_plugin.models import PlatformMapping

        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform_by_name("Secret Junos"))
        assessment = _assess(make_device("label-new"), _facts())

        label = target_label(assessment, _platform_viewer_without_platforms("label-new-viewer"))

        assert "Secret" not in label
        assert "21.2R3-S4.8" in label


def _platform_by_name(name):
    from dcim.models import Platform

    return Platform.objects.get(name=name)


@pytest.mark.django_db
class TestCollisionSafeSlugs:
    def test_versions_whose_slugs_collide_get_the_next_free_suffix(self):
        from dcim.models import Device

        os_platform = _platform("Linux")
        actor = make_superuser("slug-su")
        first, second = make_device("slug-first"), make_device("slug-second")

        apply_platform(Device, first.pk, _facts(os="Linux", version="6.1.0"), tracking=True, actor=actor)
        apply_platform(Device, second.pk, _facts(os="Linux", version="6.1.0+"), tracking=True, actor=actor)

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.platform.name, first.platform.slug) == ("Linux 6.1.0", "linux-6-1-0")
        assert (second.platform.name, second.platform.slug) == ("Linux 6.1.0+", "linux-6-1-0-2")
        assert {first.platform.parent, second.platform.parent} == {os_platform}

    def test_a_named_os_platform_whose_slug_is_taken_gets_a_suffix(self):
        from dcim.models import Device

        _platform("Cisco IOS XR", slug="cisco-ios-xr")
        device = make_device("slug-named")

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(os="iosxr", version=""),
            tracking=False,
            actor=make_superuser("slug-named-su"),
            new_os_platform=NewOSPlatform("Cisco IOS-XR", None),
        )

        assert outcome.status is OutcomeStatus.APPLIED
        assert outcome.created[0].slug == "cisco-ios-xr-2"


@pytest.mark.django_db
class TestStaleNamedCreate:
    def test_a_changed_version_since_the_form_was_rendered_changes_nothing(self):
        from dcim.models import Device, Platform

        device = make_device("stale-version")
        count = Platform.objects.count()

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(version="22.1R1"),
            tracking=True,
            actor=make_superuser("stale-version-su"),
            new_os_platform=NewOSPlatform("Juniper Junos", None, reported_version="21.2R3-S4.8"),
        )

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.STALE
        assert outcome.reason is PlatformReason.VERSION_CHANGED
        assert device.platform is None
        assert Platform.objects.count() == count

    def test_an_os_platform_that_resolves_since_the_form_was_rendered_changes_nothing(self):
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.models import PlatformMapping

        device = make_device("stale-mapping")
        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform("Mapped Junos"))
        count = Platform.objects.count()

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(),
            tracking=True,
            actor=make_superuser("stale-mapping-su"),
            new_os_platform=NewOSPlatform("Juniper Junos", None, reported_version="21.2R3-S4.8"),
        )

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.STALE
        assert outcome.reason is PlatformReason.OS_PLATFORM_RESOLVED
        assert device.platform is None
        assert Platform.objects.count() == count


def _add_only_actor(username):
    """A user who may change devices and add platforms, but may not view platforms."""
    from dcim.models import Device, Platform

    return make_user_with_perms(username, [("change", Device), ("add", Platform)])


@pytest.mark.django_db
class TestANamedPlatformThatExistsNeedsViewScope:
    def test_a_named_platform_found_at_apply_time_is_not_reused_without_view(self):
        from dcim.models import Device

        existing = _platform("Juniper Junos")
        device = make_device("named-hidden-existing")

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(os="", version=""),
            tracking=False,
            actor=_add_only_actor("named-hidden-existing-actor"),
            new_os_platform=NewOSPlatform("juniper junos", None),
        )

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.BLOCKED
        assert outcome.reason is PlatformReason.NAMED_PLATFORM_UNAVAILABLE
        assert device.platform is None
        assert existing.name not in reason_text_for(outcome, None)

    def test_a_named_platform_found_at_apply_time_is_reused_with_view(self):
        from dcim.models import Device, Platform

        existing = _platform("Juniper Junos")
        device = make_device("named-visible-existing")
        actor = grant(_add_only_actor("named-visible-existing-actor"), "view", Platform)

        outcome = apply_platform(
            Device,
            device.pk,
            _facts(os="", version=""),
            tracking=False,
            actor=actor,
            new_os_platform=NewOSPlatform("Juniper Junos", None),
        )

        device.refresh_from_db()
        assert outcome.status is OutcomeStatus.APPLIED
        assert device.platform == existing


def reason_text_for(outcome, viewer):
    from netbox_librenms_plugin.platform_versions import reason_text

    return reason_text(outcome.reason, outcome.assessment, viewer)


@transactional_db_with_all_apps()
class TestANamedPlatformRaceWinnerNeedsViewScope:
    def test_a_winner_created_between_preflight_and_apply_is_not_reused_without_view(self, client):
        """Another session commits the named platform after the view's preflight; the actor may add, not view."""
        from dcim.models import Platform
        from django.db import connection, connections
        from django.urls import reverse

        device = make_device("named-race")
        client.force_login(_add_only_actor("named-race-actor"))
        winners = []

        def create_winner():
            try:
                winners.append(Platform.objects.create(name="Juniper Junos", slug="juniper-junos-winner"))
            finally:
                connections.close_all()

        def insert_after_the_winner(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO "dcim_platform"') and not winners:
                with bounded_executor(1) as executor:
                    executor.submit(create_winner).result(timeout=10)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(insert_after_the_winner):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:create_and_assign_platform", kwargs={"pk": device.pk}),
                {"platform_name": "Juniper Junos"},
            )

        device.refresh_from_db()
        assert winners
        assert response.status_code == 302
        assert device.platform is None
        assert Platform.objects.filter(name="Juniper Junos").count() == 1


def _mapping_actor(username, **constraints):
    """A user who may view platforms and add Platform Mappings, within *constraints* when given."""
    from dcim.models import Platform

    from netbox_librenms_plugin.models import PlatformMapping

    actor = make_user_with_perms(username, [("view", Platform)])
    return grant(actor, "add", PlatformMapping, constraints=constraints or None)


@pytest.mark.django_db
class TestMapOSPlatform:
    def test_a_missing_mapping_is_added_in_lowercase(self):
        from django.contrib.messages import constants

        from netbox_librenms_plugin.models import PlatformMapping

        platform = _platform("Juniper Junos")

        report = map_os_platform("JunOS", platform, _mapping_actor("map-added"))

        assert report == (constants.SUCCESS, "Platform mapping 'junos' to 'Juniper Junos' added")
        assert PlatformMapping.objects.get(librenms_os="junos").netbox_platform == platform

    def test_a_mapping_to_the_same_platform_is_not_modified(self):
        from django.contrib.messages import constants

        from netbox_librenms_plugin.models import PlatformMapping

        platform = _platform("Juniper Junos")
        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=platform)

        report = map_os_platform("junos", platform, _mapping_actor("map-present"))

        assert report == (constants.INFO, "Platform mapping for 'junos' already exists; not modified.")
        assert PlatformMapping.objects.count() == 1

    def test_a_mapping_to_another_platform_is_kept_and_named(self):
        from django.contrib.messages import constants

        from netbox_librenms_plugin.models import PlatformMapping

        other = _platform("Other Junos")
        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=other)

        report = map_os_platform("junos", _platform("Juniper Junos"), _mapping_actor("map-conflict"))

        assert report == (
            constants.WARNING,
            "Platform mapping for 'junos' not created: a mapping already points to 'Other Junos'.",
        )
        assert PlatformMapping.objects.get().netbox_platform == other

    def test_case_variant_mappings_are_reported_as_ambiguous(self):
        from django.contrib.messages import constants

        from netbox_librenms_plugin.models import PlatformMapping

        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform("Lab Junos"))
        variant = PlatformMapping.objects.create(librenms_os="junos-variant", netbox_platform=_platform("Core Junos"))
        PlatformMapping.objects.filter(pk=variant.pk).update(librenms_os="JUNOS")

        report = map_os_platform("junos", _platform("Juniper Junos"), _mapping_actor("map-ambiguous"))

        assert report.level == constants.WARNING
        assert "Multiple Platform Mappings match 'junos'" in report.text
        assert PlatformMapping.objects.count() == 2

    def test_an_actor_who_cannot_add_mappings_gets_a_warning(self):
        from dcim.models import Platform
        from django.contrib.messages import constants

        from netbox_librenms_plugin.models import PlatformMapping

        actor = make_user_with_perms("map-denied", [("view", Platform)])

        report = map_os_platform("junos", _platform("Juniper Junos"), actor)

        assert report == (
            constants.WARNING,
            "Platform mapping for 'junos' not created: you lack permission to add mappings.",
        )
        assert not PlatformMapping.objects.exists()

    def test_a_constrained_add_grant_that_excludes_the_new_mapping_rolls_it_back(self):
        from django.contrib.messages import constants

        from netbox_librenms_plugin.models import PlatformMapping

        actor = _mapping_actor("map-constrained", librenms_os="eos")

        report = map_os_platform("junos", _platform("Juniper Junos"), actor)

        assert report == (
            constants.WARNING,
            "Platform mapping for 'junos' not created: you lack permission to add mappings.",
        )
        assert not PlatformMapping.objects.exists()


@transactional_db_with_all_apps()
class TestMapOSPlatformRace:
    def test_a_concurrent_mapping_to_another_platform_is_reported_from_the_winner(self):
        """Another session commits the mapping first; this insert meets a real unique violation."""
        from django.contrib.messages import constants
        from django.db import connection, connections

        from netbox_librenms_plugin.models import PlatformMapping

        other = _platform("Other Junos")
        actor = _mapping_actor("map-race")
        winners = []

        def create_winner():
            try:
                winners.append(PlatformMapping.objects.create(librenms_os="junos", netbox_platform=other))
            finally:
                connections.close_all()

        def insert_after_the_winner(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO "netbox_librenms_plugin_platformmapping"') and not winners:
                with bounded_executor(1) as executor:
                    executor.submit(create_winner).result(timeout=10)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(insert_after_the_winner):
            report = map_os_platform("junos", _platform("Juniper Junos"), actor)

        assert winners
        assert report == (
            constants.WARNING,
            "Platform mapping for 'junos' not created: a mapping already points to 'Other Junos'.",
        )
        assert PlatformMapping.objects.get().netbox_platform == other


@pytest.mark.django_db
class TestCreateOSPlatform:
    def test_the_named_platform_is_created_without_its_version_platform(self):
        from dcim.models import Manufacturer, Platform

        manufacturer = Manufacturer.objects.get_or_create(name="TestMfr", slug="test-mfr")[0]

        outcome = create_os_platform(
            NewOSPlatform("Juniper Junos", manufacturer, reported_version="21.2R3-S4.8"),
            _facts(),
            tracking=True,
            actor=make_superuser("create-os-su"),
        )

        created = Platform.objects.get(name="Juniper Junos")
        assert outcome.status is OutcomeStatus.APPLIED
        assert outcome.created == (created,)
        assert outcome.assessment.os_platform == created
        assert (created.slug, created.manufacturer) == ("juniper-junos", manufacturer)
        assert not Platform.objects.filter(parent=created).exists()

    def test_a_visible_platform_with_the_name_is_reused(self):
        from dcim.models import Platform

        existing = _platform("Juniper Junos")
        actor = grant(make_user_with_perms("create-os-reuse", []), "view", Platform)

        outcome = create_os_platform(NewOSPlatform("juniper junos"), _facts(), tracking=False, actor=actor)

        assert outcome.status is OutcomeStatus.UNCHANGED
        assert outcome.created == ()
        assert outcome.assessment.os_platform == existing
        assert Platform.objects.filter(name__iexact="juniper junos").count() == 1

    def test_a_changed_version_creates_nothing(self):
        from dcim.models import Platform

        count = Platform.objects.count()

        outcome = create_os_platform(
            NewOSPlatform("Juniper Junos", reported_version="21.2R3-S4.8"),
            _facts(version="22.1R1"),
            tracking=True,
            actor=make_superuser("create-os-stale-su"),
        )

        assert (outcome.status, outcome.reason) == (OutcomeStatus.STALE, PlatformReason.VERSION_CHANGED)
        assert Platform.objects.count() == count

    def test_an_os_platform_that_resolves_since_the_form_was_rendered_creates_nothing(self):
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping

        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform("Mapped Junos"))
        count = Platform.objects.count()

        outcome = create_os_platform(
            NewOSPlatform("Juniper Junos"), _facts(), tracking=False, actor=make_superuser("create-os-resolved-su")
        )

        assert (outcome.status, outcome.reason) == (OutcomeStatus.STALE, PlatformReason.OS_PLATFORM_RESOLVED)
        assert Platform.objects.count() == count

    def test_a_constrained_add_grant_that_excludes_the_platform_creates_nothing(self):
        from dcim.models import Platform

        actor = grant(make_user_with_perms("create-os-denied", []), "add", Platform, constraints={"name": "other"})

        outcome = create_os_platform(NewOSPlatform("Juniper Junos"), _facts(), tracking=False, actor=actor)

        assert (outcome.status, outcome.reason) == (OutcomeStatus.BLOCKED, PlatformReason.ADD_PLATFORM_DENIED)
        assert not Platform.objects.filter(name="Juniper Junos").exists()

    def test_a_refused_platform_is_a_failure(self, settings):
        from dcim.models import Platform
        from extras.validators import CustomValidator

        class Refuses(CustomValidator):
            def validate(self, instance, request):
                self.fail("Refused.")

        settings.CUSTOM_VALIDATORS = {"dcim.platform": [Refuses()]}

        outcome = create_os_platform(
            NewOSPlatform("Juniper Junos"), _facts(), tracking=False, actor=make_superuser("create-os-invalid-su")
        )

        assert (outcome.status, outcome.reason) == (OutcomeStatus.FAILED, PlatformReason.PLATFORM_INVALID)
        assert outcome.error_model is Platform
        assert not Platform.objects.filter(name="Juniper Junos").exists()

"""Tests for PortStackLagPattern model."""

import pytest
from django.core.exceptions import ValidationError


@pytest.mark.django_db
class TestPortStackLagPattern:
    """Exercise the real ORM path: save() -> FullCleanOnSaveMixin.full_clean() -> clean() plus the unique librenms_os constraint, rather than a __new__ + patched-clean stand-in that bypasses validation entirely."""

    def _model(self):
        from netbox_librenms_plugin.models import PortStackLagPattern

        return PortStackLagPattern

    def test_migration_seeded_the_lowercased_default_patterns(self):
        """Migration 0013's RunPython seed actually committed rows through the ORM (e.g. the lower-cased 'ios' default) — a real-DB check the __new__/patched-clean stand-in could never make."""
        assert self._model().objects.filter(librenms_os="ios", lag_name_pattern=r"^Po\d+$").exists()

    def test_migration_seeded_the_nokia_sap_pattern_only(self):
        """Migration 0016 gives Nokia SR OS a SAP rule and leaves every other OS without one."""
        model = self._model()
        assert model.objects.get(librenms_os="timos").sap_name_pattern == ":"
        # A vendor that spells a real interface with a colon (junos xe-1/1/3:1) must not inherit
        # Nokia's notation, or its whole breakout chassis resolves no relationships.
        assert model.objects.get(librenms_os="junos").sap_name_pattern == ""

    def test_compiled_sap_patterns_are_scoped_to_the_os(self):
        """The SAP rule is read per OS, like the LAG rule, so one vendor's notation stays its own."""
        model = self._model()
        assert [pattern.pattern for pattern in model.compiled_sap_patterns_for_os("timos")] == [":"]
        # A KNOWN OS with no SAP notation gets no rule, so its colon-bearing interface names
        # (a Junos breakout is xe-1/1/3:1) keep their relationships. An OS that cannot be
        # resolved is a different case, covered by the test below.
        assert model.compiled_sap_patterns_for_os("junos") == []

    def test_an_unknown_os_applies_every_stored_sap_rule(self):
        """The SAP reader over-skips rather than under-skips, the opposite of the LAG reader.

        An unmatched LAG regex invents a relationship; an unmatched SAP regex only suppresses
        one. So an OS this model cannot resolve must keep every vendor's SAP rule, which is also
        what the unconditional colon skip it replaced did.
        """
        model = self._model()

        # A non-blank OS with no row of its own is unknown too: LibreNMS reporting an unseeded
        # name for a Nokia-like platform must not read as "this platform has no SAP notation".
        for unknown in (None, "", "   ", "sros-unregistered"):
            assert ":" in [pattern.pattern for pattern in model.compiled_sap_patterns_for_os(unknown)], unknown
        # A REGISTERED OS whose row says it has no SAP notation is answered, not unknown.
        assert model.compiled_sap_patterns_for_os("junos") == []
        # The LAG reader keeps its opposite default: a blank OS matches nothing.
        assert model.compiled_patterns_for_os("") == []

    def test_a_transactional_flush_restores_the_sap_rule_too(self, django_db_reset_sequences):
        """The reseed fixture must restore BOTH fields, or one transactional test disarms the SAP rule for the rest of the run."""
        from netbox_librenms_plugin.tests.conftest import seed_migration_rows

        model = self._model()
        model.objects.all().delete()

        seed_migration_rows()

        assert model.objects.get(librenms_os="timos").sap_name_pattern == ":"

    def test_yaml_export_carries_the_sap_pattern(self):
        """A customized SAP rule has to survive export and re-import, or it silently reverts to blank."""
        import yaml

        obj = self._model().objects.get(librenms_os="timos")
        obj.sap_name_pattern = r":\d+$"
        obj.save()

        data = yaml.safe_load(obj.to_yaml())

        assert data["sap_name_pattern"] == r":\d+$"

    def test_save_rejects_an_invalid_sap_regex(self):
        """A SAP pattern that will not compile is refused at save, like the LAG pattern."""
        model = self._model()
        with pytest.raises(ValidationError) as excinfo:
            model.objects.create(librenms_os="zzsap", lag_name_pattern=r"^Po\d+$", sap_name_pattern="[")
        assert "sap_name_pattern" in excinfo.value.message_dict
        assert not model.objects.filter(librenms_os="zzsap").exists()

    def test_a_blank_sap_pattern_is_allowed_and_contributes_no_rule(self):
        """Most operating systems have no SAP notation, so blank must stay a valid answer."""
        model = self._model()
        model.objects.create(librenms_os="zzblank", lag_name_pattern=r"^Po\d+$", sap_name_pattern="  ")
        assert model.objects.get(librenms_os="zzblank").sap_name_pattern == ""
        assert model.compiled_sap_patterns_for_os("zzblank") == []

    def test_save_normalizes_os_and_pattern(self):
        """A real save lower-cases/strips librenms_os and strips lag_name_pattern; the normalized row round-trips from the DB."""
        obj = self._model().objects.create(librenms_os="  ZZNORM  ", lag_name_pattern=r"  ^Po\d+$  ")
        obj.refresh_from_db()
        assert obj.librenms_os == "zznorm"
        assert obj.lag_name_pattern == r"^Po\d+$"

    def test_str_representation(self):
        obj = self._model()(librenms_os="ios", lag_name_pattern=r"^Po\d+$")
        assert str(obj) == r"ios -> ^Po\d+$"

    def test_save_rejects_invalid_regex_and_does_not_persist(self):
        """An invalid regex must raise on save (via full_clean) AND leave no row behind."""
        model = self._model()
        with pytest.raises(ValidationError) as exc_info:
            model.objects.create(librenms_os="zzbadregex", lag_name_pattern="[invalid(regex")
        assert "lag_name_pattern" in exc_info.value.message_dict
        assert "Invalid regex:" in exc_info.value.message_dict["lag_name_pattern"][0]
        assert not model.objects.filter(librenms_os="zzbadregex").exists()

    def test_save_rejects_blank_os(self):
        with pytest.raises(ValidationError) as exc_info:
            self._model().objects.create(librenms_os="   ", lag_name_pattern=r"^Po\d+$")
        assert "librenms_os" in exc_info.value.message_dict

    def test_save_rejects_blank_pattern(self):
        with pytest.raises(ValidationError) as exc_info:
            self._model().objects.create(librenms_os="zzblankpat", lag_name_pattern="   ")
        assert "lag_name_pattern" in exc_info.value.message_dict

    def test_unique_librenms_os_rejects_case_variant_duplicate(self):
        """librenms_os is unique case-insensitively (functional UniqueConstraint) AND lower-cased in clean(), so a case-variant duplicate ('ZZDUP' for an existing 'zzdup') is rejected by full_clean's constraint check — not silently inserted as a second row."""
        model = self._model()
        model.objects.create(librenms_os="zzdup", lag_name_pattern=r"^Po\d+$")
        with pytest.raises(ValidationError):
            model.objects.create(librenms_os="ZZDUP", lag_name_pattern=r"^Bundle-Ether\d+$")
        assert model.objects.filter(librenms_os="zzdup").count() == 1

    def test_db_enforces_case_insensitive_unique_when_full_clean_bypassed(self):
        """The functional unique on Lower(Trim(librenms_os)) rejects a case-variant duplicate at the DB level even via bulk_create (which skips full_clean/clean), so a path that bypasses the lowercasing can't insert 'IOS' alongside 'ios'."""
        from django.db import IntegrityError, transaction

        model = self._model()
        model.objects.create(librenms_os="zzci", lag_name_pattern=r"^Po\d+$")  # clean() lowercases -> "zzci"
        with pytest.raises(IntegrityError):
            # bulk_create skips full_clean, so "ZZCI" reaches the DB un-lowercased; the functional
            # unique on Lower(Trim(librenms_os)) must still reject it. atomic() so the aborted DB
            # state rolls back to a savepoint and the test transaction stays usable.
            with transaction.atomic():
                model.objects.bulk_create([model(librenms_os="ZZCI", lag_name_pattern=r"^X\d+$")])
        assert model.objects.filter(librenms_os="zzci").count() == 1

    def test_db_enforces_whitespace_variant_unique_when_full_clean_bypassed(self):
        """The constraint must treat ' ZZWSC ' and 'zzwsc' as the SAME key: clean() canonicalizes with .strip().lower(), so the DB enforces Lower(Trim(...)) — a Lower()-only unique would let a full_clean-bypassing insert park a padded duplicate behind the app's normalization."""
        from django.db import IntegrityError, transaction

        model = self._model()
        model.objects.create(librenms_os="zzwsc", lag_name_pattern=r"^Po\d+$")
        with pytest.raises(IntegrityError):
            # bulk_create skips full_clean, so the padded case-variant reaches the DB verbatim;
            # only Trim in the constraint expression can catch it.
            with transaction.atomic():
                model.objects.bulk_create([model(librenms_os=" ZZWSC ", lag_name_pattern=r"^Y\d+$")])
        assert model.objects.filter(librenms_os="zzwsc").count() == 1

    def test_db_enforces_tab_variant_unique_when_full_clean_bypassed(self):
        """The database constraint must match ``str.strip()`` for non-space whitespace."""
        from django.db import IntegrityError, transaction

        model = self._model()
        model.objects.create(librenms_os="zztab", lag_name_pattern=r"^Po\d+$")

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                model.objects.bulk_create([model(librenms_os="\tZZTAB\t", lag_name_pattern=r"^Y\d+$")])

        assert model.objects.filter(librenms_os="zztab").count() == 1

    def test_scoped_read_normalizes_a_lone_bulk_created_os(self):
        """The reader must use the same trim/lower key as the database constraint."""
        model = self._model()
        model.objects.bulk_create([model(librenms_os=" ZZFUT ", lag_name_pattern=r"^Po\d+$")])

        patterns = model.compiled_patterns_for_os("zzfut")

        assert len(patterns) == 1
        assert patterns[0].fullmatch("Po7")

    def test_api_requires_plugin_permission_and_supports_crud(self, client):
        """The rule is available through the same permission-gated API as sibling rule models."""
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_superuser

        url = reverse("plugins-api:netbox_librenms_plugin-api:portstacklagpattern-list")
        user = get_user_model().objects.create_user(username="port-stack-api-denied", password="x")
        client.force_login(user)
        assert client.get(url).status_code == 403

        client.force_login(make_superuser())
        response = client.post(
            url,
            {
                "librenms_os": "zzapi",
                "lag_name_pattern": r"^Bundle-Ether\d+$",
                "description": "Created through API",
            },
            content_type="application/json",
        )

        assert response.status_code == 201, response.json()
        detail_url = reverse(
            "plugins-api:netbox_librenms_plugin-api:portstacklagpattern-detail",
            args=[response.json()["id"]],
        )
        response = client.patch(
            detail_url,
            {"description": "Updated through API"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json()["description"] == "Updated through API"

        response = client.delete(detail_url)
        assert response.status_code == 204
        assert not self._model().objects.filter(librenms_os="zzapi").exists()


@pytest.mark.django_db
class TestHasLagSignalsFieldSelection:
    """Structural signals scan the active name field plus ifName and ifDescr."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        return object.__new__(BaseInterfaceTableView)

    def test_subiface_signal_in_ifdescr_detected(self):
        """ifDescr-driven device: the sub-interface base+child names live only in ifDescr."""
        view = self._make_view()
        ports = [
            {"ifName": "", "ifDescr": "ge-0/0/0"},  # parent
            {"ifName": "", "ifDescr": "ge-0/0/0.100"},  # sub-interface child
        ]
        # Default (ifName/ifDescr) already covers the ifDescr-driven case CR flagged.
        assert view._has_structural_relationship_signals(ports) is True
        assert view._has_structural_relationship_signals(ports, "ifDescr") is True

    def test_field_parameter_changes_outcome(self):
        """The signal lives only in a non-default field (ifAlias)."""
        view = self._make_view()
        ports = [
            {"ifAlias": "ae0"},  # parent
            {"ifAlias": "ae0.100"},  # sub-interface child
        ]
        # ifAlias is neither ifName nor ifDescr, so the default scan misses it...
        assert view._has_structural_relationship_signals(ports) is False
        # ...but passing it as the selected field surfaces the LAG signal.
        assert view._has_structural_relationship_signals(ports, "ifAlias") is True

    def test_no_signal_returns_false(self):
        """Plain access ports with no LAG/sub-interface signal stay False (not vacuously True)."""
        view = self._make_view()
        ports = [
            {"ifName": "Gi0/0", "ifDescr": "GigabitEthernet0/0", "ifType": "ethernetCsmacd"},
            {"ifName": "Gi0/1", "ifDescr": "GigabitEthernet0/1", "ifType": "ethernetCsmacd"},
        ]
        assert view._has_structural_relationship_signals(ports, "ifDescr") is False

    def test_propvirtual_alone_does_not_trigger_fetch(self):
        """A propVirtual ifType is NOT a LAG signal: loopbacks/SVIs/tunnels are propVirtual, so gating on it fired the lazy port_stack/device_info round-trips for nearly every device. Only ieee8023adLag, a name-pattern match, or a real sub-interface should gate the fetch — matching what the resolver can actually classify."""
        view = self._make_view()
        # Lone propVirtual ports whose names match no PortStackLagPattern and have no
        # sub-interface child: must NOT trigger the fetch (the old code returned True here).
        virtuals = [
            {"ifName": "Loopback0", "ifType": "propVirtual"},
            {"ifName": "Vlan100", "ifType": "propVirtual"},
        ]
        assert view._has_structural_relationship_signals(virtuals) is False
        # A structural aggregate (ieee8023adLag) still triggers it, regardless of name.
        assert view._has_structural_relationship_signals([{"ifName": "agg-x", "ifType": "ieee8023adLag"}]) is True
        # A propVirtual port-channel whose name matches a seeded pattern (^Po\\d+$) still
        # triggers it via the name-pattern branch — so real IOS LAGs are unaffected.
        from netbox_librenms_plugin.models import PortStackLagPattern

        PortStackLagPattern.objects.get_or_create(librenms_os="ios", lag_name_pattern=r"^Po\d+$")
        patterns = PortStackLagPattern.compiled_patterns_for_os(None)
        assert view._has_lag_name_signals([{"ifName": "Po10", "ifType": "propVirtual"}], "ifName", patterns) is True

    def test_non_string_name_is_skipped_not_crashed(self):
        """A truthy non-string ifName/ifDescr (numeric/list from a malformed payload) is skipped, not crashed.

        Without the isinstance(str) guard the non-string reaches pat.search()/sub_iface_re.match() and
        raises TypeError, which 500s the whole interface refresh — the resolver's _port_names skips it.
        """
        view = self._make_view()
        ports = [
            {"ifName": 123, "ifType": "ethernetCsmacd"},  # truthy non-string name
            {"ifDescr": ["x", "y"], "ifType": "ethernetCsmacd"},  # truthy non-string in another field
        ]
        # Returns a bool (no TypeError); neither is a real LAG/sub-interface signal.
        assert view._has_structural_relationship_signals(ports) is False


@pytest.mark.django_db
class TestHasLagSignalsOsScoped:
    """The name-pattern signal must be scoped to the device OS (matching resolve_port_relationships), so a name matching another platform's LAG regex doesn't trigger a wasted port_stack fetch + empty Parent/LAG column."""

    @staticmethod
    def _view():
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        return object.__new__(BaseInterfaceTableView)

    def test_name_pattern_signal_scoped_to_device_os(self):
        from netbox_librenms_plugin.models import PortStackLagPattern

        PortStackLagPattern.objects.create(librenms_os="znos", lag_name_pattern=r"^Zo\d+$")
        view = self._view()
        ports = [{"ifName": "Zo1", "ifType": "propVirtual"}]
        assert view._has_lag_name_signals(ports, "ifName", PortStackLagPattern.compiled_patterns_for_os("znos"))
        assert not view._has_lag_name_signals(
            ports, "ifName", PortStackLagPattern.compiled_patterns_for_os("some-other-os")
        )
        assert view._has_lag_name_signals(ports, "ifName", PortStackLagPattern.compiled_patterns_for_os(None))

    def test_structural_signal_is_os_independent(self):
        view = self._view()
        agg = [{"ifName": "agg0", "ifType": "ieee8023adLag"}]
        # ieee8023adLag is structural -> fires regardless of OS scope.
        assert view._has_structural_relationship_signals(agg) is True


@pytest.mark.django_db
class TestMigration0014Preflight:
    """Exercise 0014's RunPython preflight (normalize_librenms_os_case) against the real ORM: it canonicalizes casing and aborts with an actionable error before the CI-unique constraint would fail opaquely on legacy mixed-case duplicates."""

    @staticmethod
    def _preflight():
        import importlib

        mig = importlib.import_module("netbox_librenms_plugin.migrations.0014_portstacklagpattern_ci_unique")
        return mig.normalize_librenms_os_case

    @staticmethod
    def _schema_editor():
        """A schema_editor stand-in carrying the REAL test connection, as migrate would pass one — the preflight reads schema_editor.connection.alias to pin its ORM traffic to the migration's database."""
        from types import SimpleNamespace

        from django.db import connection

        return SimpleNamespace(connection=connection)

    def test_preflight_lowercases_mixed_case_rows(self):
        """A mixed-case librenms_os an old full_clean-bypassing path left behind is canonicalized to lowercase."""
        from unittest.mock import patch

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        from netbox_librenms_plugin.models import PortStackLagPattern

        historical_apps = (
            MigrationExecutor(connection)
            .loader.project_state(("netbox_librenms_plugin", "0013_portstacklagpattern"))
            .apps
        )
        historical_model = historical_apps.get_model("netbox_librenms_plugin", "portstacklagpattern")

        # bulk_create skips clean()'s lowercasing, so "ZZOSX" reaches the row verbatim (unique on its
        # own, so the CI constraint permits it). The preflight must then canonicalize it. Use a
        # distinctive OS name so it can't collide with a migration-seeded default pattern.
        PortStackLagPattern.objects.bulk_create([PortStackLagPattern(librenms_os="ZZOSX", lag_name_pattern=r"^zz\d+$")])
        with patch.object(historical_model, "full_clean", lambda self, *a, **k: None):
            self._preflight()(historical_apps, self._schema_editor())
        assert PortStackLagPattern.objects.filter(librenms_os="zzosx").exists()
        assert not PortStackLagPattern.objects.filter(librenms_os="ZZOSX").exists()

    def test_preflight_blocks_case_variant_duplicates_with_clear_error(self):
        """When case-variant duplicates predate the CI-unique, the preflight raises a clear RuntimeError naming the value rather than letting AddConstraint fail with an opaque IntegrityError."""
        from django.apps import apps as django_apps
        from django.db import connection

        from netbox_librenms_plugin.models import PortStackLagPattern

        constraint = next(
            c for c in PortStackLagPattern._meta.constraints if c.name == "unique_portstacklagpattern_librenms_os_ci"
        )
        # Reproduce the pre-0014 world (case-sensitive unique only) so the colliding rows can
        # coexist. Postgres DDL is transactional and this runs inside the test's transaction, so
        # the drop — and the rows below — roll back on teardown, restoring the constraint.
        with connection.schema_editor() as schema_editor:
            schema_editor.remove_constraint(PortStackLagPattern, constraint)
        PortStackLagPattern.objects.bulk_create(
            [
                PortStackLagPattern(librenms_os="iosdup", lag_name_pattern=r"^ae\d+$"),
                PortStackLagPattern(librenms_os="IOSDUP", lag_name_pattern=r"^bundle\d+$"),
            ]
        )
        with pytest.raises(RuntimeError, match="iosdup"):
            self._preflight()(django_apps, self._schema_editor())

    def test_preflight_normalizes_row_with_uncompilable_pattern(self):
        """A bypassing insert can leave a row the concrete model's clean() would reject (uncompilable lag_name_pattern); the preflight rewrite must still canonicalize librenms_os instead of aborting on unrelated-field validation."""
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        from netbox_librenms_plugin.models import PortStackLagPattern

        historical_apps = (
            MigrationExecutor(connection)
            .loader.project_state(("netbox_librenms_plugin", "0013_portstacklagpattern"))
            .apps
        )
        # bulk_create bypasses clean(): both the mixed-case OS name and the invalid regex land
        # verbatim. No full_clean stub here — the preflight itself must tolerate the row.
        PortStackLagPattern.objects.bulk_create(
            [PortStackLagPattern(librenms_os="ZZBAD", lag_name_pattern="([invalid")]
        )
        pk = PortStackLagPattern.objects.get(librenms_os="ZZBAD").pk

        self._preflight()(historical_apps, self._schema_editor())

        assert PortStackLagPattern.objects.get(pk=pk).librenms_os == "zzbad"

    def test_preflight_strips_surrounding_whitespace(self):
        """A whitespace-padded librenms_os a bypassing path left behind is canonicalized to .strip().lower() (matching clean()), not left with surrounding spaces behind the Lower()-only constraint."""
        from unittest.mock import patch

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        from netbox_librenms_plugin.models import PortStackLagPattern

        # Invoke the preflight with the HISTORICAL model as the migration actually sees it (state at
        # 0013, before 0014). 0013 serialized FullCleanOnSaveMixin into the historical model's bases,
        # so its save() still runs full_clean() — we stub full_clean below during the preflight so a
        # future clean() gaining a strip can't mask a Lower()-only migration bug. With it stubbed the
        # migration's OWN normalization is the only thing that writes the value, so this is the only
        # way the test can tell a Lower()-only rewrite (leaves " zzws ") from .strip().lower() ("zzws").
        historical_apps = (
            MigrationExecutor(connection)
            .loader.project_state(("netbox_librenms_plugin", "0013_portstacklagpattern"))
            .apps
        )
        historical_model = historical_apps.get_model("netbox_librenms_plugin", "portstacklagpattern")

        # bulk_create skips clean(): " ZZWS " reaches the row verbatim (Lower() of it is unique, so
        # the CI constraint permits it). The preflight must strip+lower it, not just lower it.
        PortStackLagPattern.objects.bulk_create(
            [PortStackLagPattern(librenms_os=" ZZWS ", lag_name_pattern=r"^zw\d+$")]
        )
        pk = PortStackLagPattern.objects.get(lag_name_pattern=r"^zw\d+$").pk
        # Sanity: the padded value really landed in the row (bulk_create did not canonicalize it).
        assert PortStackLagPattern.objects.get(pk=pk).librenms_os == " ZZWS "
        # Stub the historical model's full_clean so ONLY the migration's rewrite can normalize the
        # row — otherwise a save-time strip could pass this test even if the migration itself is wrong.
        with patch.object(historical_model, "full_clean", lambda self, *a, **k: None):
            self._preflight()(historical_apps, self._schema_editor())
        # Exact canonical value, not a DB filter: trailing/leading-space equality is collation-fuzzy
        # and would let " zzws " match "zzws".
        assert PortStackLagPattern.objects.get(pk=pk).librenms_os == "zzws"

    def test_preflight_pins_orm_traffic_to_the_migration_alias_not_the_router(self):
        """Multi-DB safety (the same pinned-rewrite pattern as the sibling data migrations): the preflight's OWN queryset iteration and save must be pinned to schema_editor.connection.alias via .using()/save(using=...). Unpinned ORM calls consult the database router, so `migrate --database=other` would normalize rows on the router's choice instead of the migration's target database — detected here by recording router consultations whose call stack passes through the migration module. Consults from INSIDE Model.full_clean are excluded: 0013 serialized FullCleanOnSaveMixin into the historical model's bases, so even a fully pinned save() runs full_clean, whose validate_unique/validate_constraints ask the router with instance hints and correctly fall back to the pinned instance._state.db — Django offers no using hook there, and the migration cannot avoid it."""
        import traceback

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from django.test import override_settings

        from netbox_librenms_plugin.models import PortStackLagPattern

        historical_apps = (
            MigrationExecutor(connection)
            .loader.project_state(("netbox_librenms_plugin", "0013_portstacklagpattern"))
            .apps
        )

        # A mixed-case row so the rewrite loop performs a real save (write path exercised too).
        PortStackLagPattern.objects.bulk_create([PortStackLagPattern(librenms_os="ZZRTR", lag_name_pattern=r"^zr\d+$")])

        unpinned = []

        class _RecordingRouter:
            """Records unpinned routing questions coming from the migration's own code."""

            @staticmethod
            def _record(kind, model):
                if model._meta.model_name != "portstacklagpattern":
                    return
                frames = traceback.extract_stack()
                in_migration = any("0014_portstacklagpattern_ci_unique" in f.filename for f in frames)
                from_full_clean = any(f.name == "full_clean" for f in frames)
                if in_migration and not from_full_clean:
                    unpinned.append(kind)

            def db_for_read(self, model, **hints):
                self._record("read", model)
                return None

            def db_for_write(self, model, **hints):
                self._record("write", model)
                return None

        with override_settings(DATABASE_ROUTERS=[_RecordingRouter()]):
            self._preflight()(historical_apps, self._schema_editor())

        assert unpinned == [], f"preflight consulted the router ({unpinned}) instead of pinning the migration alias"
        assert PortStackLagPattern.objects.filter(librenms_os="zzrtr").exists()

    def test_preflight_blocks_whitespace_and_case_variant_duplicates(self):
        """Rows differing only by surrounding whitespace/case (' WSDUP ' vs 'wsdup') are the same pattern to clean(); the preflight must flag them as a collision via .strip().lower() — a Lower()-only check misses the whitespace variant and would leave a semantic duplicate behind the constraint."""
        from django.apps import apps as django_apps
        from django.db import connection

        from netbox_librenms_plugin.models import PortStackLagPattern

        constraint = next(
            c for c in PortStackLagPattern._meta.constraints if c.name == "unique_portstacklagpattern_librenms_os_ci"
        )
        with connection.schema_editor() as schema_editor:
            schema_editor.remove_constraint(PortStackLagPattern, constraint)
        PortStackLagPattern.objects.bulk_create(
            [
                PortStackLagPattern(librenms_os="wsdup", lag_name_pattern=r"^ae\d+$"),
                PortStackLagPattern(librenms_os=" WSDUP ", lag_name_pattern=r"^bundle\d+$"),
            ]
        )
        with pytest.raises(RuntimeError, match="wsdup"):
            self._preflight()(django_apps, self._schema_editor())


@pytest.mark.django_db
class TestLagPatternSharedLoad:
    """The interface-refresh LAG gating loads OS-scoped patterns once and shares them.

    The signal check and resolve_port_relationships each re-queried and recompiled
    PortStackLagPattern per call, so a single refresh loaded the scoped patterns twice (plus the
    resolver's own load). Both now accept a pre-loaded compiled list so the caller loads once.
    """

    def _view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        return object.__new__(BaseInterfaceTableView)

    @staticmethod
    def _seed_and_compile(os_name="sharedos"):
        from netbox_librenms_plugin.models import PortStackLagPattern

        PortStackLagPattern.objects.create(librenms_os=os_name, lag_name_pattern=r"^Po\d+$")
        return PortStackLagPattern.compiled_patterns_for_os(os_name)

    @staticmethod
    def _no_pattern_query(ctx):
        return not any("portstacklagpattern" in q["sql"].lower() for q in ctx.captured_queries)

    def test_name_signal_reuses_supplied_patterns_without_db(self):
        """The name signal scans supplied compiled patterns without a database query."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        compiled = self._seed_and_compile()
        view = self._view()
        ports = [{"ifName": "Gi0/0", "ifType": "ethernetCsmacd"}]

        with CaptureQueriesContext(connection) as ctx:
            view._has_lag_name_signals(ports, "ifName", compiled)

        assert self._no_pattern_query(ctx)

    def test_resolver_reuses_supplied_compiled_patterns_without_db(self, mock_librenms_api):
        """resolve_port_relationships(compiled_lag_patterns=...) resolves without re-loading patterns."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        compiled = self._seed_and_compile()
        ports = [
            {"port_id": 11, "ifName": "Gi0/1", "ifType": "ethernetCsmacd"},
            {"port_id": 12, "ifName": "Po1", "ifType": "propVirtual"},
        ]
        port_stack = [{"high_port_id": 11, "low_port_id": 12}]

        with CaptureQueriesContext(connection) as ctx:
            result = mock_librenms_api.resolve_port_relationships(
                ports, port_stack, device_os="sharedos", compiled_lag_patterns=compiled
            )

        # Po1 aggregate matched via the supplied pattern (behavior preserved) and no pattern query.
        assert result["lag_members"] == {11: 12}
        assert self._no_pattern_query(ctx)

    def test_resolver_reads_the_stored_sap_rule_from_device_os_alone(self, mock_librenms_api):
        """Given only device_os, the resolver applies that OS's stored SAP rule from the database."""
        # The other SAP tests inject a compiled pattern, so none of them prove the stored rule is
        # ever read. This one passes no pattern overrides at all.
        from netbox_librenms_plugin.models import PortStackLagPattern

        # A pattern that is NOT a colon: the rule this commit replaced skipped colons
        # unconditionally, so a colon pattern here would pass without the database ever being read.
        PortStackLagPattern.objects.update_or_create(
            librenms_os="storedos",
            defaults={"lag_name_pattern": r"^lag-\d+$", "sap_name_pattern": r"^svc-"},
        )
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "svc-100", "ifType": "ipForward"},
        ]
        port_stack = [
            {"high_port_id": 101, "low_port_id": 102},
            {"high_port_id": 200, "low_port_id": 102},
        ]

        result = mock_librenms_api.resolve_port_relationships(ports, port_stack, device_os="storedos")

        assert result["lag_members"] == {101: 102}
        assert 200 not in result["lag_members"], "the stored SAP rule was not read"

    def test_string_zero_high_id_sentinel_is_skipped(self, mock_librenms_api):
        """A string "0" port ID (the ifStack 'no port' sentinel) is skipped, not looked up."""
        compiled = self._seed_and_compile()
        # A port whose id is 0 exists in by_id (keyed "0"); the sentinel entry references it as the
        # STRING "0". `not "0"` is False, so the old check would look it up and relate it to Po1.
        ports = [
            {"port_id": 0, "ifName": "phantom", "ifType": "ethernetCsmacd"},
            {"port_id": 12, "ifName": "Po1", "ifType": "propVirtual"},
        ]
        port_stack = [{"high_port_id": "0", "low_port_id": 12}]

        result = mock_librenms_api.resolve_port_relationships(
            ports, port_stack, device_os="sharedos", compiled_lag_patterns=compiled
        )

        # The sentinel entry created no relationship at all (nothing references the phantom port 0).
        assert result["lag_members"] == {}
        assert result["sub_interfaces"] == {}


@pytest.mark.django_db
def test_mapping_bulk_import_routes_resolve_without_the_model_view_registry():
    """urls.py owns every mapping bulk-import route, so no register_model_view is needed.

    The decorators added no URL because urls.py never includes get_model_urls(). This pins the
    explicit routes, so removing them cannot silently take the Import views offline.
    """
    from django.urls import resolve, reverse

    from netbox_librenms_plugin.views import mapping_views

    expected = {
        "interfacetypemapping_bulk_import": mapping_views.InterfaceTypeMappingBulkImportView,
        "devicetypemapping_bulk_import": mapping_views.DeviceTypeMappingBulkImportView,
        "moduletypemapping_bulk_import": mapping_views.ModuleTypeMappingBulkImportView,
        "modulebaymapping_bulk_import": mapping_views.ModuleBayMappingBulkImportView,
        "normalizationrule_bulk_import": mapping_views.NormalizationRuleBulkImportView,
        "inventoryignorerule_bulk_import": mapping_views.InventoryIgnoreRuleBulkImportView,
        "platformmapping_bulk_import": mapping_views.PlatformMappingBulkImportView,
        "carrierautoinstallrule_bulk_import": mapping_views.CarrierAutoInstallRuleBulkImportView,
        "portstacklagpattern_bulk_import": mapping_views.PortStackLagPatternBulkImportView,
    }

    for route, view_class in expected.items():
        url = reverse(f"plugins:netbox_librenms_plugin:{route}")
        assert resolve(url).func.view_class is view_class, route

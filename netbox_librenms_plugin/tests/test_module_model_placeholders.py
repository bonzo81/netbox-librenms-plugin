"""A placeholder model name is absent data, not a lookup key.

When a vendor reports ``unspecified`` (or ``builtin`` / ``n/a`` / ``unknown``) as the model for
every SFP in the box, all of those rows collapse onto one lookup key. The schema allows one
``ModuleTypeMapping`` per key per manufacturer, so either every unspecified SFP resolves to the
same wrong ModuleType or none resolve at all. Treat the placeholder as absent and fall back to
``entPhysicalDescr``, which also carries the transceiver API's type on a synthetic row.
"""

import pytest

PLACEHOLDERS = ["unspecified", "unknown", "n/a", "builtin", "UNSPECIFIED", "Builtin"]


def _module_type(model, manufacturer_name="Placeholder Vendor"):
    from dcim.models import Manufacturer, ModuleType

    manufacturer, _ = Manufacturer.objects.get_or_create(
        name=manufacturer_name, defaults={"slug": manufacturer_name.lower().replace(" ", "-")}
    )
    return ModuleType.objects.create(manufacturer=manufacturer, model=model)


def _index():
    from netbox_librenms_plugin.utils import get_module_types_indexed

    return get_module_types_indexed()


@pytest.mark.django_db
class TestPlaceholderIsNotALookupKey:
    """A mapping keyed on a placeholder must not answer for unrelated hardware."""

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_a_placeholder_model_alone_resolves_to_nothing(self, placeholder):
        """Without a fallback there is no signal at all, so No Type is the honest answer."""
        from netbox_librenms_plugin.utils import resolve_module_type

        _module_type("SFP-10G-LR")

        assert resolve_module_type(placeholder, _index()) is None

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_a_placeholder_model_falls_back_to_the_description(self, placeholder):
        """The description carries the real part number when the model field is a placeholder."""
        from netbox_librenms_plugin.utils import resolve_module_type

        module_type = _module_type("SFP-10G-LR")

        matched = resolve_module_type(placeholder, _index(), fallback_names=["SFP-10G-LR"])

        assert matched == module_type

    def test_a_placeholder_mapping_row_cannot_match_a_placeholder_model(self):
        """The defect: one mapping row keyed "unspecified" answered for every unspecified SFP."""
        from dcim.models import Manufacturer
        from netbox_librenms_plugin.models import ModuleTypeMapping
        from netbox_librenms_plugin.utils import resolve_module_type

        wrong_type = _module_type("SFP-1G-SX")
        manufacturer = Manufacturer.objects.get(name="Placeholder Vendor")
        ModuleTypeMapping.objects.create(
            librenms_model="unspecified",
            netbox_module_type=wrong_type,
            manufacturer=manufacturer,
        )
        right_type = _module_type("SFP-10G-LR")

        matched = resolve_module_type(
            "unspecified",
            _index(),
            manufacturer=manufacturer,
            fallback_names=["SFP-10G-LR"],
        )

        assert matched == right_type

    def test_a_real_model_still_wins_over_its_fallback(self):
        """Positive control: the fallback must only apply when the model is absent."""
        from netbox_librenms_plugin.utils import resolve_module_type

        real = _module_type("SFP-10G-LR")
        _module_type("SFP-1G-SX")

        matched = resolve_module_type("SFP-10G-LR", _index(), fallback_names=["SFP-1G-SX"])

        assert matched == real

    def test_a_real_model_that_matches_nothing_does_not_fall_back(self):
        """A reported model that simply has no ModuleType is not a placeholder."""
        from netbox_librenms_plugin.utils import resolve_module_type

        _module_type("SFP-1G-SX")

        assert resolve_module_type("SFP-40G-SR4", _index(), fallback_names=["SFP-1G-SX"]) is None

    def test_a_placeholder_fallback_is_skipped_too(self):
        """A fallback that is itself a placeholder carries no more signal than the model did."""
        from netbox_librenms_plugin.utils import resolve_module_type

        _module_type("SFP-10G-LR")

        assert resolve_module_type("unspecified", _index(), fallback_names=["builtin", ""]) is None


@pytest.mark.django_db
class TestLookupCandidatesComeFromOneDefinition:
    """Every call site must derive the same candidate order from a row."""

    def test_the_model_leads_and_the_description_follows(self):
        from netbox_librenms_plugin.utils import module_type_lookup_candidates

        candidates = module_type_lookup_candidates(
            {"entPhysicalModelName": "SFP-10G-LR", "entPhysicalDescr": "10GBASE-LR SFP+"}
        )

        assert candidates == ["SFP-10G-LR", "10GBASE-LR SFP+"]

    def test_a_placeholder_model_is_dropped_from_the_candidates(self):
        from netbox_librenms_plugin.utils import module_type_lookup_candidates

        candidates = module_type_lookup_candidates(
            {"entPhysicalModelName": "unspecified", "entPhysicalDescr": "10GBASE-LR SFP+"}
        )

        assert candidates == ["10GBASE-LR SFP+"]

    def test_a_repeated_value_appears_once(self):
        from netbox_librenms_plugin.utils import module_type_lookup_candidates

        candidates = module_type_lookup_candidates(
            {"entPhysicalModelName": "SFP-10G-LR", "entPhysicalDescr": "SFP-10G-LR"}
        )

        assert candidates == ["SFP-10G-LR"]

    def test_a_row_with_nothing_usable_yields_no_candidates(self):
        from netbox_librenms_plugin.utils import module_type_lookup_candidates

        assert module_type_lookup_candidates({"entPhysicalModelName": "builtin", "entPhysicalDescr": "n/a"}) == []


@pytest.mark.django_db
class TestTransceiverMergeTreatsPlaceholdersAsMissing:
    """The merge only replaced an empty or literally "builtin" model, so "unspecified" stuck."""

    @staticmethod
    def _merge(settings, server, existing_model):
        """Merge one transceiver over one ENTITY-MIB row, through the real HTTP client."""
        from netbox_librenms_plugin.tests.test_modules_view import _real_api_view

        view = _real_api_view(settings, server, librenms_id=101)
        server.register(
            "/api/v0/devices/101/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 300,
                        "port_id": 99,
                        "model": "SFP-10G-LR",
                        "serial": "ABC123",
                        "type": "SFP+",
                    }
                ],
            },
            method="GET",
        )
        server.register(
            "/api/v0/devices/101/ports",
            {"status": "ok", "ports": [{"port_id": 99, "ifName": "Eth2/1"}]},
            method="GET",
        )
        inventory = [
            {
                "entPhysicalIndex": 300,
                "entPhysicalName": "Transceiver slot",
                "entPhysicalClass": "port",
                "entPhysicalModelName": existing_model,
                "entPhysicalSerialNum": "",
                "entPhysicalDescr": "",
                "entPhysicalContainedIn": 0,
            }
        ]
        merged, error = view._merge_transceiver_data(inventory)
        assert error is None
        return merged[0]

    @pytest.mark.parametrize("placeholder", ["unspecified", "unknown", "n/a", "builtin"])
    def test_a_placeholder_model_is_supplemented_from_the_transceiver_api(self, settings, librenms_server, placeholder):
        assert self._merge(settings, librenms_server, placeholder)["entPhysicalModelName"] == "SFP-10G-LR"

    def test_a_real_model_is_not_overwritten(self, settings, librenms_server):
        """Positive control: the API must not replace a model ENTITY-MIB actually reported."""
        assert self._merge(settings, librenms_server, "SFP-1G-SX")["entPhysicalModelName"] == "SFP-1G-SX"

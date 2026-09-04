"""
Tests for the LocationMapping model, resolve_location_mapping() resolver, and
the find_matching_site() mapping fallback.

Following the repo convention (see test_platform_mapping.py): plain pytest
classes with all DB interactions mocked — no @pytest.mark.django_db.
"""

from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# TestLocationMappingTable
# =============================================================================


class TestLocationMappingTable:
    """Tests for LocationMappingTable configuration."""

    def test_selection_column_uses_standard_checkbox_contract(self):
        from netbox.tables import columns

        from netbox_librenms_plugin.tables.mappings import LocationMappingTable

        column = LocationMappingTable.base_columns["pk"]
        assert isinstance(column, columns.ToggleColumn)
        assert column.attrs["input"]["name"] == "select"


# =============================================================================
# TestLocationMappingModel
# =============================================================================


class TestLocationMappingModel:
    """Tests for LocationMapping model behaviour."""

    def _make(self, field_type="site", librenms_value="NYC", description=""):
        from django.db.models.base import ModelState

        from netbox_librenms_plugin.models import LocationMapping

        mapping = LocationMapping.__new__(LocationMapping)
        mapping._state = ModelState()
        mapping.field_type = field_type
        mapping.librenms_value = librenms_value
        mapping.description = description
        mapping.content_type_id = None
        mapping.pk = None
        return mapping

    def test_str_representation(self):
        """__str__ shows '<Type>: value -> object'."""
        mapping = self._make(field_type="site", librenms_value="NYC")
        mapping.object_id = 1
        with patch.object(
            type(mapping),
            "netbox_object",
            new_callable=lambda: property(lambda s: "New York"),
        ):
            assert str(mapping) == "Site: NYC -> New York"

    def test_clean_strips_whitespace(self):
        """clean() strips leading/trailing whitespace from librenms_value."""
        from netbox_librenms_plugin.models import LocationMapping

        # Use a parent-scoped type so no global uniqueness query runs.
        mapping = self._make(field_type="location", librenms_value="  Aisle 1  ")
        with patch("netbox.models.NetBoxModel.clean"):
            with patch.object(LocationMapping, "_validate_no_scoped_collision"):
                mapping.clean()
        assert mapping.librenms_value == "Aisle 1"

    def test_clean_raises_on_blank(self):
        """clean() raises ValidationError when librenms_value is blank after strip."""
        from django.core.exceptions import ValidationError

        mapping = self._make(librenms_value="   ")
        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                mapping.clean()
        assert "librenms_value" in str(exc_info.value)

    def test_clean_raises_on_unknown_field_type(self):
        """clean() raises ValidationError for an unknown field_type."""
        from django.core.exceptions import ValidationError

        mapping = self._make(field_type="bogus", librenms_value="NYC")
        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                mapping.clean()
        assert "field_type" in str(exc_info.value)

    def test_clean_raises_on_content_type_mismatch(self):
        """clean() raises when the content_type does not match the field_type."""
        from django.core.exceptions import ValidationError

        mapping = self._make(field_type="site", librenms_value="NYC")
        mapping.content_type_id = 99
        ct = MagicMock(app_label="dcim", model="region")
        mapping._state.fields_cache["content_type"] = ct
        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                mapping.clean()
        assert "content_type" in str(exc_info.value)

    def test_clean_accepts_matching_content_type(self):
        """clean() passes when content_type matches the field_type."""
        from netbox_librenms_plugin.models import LocationMapping

        mapping = self._make(field_type="site", librenms_value="NYC")
        mapping.content_type_id = 5
        mapping.object_id = 1
        mapping._state.fields_cache["content_type"] = MagicMock(app_label="dcim", model="site")
        with patch("netbox.models.NetBoxModel.clean"):
            with patch.object(
                LocationMapping,
                "netbox_object",
                new_callable=lambda: property(lambda s: MagicMock()),
            ):
                with patch.object(LocationMapping, "objects") as mock_objects:
                    mock_objects.filter.return_value.exists.return_value = False
                    mapping.clean()
        assert mapping.librenms_value == "NYC"

    def test_clean_raises_on_unresolved_object(self):
        """clean() raises when object_id does not resolve to an existing object."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import LocationMapping

        mapping = self._make(field_type="site", librenms_value="NYC")
        mapping.content_type_id = 5
        mapping.object_id = 999999
        mapping._state.fields_cache["content_type"] = MagicMock(app_label="dcim", model="site")
        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                with patch.object(
                    LocationMapping,
                    "netbox_object",
                    new_callable=lambda: property(lambda s: None),
                ):
                    mapping.clean()
        assert "object_id" in str(exc_info.value)

    def test_clean_enforces_uniqueness_for_site(self):
        """clean() raises when a duplicate site mapping value already exists."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import LocationMapping

        mapping = self._make(field_type="site", librenms_value="NYC")
        with patch("netbox.models.NetBoxModel.clean"):
            with patch.object(LocationMapping, "objects") as mock_objects:
                mock_objects.filter.return_value.exists.return_value = True
                with pytest.raises(ValidationError) as exc_info:
                    mapping.clean()
        assert "librenms_value" in str(exc_info.value)

    def test_clean_allows_duplicate_for_rack(self):
        """clean() does not enforce global uniqueness for parent-scoped rack mappings."""
        from netbox_librenms_plugin.models import LocationMapping

        mapping = self._make(field_type="rack", librenms_value="R1")
        mapping.object_id = 1
        with patch("netbox.models.NetBoxModel.clean"):
            with patch.object(LocationMapping, "_validate_no_scoped_collision") as mock_check:
                with patch.object(LocationMapping, "objects") as mock_objects:
                    mapping.clean()
                    # The global uniqueness query must not run for rack
                    mock_objects.filter.assert_not_called()
        mock_check.assert_called_once()

    def test_clean_raises_on_same_site_scoped_collision(self):
        """clean() rejects a location alias already mapped to another object in the same site."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import LocationMapping

        target = MagicMock(name="hall-a")
        other_target = MagicMock(name="hall-b")

        mapping = self._make(field_type="location", librenms_value="Hall A")
        mapping.object_id = 1
        with pytest.raises(ValidationError) as exc_info:
            with patch("netbox.models.NetBoxModel.clean"):
                with patch("netbox_librenms_plugin.models.get_object_site_id", return_value=7):
                    with patch.object(
                        LocationMapping,
                        "netbox_object",
                        new_callable=lambda: property(lambda s: target),
                    ):
                        with patch.object(LocationMapping, "objects") as mock_objects:
                            mock_objects.filter.return_value.select_related.return_value = [
                                MagicMock(netbox_object=other_target)
                            ]
                            mapping.clean()
        assert "librenms_value" in str(exc_info.value)

    def test_clean_allows_scoped_alias_in_different_site(self):
        """clean() permits the same location alias when the objects sit in different sites."""
        from netbox_librenms_plugin.models import LocationMapping

        target = MagicMock(name="hall-a")
        other_target = MagicMock(name="hall-b")

        mapping = self._make(field_type="location", librenms_value="Hall A")
        mapping.object_id = 1
        with patch("netbox.models.NetBoxModel.clean"):
            with patch(
                "netbox_librenms_plugin.models.get_object_site_id",
                side_effect=lambda obj: 7 if obj is target else 9,
            ):
                with patch.object(
                    LocationMapping,
                    "netbox_object",
                    new_callable=lambda: property(lambda s: target),
                ):
                    with patch.object(LocationMapping, "objects") as mock_objects:
                        mock_objects.filter.return_value.select_related.return_value = [
                            MagicMock(netbox_object=other_target)
                        ]
                        mapping.clean()
        assert mapping.librenms_value == "Hall A"

    def test_get_absolute_url(self):
        """get_absolute_url returns the detail URL."""
        mapping = self._make()
        mapping.pk = 42
        with patch("netbox_librenms_plugin.models.reverse") as mock_reverse:
            mock_reverse.return_value = "/plugins/librenms/location-mappings/42/"
            url = mapping.get_absolute_url()
        mock_reverse.assert_called_once_with("plugins:netbox_librenms_plugin:locationmapping_detail", args=[42])
        assert url == "/plugins/librenms/location-mappings/42/"

    def test_meta_ordering(self):
        """Model Meta ordering is by field_type then librenms_value."""
        from netbox_librenms_plugin.models import LocationMapping

        assert LocationMapping._meta.ordering == ["field_type", "librenms_value"]

    def test_to_yaml(self):
        """to_yaml() emits field_type, librenms_value, netbox_object, description."""
        import yaml

        mapping = self._make(field_type="site", librenms_value="NYC", description="east coast")
        mapping.object_id = 1
        with patch.object(
            type(mapping),
            "netbox_object",
            new_callable=lambda: property(lambda s: "New York"),
        ):
            data = yaml.safe_load(mapping.to_yaml())
        assert data == {
            "field_type": "site",
            "librenms_value": "NYC",
            "netbox_object": "New York",
            "description": "east coast",
        }

    def test_to_yaml_includes_parent_site_for_scoped_types(self):
        """to_yaml() emits parent_site for location/rack so the export can be re-imported."""
        import yaml

        target = MagicMock()
        target.__str__ = lambda s: "Data Hall A"
        target.site.__str__ = lambda s: "New York"

        mapping = self._make(field_type="location", librenms_value="Hall A")
        mapping.object_id = 1
        with patch.object(
            type(mapping),
            "netbox_object",
            new_callable=lambda: property(lambda s: target),
        ):
            data = yaml.safe_load(mapping.to_yaml())
        assert data == {
            "field_type": "location",
            "librenms_value": "Hall A",
            "netbox_object": "Data Hall A",
            "parent_site": "New York",
            "description": "",
        }


# =============================================================================
# TestResolveLocationMapping
# =============================================================================


class TestResolveLocationMapping:
    """Tests for resolve_location_mapping()."""

    def test_returns_none_for_empty_value(self):
        from netbox_librenms_plugin.utils import resolve_location_mapping

        assert resolve_location_mapping("site", "") is None
        assert resolve_location_mapping("site", None) is None

    def test_returns_matched_object(self):
        from netbox_librenms_plugin import utils

        target = MagicMock(name="site-obj")
        mapping = MagicMock(netbox_object=target)

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.select_related.return_value = [mapping]

        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LocationMapping=fake_model)},
        ):
            result = utils.resolve_location_mapping("site", "NYC")
        assert result is target

    def test_parent_site_scoping_matches(self):
        """For rack/location, only an object under the parent site is returned."""
        from netbox_librenms_plugin import utils

        parent_site = MagicMock(pk=7)
        target = MagicMock()
        target.site_id = 7
        mapping = MagicMock(netbox_object=target)

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.select_related.return_value = [mapping]

        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LocationMapping=fake_model)},
        ):
            result = utils.resolve_location_mapping("rack", "R1", parent_site=parent_site)
        assert result is target

    def test_parent_site_scoping_skips_mismatch(self):
        """An object under a different site is skipped."""
        from netbox_librenms_plugin import utils

        parent_site = MagicMock(pk=7)
        target = MagicMock()
        target.site_id = 99
        mapping = MagicMock(netbox_object=target)

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.select_related.return_value = [mapping]

        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LocationMapping=fake_model)},
        ):
            result = utils.resolve_location_mapping("rack", "R1", parent_site=parent_site)
        assert result is None

    def test_returns_none_when_candidates_are_ambiguous(self):
        """Two mappings resolving to different objects in the same site yield no result."""
        from netbox_librenms_plugin import utils

        parent_site = MagicMock(pk=7)
        first = MagicMock(site_id=7, pk=1)
        second = MagicMock(site_id=7, pk=2)

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.select_related.return_value = [
            MagicMock(netbox_object=first),
            MagicMock(netbox_object=second),
        ]

        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LocationMapping=fake_model)},
        ):
            result = utils.resolve_location_mapping("location", "Hall A", parent_site=parent_site)
        assert result is None

    def test_duplicate_mappings_to_same_object_are_not_ambiguous(self):
        """Several mappings pointing at one object still resolve."""
        from netbox_librenms_plugin import utils

        target = MagicMock(site_id=7, pk=1)

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.select_related.return_value = [
            MagicMock(netbox_object=target),
            MagicMock(netbox_object=target),
        ]

        with patch.dict(
            "sys.modules",
            {"netbox_librenms_plugin.models": MagicMock(LocationMapping=fake_model)},
        ):
            result = utils.resolve_location_mapping("location", "Hall A", parent_site=MagicMock(pk=7))
        assert result is target


# =============================================================================
# Test_GetObjectSiteId
# =============================================================================


class TestGetObjectSiteId:
    """Tests for the get_object_site_id() helper."""

    def test_direct_site_id(self):
        from netbox_librenms_plugin.utils import get_object_site_id

        obj = MagicMock(site_id=3)
        assert get_object_site_id(obj) == 3

    def test_via_location(self):
        from netbox_librenms_plugin.utils import get_object_site_id

        obj = MagicMock(site_id=None)
        obj.location = MagicMock(site_id=8)
        assert get_object_site_id(obj) == 8

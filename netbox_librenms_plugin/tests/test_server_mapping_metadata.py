"""Tests for reserved metadata in server-scoped object mappings."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from netbox_librenms_plugin import LibreNMSSyncConfig
from netbox_librenms_plugin.server_mappings import PREFERRED_SERVER_FIELD, iter_server_mapping_entries
from netbox_librenms_plugin.sync_cache import _explicit_server_keys
from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
from netbox_librenms_plugin.utils import get_librenms_device_id, get_librenms_sync_device, set_librenms_device_id


def test_reserved_preference_key_is_rejected_in_server_configuration():
    """A configured server cannot collide with stored preference metadata."""
    config = SimpleNamespace(name="netbox_librenms_plugin")

    with pytest.raises(ImproperlyConfigured, match="reserved for object metadata"):
        LibreNMSSyncConfig._validate_multi_server_config(
            config,
            {
                PREFERRED_SERVER_FIELD: {
                    "librenms_url": "https://librenms.example.com",
                    "api_token": "test-token",
                }
            },
        )


def test_mapping_iteration_excludes_reserved_preference_metadata():
    """Generic mapping iteration cannot treat preference metadata as an identity."""
    mapping = {"primary": 42, PREFERRED_SERVER_FIELD: "13521"}

    assert list(iter_server_mapping_entries(mapping)) == [("primary", 42)]
    assert _explicit_server_keys(SimpleNamespace(custom_field_data={"librenms_id": mapping})) == {"primary"}


def test_identity_reader_and_writer_reject_reserved_metadata_key():
    """Keyed identity access fails fast before it can read or overwrite metadata."""
    obj = SimpleNamespace(
        cf={"librenms_id": {PREFERRED_SERVER_FIELD: "primary"}},
        custom_field_data={"librenms_id": {PREFERRED_SERVER_FIELD: "primary"}},
    )

    with pytest.raises(ValueError, match="reserved for object metadata"):
        get_librenms_device_id(obj, PREFERRED_SERVER_FIELD, auto_save=False)
    with pytest.raises(ValueError, match="reserved for object metadata"):
        set_librenms_device_id(obj, 42, PREFERRED_SERVER_FIELD)
    assert obj.custom_field_data["librenms_id"] == {PREFERRED_SERVER_FIELD: "primary"}


@pytest.mark.django_db
def test_numeric_preference_metadata_cannot_become_vc_mapping_owner():
    """VC owner discovery ignores metadata even when its malformed value looks like an ID."""
    _chassis, (metadata_member, mapping_owner) = make_virtual_chassis_members("reserved-owner", count=2)
    metadata_member.custom_field_data["librenms_id"] = {PREFERRED_SERVER_FIELD: "13522"}
    metadata_member.save(update_fields=["custom_field_data"])
    mapping_owner.custom_field_data["librenms_id"] = {"primary": 13523}
    mapping_owner.save(update_fields=["custom_field_data"])

    assert get_librenms_sync_device(metadata_member, server_key=None) == mapping_owner

"""Tests for reserved metadata in server-scoped object mappings."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from netbox_librenms_plugin import LibreNMSSyncConfig
from netbox_librenms_plugin.server_mappings import (
    PREFERRED_SERVER_FIELD,
    assign_own,
    get_librenms_sync_device,
    mapped_device_servers,
    read_mapping,
)
from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis_members
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping, seed_stored_mapping, stored_mapping_for_test


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


@pytest.mark.parametrize("server_key", [" primary ", "dc__west", "contains"])
def test_unsafe_server_key_is_rejected_in_server_configuration(server_key):
    """Configured keys must preserve identity and remain one JSON path component."""
    config = SimpleNamespace(name="netbox_librenms_plugin")

    with pytest.raises(ImproperlyConfigured, match="invalid"):
        LibreNMSSyncConfig._validate_multi_server_config(
            config,
            {
                server_key: {
                    "librenms_url": "https://librenms.example.com",
                    "api_token": "test-token",
                }
            },
        )


@pytest.mark.django_db
def test_mapping_servers_exclude_reserved_preference_metadata():
    """Server enumeration cannot treat preference metadata as an identity."""
    mapping = {"primary": 42, PREFERRED_SERVER_FIELD: "13521"}
    device = make_device("reserved-metadata-servers", librenms_cf=mapping)

    assert [(entry.server, entry.own_id) for entry in read_mapping(device).servers] == [("primary", 42)]
    assert mapped_device_servers(device) == ("primary",)


def test_identity_reader_and_writer_reject_reserved_metadata_key():
    """Keyed identity access fails fast before it can read or overwrite metadata."""
    from dcim.models import Device

    obj = seed_stored_mapping(Device(), {PREFERRED_SERVER_FIELD: "primary"})

    with pytest.raises(ValueError, match="reserved for object metadata"):
        read_mapping(obj).own_id(PREFERRED_SERVER_FIELD)
    with pytest.raises(ValueError, match="reserved for object metadata"):
        assign_own(obj, PREFERRED_SERVER_FIELD, 42)
    assert stored_mapping_for_test(obj) == {PREFERRED_SERVER_FIELD: "primary"}


@pytest.mark.django_db
def test_numeric_preference_metadata_cannot_become_vc_mapping_owner():
    """VC owner discovery ignores metadata even when its malformed value looks like an ID."""
    _chassis, (metadata_member, mapping_owner) = make_virtual_chassis_members("reserved-owner", count=2)
    seed_stored_mapping(metadata_member, {PREFERRED_SERVER_FIELD: "13522"}, save=True)
    seed_mapping(mapping_owner, "primary", own=13523)

    assert get_librenms_sync_device(metadata_member, server_key=None) == mapping_owner

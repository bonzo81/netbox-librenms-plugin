"""A bay mapping result must fit the module type: the Juniper MX Routing Engine and CB bay mappings."""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_bay, map_device_to_librenms
from netbox_librenms_plugin.tests.test_inventory_class_admission import _inventory, _recording, _refresh
from netbox_librenms_plugin.tests.test_modules_view import _two_server_keys
from netbox_librenms_plugin.utils import netbox_constrains_module_bay_types

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        not netbox_constrains_module_bay_types(), reason="NetBox constrains module bays by type from 4.7"
    ),
]

RE_ROWS = (111, 112)


@pytest.fixture
def server_key(settings, librenms_server):
    return _two_server_keys(settings, librenms_server, "bay-mapping-fit")[0]


def _juniper():
    from dcim.models import Manufacturer

    return Manufacturer.objects.get_or_create(name="Juniper", slug="juniper")[0]


def _mx_recording():
    """Return the MX480 recording with the public Routing Engine labels the mappings read put back."""
    recording = _recording("junos-mx-scb")
    for slot, index in enumerate(RE_ROWS):
        row = next(row for row in _inventory(recording) if row["entPhysicalIndex"] == index)
        row["entPhysicalDescr"] = f"Routing Engine {slot}"
    return recording


def _seed_mappings(regex):
    """Seed the class-scoped RE mapping and the classless CB mapping that a peer runs (ids 45 and 46)."""
    from netbox_librenms_plugin.models import ModuleBayMapping

    manufacturer = _juniper()
    if regex:
        pairs = [(r"^Routing Engine (\d+)$", "other", r"RE\1"), (r"^Routing Engine (\d+)$", "", r"CB\1")]
    else:
        pairs = [
            *((f"Routing Engine {slot}", "other", f"RE{slot}") for slot in (0, 1)),
            *((f"Routing Engine {slot}", "", f"CB{slot}") for slot in (0, 1)),
        ]
    for name, entity_class, bay in pairs:
        ModuleBayMapping.objects.create(
            librenms_name=name,
            librenms_class=entity_class,
            netbox_bay_name=bay,
            is_regex=regex,
            manufacturer=manufacturer,
        )


def _setup(tag, recording, server_key, librenms_server, *, re_bay_types, cb_bay_types, regex=True):
    """Return an MX480 without an SCB: CB0 and CB1 only, no RE bay, and a Routing Engine module type."""
    from netbox_librenms_plugin.tests.conftest import make_module_bay_type, make_module_type

    re_row = next(row for row in _inventory(recording) if row["entPhysicalIndex"] == RE_ROWS[0])
    re_type = make_module_type(re_row["entPhysicalModelName"], manufacturer=_juniper())
    re_type.module_bay_types.set([make_module_bay_type(slug) for slug in re_bay_types])
    _seed_mappings(regex)
    librenms_server.load_recording(recording)
    device = make_device_with_module_bays(f"mx-fit-{tag}", [], manufacturer=_juniper())
    for name in ("CB0", "CB1"):
        make_module_bay(device, name, bay_types=cb_bay_types)
    map_device_to_librenms(device, recording["device_id"], server_key=server_key)
    return device


def _re_bays(device, server_key):
    _response, module_sync = _refresh(device, server_key)
    rows = {row["ent_physical_index"]: row for row in module_sync["table"].data}
    return [rows[index]["module_bay"] for index in RE_ROWS]


@pytest.mark.parametrize("regex", [True, False], ids=["regex", "exact"])
@pytest.mark.parametrize(
    ("re_bay_types", "cb_bay_types", "expected"),
    [
        (("mx-re",), ("mx-scb",), ["-", "-"]),
        ((), ("mx-scb",), ["CB0", "CB1"]),
        (("mx-re",), (), ["CB0", "CB1"]),
        (("mx-re",), ("mx-scb", "mx-re"), ["CB0", "CB1"]),
    ],
    ids=["typed-sides-share-nothing", "untyped-re-type", "untyped-cb-bays", "cb-bays-accept-the-re-type"],
)
def test_the_classless_mapping_gives_cb_only_to_a_routing_engine_that_fits(
    librenms_server, server_key, regex, re_bay_types, cb_bay_types, expected
):
    """AC5: with RE<n> absent, the classless CB<n> mapping applies only when NetBox would accept the install."""
    tag = f"{'regex' if regex else 'exact'}-{len(re_bay_types)}-{len(cb_bay_types)}"
    device = _setup(
        tag,
        _mx_recording(),
        server_key,
        librenms_server,
        re_bay_types=re_bay_types,
        cb_bay_types=cb_bay_types,
        regex=regex,
    )

    assert _re_bays(device, server_key) == expected


def test_the_class_scoped_mapping_still_wins_when_its_bay_fits(librenms_server, server_key):
    """RE0 exists and fits, so the class-scoped mapping keeps precedence over the classless CB mapping."""
    device = _setup(
        "re-bay", _mx_recording(), server_key, librenms_server, re_bay_types=("mx-re",), cb_bay_types=("mx-re",)
    )
    make_module_bay(device, "RE0", bay_types=("mx-re",))

    assert _re_bays(device, server_key) == ["RE0", "CB1"]


def test_the_installer_skips_the_bay_that_the_table_refuses(librenms_server, server_key):
    """The installer runs the same matcher, so a typed CB bay never receives the typed Routing Engine."""
    from dcim.models import Module

    from netbox_librenms_plugin.tests.conftest import make_superuser
    from netbox_librenms_plugin.tests.test_inventory_tree import _cached_inventory
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
    from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_snapshot_digest
    from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

    device = _setup(
        "installer", _mx_recording(), server_key, librenms_server, re_bay_types=("mx-re",), cb_bay_types=("mx-scb",)
    )
    _refresh(device, server_key)
    digest = module_inventory_snapshot_digest(list(_cached_inventory(device, server_key).values()))
    request = make_request(
        "post",
        {
            "select": [str(RE_ROWS[0])],
            "server_key": server_key,
            "inventory_binding": module_inventory_binding_token(
                device.pk, server_key, "install_selected", {}, None, digest
            ),
        },
        user=make_superuser(),
        path="/modules/",
    )

    post(InstallSelectedView(), request, pk=device.pk)

    assert not Module.objects.filter(device=device).exists()
    assert any("no matching bay" in text for text in message_texts(request)), message_texts(request)


@pytest.mark.parametrize("entity_class", ["other", ""], ids=["class-scoped", "classless"])
def test_an_exact_mapping_that_does_not_fit_gives_way_to_the_next_one(librenms_server, server_key, entity_class):
    """A Juniper exact mapping to an SCB-only bay is skipped, and the global exact mapping of the same name applies."""
    from netbox_librenms_plugin.models import ModuleBayMapping

    device = _setup(
        f"exact-next-{entity_class or 'none'}",
        _mx_recording(),
        server_key,
        librenms_server,
        re_bay_types=("mx-re",),
        cb_bay_types=("mx-scb",),
        regex=False,
    )
    ModuleBayMapping.objects.all().delete()
    make_module_bay(device, "RE0", bay_types=("mx-re",))
    for manufacturer, bay in ((_juniper(), "CB0"), (None, "RE0")):
        ModuleBayMapping.objects.create(
            librenms_name="Routing Engine 0",
            librenms_class=entity_class,
            netbox_bay_name=bay,
            is_regex=False,
            manufacturer=manufacturer,
        )

    assert _re_bays(device, server_key)[0] == "RE0"

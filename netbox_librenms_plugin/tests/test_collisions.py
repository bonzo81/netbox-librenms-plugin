"""Unit tests for ``netbox_librenms_plugin.import_utils.collisions``."""

from netbox_librenms_plugin.import_utils.collisions import detect_bulk_collisions


class Device:
    """Minimal NetBox Device stand-in for collision tests.

    The class name matters: ``detect_bulk_collisions`` keys collision
    buckets on ``type(obj).__name__`` so a Device and a VirtualMachine
    sharing an integer pk are not falsely grouped. A ``SimpleNamespace``
    would bucket as ``"SimpleNamespace"`` and never match the pk-only
    merge/promote paths, which assume ``"Device"``.
    """

    def __init__(self, pk, name):
        self.pk = pk
        self.name = name


class VirtualMachine:
    """Minimal VirtualMachine stand-in — same shape as Device but different class name."""

    def __init__(self, pk, name):
        self.pk = pk
        self.name = name


def _row(libre_id, hostname, validation):
    return {"device_id": libre_id, "device_name": hostname, "validation": validation}


def test_no_devices_returns_empty():
    assert detect_bulk_collisions([]) == []
    assert detect_bulk_collisions(None) == []


def test_no_overlap_returns_empty():
    devices = [
        _row(1, "alpha", {"existing_device": Device(pk=10, name="nb-alpha")}),
        _row(2, "beta", {"existing_device": Device(pk=11, name="nb-beta")}),
    ]
    assert detect_bulk_collisions(devices) == []


def test_host_and_oob_collide_on_same_nb_device():
    nb_device = Device(pk=42, name="srv-01")
    devices = [
        _row(100, "host-row", {"existing_device": nb_device}),
        _row(101, "oob-row", {"oob_candidate": {"device": nb_device, "type": "idrac"}}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    g = groups[0]
    assert g["nb_device_pk"] == 42
    assert g["nb_device_name"] == "srv-01"
    rows = {r["device_id"]: r for r in g["librenms_rows"]}
    assert rows[100]["role"] == "host"
    assert rows[101]["role"] == "oob"


def test_two_promote_targets_to_same_nb_device_collide():
    nb_device = Device(pk=7, name="core-sw")
    devices = [
        _row(200, "row-a", {"promote_to_host": {"existing_device": nb_device}}),
        _row(201, "row-b", {"promote_to_host": {"existing_device": nb_device}}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    assert {r["device_id"] for r in groups[0]["librenms_rows"]} == {200, 201}


def test_merge_candidates_overlap_with_direct_existing():
    devices = [
        _row(
            300,
            "row-merge",
            {
                "merge_candidates": {
                    "host_named": {"pk": 99, "name": "host-side"},
                    "oob_named": {"pk": 100, "name": "oob-side"},
                }
            },
        ),
        _row(301, "row-direct", {"existing_device": Device(pk=99, name="host-side")}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    assert groups[0]["nb_device_pk"] == 99
    rows = {r["device_id"] for r in groups[0]["librenms_rows"]}
    assert rows == {300, 301}


def test_promote_via_existing_device_field_collides_with_direct_match():
    """promote_to_host carries existing_device; collision fires against a direct existing_device row."""
    nb_device = Device(pk=55, name="shared")
    devices = [
        _row(400, "row-a", {"promote_to_host": {"existing_device": nb_device, "existing_libre_id": 9}}),
        _row(401, "row-b", {"existing_device": nb_device}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    assert groups[0]["nb_device_pk"] == 55
    assert groups[0]["nb_device_name"] == "shared"


def test_three_way_collision():
    nb_device = Device(pk=8, name="busy")
    devices = [
        _row(500, "a", {"existing_device": nb_device}),
        _row(501, "b", {"oob_candidate": {"device": nb_device}}),
        _row(502, "c", {"promote_to_host": {"existing_device": nb_device}}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    assert {r["device_id"] for r in groups[0]["librenms_rows"]} == {500, 501, 502}


def test_same_libre_row_under_multiple_roles_does_not_collide_with_itself():
    """A single LibreNMS row whose validation lists the same NB pk under
    multiple roles must NOT be reported as a collision — collisions
    require at least two *distinct* LibreNMS device_ids."""
    nb_device = Device(pk=12, name="solo")
    devices = [
        _row(
            600,
            "lonely",
            {
                "existing_device": nb_device,
                "oob_candidate": {"device": nb_device},
            },
        ),
    ]
    assert detect_bulk_collisions(devices) == []


def test_invalid_pks_are_skipped():
    devices = [
        _row(700, "a", {"existing_device": Device(pk=None, name="bad")}),
        _row(701, "b", {"existing_device": Device(pk="not-int", name="worse")}),
    ]
    assert detect_bulk_collisions(devices) == []


def test_invalid_libre_id_is_skipped():
    """Rows with non-int device_id are dropped silently."""
    nb_device = Device(pk=20, name="nb")
    devices = [
        _row("not-int", "weird", {"existing_device": nb_device}),
        _row(800, "ok", {"existing_device": nb_device}),
    ]
    # Only one valid row -> no collision.
    assert detect_bulk_collisions(devices) == []


def test_groups_sorted_by_nb_pk_for_stable_render():
    nb_high = Device(pk=999, name="z")
    nb_low = Device(pk=1, name="a")
    devices = [
        _row(10, "x", {"existing_device": nb_high}),
        _row(11, "y", {"existing_device": nb_high}),
        _row(20, "p", {"existing_device": nb_low}),
        _row(21, "q", {"existing_device": nb_low}),
    ]
    groups = detect_bulk_collisions(devices)
    assert [g["nb_device_pk"] for g in groups] == [1, 999]


def test_row_roles_are_joined_when_same_libre_row_targets_pk_via_multiple_paths():
    """When LibreNMS row R touches NB pk P via both 'host' and
    'merge_host_named' (rare but possible during transitional states),
    its row entry in the collision group should list both roles."""
    devices = [
        _row(
            900,
            "dual",
            {
                "existing_device": Device(pk=77, name="shared"),
                "merge_candidates": {
                    "host_named": {"pk": 77, "name": "shared"},
                    "oob_named": {"pk": 78, "name": "other"},
                },
            },
        ),
        _row(901, "neighbour", {"existing_device": Device(pk=77, name="shared")}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    rows = {r["device_id"]: r["role"] for r in groups[0]["librenms_rows"]}
    roles_900 = {role.strip() for role in rows[900].split(",")}
    assert {"host", "merge_host_named"} <= roles_900
    assert rows[901] == "host"


def test_malformed_rows_are_skipped_not_crashed():
    """A single malformed entry (non-dict row, or a non-dict ``validation`` payload) must be
    skipped rather than crashing the whole bulk-confirm flow on ``.get()``. Valid colliding
    rows in the same batch must still be detected. Pre-fix this raised AttributeError."""
    shared = Device(pk=55, name="shared")
    devices = [
        42,  # non-dict row
        None,  # non-dict row
        {"device_id": 700, "device_name": "bad-validation", "validation": "not-a-dict"},  # non-dict validation
        _row(800, "host", {"existing_device": shared}),
        _row(801, "neighbour", {"existing_device": shared}),
    ]
    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    assert groups[0]["nb_device_pk"] == 55
    assert {r["device_id"] for r in groups[0]["librenms_rows"]} == {800, 801}


def test_device_and_vm_with_same_pk_do_not_collide():
    """Device and VirtualMachine sharing a pk must NOT be reported as a collision
    because detect_bulk_collisions keys buckets on (model_name, pk)."""
    devices = [
        _row(1000, "device-row", {"existing_device": Device(pk=42, name="srv")}),
        _row(1001, "vm-row", {"existing_device": VirtualMachine(pk=42, name="srv")}),
    ]
    assert detect_bulk_collisions(devices) == []


def test_collision_payload_carries_model_name_for_link_targeting():
    """Each group must expose nb_model_name so the template links to the right object
    type — a VM collision must not render a dcim:device URL."""
    vm = VirtualMachine(pk=77, name="vm-host")
    groups = detect_bulk_collisions(
        [
            _row(2000, "row-a", {"existing_device": vm}),
            _row(2001, "row-b", {"existing_device": vm}),
        ]
    )
    assert len(groups) == 1
    assert groups[0]["nb_device_pk"] == 77
    assert groups[0]["nb_model_name"] == "VirtualMachine"

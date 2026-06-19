"""Unit tests for ``netbox_librenms_plugin.import_utils.collisions``."""

import pytest

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


@pytest.mark.django_db
def test_real_validator_output_feeds_detector_end_to_end():
    """End-to-end contract test: the REAL ``validate_device_for_import`` output must carry
    the exact keys ``detect_bulk_collisions`` reads.

    The unit tests above hand-build the ``existing_device`` key, and the view-level test
    stubs the validator — so neither would catch the validator renaming/moving that key
    (collision detection would silently never fire while every test stayed green). Here two
    LibreNMS rows resolve to the SAME real NetBox device by hostname, run through the real
    validator (real ORM name-match), and the real detector must report one collision."""
    from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import
    from netbox_librenms_plugin.tests.conftest import make_device

    nb_device = make_device("srv-collide-e2e")

    # Two distinct LibreNMS devices whose name resolves to the one existing NetBox device.
    libre_a = {"device_id": 5001, "sysName": "srv-collide-e2e", "hostname": "srv-collide-e2e"}
    libre_b = {"device_id": 5002, "sysName": "srv-collide-e2e", "hostname": "srv-collide-e2e"}

    devices = []
    for libre in (libre_a, libre_b):
        validation = validate_device_for_import(libre, include_vc_detection=False, use_sysname=True)
        # Sanity: the real validator matched our device by hostname (the key the detector reads).
        assert validation["existing_device"] is not None
        assert validation["existing_device"].pk == nb_device.pk
        devices.append({"device_id": libre["device_id"], "device_name": libre["sysName"], "validation": validation})

    groups = detect_bulk_collisions(devices)
    assert len(groups) == 1
    assert groups[0]["nb_device_pk"] == nb_device.pk
    assert groups[0]["nb_model_name"] == "Device"
    assert {r["device_id"] for r in groups[0]["librenms_rows"]} == {5001, 5002}


def test_collision_template_renders_correct_link_targets_and_escapes():
    """The collision template must actually render: link to the right object type per
    ``nb_model_name`` and auto-escape device-supplied names.

    The view-level test mocks ``render`` (asserts only the template name), so a typo in the
    VM ``{% url %}`` or a lost auto-escape would ship undetected. Render the real template
    and assert the Device group emits a dcim:device URL, the VM group a
    virtualization:virtualmachine URL, and a script-y hostname is HTML-escaped."""
    from django.template.loader import render_to_string
    from django.urls import reverse

    collisions = [
        {
            "nb_device_pk": 42,
            "nb_device_name": "srv-collide",
            "nb_model_name": "Device",
            "librenms_rows": [
                {"device_id": 1, "hostname": "<script>alpha</script>", "role": "host"},
                {"device_id": 2, "hostname": "beta", "role": "oob"},
            ],
        },
        {
            "nb_device_pk": 7,
            "nb_device_name": "vm-collide",
            "nb_model_name": "VirtualMachine",
            "librenms_rows": [
                {"device_id": 3, "hostname": "gamma", "role": "merge_host_named"},
                {"device_id": 4, "hostname": "delta", "role": "merge_oob_named"},
            ],
        },
    ]
    html = render_to_string(
        "netbox_librenms_plugin/htmx/bulk_import_collision.html",
        {"collisions": collisions, "device_count": 2},
    )

    # Each group links to the correct object type — a VM collision must not get a device URL.
    assert reverse("dcim:device", kwargs={"pk": 42}) in html
    assert reverse("virtualization:virtualmachine", kwargs={"pk": 7}) in html
    # The device names / rows render.
    assert "srv-collide" in html
    assert "vm-collide" in html
    assert "beta" in html
    # Device-supplied hostname is auto-escaped (no raw <script> injected into the modal).
    assert "<script>alpha</script>" not in html
    assert "&lt;script&gt;alpha&lt;/script&gt;" in html
    # The "Collision" badge must pair its red fill with a text colour: a bare bg-danger leaves
    # Tabler's muted badge text (grey-on-red), unreadable in both themes.
    assert "badge bg-danger text-white" in html
    assert 'badge bg-danger"' not in html

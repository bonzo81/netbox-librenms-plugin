"""Import-preview disclosure control: an unrestricted match must not name an unviewable object.

The conflict searches in ``validate_device_for_import`` are deliberately unrestricted — a duplicate
the viewer cannot see is still a real conflict that must block the import. Identity therefore has to
be withheld at DISPLAY time, so every test here drives a real view with a constrained grant.
"""

import json
import logging
import os
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_cluster,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

IMPORT_DEVICE_ID = 90210
HIDDEN_SERIAL = "ZZZ-HIDDEN-SERIAL"


def _point_plugin_at(settings, url):
    """Configure one server and return its key, so no test hardcodes an environment's key."""
    configure_librenms_servers(
        settings, {"default": {"librenms_url": url, "api_token": "test-token", "verify_ssl": False}}
    )
    return "default"


def _register_device(librenms_server, **overrides):
    """Register one LibreNMS device on the loopback server and return its payload."""
    payload = {
        "device_id": IMPORT_DEVICE_ID,
        "hostname": "disclosure-import-host.example.net",
        "sysName": "disclosure-import-host.example.net",
        "serial": "",
        "os": "ios",
        "hardware": "",
        "version": "",
        "ip": "",
    }
    payload.update(overrides)
    librenms_server.register(
        f"/api/v0/devices/{IMPORT_DEVICE_ID}",
        {"status": "ok", "devices": [payload]},
        method="GET",
    )
    return payload


def _open_validation_modal(client, server_key):
    """GET the validation-details modal the way the import table's Details button does."""
    return client.get(
        reverse("plugins:netbox_librenms_plugin:device_validation_details", args=[IMPORT_DEVICE_ID]),
        {"server_key": server_key},
        headers={"HX-Request": "true"},
    )


def _viewer_scoped_to(username, device):
    """A real non-superuser whose Device view grant is constrained to *device* alone."""
    from dcim.models import Device

    return make_user_with_perms(username, [("view", Device)], constraints={"pk": device.pk})


@pytest.mark.django_db
def test_a_serial_conflict_owner_outside_the_view_scope_is_not_named(client, librenms_server, settings):
    """The serial search is unrestricted, so naming its owner would disclose an unviewable device."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    visible = make_device("disclosure-import-host.example.net", serial="AAA-VISIBLE")
    hidden_owner = make_device("disclosure-hidden-serial-owner", serial=HIDDEN_SERIAL)
    _register_device(librenms_server, serial=HIDDEN_SERIAL)
    client.force_login(_viewer_scoped_to("disclosure-serial-viewer", visible))

    response = _open_validation_modal(client, server_key)
    body = unescape(response.content.decode())

    assert response.status_code == 200
    # The hostname-matched device is inside the grant, so it is still named.
    assert visible.name in body
    assert hidden_owner.name not in body, "the serial conflict named a device this user may not view"
    assert f"(ID: {hidden_owner.pk})" not in body
    assert "Serial conflict" in body, "the conflict itself must still be reported"


@pytest.mark.django_db
def test_a_visible_serial_conflict_owner_is_still_named(client, librenms_server, settings):
    """Withholding identity must be scoped to what the viewer cannot see, not applied to everyone."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    make_device("disclosure-import-host.example.net", serial="AAA-VISIBLE")
    owner = make_device("disclosure-visible-serial-owner", serial=HIDDEN_SERIAL)
    _register_device(librenms_server, serial=HIDDEN_SERIAL)
    client.force_login(make_superuser("disclosure-serial-superuser"))

    response = _open_validation_modal(client, server_key)
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert owner.name in body
    assert f"(ID: {owner.pk})" in body


@pytest.mark.django_db
def test_a_hostname_matched_device_outside_the_view_scope_is_not_named(client, librenms_server, settings):
    """A hostname match found by an unrestricted search must not be named or linked."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-import-host.example.net")
    elsewhere = make_device("disclosure-unrelated-in-scope")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-hostname-viewer", elsewhere))

    response = _open_validation_modal(client, server_key)
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert reverse("dcim:device", kwargs={"pk": hidden_match.pk}) not in body, "linked an unviewable device"
    assert "outside your view scope" in body, "the blocked state must be explained"
    assert "Ready to Import" not in body


@pytest.mark.django_db
def test_a_primary_ip_matched_device_outside_the_view_scope_is_not_named(client, librenms_server, settings):
    """The management-IP match runs the same unrestricted lookup and needs the same control."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-ip-owner")
    make_ip("10.77.44.5/24", assigned_object=make_interface(hidden_match, "eth0"))
    elsewhere = make_device("disclosure-unrelated-ip-scope")
    _register_device(
        librenms_server,
        hostname="disclosure-ip-nomatch.example.net",
        sysName="disclosure-ip-nomatch.example.net",
        ip="10.77.44.5",
    )
    client.force_login(_viewer_scoped_to("disclosure-ip-viewer", elsewhere))

    response = _open_validation_modal(client, server_key)
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert hidden_match.name not in body, "the IP match named a device this user may not view"
    assert reverse("dcim:device", kwargs={"pk": hidden_match.pk}) not in body
    assert "outside your view scope" in body


@pytest.mark.django_db
def test_the_import_row_does_not_link_an_out_of_scope_match(client, librenms_server, settings):
    """The table row is a second display surface and must be scoped like the modal."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-import-host.example.net")
    elsewhere = make_device("disclosure-unrelated-row-scope")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-row-viewer", elsewhere))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_role_update", args=[IMPORT_DEVICE_ID]),
        {"server_key": server_key},
        headers={"HX-Request": "true"},
    )
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert reverse("dcim:device", kwargs={"pk": hidden_match.pk}) not in body, "row linked an unviewable device"


@pytest.mark.django_db
def test_the_import_row_still_links_a_visible_match(client, librenms_server, settings):
    """Guard the row scoping against over-redaction."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    match = make_device("disclosure-import-host.example.net")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-row-visible-viewer", match))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_role_update", args=[IMPORT_DEVICE_ID]),
        {"server_key": server_key},
        headers={"HX-Request": "true"},
    )
    body = unescape(response.content.decode())

    assert response.status_code == 200
    assert reverse("dcim:device", kwargs={"pk": match.pk}) in body


@pytest.mark.django_db
@pytest.mark.parametrize("owner_is_visible", [False, True])
def test_the_serial_write_guard_names_its_conflict_only_within_the_view_scope(
    client, librenms_server, settings, owner_is_visible, caplog
):
    """The write path runs the same unrestricted serial lookup as the preview and needs the gate too."""
    from dcim.models import Device

    server_key = _point_plugin_at(settings, librenms_server.url)
    target = make_device("disclosure-import-host.example.net", serial="AAA-VISIBLE")
    owner = make_device("disclosure-write-serial-owner", serial=HIDDEN_SERIAL)
    _register_device(librenms_server, serial=HIDDEN_SERIAL)

    constraints = {"pk__in": [target.pk, owner.pk]} if owner_is_visible else {"pk": target.pk}
    viewer = make_user_with_perms(
        f"disclosure-write-viewer-{owner_is_visible}",
        [("view", Device), ("change", Device)],
        constraints=constraints,
    )
    client.force_login(viewer)

    with caplog.at_level(logging.WARNING):
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:device_conflict_action", args=[IMPORT_DEVICE_ID]),
            {
                "action": "update_serial",
                "existing_device_id": target.pk,
                "existing_device_type": "device",
                "server_key": server_key,
            },
            headers={"HX-Request": "true"},
        )
    body = unescape(response.content.decode())

    # The lookup behind this block is unrestricted, so the log must not name its owner either.
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Serial assignment blocked" in logged, "the guard did not run"
    assert owner.name not in logged

    # Either way the duplicate serial blocks the write.
    target.refresh_from_db()
    assert target.serial == "AAA-VISIBLE"
    if owner_is_visible:
        assert owner.name in body
    else:
        assert owner.name not in body, "the write guard named a device this user may not view"
        assert "outside your view scope" in body


@pytest.mark.django_db
def test_a_second_viewers_request_is_unaffected_by_the_first_ones_redaction(client, librenms_server, settings):
    """Redaction must stay per-request, because the row data behind it is cached with no user in its key.

    Scope: this drives the details modal, which re-validates and reads only the shared raw-device
    cache. It does not exercise the validated-row cache the import list uses.
    """
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-import-host.example.net")
    elsewhere = make_device("disclosure-shared-cache-scope")
    _register_device(librenms_server)

    client.force_login(_viewer_scoped_to("disclosure-cache-constrained", elsewhere))
    constrained_body = unescape(_open_validation_modal(client, server_key).content.decode())
    assert reverse("dcim:device", kwargs={"pk": hidden_match.pk}) not in constrained_body

    client.force_login(make_superuser("disclosure-cache-superuser"))
    privileged_body = unescape(_open_validation_modal(client, server_key).content.decode())

    assert hidden_match.name in privileged_body, "the constrained viewer's redaction leaked into the cache"
    assert reverse("dcim:device", kwargs={"pk": hidden_match.pk}) in privileged_body


@pytest.mark.django_db
def test_a_withheld_match_does_not_disclose_its_role(client, librenms_server, settings):
    """Dropping the match is not enough: the validator copies its role into the row as well.

    The modal prints ``validation.device_role.role`` exactly when ``existing_device`` is absent, so
    withholding the object without the rest of the match teardown would newly expose its role.
    """
    from dcim.models import DeviceRole

    server_key = _point_plugin_at(settings, librenms_server.url)
    secret_role = DeviceRole.objects.create(name="Disclosure Secret Role", slug="disclosure-secret-role")
    hidden_match = make_device("disclosure-import-host.example.net")
    hidden_match.role = secret_role
    hidden_match.save()
    elsewhere = make_device("disclosure-unrelated-role-scope")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-role-viewer", elsewhere))

    body = unescape(_open_validation_modal(client, server_key).content.decode())

    # Precondition: the row really did resolve to the hidden match and was withheld.
    assert "outside your view scope" in body
    assert secret_role.name not in body, "the withheld match's role was disclosed"


@pytest.mark.django_db
def test_a_withheld_match_does_not_disclose_its_librenms_linkage(client, librenms_server, settings):
    """The modal must not render the LibreNMS host ID of a withheld hostname match."""
    from netbox_librenms_plugin.utils import set_librenms_device_id

    stale_host_id = 987654
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-import-host.example.net")
    # Linked to a DIFFERENT LibreNMS host than the one being imported, so the row still matches by
    # hostname while carrying a linkage of its own to disclose.
    set_librenms_device_id(hidden_match, stale_host_id, server_key)
    hidden_match.save()
    elsewhere = make_device("disclosure-unrelated-link-scope")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-link-viewer", elsewhere))

    body = unescape(_open_validation_modal(client, server_key).content.decode())

    # Precondition: the row really did resolve to the hidden match and was withheld.
    # The hostname itself is the LibreNMS device being imported, so it legitimately appears; the
    # linkage of the NetBox object behind it is what must not.
    assert "outside your view scope" in body
    assert str(stale_host_id) not in body, "the withheld match's LibreNMS host ID was disclosed"


@pytest.mark.django_db
def test_a_withheld_match_leaves_no_match_state_behind(client, librenms_server, settings):
    """The teardown must demote the match itself, not only the fields today's templates render."""
    from netbox_librenms_plugin.import_utils.disclosure import OUT_OF_SCOPE_MATCH_MESSAGE
    from netbox_librenms_plugin.utils import set_librenms_device_id

    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-import-host.example.net")
    set_librenms_device_id(hidden_match, 987654, server_key)
    hidden_match.save()
    elsewhere = make_device("disclosure-unrelated-teardown-scope")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-teardown-viewer", elsewhere))

    validation = _open_validation_modal(client, server_key).context["validation"]

    # Precondition: the row really did resolve to the hidden match and was withheld.
    assert validation["existing_device"] is None
    assert OUT_OF_SCOPE_MATCH_MESSAGE in validation["warnings"]

    assert validation["existing_match_type"] is None, "the withheld match's match type survived the teardown"
    assert validation["existing_librenms_link"] is None, "the withheld match's linkage survived the teardown"


@pytest.mark.django_db
def test_a_withheld_match_does_not_disclose_that_it_is_a_vm(client, librenms_server, settings):
    """A hostname match to a VM flips the row into VM mode, which the modal shows as a Cluster row."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_vm = make_vm("disclosure-import-host.example.net", cluster=make_cluster("disclosure-secret-cluster"))
    elsewhere = make_device("disclosure-unrelated-vm-scope")
    _register_device(librenms_server)
    client.force_login(_viewer_scoped_to("disclosure-vm-kind-viewer", elsewhere))

    body = unescape(_open_validation_modal(client, server_key).content.decode())

    # Precondition: the row really did resolve to the hidden VM and was withheld. Its NAME is the
    # incoming LibreNMS hostname, which the modal title renders either way, so assert on the link.
    assert "outside your view scope" in body
    assert reverse("virtualization:virtualmachine", kwargs={"pk": hidden_vm.pk}) not in body
    assert "No cluster assigned" not in body, "the VM-only Cluster row revealed the withheld object's kind"


@pytest.mark.django_db
def test_a_withheld_match_still_reports_a_visible_serial_conflict(client, librenms_server, settings):
    """The serial owner is a different object, so its visibility is decided on its own."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    hidden_match = make_device("disclosure-import-host.example.net", serial="AAA-HIDDEN-MATCH")
    visible_owner = make_device("disclosure-visible-conflict-owner", serial=HIDDEN_SERIAL)
    _register_device(librenms_server, serial=HIDDEN_SERIAL)
    client.force_login(_viewer_scoped_to("disclosure-split-scope-viewer", visible_owner))

    body = unescape(_open_validation_modal(client, server_key).content.decode())

    # The match's NAME is the incoming LibreNMS hostname, so assert on its link, not its name.
    assert reverse("dcim:device", kwargs={"pk": hidden_match.pk}) not in body, "the withheld match was linked"
    assert "outside your view scope" in body
    # The conflict owner IS in scope, so withholding the match must not swallow it.
    assert visible_owner.name in body
    assert f"(ID: {visible_owner.pk})" in body


@pytest.mark.django_db
def test_a_change_grant_alone_is_enough_to_be_told_what_the_match_is(client, librenms_server, settings):
    """Deliberate: ``change`` without ``view`` still counts as "may see".

    NetBox's own edit form renders every field of an object the caller may change, so withholding
    its name would hide something the caller can already read. It would also break this plugin's
    conflict actions, which authorize with ``change`` and never ask for ``view``. The grants here
    are constrained to DIFFERENT objects on purpose, so the two actions cannot be conflated.
    """
    from dcim.models import Device

    from netbox_librenms_plugin.tests.view_test_helpers import grant

    server_key = _point_plugin_at(settings, librenms_server.url)
    changeable = make_device("disclosure-import-host.example.net")
    viewable = make_device("disclosure-viewable-elsewhere")
    _register_device(librenms_server)

    viewer = make_user_with_perms("disclosure-split-grant-viewer", [("view", Device)], constraints={"pk": viewable.pk})
    viewer = grant(viewer, "change", Device, constraints={"pk": changeable.pk})
    assert not Device.objects.restrict(viewer, "view").filter(pk=changeable.pk).exists()
    client.force_login(viewer)

    body = unescape(_open_validation_modal(client, server_key).content.decode())

    assert changeable.name in body
    assert "outside your view scope" not in body


@pytest.mark.django_db
def test_a_visible_but_unnamed_match_is_not_withheld(client, librenms_server, settings):
    """A NetBox Device may legitimately have no name, which must not read as "out of scope"."""
    server_key = _point_plugin_at(settings, librenms_server.url)
    unnamed = make_device("disclosure-unnamed-match", serial=HIDDEN_SERIAL)
    unnamed.name = None
    unnamed.save()
    _register_device(
        librenms_server,
        hostname="disclosure-serial-only.example.net",
        sysName="disclosure-serial-only.example.net",
        serial=HIDDEN_SERIAL,
    )
    client.force_login(_viewer_scoped_to("disclosure-unnamed-viewer", unnamed))

    body = unescape(_open_validation_modal(client, server_key).content.decode())

    # The link proves the serial match bound AND that it survived the gate.
    assert reverse("dcim:device", kwargs={"pk": unnamed.pk}) in body
    assert "outside your view scope" not in body


@pytest.mark.django_db
def test_the_shared_device_cache_never_stores_validation(client, librenms_server, settings):
    """That cache key is shared by every viewer, so it must hold no unrestricted match."""
    from dcim.models import Device, DeviceRole
    from django.core.cache import cache

    from netbox_librenms_plugin.import_utils import get_import_device_cache_key

    server_key = _point_plugin_at(settings, librenms_server.url)
    make_device("disclosure-cache-infra")  # seeds the shared site / device type / role
    role = DeviceRole.objects.get(slug="test-role")
    name = "disclosure-fresh-import.example.net"
    _register_device(librenms_server, hostname=name, sysName=name, hardware="TestDT", location="TestSite")
    client.force_login(make_superuser("disclosure-cache-importer"))

    client.post(
        reverse("plugins:netbox_librenms_plugin:bulk_import_devices"),
        {"select": [str(IMPORT_DEVICE_ID)], "server_key": server_key, f"role_{IMPORT_DEVICE_ID}": str(role.pk)},
        headers={"HX-Request": "true"},
    )

    # Precondition: the import really ran, so the re-render path that writes the cache was reached.
    assert Device.objects.filter(name=name).exists(), "the row did not import"
    cached = cache.get(get_import_device_cache_key(IMPORT_DEVICE_ID, server_key))
    assert cached is not None, "the raw device payload was not cached"
    assert "_validation" not in cached


# ---------------------------------------------------------------------------
# Structural guard
# ---------------------------------------------------------------------------
#
# Paired with the end-to-end cases above, per the rule that a structural check is not proof on its
# own: this pins the invariant across the whole package, the cases above pin the runtime effect.
# The checker itself is an opengrep rule; its own behaviour is pinned by the annotated fixtures in
# .opengrep/tests/, run by scripts/opengrep-test.sh.

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPENGREP_NOT_INSTALLED = 127


def test_the_package_names_no_unrestricted_object_in_a_warning():
    """The live invariant: no warning or issue anywhere in the plugin names an unrestricted match."""
    scan = REPOSITORY_ROOT / "scripts" / "opengrep-scan.sh"
    if not scan.exists():
        pytest.skip("opengrep scan script not present")
    result = subprocess.run([str(scan)], capture_output=True, text=True)
    # 127 is the only "could not run" code the script emits; anything else is a real verdict, so a
    # broken scan cannot masquerade as a passing gate.
    if result.returncode == OPENGREP_NOT_INSTALLED:
        pytest.skip("opengrep is not installed; see .opengrep/README.md")
    assert result.returncode == 0, result.stdout or result.stderr


def test_the_rule_test_script_removes_its_staging_directory():
    """The script stages fixture/rule pairs under a temp dir; each run must take it away again."""
    script = REPOSITORY_ROOT / "scripts" / "opengrep-test.sh"
    if not script.exists():
        pytest.skip("opengrep test script not present")
    with tempfile.TemporaryDirectory() as private_tmp:
        result = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            env={**os.environ, "TMPDIR": private_tmp},
        )
        if result.returncode == OPENGREP_NOT_INSTALLED:
            pytest.skip("opengrep is not installed; see .opengrep/README.md")
        # Precondition: the rule-tests really ran, so the staging directory really was created.
        assert result.returncode == 0, result.stdout or result.stderr
        assert list(Path(private_tmp).iterdir()) == [], "the staging directory outlived the run"


def _scan(*args, expect=0):
    """Run the scan script, skipping when opengrep is absent, and return its parsed JSON report."""
    script = REPOSITORY_ROOT / "scripts" / "opengrep-scan.sh"
    if not script.exists():
        pytest.skip("opengrep scan script not present")
    result = subprocess.run([str(script), *map(str, args)], capture_output=True, text=True)
    if result.returncode == OPENGREP_NOT_INSTALLED:
        pytest.skip("opengrep is not installed; see .opengrep/README.md")
    assert result.returncode == expect, result.stdout or result.stderr
    return json.loads(result.stdout)


def _scanned(report):
    return {Path(path).resolve() for path in report["paths"]["scanned"]}


def test_an_option_only_scan_keeps_the_default_targets():
    """`--json` is an option, not a target: passing it must not drop the test tree from the scan."""
    tests = set((REPOSITORY_ROOT / "netbox_librenms_plugin" / "tests").rglob("*.py"))
    # Precondition: there is a test tree to lose in the first place.
    assert tests, "no test files found to scan"
    scanned = _scanned(_scan("--json"))
    assert not tests - scanned, f"the option-only scan omitted {len(tests - scanned)} test files"


def test_an_explicit_target_replaces_the_defaults():
    """A caller that names a target scans that target alone, whichever side of the option it sits."""
    target = sorted((REPOSITORY_ROOT / "netbox_librenms_plugin" / "tests").rglob("*.py"))[0]
    for args in (("--json", target), (target, "--json")):
        assert _scanned(_scan(*args)) == {target}, args


def test_each_rule_applies_to_its_declared_paths(tmp_path):
    """The path scoping is the rules' real boundary, so pin it against a staged tree, not fixtures."""
    http_call = "import requests\nrequests.get(url)\n"
    sources = {
        # Flagged: a direct HTTP call outside the client.
        "netbox_librenms_plugin/worker.py": http_call,
        # Clean: the client itself is the one place allowed to make them.
        "netbox_librenms_plugin/librenms_api.py": http_call,
        # Clean: outside the package entirely.
        "elsewhere/worker.py": http_call,
        # Clean: `requests` here is a parameter, not the library.
        "netbox_librenms_plugin/lookup.py": "def lookup(requests, key):\n    return requests.get(key)\n",
        # Flagged twice: the test-tree rules apply here and the HTTP rule does not.
        "netbox_librenms_plugin/tests/test_worker.py": (
            http_call + "from django.test import TestCase\n"
            "class TestWorker:\n    def check(self):\n        self.assertEqual(1, 1)\n"
        ),
    }
    for name, source in sources.items():
        staged = tmp_path / name
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(source)

    report = _scan("--json", *[tmp_path / name for name in sources], expect=1)
    found = {
        (Path(f["path"]).relative_to(tmp_path).as_posix(), f["check_id"].split(".")[-1]) for f in report["results"]
    }
    assert found == {
        ("netbox_librenms_plugin/worker.py", "no-requests-outside-http-client"),
        ("netbox_librenms_plugin/tests/test_worker.py", "no-django-testcase-in-tests"),
        ("netbox_librenms_plugin/tests/test_worker.py", "no-unittest-assertions"),
    }, found

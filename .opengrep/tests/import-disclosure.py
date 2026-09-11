# Rule-test fixtures for .opengrep/librenms-rules.yaml.
#
# Ported from the checker's own test suite, so the verdicts are the ones that suite pinned.
# A line marked to match must be reported; a line marked clean must not. Run scripts/opengrep-test.sh.
#
# Each case is namespaced with its index because the originals all share the name `probe`.

# --- [exact] same-named methods both get checked
class A:
    def probe(self, result, serial):
        from dcim.models import Device
        owner = Device.objects.filter(serial=serial).first()
        # ruleid: import-disclosure
        result["warnings"].append(f"owner {owner.name}")


class B:
    def probe(self, result):
        from dcim.models import Device
        device = Device.objects.first()
        # ruleid: import-disclosure
        result["warnings"].append(device.serial)


# --- [exact] a nested helper captures an unrestricted object
from dcim.models import Device
def preview_exact01(result):
    device = Device.objects.first()
    def helper():
        # ruleid: import-disclosure
        result["warnings"].append(device.name)
    helper()


# --- [exact] a nested helper shadows a captured object
from dcim.models import Device
def preview_exact02(result):
    device = Device.objects.first()
    def helper(device):
        # ok: import-disclosure
        result["warnings"].append(str(device))
    helper("safe")


# --- [exact] a variadic helper returns the whole tuple
from dcim.models import Device
def preview_exact03(result):
    device = Device.objects.first()
    # ruleid: import-disclosure
    result["warnings"].append(_describe_exact03(device))
def _describe_exact03(*values):
    return str(values)


# --- [exact] a variadic keyword helper returns an untainted element
from dcim.models import Device
def preview_exact04(result):
    device = Device.objects.first()
    # ok: import-disclosure
    result["warnings"].append(_describe_exact04(hidden=device, label="safe"))
def _describe_exact04(**values):
    return str(values["label"])


# --- [exact] a nested helper shadows an identity-returning module helper
from dcim.models import Device
def _describe_exact05(value):
    return value.name
def preview_exact05():
    def _describe_exact05(value):
        return "generic conflict"
    device = Device.objects.first()
    warnings = []
    warnings.append(_describe_exact05(device))
    # ok: import-disclosure
    return {"warnings": warnings}


# --- [exact] aggregate helpers consume identity and starred arguments
from dcim.models import Device
def probe_exact06(result):
    devices = Device.objects.all()
    result["warnings"].append(_count_exact06(devices[0].name))
    # ok: import-disclosure
    result["warnings"].append(_count_args_exact06(*devices))
def _count_exact06(value):
    return len(value)
def _count_args_exact06(*values):
    return len(values)


# --- [exact] a variadic helper returns an untainted element
from dcim.models import Device
def probe_exact07(result):
    device = Device.objects.first()
    # ok: import-disclosure
    result["warnings"].append(_describe_exact07(device, "safe"))
def _describe_exact07(*values):
    return str(values[1])


# --- [exact] a nested same-named function reports each sink once
from dcim.models import Device
def probe_exact08(result):
    def probe_exact08():
        device = Device.objects.first()
        # ruleid: import-disclosure
        result["warnings"].append(device.name)
    probe_exact08()


# --- [flag] identity passed into a pruned local helper
def probe_flag00(result, serial):
    from dcim.models import Device
    device = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"].append(_describe_flag00(device.name))


def _describe_flag00(value):
    return str(value)


# --- [flag] identity passed through a variadic positional helper
def probe_flag01(result, serial):
    from dcim.models import Device
    device = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"].append(_describe_flag01(device))


def _describe_flag01(*values):
    return str(values[0])


# --- [flag] a local helper that returns the unrestricted object itself
def probe_flag03(result):
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {_device_flag03().name}")


def _device_flag03():
    from dcim.models import Device
    return Device.objects.filter(serial="x").first()


# --- [flag] an object-returning helper bound to a local first
def probe_flag04(result):
    owner = _device_flag04()
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {owner.name}")


def _device_flag04():
    from dcim.models import Device
    return Device.objects.filter(serial="x").first()


# --- [flag] a scoped nested filter does not clear the unrestricted outer query
def probe_flag05(result, user):
    from dcim.models import Device, Site
    owner = Device.objects.filter(site=Site.objects.restrict(user).first()).first()
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {owner.name}")


# --- [flag] identity read straight off an inline unrestricted query
def probe_flag06(result, serial):
    from dcim.models import Device
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {Device.objects.filter(serial=serial).first().name}")


# --- [flag] identity returned by a no-argument local helper
def probe_flag07(result):
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {_owner_name_flag07()}")


def _owner_name_flag07():
    from dcim.models import Device
    return Device.objects.filter(serial="x").first().name


# --- [flag] f-string
def probe_flag08(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {owner.name}")


# --- [flag] concatenation
def probe_flag09(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["issues"].append("owner " + owner.name)


# --- [flag] whole-list assignment
def probe_flag10(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"] = [f"{owner.pk}"]


# --- [flag] tuple unpacking
def probe_flag11(result, ip):
    matched, ambiguous, rows = resolve_device_by_host_ip(ip)
    # ruleid: import-disclosure
    result["warnings"].append(f"assigned to {matched.name}")


# --- [flag] ternary rebind
def probe_flag12(result, serial):
    matches = find_devices_by_serial(serial)
    owner = matches[0] if matches else None
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {owner.name}")


# --- [flag] read straight out of the validation dict
def probe_flag13(result):
    # ruleid: import-disclosure
    result["warnings"].append(f"{result['existing_device'].name}")


# --- [flag] built inside a module-private helper
def probe_flag14(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"].extend(_describe_flag14(owner))


def _describe_flag14(matched):
    notes = []
    notes.append(f"matched {matched.name}")
    # ruleid: import-disclosure
    return {"warnings": notes}["warnings"]


# --- [flag] a scoped sibling branch does not clear the unrestricted one
def probe_flag15(result, serial, user):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first() or Device.objects.restrict(user).first()
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {owner.name}")


# --- [flag] a scoped ternary branch does not clear the unrestricted one
def probe_flag16(result, serial, user, any_match):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first() if any_match else Device.objects.restrict(user).first()
    # ruleid: import-disclosure
    result["warnings"].append(f"owner {owner.name}")


# --- [flag] a local message list built by its initializer
def probe_flag17(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    notes = [f"owner {owner.name}"]
    # ruleid: import-disclosure
    return {"warnings": notes}


# --- [flag] an annotated local message list initializer
def probe_flag18(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    notes: list = [f"owner {owner.name}"]
    # ruleid: import-disclosure
    return {"warnings": notes}


# --- [flag] identity handed back by a local helper
def probe_flag19(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"].append(_label_flag19(owner))


def _label_flag19(device):
    return device.name


# --- [flag] transformed on the way in
def probe_flag20(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    # ruleid: import-disclosure
    result["warnings"].append(f"{normalize_serial(owner.serial)}")


# --- [quiet] an aggregate passed into a pruned local helper
def probe_quiet00(result, serial):
    from dcim.models import Device
    devices = Device.objects.filter(serial=serial)
    # ok: import-disclosure
    result["warnings"].append(_describe_quiet00(devices.count()))


def _describe_quiet00(value):
    return str(value)


# --- [quiet] a queryset wrapper passed into an aggregate helper
def probe_quiet01(result, serial):
    from dcim.models import Device
    devices = Device.objects.filter(serial=serial)
    # ok: import-disclosure
    result["warnings"].append(_count_quiet01(devices.all()))


def _count_quiet01(values):
    return len(values)


# --- [quiet] a list wrapper passed into an aggregate helper
def probe_quiet02(result, serial):
    from dcim.models import Device
    devices = Device.objects.filter(serial=serial)
    # ok: import-disclosure
    result["warnings"].append(_count_quiet02(list(devices)))


def _count_quiet02(values):
    return len(values)


# --- [quiet] a helper that returns a scoped object stays quiet
def probe_quiet03(result, user):
    # ok: import-disclosure
    result["warnings"].append(f"in scope {_allowed_quiet03(user).name}")


def _allowed_quiet03(user):
    from dcim.models import Device
    return Device.objects.restrict(user, "view").filter(serial="x").first()


# --- [quiet] an inline scoped query names an object the viewer may see
def probe_quiet04(result, user, serial):
    from dcim.models import Device
    # ok: import-disclosure
    result["warnings"].append(
        f"in scope {Device.objects.restrict(user, 'view').filter(serial=serial).first().name}"
    )


# --- [quiet] a scoped lookup
def probe_quiet05(result, view, serial):
    from dcim.models import Device
    allowed = view.restricted_queryset(Device, "view").filter(serial=serial).first()
    # ok: import-disclosure
    result["warnings"].append(f"in scope: {allowed.name}")


# --- [quiet] a scoped manager call
def probe_quiet06(result, user, serial):
    from dcim.models import Device
    allowed = Device.objects.restrict(user, "view").filter(serial=serial).first()
    # ok: import-disclosure
    result["warnings"].append(f"in scope: {allowed.name}")


# --- [quiet] an aggregate rather than an identity
def probe_quiet07(result, serial):
    from dcim.models import Device
    rows = Device.objects.filter(serial=serial)
    # ok: import-disclosure
    result["warnings"].append(f"{rows.count()} conflicts, {len(rows)} rows")


# --- [quiet] a helper that only aggregates stays pruned at its call site
def probe_quiet08(result, serial):
    from dcim.models import Device
    rows = Device.objects.filter(serial=serial)
    # ok: import-disclosure
    result["warnings"].append(f"{_summarise_quiet08(rows)} conflicts")


def _summarise_quiet08(matches):
    return matches.count()


# --- [quiet] a message that names nothing
def probe_quiet09(result, serial):
    from dcim.models import Device
    owner = Device.objects.filter(serial=serial).first()
    if owner:
        # ok: import-disclosure
        result["warnings"].append(f"serial {serial} is already assigned in NetBox")


# --- [flag] caller-side * unpacking into a module-private helper
#     (the shape the previous AST checker documented as an unfixable limitation)
def probe_unpack_pos(result):
    device = Device.objects.first()
    _warn_pos(result, *[device])


def _warn_pos(result, *devices):
    # ruleid: import-disclosure
    result["warnings"].append(devices[0].name)


# --- [flag] caller-side ** unpacking into a module-private helper
def probe_unpack_kw(result):
    device = Device.objects.first()
    _warn_kw(result, **{"device": device})


def _warn_kw(result, **devices):
    # ruleid: import-disclosure
    result["warnings"].append(devices["device"].name)


# The one shape this rule does not report is a keyword argument read back out of a **kwargs
# dict. See "Known limitation" in .opengrep/README.md; the reverse shape, a caller-side
# **{...} unpacking, is covered by probe_unpack_kw above.

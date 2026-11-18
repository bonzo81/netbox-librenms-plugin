class UpdateModuleSerialView(View):
    def post(self):
        # ruleid: module-writer-claims-released-identities
        assignment_lock([incoming], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        assignment_lock([observed_serial, incoming], model=Module, rules=rules)
        # ruleid: module-writer-claims-released-identities
        lock_module_write(observe_module_write(device_ids=[device_id]), incoming=[incoming], rules=rules)
        # ok: module-writer-claims-released-identities
        lock_module_write(
            observe_module_write(device_ids=[device_id], module_ids=[module_id]), incoming=[incoming], rules=rules
        )


class ReplaceModuleView(View):
    def post(self):
        # ruleid: module-writer-claims-released-identities
        assignment_lock([incoming], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        assignment_lock([incoming, *removed], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        lock_module_write(observe_module_write(device_ids=[device_id], subtree_roots=removals), incoming=[incoming])


class MoveModuleView(View):
    def post(self):
        # ruleid: module-writer-claims-released-identities
        assignment_lock([serial for _pk, serial in moving], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        assignment_lock([serial for _pk, serial in (*moving, *removing)], model=Module, rules=rules)
        # ruleid: module-writer-claims-released-identities
        lock_module_write(observe_module_write(device_ids=[target_device.pk]), rules=rules)
        # ok: module-writer-claims-released-identities
        lock_module_write(
            observe_module_write(device_ids=[target_device.pk], subtree_roots=[conflict_module_id, occupant_id]),
            rules=rules,
        )


class InstallModuleView(View):
    def post(self):
        # ok: module-writer-claims-released-identities
        assignment_lock([incoming], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        lock_module_write(observe_module_write(device_ids=[device_id]), incoming=[incoming], rules=rules)


class ApplyModuleInterfaceTypesView(View):
    def post(self):
        # ruleid: module-writer-keeps-observed-serials
        observe_module_write(device_ids=[device.pk], module_ids=[module.pk])
        # ok: module-writer-keeps-observed-serials
        observe_module_write(device_ids=[device.pk], observed_ids=[module.pk])


class UpdateModuleSerialView(View):
    def update(self):
        # ok: module-writer-keeps-observed-serials
        observe_module_write(device_ids=[device.pk], module_ids=[module.pk])


def read_render_inventory(cache_key):
    cached_payload = cache.get(cache_key)
    cached_inventory = cached_module_inventory(cached_payload)
    if cached_inventory is None:
        # ruleid: module-snapshot-reader-keeps-the-snapshot
        cache.delete(cache_key)
        return None
    return cached_inventory


def get_cached_module_snapshot(cache_key):
    cached_payload = cache.get(cache_key)
    if cached_module_inventory(cached_payload) is None:
        # ruleid: module-snapshot-reader-keeps-the-snapshot
        cache.delete(cache_key)
        return None
    return cached_payload


def build_context(cache_key):
    try:
        return build_table_context()
    except _StaleInventorySnapshot:
        # ruleid: module-snapshot-reader-keeps-the-snapshot
        cache.delete(cache_key)
        return {"table": None}


def refresh_failed(cache_key):
    # ok: module-snapshot-reader-keeps-the-snapshot
    cache.delete(cache_key)


def move_module(queryset, apply):
    with transaction.atomic():
        # ruleid: event-aware-block-inside-plain-atomic
        update_existing_row(queryset, apply)
    with atomic_with_events():
        # ok: event-aware-block-inside-plain-atomic
        update_existing_row(queryset, apply)


def replace_rows(rows, apply):
    with transaction.atomic(savepoint=True):
        # ruleid: event-aware-block-inside-plain-atomic
        with row_savepoint():
            apply(rows)
    with atomic_with_events():
        # ok: event-aware-block-inside-plain-atomic
        with row_savepoint():
            apply(rows)

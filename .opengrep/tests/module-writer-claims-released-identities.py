class UpdateModuleSerialView(View):
    def post(self):
        # ruleid: module-writer-claims-released-identities
        assignment_lock([incoming], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        assignment_lock([observed_serial, incoming], model=Module, rules=rules)


class ReplaceModuleView(View):
    def post(self):
        # ruleid: module-writer-claims-released-identities
        assignment_lock([incoming], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        assignment_lock([incoming, *removed], model=Module, rules=rules)


class MoveModuleView(View):
    def post(self):
        # ruleid: module-writer-claims-released-identities
        assignment_lock([serial for _pk, serial in moving], model=Module, rules=rules)
        # ok: module-writer-claims-released-identities
        assignment_lock([serial for _pk, serial in (*moving, *removing)], model=Module, rules=rules)


class InstallModuleView(View):
    def post(self):
        # ok: module-writer-claims-released-identities
        assignment_lock([incoming], model=Module, rules=rules)


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

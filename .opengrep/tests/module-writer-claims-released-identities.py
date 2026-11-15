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

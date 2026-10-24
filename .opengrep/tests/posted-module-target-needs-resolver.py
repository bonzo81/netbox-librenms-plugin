def resolves(self, request, page_device, devices, grant):
    # ruleid: posted-module-target-needs-resolver
    target, invalid = _resolve_target_device_with_validation(page_device, request.POST.get("selected_device_id"), devices)
    # ruleid: posted-module-target-needs-resolver
    target = devices.filter(pk=request.GET.get("selected_device_id")).first() or page_device
    # ruleid: posted-module-target-needs-resolver
    target_id = int(request.POST["selected_device_id"])
    # ruleid: posted-module-target-needs-resolver
    target_ids = request.POST.getlist("selected_device_id")
    # ruleid: posted-module-target-needs-resolver
    target = self.restrict_object_or_404(Device, pk=self.request.GET.get("selected_device_id", page_device.pk))
    # ok: posted-module-target-needs-resolver
    target = _resolve_destination(page_device, request.POST.get("selected_device_id"), devices)
    # ok: posted-module-target-needs-resolver
    target = _resolve_destination(page_device, request.GET.get("selected_device_id"), devices)
    # ok: posted-module-target-needs-resolver
    granted = grant.require(device_id=request.POST.get("selected_device_id", ""), ent_index=1)
    # ok: posted-module-target-needs-resolver
    override = request.POST.get("device_selection_1")
    # ok: posted-module-target-needs-resolver
    return record.get("selected_device_id")

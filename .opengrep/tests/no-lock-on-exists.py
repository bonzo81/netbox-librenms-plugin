def locks(Device, pk):
    # ruleid: no-lock-on-exists
    Device.objects.select_for_update().filter(pk=pk).exists()
    # ruleid: no-lock-on-exists
    Device.objects.select_for_update().exists()
    # ruleid: no-lock-on-exists
    Device.objects.filter(pk=pk).select_for_update(nowait=True).filter(site=1).order_by("pk").count()
    # ok: no-lock-on-exists
    Device.objects.select_for_update().get(pk=pk)
    # ok: no-lock-on-exists
    Device.objects.filter(pk=pk).exists()
    # ok: no-lock-on-exists
    return list(Device.objects.select_for_update().filter(pk=pk).order_by("pk"))

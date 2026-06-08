# Extension Points

The plugin exposes a small number of public hooks so other NetBox plugins can adjust its behaviour without forking the code. These are intentionally stable — internal helpers may change between releases, but the hooks described here are meant to be relied on.

## Module Interface Name Prediction

When Module Sync adopts a module, it derives the interface names that module *should* have by instantiating the module type's interface templates against the device. Some vendors use naming conventions this can't capture (for example, breakout ports that expand one physical port into several logical interfaces). The `predict_module_interface_names` signal lets another plugin rewrite those names before they are used for matching.

The signal is defined in [signals.py](../../netbox_librenms_plugin/signals.py) and fired from `get_module_template_interface_names()` in [utils.py](../../netbox_librenms_plugin/utils.py). Each receiver is called with keyword arguments `sender` (the `Module` class), `device`, `module`, and `names` (the predicted `list[str]`, already rewritten for the VC member position where applicable). Always accept `**kwargs` so future arguments don't break your receiver.

Return the list of names to use, or `None` to leave the current list unchanged (an empty list is valid and means "no interface names"). When multiple receivers are connected they run in connection order and the **last non-`None` return wins**. The signal is dispatched with Django's `send_robust()`, so a receiver that raises is logged and skipped rather than breaking module adoption.

```python
from django.dispatch import receiver

from netbox_librenms_plugin.signals import predict_module_interface_names


@receiver(predict_module_interface_names)
def expand_breakout_ports(sender, device, module, names, **kwargs):
    """Expand each predicted name into four breakout sub-interfaces."""
    expanded = []
    for name in names:
        expanded.extend(f"{name}/{lane}" for lane in range(1, 5))
    return expanded
```

Connect the receiver from your plugin's `ready()` method so it is registered when NetBox starts.

When a module on a Virtual Chassis member has no template names matching the member-position pattern, the Module Sync table offers a pre-filled report (built by `build_vc_normalization_report()` in [utils.py](../../netbox_librenms_plugin/utils.py)) that a user can copy into a GitHub issue to request support for that naming convention. This signal is the recommended way to add such support yourself without waiting for an upstream change.

"""Plugin extension signals — external plugins may subscribe to alter behavior."""

from django.dispatch import Signal

# Args: device, module, names (list[str]). Receivers may return a rewritten list,
# or None to leave unchanged. Last non-None return wins.
predict_module_interface_names = Signal()

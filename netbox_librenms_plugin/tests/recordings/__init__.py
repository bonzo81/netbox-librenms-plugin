"""Back-compat shim: recordings now live in ``data_shapes/recordings/``.

Only ``manifest.json`` ships in the wheel (see ``[tool.setuptools.package-data]``); the full
recordings remain source/dev-time fixtures, so a wheel install cannot load them.

Re-exports the loader helpers from :mod:`netbox_librenms_plugin.data_shapes.recordings_store` so
existing ``from netbox_librenms_plugin.tests.recordings import ...`` test imports keep working.
"""

from netbox_librenms_plugin.data_shapes.recordings_store import (
    iter_recording_paths,
    iter_recordings,
    load_recording,
)

__all__ = ["iter_recording_paths", "iter_recordings", "load_recording"]

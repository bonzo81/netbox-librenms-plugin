# Views & Inheritance

Views are organized by resource type (e.g., devices, mappings, VMs) in the `views/` directory. The codebase uses a layered approach to views, leveraging inheritance and mixins to maximize code reuse and maintainability.

### View Organization

**Resource-specific views:**

  - `device_views.py`, `vm_views.py`, `mapping_views.py`, etc. contain views for each main resource type.

**Base views:**

  - The `base/` subdirectory contains abstract base views (e.g., `BaseLibreNMSSyncView`, `BaseInterfaceTableView`) that encapsulate shared logic for related resources.

**Mixins:**

  - Shared behaviors (e.g., API access, caching) are factored into mixins in `mixins.py` and combined with base or resource-specific views as needed.

### Inheritance Patterns

- Most resource-specific views inherit from a base view in `base/` and one or more mixins.
- Base views themselves often inherit from NetBox or Django generic views (e.g., `generic.ObjectListView`, `django.views.View`).
- This allows resource-specific views to override or extend only the methods they need, while inheriting default behaviors from base classes and mixins.

#### Example: Device Sync View

```python
from .base.librenms_sync_view import BaseLibreNMSSyncView
from .mixins import LibreNMSAPIMixin

class DeviceLibreNMSSyncView(BaseLibreNMSSyncView):
    # Inherits API access and sync logic from base/mixins
    # Only device-specific logic needs to be implemented here
    ...
```

#### Example: Interface Table View

```python
from .base.interfaces_view import BaseInterfaceTableView
from .mixins import CacheMixin, LibreNMSAPIMixin

class DeviceInterfaceTableView(BaseInterfaceTableView):
    model = Device
    # Implements get_interfaces and get_redirect_url for devices
    ...
```

### Customizing or Adding Views

- To add a new view for a resource, inherit from the relevant base view and mixins, then override or extend methods as needed.
- Use the base views as templates for structure and required methods.
- Register new views in `urls.py` and add templates if needed.

### Tips

- Check the `base/` directory for reusable logic before writing new view code.
- Use mixins for cross-cutting concerns (API, caching, permissions).
- Keep resource-specific views focused on their unique logic; delegate shared logic to base classes and mixins.

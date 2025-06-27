# Mixins

Mixins in `views/mixins.py` provide reusable logic to keep views clean and DRY (Don't Repeat Yourself). They are designed to be combined with Django or NetBox views to add specific behaviors or shared functionality. When adding new views, consider using or extending these mixins to maintain consistency and reduce code duplication.

### Key Mixins

**LibreNMSAPIMixin**

  - Provides a `librenms_api` property for accessing the LibreNMS API from any view.
  - Ensures a single instance of the API client is reused per view instance.
  - Example usage: Add to views that need to fetch or sync data with LibreNMS.

**CacheMixin**

  - Supplies helper methods for generating cache keys related to objects and data types (e.g., ports, links).
  - Useful for views that cache data fetched from LibreNMS to improve performance.
  - Methods:
    - `get_cache_key(obj, data_type="ports")`: Returns a unique cache key for the object and data type.
    - `get_last_fetched_key(obj, data_type="ports")`: Returns a cache key for tracking when data was last fetched.

### How to Use Mixins

To use a mixin, simply add it to the inheritance list of your view class. For example:

```python
from .mixins import LibreNMSAPIMixin, CacheMixin

class MyCustomView(LibreNMSAPIMixin, CacheMixin, SomeBaseView):
    # ... your view logic ...
```

Mixins can be combined as needed. Place mixins before the main base view to ensure their methods and properties are available.


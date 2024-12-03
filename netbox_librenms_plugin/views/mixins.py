from netbox_librenms_plugin.librenms_api import LibreNMSAPI


class LibreNMSAPIMixin:
    """
    A mixin class that provides access to the LibreNMS API.

    This mixin initializes a LibreNMSAPI instance and provides a property
    to access it. It's designed to be used with other view classes that
    need to interact with the LibreNMS API.

    Attributes:
        _librenms_api (LibreNMSAPI): An instance of the LibreNMSAPI class.

    Properties:
        librenms_api (LibreNMSAPI): A property that returns the LibreNMSAPI instance,
                                    creating it if it doesn't exist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._librenms_api = None

    @property
    def librenms_api(self):
        """
        Get or create an instance of LibreNMSAPI.

        This property ensures that only one instance of LibreNMSAPI is created
        and reused for subsequent calls.

        Returns:
            LibreNMSAPI: An instance of the LibreNMSAPI class.
        """
        if self._librenms_api is None:
            self._librenms_api = LibreNMSAPI()
        return self._librenms_api


class CacheMixin:
    """
    A mixin class that provides caching functionality.
    """

    def get_cache_key(self, obj, data_type="ports"):
        """
        Get the cache key for the object.

        Args:
            obj: The object to cache data for
            data_type: Type of data being cached ('ports' or 'links')
        """
        model_name = obj._meta.model_name
        return f"librenms_{data_type}_{model_name}_{obj.pk}"

    def get_last_fetched_key(self, obj, data_type="ports"):
        """
        Get the cache key for the last fetched time of the object.
        """
        model_name = obj._meta.model_name
        return f"librenms_{data_type}_last_fetched_{model_name}_{obj.pk}"

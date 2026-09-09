###################################################################

import os
#  This file serves as a base configuration for testing purposes  #
#  only. It is not intended for production use.                   #
###################################################################

ALLOWED_HOSTS = ["*"]

DATABASE = {
    "NAME": "netbox",
    "USER": "netbox",
    "PASSWORD": "netbox",
    "HOST": "localhost",
    "PORT": "",
    "CONN_MAX_AGE": 300,
}

PLUGINS = [
    "netbox_librenms_plugin",
]

PLUGINS_CONFIG = {
    "netbox_librenms_plugin": {
        "servers": {
            "default": {
                "librenms_url": "https://librenms.example.com",
                "api_token": "test-token-for-testing",
            }
        }
    }
}

REDIS = {
    "tasks": {
        "HOST": os.environ.get("REDIS_HOST", "localhost"),
        "PORT": 6379,
        "PASSWORD": "",
        "DATABASE": int(os.environ.get("REDIS_DATABASE", "0")),
        "SSL": False,
    },
    "caching": {
        "HOST": os.environ.get("REDIS_CACHE_HOST", "localhost"),
        "PORT": 6379,
        "PASSWORD": "",
        "DATABASE": int(os.environ.get("REDIS_CACHE_DATABASE", "1")),
        "SSL": False,
    },
}

SECRET_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
API_TOKEN_PEPPERS = {
    1: "TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE",
}

PLUGINS = ["netbox_librenms_plugin"]

PLUGINS_CONFIG = {
    "netbox_librenms_plugin": {
        "servers": {
            "e2e": {
                "display_name": "End-to-end LibreNMS stub",
                "librenms_url": "http://librenms-stub:8001",
                "api_token": "e2e-stub-token",
                "verify_ssl": False,
            },
        },
    },
}

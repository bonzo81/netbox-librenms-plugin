# Multi-Server LibreNMS Configuration

## Overview

The NetBox LibreNMS plugin now supports multiple LibreNMS servers. This allows you to:

- Configure multiple LibreNMS instances in your NetBox configuration
- Switch between different LibreNMS servers through the web interface
- Maintain backward compatibility with single-server configurations

## Configuration

### Multi-Server Configuration

Update your NetBox `configuration.py` file:

```python
PLUGINS_CONFIG = {
    'netbox_librenms_plugin': {
        'servers': {
            'production': {
                'display_name': 'Production LibreNMS',
                'librenms_url': 'https://librenms-prod.example.com',
                'api_token': 'your_production_token',
                'cache_timeout': 300,
                'verify_ssl': True,
                'interface_name_field': 'ifDescr'
            },
            'testing': {
                'display_name': 'Test LibreNMS',
                'librenms_url': 'https://librenms-test.example.com',
                'api_token': 'your_test_token',
                'cache_timeout': 300,
                'verify_ssl': False,
                'interface_name_field': 'ifName'
            },
            'development': {
                'display_name': 'Dev LibreNMS',
                'librenms_url': 'https://librenms-dev.example.com',
                'api_token': 'your_dev_token',
                'cache_timeout': 180,
                'verify_ssl': False,
                'interface_name_field': 'ifDescr'
            }
        }
    }
}
```

### Legacy Single-Server Configuration (Backward Compatible)

The original configuration format is still supported:

```python
PLUGINS_CONFIG = {
    'netbox_librenms_plugin': {
        'librenms_url': 'https://your-librenms-instance.com',
        'api_token': 'your_librenms_api_token',
        'cache_timeout': 300,
        'verify_ssl': True,
        'interface_name_field': 'ifDescr'
    }
}
```

## Usage

1. Navigate to **LibreNMS Plugin** > **Settings** > **Server Settings**
2. Select your desired LibreNMS server from the dropdown
3. Click **Save Settings**

All subsequent LibreNMS operations will use the selected server.

## Configuration Options

Each server configuration supports the following options:

- `display_name`: Human-readable name for the server (optional)
- `librenms_url`: URL of the LibreNMS instance (required)
- `api_token`: API token for authentication (required)
- `cache_timeout`: Cache timeout in seconds (optional, default: 300)
- `verify_ssl`: Whether to verify SSL certificates (optional, default: True)
- `interface_name_field`: LibreNMS field for interface names (optional, default: 'ifDescr')

## Migration from Single to Multi-Server

1. Add the `servers` configuration block to your `configuration.py`
2. Move your existing single-server configuration into a server block (e.g., 'default' or 'production')
3. Restart NetBox
4. Select your server in the plugin settings

The plugin will automatically detect and use the new configuration format.

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

This server is the installation default. Import uses its own transient server selection. An object sync page uses only servers that have a mapping in the object's `librenms_id` field.

### Import server selection

The Device Import page starts on the installation default. Use the server selector on that page to search another configured server for the current workflow. This transient selection does not change Plugin Settings.

Changing the import server clears the current filters and results. The **Clear** action also clears filters and results, but it keeps the active server. Searches, cached results, background jobs, validation actions, and imports stay on that server until you choose another one.

### Replacing an identity on the active server

An import link or update action never replaces a different host ID that the object already stores for the active server. It is blocked, and a separate confirmation offers the replacement. Confirming changes only that server's host ID and keeps its out-of-band link, every other server mapping, and the preferred server. See [Validation](../librenms_import/validation.md#replacing-a-librenms-identity).

### Object sync server selection

The LibreNMS Connections card uses separate indicators for the active server and the preferred server:

- The check mark identifies the active server for the current page.
- The filled star identifies the object's preferred server.
- The server selector changes the active server for the current page. It does not change the preference.
- An authorized user can select an outline star to change the preference. This action keeps the current active server and sync tab.

The preference controls are available only when the object has more than one usable mapping. One usable mapping is implicit and does not store a preference.

Without a transient selection, the page uses a valid object preference first. If no valid preference exists, it uses the installation default only when the object has a mapping for that server. Otherwise, the page asks the user to select a mapped server.

The page shows a warning when a preference is missing or invalid. A preference is invalid when it is malformed, has no object mapping, or names a server that is not configured and usable. Loading the page does not repair or remove the stored value.

## Configuration Options

Each server configuration supports the following options:

- `display_name`: Human-readable name for the server (optional)
- `librenms_url`: URL of the LibreNMS instance (required)
- `api_token`: API token for authentication (required)
- `cache_timeout`: Cache timeout in seconds (optional, default: 300)
- `verify_ssl`: Whether to verify SSL certificates (optional, default: True)
- `interface_name_field`: LibreNMS field for interface names (optional, default: 'ifDescr')

The `_preferred_server` key is reserved for object metadata. Do not use it as a configured server key. `LibreNMSSyncConfig` rejects a configuration that uses this key during plugin startup.

## Migration from Single to Multi-Server

1. Add the `servers` configuration block to your `configuration.py`
2. Move your existing single-server configuration into a server block (e.g., 'default' or 'production')
3. Restart NetBox
4. Select your server in the plugin settings

The plugin will automatically detect and use the new configuration format.

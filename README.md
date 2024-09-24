# NetBox Librenms Plugin

This plugin provides a way to integrate data from Librenms into NetBox. 

## Features

Sync interface data from Librenms to NetBox. The following data is synced:
- Interfaces Name
- Interface Description
- Interface Status
- Interface Type
- Interface Speed

Set custom mappings for interface type to ensure that the correct interface type is used when syncing.

## Contributing
Contributions are welcome! Please [contribute](CONTRIBUTING.md) if you can. All contributions are welcome.

## Compatibility

| NetBox Version | Plugin Version |
|----------------|----------------|
|     4.0        |      0.1.0     |

## Installing

Netbox 4.0+ is required.

Activate your virtual environment and install the plugin:

```bash
source /opt/netbox/venv/bin/activate
```
While this is still in development and not yet on pypi you can install with pip:

```bash
(venv) $ pip install git+https://github.com/bonzo81/netbox-librenms-plugin
```

Add to your `local_requirements.txt to ensurere it is automatically reinstalled durintg future upgrades:

```bash
# echo "git+https://github.com/bonzo81/netbox-librenms-plugin" >> /opt/netbox/netbox/local_requirements.txt
```

## Docker

For adding to a NetBox Docker setup see
[the general instructions for using netbox-docker with plugins](https://github.com/netbox-community/netbox-docker/wiki/Using-Netbox-Plugins).



` or `plugin_requirements.txt` (netbox-docker):

```bash
git+https://github.com//netbox-librenms-plugin
```

Enable the plugin in `/opt/netbox/netbox/netbox/configuration.py`,
 or if you use netbox-docker, your `/configuration/plugins.py` file :

```python
PLUGINS = [
    'netbox-librenms-plugin'
]

PLUGINS_CONFIG = {
    'netbox_librenms_plugin': {
        'librenms_url': 'https://your-librenms-instance.com',
        'api_token': 'your_librenms_api_token',
        'cache_timeout': 300,
    }
}
```

## Credits

Based on the NetBox plugin tutorial and docs:

- [demo repository](https://github.com/netbox-community/netbox-plugin-demo)
- [tutorial](https://github.com/netbox-community/netbox-plugin-tutorial)
- [docs](https://netboxlabs.com/docs/netbox/en/stable/plugins/development/)

This package was created with [Cookiecutter](https://github.com/audreyr/cookiecutter) and the [`netbox-community/cookiecutter-netbox-plugin`](https://github.com/netbox-community/cookiecutter-netbox-plugin) project template.

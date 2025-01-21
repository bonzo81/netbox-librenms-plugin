# NetBox LibreNMS Plugin

The NetBox LibreNMS Plugin enables integration between NetBox and LibreNMS, allowing you to leverage data from both systems. NetBox remains the Source of Truth (SoT) for you network, but 
this plugin allows you to easily onboard device objects from existing data in LibreNMS. The plugin does not automatically create objects in NetBox to ensure only verified data is used to populate NetBox. 

This is in early development.

## Features

The plugin offers the following key features:

### Interface Sync
Pull interface data from Devices and Virtual Machines from LibreNMS into NetBox. The following interface attributes are synchronized:

- Name
- Description
- Status (Enabled/Disabled)
- Type (with [custom mapping support](docs/usage_tips/interface_mappings.md))
- Speed 
- MTU 
- MAC Address

> Set custom mappings for interface types to ensure that the correct interface type is used when syncing from LibreNMS to NetBox.

### Cable Sync
Create cable connection in NetBox from LibreNMS links data.

### IP Address Sync
Create IP address in NetBox from LibreNMS device IP data.

### Add device to LibreNMS from Netbox

- Add device to LibreNMS from Netbox device page. SNMP v2c and v3 are supported.

### Site & Location Synchronization
The plugin also supports synchronizing NetBox Sites with LibreNMS locations:
- Compare NetBox sites to LibreNMS location data
- Create LibreNMS locations to match NetBox sites
- Update existing LibreNMS locations langitude and longitude values based on NetBox data
- Sync device site to LibreNMS location


## Screenshots/GIFs
>Screenshots from older plugin version
#### Site & Location Sync
![Site Location Sync](docs/img/Netbox-librenms-plugin-Sites.gif)

#### Sync devices and Interfaces
![Add device and interfaces](docs/img/Netbox-librenms-plugin-interfaceadd.gif)

#### Virtual Chassis Member Select
![Virtual Chassis Member Selection](docs/img/Netbox-librenms-plugin-virtualchassis.gif)

#### Interface Type Mappings
![Interfaces Type Mappings](docs/img/Netbox-librenms-plugin-mappings.png)



## Contributing
There's more to do! Coding is not my day job. Bugs will exist and imporvements will be needed. Contributions are very welcome!  I've got more ideas for new features and imporvements but please [contribute](docs/contributing.md) if you can!

Or just share your ideas for the plugin over in [discussions](https://github.com/bonzo81/netbox-librenms-plugin/discussions ).

## Compatibility

| NetBox Version | Plugin Version |
|----------------|----------------|
|     4.1        | 0.2.x - 0.3.5  |
|     4.2        | 0.3.6          |
## Installing


### Standard Installation

Activate your virtual environment and install the plugin:

```bash
source /opt/netbox/venv/bin/activate
```
Install with pip:

```bash
(venv) $ pip install netbox-librenms-plugin
```

Add to your `local_requirements.txt` to ensure it is automatically reinstalled during future upgrades.

```bash
 "netbox-librenms-plugin" >> /opt/netbox/local_requirements.txt
```

### Docker

For adding to a NetBox Docker setup see how to create a custom Docker image.
[the general instructions for using netbox-docker with plugins](https://github.com/netbox-community/netbox-docker/wiki/Using-Netbox-Plugins).

Add the plugin to `plugin_requirements.txt` (netbox-docker):

```bash
# plugin_requirements.txt
netbox-librenms-plugin
```

## Configuration

### 1. Enable the Plugin
Enable the plugin in `/opt/netbox/netbox/netbox/configuration.py`,
 or if you use netbox-docker, your `/configuration/plugins.py` file :

```python
PLUGINS = [
    'netbox_librenms_plugin'
]

PLUGINS_CONFIG = {
    'netbox_librenms_plugin': {
        'librenms_url': 'https://your-librenms-instance.com',
        'api_token': 'your_librenms_api_token',
        'cache_timeout': 300,
        'verify_ssl': True, # Optional: Change to False if needed,
        'interface_name_field': 'ifDescr', # Optional: LibreNMS field used for interface name. ifName used as default
    }
}
```

### 2. Apply Database Migrations

Apply database migrations with Netbox `manage.py`:

```
(venv) $ python manage.py migrate
```

### 3. Collect Static Files

The plugin includes static files that need to be collected by NetBox. Run the following command to collect static files:

```
(venv) $ python manage.py collectstatic --no-input
```

### 4. Restart Netbox

Restart the Netbox service to apply changes:

```
sudo systemctl restart netbox
```

### 5. Custom Field
It is recommended (but not essential) to add a custom field `librenms_id` to the Device, Virtual Machine and Interface models in NetBox. Use the following settings:

- **Object Types:** 
    - Check **dcim > device**
    - Check **virtualization > virtual machine**
    - Check **dcim > interface**
    - Check **virtualization > interfaces (optional)**
- **Name:** `librenms_id`
- **Label:** `LibreNMS ID`
- **Description:** (Optional) Add a description like "LibreNMS ID for LibreNMS Plugin".
- **Type:** Integer
- **Required:** Leave unchecked.
- **Default Value:** Leave blank.

For more info check out [custom field docs](docs/usage_tips/custom_field.md)

## Update

```
source /opt/netbox/venv/bin/activate
pip install -U netbox-librenms-plugin
python manage.py migrate
python manage.py collectstatic --no-input
systemctl restart netbox
```

## Uninstall

See [the instructions for uninstalling plugins](https://netboxlabs.com/docs/netbox/en/stable/plugins/removal/).

## Credits

Based on the NetBox plugin tutorial and docs:

- [demo repository](https://github.com/netbox-community/netbox-plugin-demo)
- [tutorial](https://github.com/netbox-community/netbox-plugin-tutorial)
- [docs](https://netboxlabs.com/docs/netbox/en/stable/plugins/development/)

This package was created with [Cookiecutter](https://github.com/audreyr/cookiecutter). Thanks to the [`netbox-community/cookiecutter-netbox-plugin`](https://github.com/netbox-community/cookiecutter-netbox-plugin) for the project template. 

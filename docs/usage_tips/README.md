# Usage Tips

## Initial Setup

1. [Configure Custom Field](custom_field.md)
    - Set up the `librenms_id` custom field for optimal device matching
    - This ensures reliable device identification between NetBox and LibreNMS

2. [Configure Interface Mappings](interface_mappings.md)
    - Review and set up interface type mappings before synchronization
    - Create specific mappings for your network equipment types
    - Pay attention to speed-based mappings for accurate interface types

## Device Synchronization

### Devices
1. Ensure devices have either:
    - Primary IP configured
    - Valid DNS name (set on the Primary IP)
    - hostname (that matches LibreNMS hostname)
2. The plugin will populate the `librenms_id` custom field if the device is found in LibreNMS

### Virtual Chassis
1. Master or first member with primary IP is used for LibreNMS Sync
2. When possible chassis member position should match interface names 
    
    *e.g. swtich 1 = eth1/0/1, switch 2 = eth2/0/1*

3. Verify member selection before bulk synchronization

## Interface Management

1.  Verify Before Sync
    - Review interface mappings indicated by the link icons
    - Check speed and type matches
    - Confirm member assignments for virtual chassis
2. Exlude columns to exclude from interface sync
    - Sync only the values you want to sync

## Cable Management

1. Preparation
    - Ensure devices are properly identified in both systems
    - Open LibreNMS Sync on all devices to populate librenms_id custom field
    - Remote Device and Remote interface need to be found in NetBox for cable creation to work
    - Check Device and Interface naming

## Best Practices

1. Regular Maintenance
    - Periodically review and update interface mappings
    - Keep custom fields current


## Optimization
    - DNS lookup time can slow response of the API call to LibreNMS

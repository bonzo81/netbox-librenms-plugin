# Using the `librenms_id` Custom Field

## Overview

To enhance device identification and synchronization between NetBox and LibreNMS, this plugin supports using a custom field `librenms_id` on Device, Virtual Machine and Interface objects. While the plugin works without it, using this custom field is recommended for LibreNMS API lookups, and to assist with matching the remote device and remote interfaces for cable creation in Netbox. It can also be entered manually if no primary IP or FQDN is available.

!!! info "Automatic Creation"
    As of version 0.4.2, the plugin **automatically creates** the `librenms_id` custom field when migrations are run. You no longer need to create it manually. The field is created for Device, Virtual Machine, Interface, and VM Interface objects.

For the Device and Virtual Machine objects the plugin will automatically populate the LibreNMS ID custom field when opening the LibreNMS Sync page if the device has been found in LibreNMS.

For the Interface object, the plugin will automatically populate the LibreNMS ID custom field when the interface data is synced from LibreNMS.

## Benefits of Using `librenms_id`

- **Improved Device Matching:** Ensures accurate matching between NetBox and LibreNMS devices.
- **Fallback Identification:** Useful when devices lack a primary IP or FQDN.
- **Efficient Synchronization:** Enhances the reliability of API lookups.
- **Cable creation:** Allows better device identification for the creation of cables between NetBox devices.

## Manual Custom Field Setup

!!! note
    On 0.4.3+, the `librenms_id` custom field is created automatically by the plugin's `post_migrate` hook. Run `manage.py migrate` to trigger this. Manual recreation below is only a fallback troubleshooting step if the field was not created automatically (e.g., due to a failed migration or an existing field of an incompatible type).

If the field was not created automatically (fallback): follow these steps to create the `librenms_id` custom field in NetBox:

1. **Navigate to Custom Fields:**

    - Go to **Customization** in the NetBox sidebar.
    - Click on **Custom Fields**.

2. **Add a New Custom Field:**

    - Click the **Add a custom field** button.

3. **Configure the Custom Field:**

    - **Object Types:**
        - Check **dcim > device**
        - Check **virtualization > virtual machine**
        - Check **dcim > interface**
        - Check **virtualization > interfaces (optional)**
    - **Name:** `librenms_id`
    - **Label:** `LibreNMS ID`
    - **Description:** (Optional) Add a description like "LibreNMS Device ID for synchronization".
    - **Type:** JSON (object) — stores a per-server mapping.
      - Multi-server example:
        ```json
        {"production": 42, "staging": 17}
        ```
      - Legacy single-server example (integer) — read-only/deprecated; do not use for new entries:
        ```
        42
        ```
        > Note: to create new entries manually use the JSON format shown above.
    - **Required:** Leave unchecked (optional).
    - **Default Value:** Leave blank.


4. **Save the Custom Field:**

    - Click **Create** to save the custom field.



### Manually assign a value to `librenms_id`

You can manually assign a value to the `librenms_id` custom field for a device using the following steps:

1. **Edit the Device:**

    - Navigate to the device in NetBox.
    - Click the **Edit** button.

2. **Set the LibreNMS ID:**

    - Scroll to the **Custom Fields** section.
    - Enter the `librenms_id` value as a JSON object with your server key(s):
      ```json
      {"production": 42}
      ```
      For multiple servers: `{"production": 42, "staging": 17}`

3. **Save Changes:**

    - Click **Update** to save the device.




## Notes

- If `librenms_id` is set, the plugin will prioritize it over other identification methods.
- Ensure the `librenms_id` corresponds to the correct device ID in LibreNMS to prevent mismatches.
- The custom field is optional but recommended for optimal plugin performance.
- Using the custom field on interfaces will greatly improve the interface matching required for cable synchronization.

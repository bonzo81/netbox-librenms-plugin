# Using the `librenms_id` Custom Field

## Overview

To enhance device identification and synchronization between NetBox and LibreNMS, this plugin supports using a custom field `librenms_id` on Device and Virtual Machine objects. While the plugin works without it, using this custom field is recommended for LibreNMS API lookups, and to assist with matching the remote device and remote interface for cable creation in Netbox. It can also be entered manually if no primary IP or FQDN is available.

The plugin will automatically populate this field with the LibreNMS ID when opening the LibreNMS Sync page if the device has been found in LibreNMS.

## Benefits of Using `librenms_id`

- **Improved Device Matching:** Ensures accurate matching between NetBox and LibreNMS devices.
- **Fallback Identification:** Useful when devices lack a primary IP or FQDN.
- **Efficient Synchronization:** Enhances the reliability of API lookups.
- **Cable creation:** Allows better device identification for the creation of cables between NetBox devices.

## Suggested Custom Field Setup

Follow these steps to create the `librenms_id` custom field in NetBox:

1. **Navigate to Custom Fields:**

    - Go to **Customization** in the NetBox sidebar.
    - Click on **Custom Fields**.

2. **Add a New Custom Field:**

    - Click the **Add a custom field** button.

3. **Configure the Custom Field:**

    - **Object Types:** 
        - Check **dcim > device**
        - Check **virtualization > virtual machine**
    - **Name:** `librenms_id`
    - **Label:** `LibreNMS ID`
    - **Description:** (Optional) Add a description like "LibreNMS Device ID for synchronization".
    - **Type:** Integer
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
    - Enter the LibreNMS device ID in the `librenms_id` field.

3. **Save Changes:**

    - Click **Update** to save the device.




## Notes

- If `librenms_id` is set, the plugin will prioritize it over other identification methods.
- Ensure the `librenms_id` corresponds to the correct device ID in LibreNMS to prevent mismatches.
- The custom field is optional but recommended for optimal plugin performance.

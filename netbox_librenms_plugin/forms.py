# forms.py
from dcim.models import Device, DeviceRole, DeviceType, Location, Rack, Site
from django import forms
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from utilities.forms.fields import DynamicModelMultipleChoiceField
from virtualization.models import Cluster, VirtualMachine

from .models import InterfaceTypeMapping


class InterfaceTypeMappingForm(NetBoxModelForm):
    """
    Form for creating and editing interface type mappings between LibreNMS and NetBox.
    Allows mapping of LibreNMS interface types and speeds to NetBox interface types.
    """

    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type"]


class InterfaceTypeMappingFilterForm(NetBoxModelForm):
    """
    Form for filtering interface type mappings based on LibreNMS and NetBox attributes.
    Provides filtering options for LibreNMS type, speed, and NetBox type.
    """

    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type"]


class AddToLIbreSNMPV2(forms.Form):
    """
    Form for adding devices to LibreNMS using SNMPv2 authentication.
    Collects hostname/IP and SNMP community string information.
    """

    hostname = forms.CharField(
        label="Hostname/IP",
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"id": "id_hostname_v2"}),
    )
    snmp_version = forms.CharField(
        widget=forms.HiddenInput(attrs={"id": "id_snmp_version_v2"})
    )
    community = forms.CharField(label="SNMP Community", max_length=255, required=True)


class AddToLIbreSNMPV3(forms.Form):
    """
    Form for adding devices to LibreNMS using SNMPv3 authentication.
    Provides comprehensive SNMPv3 configuration options including authentication and encryption settings.
    """

    hostname = forms.CharField(
        label="Hostname/IP",
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"id": "id_hostname_v3"}),
    )
    snmp_version = forms.CharField(
        widget=forms.HiddenInput(attrs={"id": "id_snmp_version_v3"}), initial="v3"
    )
    authlevel = forms.ChoiceField(
        label="Auth Level",
        choices=[
            ("noAuthNoPriv", "noAuthNoPriv"),
            ("authNoPriv", "authNoPriv"),
            ("authPriv", "authPriv"),
        ],
        required=True,
    )
    authname = forms.CharField(label="Auth Username", max_length=255, required=True)
    authpass = forms.CharField(
        label="Auth Password",
        max_length=255,
        required=True,
        widget=forms.PasswordInput(render_value=True),
    )
    authalgo = forms.ChoiceField(
        label="Auth Algorithm",
        choices=[
            ("SHA", "SHA"),
            ("MD5", "MD5"),
            ("SHA-224", "SHA-224"),
            ("SHA-256", "SHA-256"),
            ("SHA-384", "SHA-384"),
            ("SHA-512", "SHA-512"),
        ],
        required=True,
    )
    cryptopass = forms.CharField(
        label="Crypto Password",
        max_length=255,
        required=True,
        widget=forms.PasswordInput(render_value=True),
    )
    cryptoalgo = forms.ChoiceField(
        label="Crypto Algorithm",
        choices=[("AES", "AES"), ("DES", "DES")],
        required=True,
    )


class DeviceStatusFilterForm(NetBoxModelFilterSetForm):
    """
    Form for filtering device status information in NetBox.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the form and remove the filter_id field if it exists."""
        super().__init__(*args, **kwargs)
        # Remove the saved filter field if it exists
        if "filter_id" in self.fields:
            del self.fields["filter_id"]

    device = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False
    )
    site = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False)
    location = DynamicModelMultipleChoiceField(
        queryset=Location.objects.all(), required=False
    )
    rack = DynamicModelMultipleChoiceField(queryset=Rack.objects.all(), required=False)
    device_type = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False
    )
    role = DynamicModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(), required=False
    )

    model = Device


class VirtualMachineStatusFilterForm(NetBoxModelFilterSetForm):
    """
    Form for filtering virtual machine status information in NetBox.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the form and remove the filter_id field if it exists."""
        super().__init__(*args, **kwargs)
        # Remove the saved filter field if it exists
        if "filter_id" in self.fields:
            del self.fields["filter_id"]

    virtualmachine = DynamicModelMultipleChoiceField(
        queryset=VirtualMachine.objects.all(), required=False
    )
    site = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False)
    cluster = DynamicModelMultipleChoiceField(
        queryset=Cluster.objects.all(), required=False
    )

    model = VirtualMachine

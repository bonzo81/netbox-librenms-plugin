# forms.py
from netbox.forms import NetBoxModelForm
from django import forms

from .models import InterfaceTypeMapping


class InterfaceTypeMappingForm(NetBoxModelForm):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type"]


class InterfaceTypeMappingFilterForm(NetBoxModelForm):
    class Meta:
        model = InterfaceTypeMapping
        fields = ["librenms_type", "librenms_speed", "netbox_type"]


class AddToLIbreSNMPV2(forms.Form):
    hostname = forms.CharField(label="Hostname/IP", max_length=255, required=True)
    snmp_version = forms.CharField(widget=forms.HiddenInput(), initial="v2c")
    community = forms.CharField(label="SNMP Community", max_length=255, required=True)


class AddToLIbreSNMPV3(forms.Form):
    hostname = forms.CharField(label="Hostname/IP", max_length=255, required=True)
    snmp_version = forms.CharField(widget=forms.HiddenInput(), initial="v3")
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

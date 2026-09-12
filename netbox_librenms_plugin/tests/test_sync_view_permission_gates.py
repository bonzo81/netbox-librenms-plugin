"""Runtime sync POST gates. Mixin contract tests live in test_permissions.py."""

import pytest


def pytest_generate_tests(metafunc):
    if "sync_case" not in metafunc.fixturenames:
        return

    import importlib
    import inspect
    import pkgutil

    from django.views import View

    from netbox_librenms_plugin.views import sync

    cases = []
    for module_info in pkgutil.walk_packages(sync.__path__, prefix=f"{sync.__name__}."):
        module = importlib.import_module(module_info.name)
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, View) or not hasattr(cls, "post"):
                continue
            # The relationship base can deny before it needs subclass attributes. Cover it too.
            variants = [("device", {})]
            if name in {
                "SyncInterfacesView",
                "DeleteNetBoxInterfacesView",
                "SyncInterfaceParentView",
                "SyncIPAddressesView",
                "AddDeviceToLibreNMSView",
                "RemoveServerMappingView",
                "SetPreferredServerView",
                "ConvertLegacyLibreNMSIdView",
            }:
                variants.append(("virtualmachine", {}))
            if name == "AddBayTemplateView":
                variants = [
                    ("device", {"target_kind": "device_type"}),
                    ("device", {"target_kind": "module_type"}),
                    ("device", {"mode": "map_existing"}),
                ]
            if name == "CreateAndAssignPlatformView":
                variants.append(("device", {"platform_name": "Existing platform", "manufacturer": "existing"}))
            if name == "SyncVLANsView":
                variants.append(("device", {"select": ["100"], "vlan_group_100": "existing"}))
            for object_type, data in variants:
                suffix = "-".join(f"{key}={value}" for key, value in data.items())
                cases.append(
                    pytest.param(
                        (cls, object_type, data),
                        id=f"{module_info.name.rsplit('.', 1)[-1]}.{name}-{object_type}-{suffix}",
                    )
                )
    assert cases, "No sync POST views discovered"
    metafunc.parametrize("sync_case", cases)


def _domain_state():
    from django.apps import apps

    labels = {"dcim", "ipam", "virtualization", "netbox_librenms_plugin", "extras", "core"}
    return {
        model._meta.label: list(model._base_manager.order_by("pk").values())
        for model in apps.get_models(include_auto_created=True)
        if model._meta.app_label in labels and model._meta.managed and not model._meta.proxy
    }


def _post_without_writes(view, request, **kwargs):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.tests.view_test_helpers import post

    before = _domain_state()
    with CaptureQueriesContext(connection) as queries:
        try:
            response = post(view, request, **kwargs)
        finally:
            after = _domain_state()
            changed = [table for table in before if before[table] != after[table]]
            assert changed == [], f"Denied POST changed tables: {changed}"
    writes = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}
    ]
    assert writes == [], "Denied POST issued write SQL"
    return response


def _assert_object_denial(view, request, response, *, htmx, expected_missing=None):
    import json

    from django.http import JsonResponse
    from django.urls import get_script_prefix
    from utilities.permissions import get_permission_for_model

    from netbox_librenms_plugin.tests.view_test_helpers import message_texts

    required = view.required_object_permissions.get("POST", [])
    missing = [
        get_permission_for_model(model, action)
        for action, model in required
        if not request.user.has_perm(get_permission_for_model(model, action))
    ]
    assert len(missing) > 0, "Case must lack a required object permission"
    if expected_missing is not None:
        assert missing == expected_missing
    message = f"Missing permissions: {', '.join(missing)}"
    if isinstance(response, JsonResponse):
        assert response.status_code == 403
        assert json.loads(response.content) == {"error": message}
    else:
        assert response.status_code == (200 if htmx else 302)
        assert response["HX-Redirect" if htmx else "Location"] == get_script_prefix()
        assert message_texts(request) == [message]


@pytest.mark.django_db
class TestSyncPostPermissionGates:
    @pytest.mark.parametrize("htmx", [False, True], ids=["normal", "htmx"])
    def test_missing_object_permissions_refuse_before_work(self, sync_case, htmx):
        import inspect

        from dcim.models import Platform
        from ipam.models import VLANGroup
        from utilities.permissions import get_permission_for_model

        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN
        from netbox_librenms_plugin.tests.conftest import make_cluster, make_device, make_interface, make_ip, make_vm
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view_class, object_type, variant = sync_case
        device = make_device("permission-device", serial="UNCHANGED")
        interface = make_interface(device, "eth0")
        ip = make_ip("198.18.0.1/24", assigned_object=interface)
        owner = (
            make_vm("permission-vm", make_cluster("permission-cluster")) if object_type == "virtualmachine" else device
        )
        data = {"object_type": object_type, **variant}
        if data.get("manufacturer") == "existing":
            Platform.objects.create(name=data["platform_name"], slug="existing-platform")
            data["manufacturer"] = str(device.device_type.manufacturer_id)
        if data.get("vlan_group_100") == "existing":
            data["vlan_group_100"] = str(
                VLANGroup.objects.create(name="Permission VLAN group", slug="permission-vlans").pk
            )
        pk = owner.pk
        if view_class.__name__ == "MoveInterfaceToWinnerView":
            pk = interface.pk
        elif view_class.__name__ == "MoveIPAddressToWinnerView":
            pk = ip.pk
        arguments = {"pk": pk, "object_id": owner.pk, "object_type": object_type, "ip_kind": "primary4"}
        parameters = inspect.signature(view_class.post).parameters
        kwargs = {key: value for key, value in arguments.items() if key in parameters}
        unknown = [
            key
            for key, parameter in parameters.items()
            if key not in {"self", "request", *kwargs}
            and parameter.default is inspect.Parameter.empty
            and parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        ]
        assert unknown == [], "Supply real route arguments for the new sync POST"
        user = make_user_with_perms("permission-reader", [])
        assert not user.is_superuser
        assert user.has_perm(PERM_CHANGE_PLUGIN)
        request = make_request(data=data, user=user, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
        view = view_class()

        response = _post_without_writes(view, request, **kwargs)

        _assert_object_denial(view, request, response, htmx=htmx)

        required = tuple(dict.fromkeys(view.required_object_permissions.get("POST", [])))
        assert required, "Sync POST must declare at least one object permission"
        for index, omitted in enumerate(required):
            granted = [requirement for requirement in required if requirement != omitted]
            user = make_user_with_perms(f"permission-omission-{index}", granted)
            assert all(user.has_perm(get_permission_for_model(model, action)) for action, model in granted)
            request = make_request(data=data, user=user, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
            view = view_class()

            response = _post_without_writes(view, request, **kwargs)

            action, model = omitted
            missing_permission = get_permission_for_model(model, action)
            assert not user.has_perm(missing_permission)
            _assert_object_denial(
                view,
                request,
                response,
                htmx=htmx,
                expected_missing=[missing_permission],
            )

    @pytest.mark.parametrize("allow_create", [False, True], ids=["missing-add-platform", "permitted-control"])
    def test_platform_creation_with_valid_payload(self, allow_create, settings, librenms_server):
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms, post
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        servers = {
            "permission-test": {"librenms_url": librenms_server.url, "api_token": "test-token", "verify_ssl": False}
        }
        configure_librenms_servers(settings, servers)
        server_key = next(iter(servers))
        device = make_device("platform-permission-device")
        perms = [("change", Device)] + ([("add", Platform)] if allow_create else [])
        user = make_user_with_perms("platform-creator", perms)
        request = make_request(data={"platform_name": "Permission platform", "server_key": server_key}, user=user)
        view = CreateAndAssignPlatformView()
        count = Platform.objects.count()

        if allow_create:
            response = post(view, request, pk=device.pk)
            assert response.status_code == 302
            assert Platform.objects.count() == count + 1
            device.refresh_from_db()
            assert device.platform.name == "Permission platform"
        else:
            response = _post_without_writes(view, request, pk=device.pk)
            _assert_object_denial(view, request, response, htmx=False)
            assert Platform.objects.count() == count
            device.refresh_from_db()
            assert device.platform_id is None
        assert librenms_server.requests == []

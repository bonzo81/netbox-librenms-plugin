"""Permission check helpers for device import operations."""

from django.core.exceptions import PermissionDenied


def check_user_permissions(user, permissions):
    """
    Check if user has all required permissions.

    Args:
        user: The user object to check permissions for
        permissions: List of permission strings (e.g., ['dcim.add_device', 'dcim.add_interface'])

    Returns:
        tuple: (has_all_permissions: bool, missing_permissions: list[str])

    Raises:
        PermissionDenied: If user is None (no user context available)
    """
    if user is None:
        raise PermissionDenied("No user context available for permission check")

    missing = [perm for perm in permissions if not user.has_perm(perm)]
    return (len(missing) == 0, missing)


def require_permissions(user, permissions, action_description="perform this action"):
    """
    Require user has all permissions, raising PermissionDenied if not.

    Args:
        user: The user object to check permissions for
        permissions: List of permission strings
        action_description: Human-readable description for error message

    Raises:
        PermissionDenied: If user lacks any required permission
    """
    has_perms, missing = check_user_permissions(user, permissions)
    if not has_perms:
        missing_str = ", ".join(missing)
        raise PermissionDenied(
            f"You do not have permission to {action_description}. Missing permissions: {missing_str}"
        )

"""
Signed confirmations for replacing a same-server LibreNMS identity.

A replacement is only offered after a normal action is blocked, so the confirmation must carry
the exact decision the user was shown. Signing that decision keeps it stateless and tamper-proof:
the confirming request derives the object, server, action, and both host IDs from the token
instead of trusting the form, so a confirmation cannot be transplanted onto other state.
"""

from dataclasses import asdict, dataclass

from django.core import signing

INTENT_FIELD = "identity_replacement_intent"
INTENT_SALT = "netbox_librenms_plugin.identity_replacement"
INTENT_MAX_AGE_SECONDS = 600


class InvalidIdentityReplacementIntent(ValueError):
    """A submitted confirmation is missing, forged, expired, or malformed."""


@dataclass(frozen=True)
class IdentityReplacementIntent:
    """One user's confirmed decision to replace a host ID on one server."""

    user_pk: int
    object_type: str
    object_pk: int
    server_key: str
    action: str
    force: bool
    current_host_id: int
    proposed_host_id: int


# The signed payload's schema: every accepted field name and the exact type it must carry.
# ``test_the_signed_schema_matches_the_intent_dataclass`` keeps it in step with the dataclass.
INTENT_FIELD_TYPES = {
    "user_pk": int,
    "object_type": str,
    "object_pk": int,
    "server_key": str,
    "action": str,
    "force": bool,
    "current_host_id": int,
    "proposed_host_id": int,
}


def sign_identity_replacement_intent(intent: IdentityReplacementIntent) -> str:
    """Return the signed token for one confirmation offer."""
    return signing.dumps(asdict(intent), salt=INTENT_SALT)


def load_identity_replacement_intent(token) -> IdentityReplacementIntent:
    """
    Return the verified intent carried by *token*.

    Args:
        token: The signed value submitted with the confirmation.

    Returns:
        IdentityReplacementIntent: The decision the user confirmed.

    Raises:
        InvalidIdentityReplacementIntent: If the token is not a current, well-formed signature.
    """
    if not isinstance(token, str) or not token:
        raise InvalidIdentityReplacementIntent("The replacement confirmation is missing.")
    try:
        payload = signing.loads(token, salt=INTENT_SALT, max_age=INTENT_MAX_AGE_SECONDS)
    except signing.SignatureExpired as exc:
        raise InvalidIdentityReplacementIntent(
            "The replacement confirmation has expired. Re-run the action to get a fresh one."
        ) from exc
    except signing.BadSignature as exc:
        raise InvalidIdentityReplacementIntent("The replacement confirmation is not valid.") from exc
    # A token signed by an older version of this plugin can carry a different shape.
    if not isinstance(payload, dict) or set(payload) != set(INTENT_FIELD_TYPES):
        raise InvalidIdentityReplacementIntent("The replacement confirmation is not valid.")
    intent = IdentityReplacementIntent(**payload)
    if not _has_expected_types(intent):
        raise InvalidIdentityReplacementIntent("The replacement confirmation is not valid.")
    return intent


def _has_expected_types(intent: IdentityReplacementIntent) -> bool:
    """Return whether every signed field still carries the type this module writes."""
    # Exact types, not isinstance: bool is a subclass of int, so True must not pass as a pk.
    return all(type(getattr(intent, name)) is expected_type for name, expected_type in INTENT_FIELD_TYPES.items())

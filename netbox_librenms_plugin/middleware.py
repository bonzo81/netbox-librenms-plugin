"""Show a lock conflict that escapes a plugin view as one "try again" answer, not as a 500 page."""

import json
import logging

from django.contrib import messages
from utilities.api import is_api_request

from netbox_librenms_plugin.transactions import CommittedFollowUpError, classify_conflict
from netbox_librenms_plugin.views.mixins import _htmx_error_response, _safe_redirect_response

logger = logging.getLogger(__name__)

PLUGIN_PACKAGE = __name__.partition(".")[0]
TRY_AGAIN_MESSAGE = "Another operation was changing the same NetBox objects. Refresh the page and try again."
FOLLOW_UP_FAILED_MESSAGE = "Changes were saved, but follow-up work failed. Refresh and check the result."
# The sync forms' script listens for this event to give back the form and its selection.
REQUEST_FAILED_EVENT = "librenmsRequestFailed"


def _is_plugin_view(request):
    """Return True when the resolved view of *request* is defined in this plugin."""
    match = request.resolver_match
    if match is None:
        return False
    view = getattr(match.func, "view_class", match.func)
    return view.__module__.partition(".")[0] == PLUGIN_PACKAGE


def _answer(request, message):
    """Return the one visible answer: an htmx toast that swaps nothing, or one message and a redirect."""
    if request.headers.get("HX-Request") == "true":
        response = _htmx_error_response(message)
        response["HX-Trigger"] = json.dumps({REQUEST_FAILED_EVENT: None})
        return response
    messages.error(request, message)
    return _safe_redirect_response(request)


class LockConflictMiddleware:
    """
    Map a lock conflict that escapes a plugin view to a "try again" answer.

    It acts only for views defined in this plugin, and not for API requests. A conflict that
    ``classify_conflict`` accepts becomes the "try again" answer. A ``CommittedFollowUpError``
    whose cause is a conflict becomes the "saved, but follow-up work failed" answer. Every other
    exception propagates unchanged.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Return the answer for a lock conflict in a plugin view, or None to let the exception propagate."""
        if is_api_request(request) or not _is_plugin_view(request):
            return None
        if isinstance(exception, CommittedFollowUpError):
            if not classify_conflict(exception.__cause__):
                return None
            message = FOLLOW_UP_FAILED_MESSAGE
        elif classify_conflict(exception):
            message = TRY_AGAIN_MESSAGE
        else:
            return None
        logger.warning("Lock conflict in %s: %s", request.path, exception, exc_info=exception)
        return _answer(request, message)

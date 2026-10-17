HTMX_HEADER = "HX-Request"


def views(request, response, self):
    # ruleid: no-direct-htmx-request-header-read
    if request.headers.get("HX-Request"):
        pass
    # ruleid: no-direct-htmx-request-header-read
    if request.headers.get("HX-Request") == "true":
        pass
    # ruleid: no-direct-htmx-request-header-read
    if request.headers.get("hx-request", "") != "true":
        pass
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = request.headers["HX-Request"] == "true"
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = "HX-Request" in request.headers
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = request.META.get("HTTP_HX_REQUEST")
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = request.META["HTTP_HX_REQUEST"]
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = "HTTP_HX_REQUEST" not in request.META
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = getattr(getattr(self, "request", None), "headers", {}).get("HX-Request") == "true"
    # ruleid: no-direct-htmx-request-header-read
    is_htmx = request.headers.get(HTMX_HEADER)
    # ok: no-direct-htmx-request-header-read
    if request.htmx:
        pass
    # ok: no-direct-htmx-request-header-read
    boosted = request.htmx.boosted
    # ok: no-direct-htmx-request-header-read
    current = request.headers.get("HX-Current-URL", "")
    # ok: no-direct-htmx-request-header-read
    response["HX-Trigger"] = "{}"
    # ok: no-direct-htmx-request-header-read
    response["Vary"] = "HX-Request"
    return is_htmx, boosted, current

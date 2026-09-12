# Cable picker actions after row verification

## 0. Ratification record

No claims are refuted yet.

## 1. Brief

The cable verification module returns cell HTML in JSON. The browser adapter writes that HTML with
`innerHTML`. The action cell can contain a remote-picker control with HTMX attributes. NetBox loads
HTMX as a module and does not expose a global `htmx` object, so HTML inserted this way has no HTMX
behavior.

The cable action module owns the rendered action. The browser adapter owns application of the verify
result. The seam must let a repainted remote-picker action open the existing modal through NetBox's
HTMX lifecycle without requiring a global HTMX object.

Constraints:

- Keep the verify JSON interface for the other cable cells unless a replacement has lower total
  complexity.
- Keep abort and rollback behavior for overlapping verification requests.
- Keep server-rendered URLs and permission decisions authoritative.
- Send a CSRF token with every HTMX request.
- Do not insert new HTML with active `hx-*` attributes through `innerHTML`.
- Preserve native form submission for the `Sync Cable` action.

Observable acceptance conditions:

- With no global `htmx` object, a picker action inserted by cable verification opens the remote-picker
  modal through an HTMX request.
- The request carries the verified row ID, active server key, and CSRF token.
- Repeated cable fragment swaps do not add duplicate click handlers.
- Existing verification success, failure, abort, and control restoration behavior remains intact.

The mechanical guard is a Playwright regression that loads HTMX in the same module-shaped wrapper as
NetBox, inserts the refreshed action through the real verification flow, clicks it, and observes the
picker request and modal swap.

## 2. Candidate designs

### Design A: fixed loader with reconstructed values

Keep the verification JSON interface. Render picker actions as inert buttons with a server-provided
URL in a data attribute. Put one HTMX-bound loader in the cable pane. A delegated click handler parses
the URL into request values and triggers that loader. The loader owns the HTMX modal swap.

### Design B: event-carried authorized URL

Keep the verification JSON interface. Render picker actions as inert buttons with the complete
server-authorized URL in a data attribute. Put one persistent HTMX-bound loader outside the swappable
cable content. A delegated click handler sends the URL in a custom event. The loader's
`htmx:configRequest` handler validates the event and replaces the request path with that URL. The
existing global handler adds the CSRF header. The loader performs the modal swap through NetBox's HTMX
lifecycle.

Both designs reject a missing loader, URL, or CSRF token before sending a request. Both remove the
inert `htmx.process()` call. Converting the complete verify operation to an HTMX response was rejected
because it expands the transport interface and risks the existing abort and rollback behavior. A full
fragment reload was rejected because it can discard the member selection that produced the verified
row.

## 3. Divergence table

| Decision | Design A | Design B | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Request identity | Parse the server URL and rebuild values | Carry the complete URL in the trigger event | The server already creates the permission-scoped picker URL in `_set_remote_picker_affordance()` | Choose B | The browser does not duplicate the server's URL interface. |
| Per-click state | Mutate loader values before triggering | Keep the URL on the trigger event | HTMX exposes the triggering event and a writable request path during `htmx:configRequest` | Choose B | Concurrent clicks do not share mutable pending values. |
| Loader lifetime | Cable content owns the loader | Cable pane owns the loader outside swapped content | The cable content is replaced by HTMX; the pane persists for ordinary table swaps | Choose B | Refreshed actions reuse one initialized adapter. |
| Dynamic action markup | Plain data button | Plain data button | NetBox has no global HTMX object and does not bind `innerHTML` | Converged | JSON repainting inserts no active HTMX markup. |

## 4. Ratification

Candidate revision r1 uses Design B. Its first implementable increment is one persistent picker loader,
one delegated action interface, inert server-rendered picker controls, and a browser regression through
the real verification flow.

An adversarial review ratified r1. The vendored HTMX passes the triggering event into
`htmx:configRequest`, accepts a replacement request path, and preserves the authorized URL query. The
persistent loader is initialized once and reaches the existing modal settle handler. One delegated
click listener handles replaced controls without listener accumulation. The browser regression must
exercise this complete path with no global `htmx` object.

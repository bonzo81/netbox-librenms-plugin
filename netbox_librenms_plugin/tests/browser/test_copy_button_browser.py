"""Browser-level checks for the copy-to-clipboard fallback."""

from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"

# No clipboard API and no `select` on the target, so wireCopyButton takes the textarea fallback.
PAGE = """
    <button id="copy-btn" data-target="report">Copy</button>
    <pre id="report">payload</pre>
"""

# `delete navigator.clipboard` would be a no-op: clipboard is an accessor on Navigator.prototype.
WIRE = """
    () => {
        Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true});
        window.execCommandCalls = [];
        document.execCommand = (command) => {
            window.execCommandCalls.push(command);
            throw new Error('execCommand unavailable');
        };
        wireCopyButton(document.querySelector('#copy-btn'), 'report', {
            idle: 'Copy', done: 'Copied', err: 'Copy failed',
        });
    }
"""


def test_the_copy_fallback_removes_its_textarea_when_exec_command_throws(page):
    """A throwing execCommand must still leave the page without the throwaway control."""
    page.set_content(PAGE)
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(WIRE)

    assert page.locator("textarea").count() == 0, "the fixture already had a textarea"
    page.click("#copy-btn")

    # The handler reports the failure, and the throwaway textarea is gone either way.
    page.wait_for_function("document.querySelector('#copy-btn').innerHTML === 'Copy failed'")
    assert page.locator("textarea").count() == 0
    # Without this the clipboard path would satisfy every assertion above and never
    # exercise the fallback the test is named for.
    assert page.evaluate("() => window.execCommandCalls") == ["copy"]


def test_the_copy_fallback_runs_when_the_clipboard_api_rejects(page):
    """A rejected Clipboard API call must fall back to execCommand."""
    page.set_content(PAGE)
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        """
        () => {
            Object.defineProperty(navigator, 'clipboard', {
                value: {writeText: () => Promise.reject(new Error('denied'))},
                configurable: true,
            });
            window.execCommandCalls = [];
            document.execCommand = (command) => {
                window.execCommandCalls.push(command);
                return true;
            };
            wireCopyButton(document.querySelector('#copy-btn'), 'report', {
                idle: 'Copy', done: 'Copied', err: 'Copy failed',
            });
        }
        """
    )

    page.click("#copy-btn")

    page.wait_for_function("document.querySelector('#copy-btn').innerHTML === 'Copied'")
    assert page.evaluate("() => window.execCommandCalls") == ["copy"]


def test_capture_download_keeps_the_blob_alive_through_the_click(page):
    """The download can consume the blob before its deferred cleanup runs."""
    from django.template import Context, Engine

    template_path = Path(__file__).parents[2] / "templates/netbox_librenms_plugin/htmx/capture_data_shape.html"
    template = Engine().from_string(template_path.read_text(encoding="utf-8"))
    page.set_content(template.render(Context({"recording_json": '{"shape": 1}'}, use_l10n=False)))
    page.evaluate(
        """() => {
            window.revokedURLs = [];
            const revoke = URL.revokeObjectURL.bind(URL);
            URL.revokeObjectURL = url => {
                window.revokedURLs.push(url);
                revoke(url);
            };
        }"""
    )
    with page.expect_download() as download:
        page.evaluate(
            """() => {
                document.querySelector('#capture-download-btn').click();
                window.revokedDuringClick = window.revokedURLs.length;
            }"""
        )
    assert page.evaluate("window.revokedDuringClick") == 0
    assert Path(download.value.path()).read_text(encoding="utf-8") == '{"shape": 1}'
    page.wait_for_function("window.revokedURLs.length === 1")

from playwright.sync_api import sync_playwright

BASE_URL = "http://[::1]:8000"
USERNAME = "admin"
PASSWORD = "admin"
DEVICE_ID = 19


def login(page):
    page.goto(f"{BASE_URL}/login/")
    page.wait_for_load_state("networkidle")
    page.fill("#id_username", USERNAME)
    page.fill("#id_password", PASSWORD)
    page.click('[type="submit"]')
    page.wait_for_load_state("networkidle")


def cleanup_mappings(page):
    """Delete any JNP10008 device type mappings via API."""
    # Try to delete via the import page or admin
    pass


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = browser.new_context()
    page = ctx.new_page()

    # Collect console logs and network requests
    console_logs = []
    network_requests = []

    page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
    page.on(
        "request",
        lambda req: (
            network_requests.append(f"REQ {req.method} {req.url}")
            if "librenms" in req.url or "role-update" in req.url or "device-type-mapping" in req.url
            else None
        ),
    )
    page.on(
        "response",
        lambda resp: (
            network_requests.append(f"RESP {resp.status} {resp.url}")
            if "librenms" in resp.url or "role-update" in resp.url or "device-type-mapping" in resp.url
            else None
        ),
    )

    login(page)

    # Navigate to import page filtered to device 19's location
    print("Navigating to import page...")
    page.goto(f"{BASE_URL}/plugins/librenms_plugin/librenms-import/?apply_filters=1&librenms_location=1")
    page.wait_for_load_state("networkidle")

    # Check initial state of device row 19
    row = page.query_selector(f"#device-row-{DEVICE_ID}")
    if not row:
        print(f"ERROR: Row device-row-{DEVICE_ID} not found!")
        print("Available rows:", [el.get_attribute("id") for el in page.query_selector_all("tr[id^='device-row-']")])
    else:
        device_ready = row.get_attribute("device-ready")
        print(f"Initial row state: device-ready={device_ready}")
        print(f"Row class: {row.get_attribute('class')}")

    # Click Details button for device 19
    print("\nOpening Details modal for device 19...")
    details_btn = page.query_selector(f"[hx-get*='validation-details/{DEVICE_ID}/']")
    if not details_btn:
        # Try other selectors
        details_btn = page.query_selector(f"#device-row-{DEVICE_ID} button")
    if details_btn:
        details_btn.click()
        page.wait_for_timeout(1000)
        page.wait_for_load_state("networkidle")
    else:
        print("ERROR: Details button not found")
        page.screenshot(path="/tmp/no_details_btn.png")

    # Check if modal is open
    modal_visible = page.is_visible("#htmx-modal")
    print(f"Modal visible: {modal_visible}")

    if modal_visible:
        # Check if device type mapping form is present
        mapping_form = page.query_selector(f"#dt-mapping-form-{DEVICE_ID}")
        print(f"Mapping form present: {mapping_form is not None}")

        if mapping_form:
            # Check if there's a device type search input
            dt_search = page.query_selector(f"#dt-search-{DEVICE_ID}")
            if dt_search:
                print("Typing in device type search...")
                dt_search.fill("JNP10008")
                page.wait_for_timeout(800)
                page.wait_for_load_state("networkidle")

                # Check for autocomplete results
                results = page.query_selector_all(".dt-suggestion")
                if not results:
                    results = page.query_selector_all(f"#dt-results-{DEVICE_ID} *")
                print(f"Search results: {len(results)} items")

                # Click first result
                if results:
                    print(f"First result text: {results[0].text_content()}")
                    results[0].click()
                    page.wait_for_timeout(300)

                    # Submit the mapping form
                    dt_id_input = page.query_selector(f"#dt-id-{DEVICE_ID}")
                    if dt_id_input:
                        dt_id_value = dt_id_input.get_attribute("value")
                        print(f"Device type ID selected: {dt_id_value}")

                    # Submit form
                    submit_btn = mapping_form.query_selector("[type='submit']")
                    if submit_btn:
                        print("\nSubmitting mapping form...")
                        console_logs.clear()
                        network_requests.clear()
                        submit_btn.click()
                        page.wait_for_timeout(2000)

                        # Check console logs
                        print("\nConsole logs after submit:")
                        for log in console_logs:
                            print(f"  {log}")

                        # Check network requests
                        print("\nNetwork requests after submit:")
                        for req in network_requests:
                            print(f"  {req}")

                        # Check row state after mapping
                        row = page.query_selector(f"#device-row-{DEVICE_ID}")
                        if row:
                            device_ready = row.get_attribute("device-ready")
                            print(f"\nRow state AFTER mapping (before role-update): device-ready={device_ready}")

                        # Wait for the setTimeout(50ms) + role-update request
                        page.wait_for_timeout(500)
                        page.wait_for_load_state("networkidle")

                        # Final row state
                        row = page.query_selector(f"#device-row-{DEVICE_ID}")
                        if row:
                            device_ready = row.get_attribute("device-ready")
                            print(f"Row state AFTER role-update: device-ready={device_ready}")

                        page.screenshot(path="/tmp/after_mapping.png")

    # Print all accumulated logs
    print("\n--- All network requests ---")
    for req in network_requests:
        print(f"  {req}")

    browser.close()

print("Done!")

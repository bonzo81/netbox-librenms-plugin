sources = netbox_librenms_plugin

.PHONY: test format lint unittest browser pre-commit clean
test: format lint unittest browser

format:
	ruff format $(sources)
	ruff check --select I --fix $(sources)

lint:
	ruff check $(sources)

unittest:
	pytest netbox_librenms_plugin/tests/ -v --ignore=netbox_librenms_plugin/tests/browser

browser:
	pytest -c netbox_librenms_plugin/tests/browser/pytest.ini netbox_librenms_plugin/tests/browser


pre-commit:
	pre-commit run --all-files

clean:
	rm -rf *.egg-info
	rm -rf .tox dist site

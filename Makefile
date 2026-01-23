sources = netbox_librenms_plugin

.PHONY: test format lint unittest pre-commit clean
test: format lint unittest

format:
	ruff format $(sources)
	ruff check --select I --fix $(sources)

lint:
	ruff check $(sources)

unittest:
	pytest netbox_librenms_plugin/tests/ -v


pre-commit:
	pre-commit run --all-files

clean:
	rm -rf *.egg-info
	rm -rf .tox dist site

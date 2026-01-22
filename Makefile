sources = netbox_librenms_plugin

.PHONY: test format lint unittest pre-commit clean
test: format lint unittest

format:
	ruff format $(sources) tests
	ruff check --select I --fix $(sources) tests

lint:
	ruff check $(sources) tests

unittest:
	pytest tests/ -v

pre-commit:
	pre-commit run --all-files

clean:
	rm -rf *.egg-info
	rm -rf .tox dist site

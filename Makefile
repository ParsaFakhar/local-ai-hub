# Local AI Hub — developer tasks
PYTHON ?= python3
VERSION := $(shell $(PYTHON) -c "import json;print(json.load(open('extension/manifest.json'))['version'])")

.PHONY: help install uninstall test unit extension-test smoke doctor icons lint package xpi zip clean api

help:
	@echo "Local AI Hub $(VERSION)"
	@echo "  make install    register the native host (./install.sh)"
	@echo "  make test       unit tests + extension logic tests"
	@echo "  make smoke      end-to-end test over the native messaging pipe"
	@echo "  make doctor     diagnose the installation"
	@echo "  make lint       syntax-check every Python and JavaScript file"
	@echo "  make package    build dist/local-ai-hub-$(VERSION).zip and the .xpi"
	@echo "  make api        run the experimental local HTTP API"

install:
	./install.sh

uninstall:
	./uninstall.sh

unit:
	$(PYTHON) -m unittest discover -s tests

extension-test:
	node tests/test_extension.js

test: unit extension-test

smoke:
	$(PYTHON) scripts/smoke_test.py

doctor:
	$(PYTHON) scripts/doctor.py

icons:
	$(PYTHON) scripts/make_icons.py

lint:
	@$(PYTHON) -m compileall -q hub native scripts plugins sdk tests >/dev/null && echo "python: ok"
	@for f in $$(find extension sdk/js plugins templates tests -name '*.js'); do node --check "$$f" || exit 1; done && echo "javascript: ok"
	@for f in $$(find . -name '*.json' -not -path './dist/*'); do $(PYTHON) -c "import json,sys;json.load(open(sys.argv[1]))" "$$f" || exit 1; done && echo "json: ok"

package:
	$(PYTHON) scripts/package.py

xpi:
	$(PYTHON) scripts/package.py --xpi-only

zip:
	$(PYTHON) scripts/package.py --zip-only

api:
	./bin/laihub-api

clean:
	rm -rf dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

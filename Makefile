PYTHON ?= python3
JOBS ?= $(shell nproc)

.PHONY: all release prepare verify test help
all: release
release:
	$(PYTHON) scripts/build-tracebox.py --jobs $(JOBS)
prepare:
	$(PYTHON) scripts/build-tracebox.py --prepare-only
verify:
	$(PYTHON) scripts/build-tracebox.py --verify-only
test:
	$(PYTHON) -m unittest discover -s tests -v
help:
	$(PYTHON) scripts/build-tracebox.py --help

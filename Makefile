# VirtConvert&Create — convenience targets.
# Arch-based distributions: use the PKGBUILD instead (makepkg -si).

PYTHON ?= python3

.PHONY: help run install install-user update uninstall test test-all lint screenshots dist clean

help:
	@echo "VirtConvert&Create make targets:"
	@echo "  make run            Start the app from this folder"
	@echo "  make install        Install system-wide (sudo)"
	@echo "  make install-user   Install for your user only"
	@echo "  make update         git pull and reinstall the same way"
	@echo "  make uninstall      Remove the installation (settings are kept)"
	@echo "  make test           Run the test suite"
	@echo "  make lint           Run ruff and check the shell scripts"
	@echo "  make screenshots    Regenerate docs/screenshots"
	@echo "  make dist           Build a wheel and sdist"

run:
	./virtconvert.sh

install:
	./packaging/install.sh

install-user:
	./packaging/install.sh --user

update:
	./packaging/update.sh

uninstall:
	./packaging/uninstall.sh

test:
	QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest -q

lint:
	ruff check virtconvert tests tools
	bash -n packaging/install.sh && bash -n packaging/update.sh && bash -n packaging/uninstall.sh && bash -n virtconvert.sh

screenshots:
	for t in dark light; do $(PYTHON) tools/screenshot.py docs/screenshots --theme $$t; done

dist:
	$(PYTHON) -m build

clean:
	rm -rf build dist ./*.egg-info .pytest_cache .ruff_cache src pkg *.pkg.tar.*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

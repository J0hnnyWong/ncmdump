.PHONY: setup build run clean

VENV := .venv
PYTHON := $(VENV)/bin/python3
BREW := /opt/homebrew/bin/brew

BUILD_DIR := build
CMAKE_FLAGS := -DCMAKE_BUILD_TYPE=Release
NPROC := $(shell sysctl -n hw.ncpu)

setup:
	@echo "=== Installing system dependencies ==="
	$(BREW) install cmake taglib python ffmpeg
	@echo ""
	@echo "=== Creating Python virtual environment ==="
	python3 -m venv $(VENV) --clear --upgrade-deps
	$(PYTHON) -m pip install wxpython cryptography mutagen
	@echo ""
	@echo "=== Setup complete ==="
	@echo "Run 'make run' to start the GUI."

build:
	@mkdir -p $(BUILD_DIR)
	cd $(BUILD_DIR) && cmake $(CMAKE_FLAGS) .. && cmake --build . -j$(NPROC)
	@echo "Build complete: $(BUILD_DIR)/ncmdump"

run: build
	@echo "Starting NCM Dump..."
	$(PYTHON) gui/ncmdump_gui.py

clean:
	rm -rf $(BUILD_DIR) $(VENV)
	@echo "Cleaned."

.PHONY: setup build run gui clean

BUILD_DIR := build
CMAKE_FLAGS := -DCMAKE_BUILD_TYPE=Release
NPROC := $(shell sysctl -n hw.ncpu 2>/dev/null || nproc)

setup:
	@echo "Installing dependencies..."
	brew install cmake taglib
	@echo "Done."

build:
	@mkdir -p $(BUILD_DIR)
	cd $(BUILD_DIR) && cmake $(CMAKE_FLAGS) .. && cmake --build . -j$(NPROC)
	@echo "Build complete: $(BUILD_DIR)/ncmdump"

run: build
	@echo "Running ncmdump..."
	./$(BUILD_DIR)/ncmdump $(ARGS)

gui:
	@echo "Starting GUI..."
	python3 gui/ncmdump_gui.py

clean:
	rm -rf $(BUILD_DIR)
	@echo "Cleaned."

# One-command pendant download + local transcript.
#
#   make sync-and-transcribe ADDRESS=AA:BB:CC:DD:EE:FF
#
# Find ADDRESS with:  pendant scan
# That scan only listens. It does not download.

ADDRESS ?=
LIBRARY ?= $(HOME)/Pendant
MODEL ?= small

.PHONY: sync-and-transcribe transcribe test

sync-and-transcribe:
	@if [ -z "$(ADDRESS)" ]; then \
		echo "Need the pendant's Bluetooth address."; \
		echo "Find it with:  pendant scan"; \
		echo "Then run:      make sync-and-transcribe ADDRESS=AA:BB:CC:DD:EE:FF"; \
		exit 1; \
	fi
	pendant-cli sync-and-transcribe "$(ADDRESS)" --library "$(LIBRARY)" --model "$(MODEL)"

transcribe:
	pendant-cli transcribe "$(LIBRARY)" --model "$(MODEL)"

test:
	python -m pytest

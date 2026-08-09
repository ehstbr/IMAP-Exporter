#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
printf '%s\n' \
    "IMAP Exporter" \
    "Running code from: $SCRIPT_DIR"
exec /usr/bin/python3 "$SCRIPT_DIR/app.py"

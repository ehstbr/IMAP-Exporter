#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
printf '%s\n' \
    "IMAP Exporter 0.4.11" \
    "Código em execução: $SCRIPT_DIR"
exec /usr/bin/python3 "$SCRIPT_DIR/app.py"

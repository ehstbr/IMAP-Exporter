#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(
    sed -n 's/^APP_VERSION = "\([^"]*\)"/\1/p' "$PROJECT_DIR/app.py"
)
OUTPUT_DIR=${1:-"$PROJECT_DIR/dist"}
BUILD_DIR=$(mktemp -d)
PACKAGE_DIR="$BUILD_DIR/imap-exporter"

cleanup() {
    rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT HUP INT TERM

if [ -z "$VERSION" ]; then
    echo "Could not determine the version from app.py." >&2
    exit 1
fi

install -d \
    "$PACKAGE_DIR/DEBIAN" \
    "$PACKAGE_DIR/usr/bin" \
    "$PACKAGE_DIR/usr/lib/imap-exporter/mail_exporter" \
    "$PACKAGE_DIR/usr/lib/imap-exporter/locales" \
    "$PACKAGE_DIR/usr/lib/imap-exporter/assets/icons" \
    "$PACKAGE_DIR/usr/share/applications" \
    "$PACKAGE_DIR/usr/share/icons/hicolor/scalable/apps" \
    "$PACKAGE_DIR/usr/share/metainfo" \
    "$PACKAGE_DIR/usr/share/polkit-1/actions" \
    "$PACKAGE_DIR/usr/share/doc/imap-exporter" \
    "$OUTPUT_DIR"

sed "s/@VERSION@/$VERSION/g" \
    "$PROJECT_DIR/packaging/debian/control.in" \
    > "$PACKAGE_DIR/DEBIAN/control"

install -m 0755 \
    "$PROJECT_DIR/packaging/imap-exporter" \
    "$PACKAGE_DIR/usr/bin/imap-exporter"
install -m 0644 \
    "$PROJECT_DIR/app.py" \
    "$PROJECT_DIR/style.css" \
    "$PROJECT_DIR/providers.json" \
    "$PROJECT_DIR/LICENSE" \
    "$PROJECT_DIR/THIRD_PARTY_NOTICES.pt_BR.md" \
    "$PROJECT_DIR/THIRD_PARTY_NOTICES.en.md" \
    "$PACKAGE_DIR/usr/lib/imap-exporter/"
install -m 0644 \
    "$PROJECT_DIR/mail_exporter/"*.py \
    "$PACKAGE_DIR/usr/lib/imap-exporter/mail_exporter/"
install -m 0644 \
    "$PROJECT_DIR/locales/"*.json \
    "$PACKAGE_DIR/usr/lib/imap-exporter/locales/"
cp -R "$PROJECT_DIR/assets/icons/." \
    "$PACKAGE_DIR/usr/lib/imap-exporter/assets/icons/"

install -m 0644 \
    "$PROJECT_DIR/packaging/io.github.ehstbr.imapexporter.desktop" \
    "$PACKAGE_DIR/usr/share/applications/"
install -m 0644 \
    "$PROJECT_DIR/assets/io.github.ehstbr.imapexporter.svg" \
    "$PACKAGE_DIR/usr/share/icons/hicolor/scalable/apps/"
for size in 16 32 48 64 128 256; do
    install -d "$PACKAGE_DIR/usr/share/icons/hicolor/${size}x${size}/apps"
    install -m 0644 \
        "$PROJECT_DIR/assets/icons/hicolor/${size}x${size}/apps/"\
"io.github.ehstbr.imapexporter.png" \
        "$PACKAGE_DIR/usr/share/icons/hicolor/${size}x${size}/apps/"
done
install -m 0644 \
    "$PROJECT_DIR/packaging/io.github.ehstbr.imapexporter.metainfo.xml" \
    "$PACKAGE_DIR/usr/share/metainfo/"
install -m 0755 \
    "$PROJECT_DIR/packaging/imap-exporter-authorize-delete" \
    "$PACKAGE_DIR/usr/lib/imap-exporter/"
install -m 0644 \
    "$PROJECT_DIR/packaging/io.github.ehstbr.imapexporter.policy" \
    "$PACKAGE_DIR/usr/share/polkit-1/actions/"
install -m 0644 \
    "$PROJECT_DIR/README.md" \
    "$PROJECT_DIR/TERMS.md" \
    "$PROJECT_DIR/LICENSE" \
    "$PROJECT_DIR/THIRD_PARTY_NOTICES.pt_BR.md" \
    "$PROJECT_DIR/THIRD_PARTY_NOTICES.en.md" \
    "$PACKAGE_DIR/usr/share/doc/imap-exporter/"
install -m 0644 \
    "$PROJECT_DIR/packaging/debian/copyright" \
    "$PACKAGE_DIR/usr/share/doc/imap-exporter/copyright"

find "$PACKAGE_DIR/usr/lib/imap-exporter/assets/icons" \
    -type f -exec chmod 0644 {} +
find "$PACKAGE_DIR/usr/lib/imap-exporter/assets/icons" \
    -type d -exec chmod 0755 {} +

dpkg-deb --root-owner-group --build \
    "$PACKAGE_DIR" \
    "$OUTPUT_DIR/imap-exporter_${VERSION}_all.deb"

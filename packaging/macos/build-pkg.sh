#!/bin/sh
set -eu
export COPYFILE_DISABLE=1

usage() {
    printf '%s\n' "usage: $0 VERSION [--binary PATH] [--output-dir DIR]" >&2
    exit 64
}

[ "$#" -ge 1 ] || usage
VERSION=$1
shift
RELEASE_TAG=${EDGEDISCO_RELEASE_TAG:-local}
BUILD_NUMBER=${EDGEDISCO_BUILD_NUMBER:-local}
case "$VERSION" in
    ''|*[!0-9A-Za-z.+-]*|.*|-*|*.) printf 'invalid package version: %s\n' "$VERSION" >&2; exit 64 ;;
esac
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) printf 'invalid package version: %s\n' "$VERSION" >&2; exit 64 ;;
esac

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
REPO=$(CDPATH= cd "$SCRIPT_DIR/../.." && pwd)
BINARY=
OUTPUT_DIR="$REPO/dist"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --binary) [ "$#" -ge 2 ] || usage; BINARY=$2; shift 2 ;;
        --output-dir) [ "$#" -ge 2 ] || usage; OUTPUT_DIR=$2; shift 2 ;;
        *) usage ;;
    esac
done

for tool in cargo pkgbuild productbuild plutil swift; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'required tool not found: %s\n' "$tool" >&2; exit 69; }
done

plutil -lint "$SCRIPT_DIR/launchd/com.edgedisco.daemon.plist" >/dev/null
plutil -lint "$SCRIPT_DIR/launchd/com.edgedisco.agent.plist" >/dev/null
if [ -z "$BINARY" ]; then
    (cd "$REPO" && cargo build --release --bin edgedisco)
    BINARY="$REPO/target/release/edgedisco"
fi
[ -f "$BINARY" ] && [ -x "$BINARY" ] || { printf 'native binary is missing or not executable: %s\n' "$BINARY" >&2; exit 66; }
case "$(file -b "$BINARY")" in
    *Mach-O*executable*) ;;
    *) printf 'native binary is not a Mach-O executable: %s\n' "$BINARY" >&2; exit 65 ;;
esac

SWIFT_PACKAGE="$REPO/macos/EdgeDiscoMenuBar"
(cd "$SWIFT_PACKAGE" && swift build -c release)
SWIFT_BIN_DIR=$(cd "$SWIFT_PACKAGE" && swift build -c release --show-bin-path)
MENU_BAR_BINARY="$SWIFT_BIN_DIR/EdgeDiscoMenuBar"
[ -f "$MENU_BAR_BINARY" ] && [ -x "$MENU_BAR_BINARY" ] || {
    printf 'menu bar binary is missing or not executable: %s\n' "$MENU_BAR_BINARY" >&2
    exit 66
}
case "$(file -b "$MENU_BAR_BINARY")" in
    *Mach-O*executable*) ;;
    *) printf 'menu bar binary is not a Mach-O executable: %s\n' "$MENU_BAR_BINARY" >&2; exit 65 ;;
esac

WORK=$(mktemp -d "${TMPDIR:-/tmp}/edgedisco-pkg.XXXXXX")
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
ROOT="$WORK/root"
COMPONENT="$WORK/EdgeDisco.pkg"
mkdir -p "$ROOT/usr/local/libexec/edgedisco" \
    "$ROOT/Applications/EdgeDisco.app/Contents/MacOS" \
    "$ROOT/Applications/EdgeDisco.app/Contents/Resources" \
    "$ROOT/Library/LaunchDaemons" \
    "$ROOT/Library/LaunchAgents" \
    "$ROOT/Library/Application Support/EdgeDisco/config" \
    "$ROOT/Library/Application Support/EdgeDisco/logs" \
    "$ROOT/Library/Application Support/EdgeDisco/data" \
    "$OUTPUT_DIR"
install -m 0755 "$BINARY" "$ROOT/usr/local/libexec/edgedisco/edgedisco"
install -m 0755 "$MENU_BAR_BINARY" "$ROOT/Applications/EdgeDisco.app/Contents/MacOS/EdgeDiscoMenuBar"
APP_INFO="$ROOT/Applications/EdgeDisco.app/Contents/Info.plist"
plutil -create xml1 "$APP_INFO"
plutil -insert CFBundleDevelopmentRegion -string en "$APP_INFO"
plutil -insert CFBundleExecutable -string EdgeDiscoMenuBar "$APP_INFO"
plutil -insert CFBundleIdentifier -string com.edgedisco.menubar "$APP_INFO"
plutil -insert CFBundleInfoDictionaryVersion -string 6.0 "$APP_INFO"
plutil -insert CFBundleName -string EdgeDisco "$APP_INFO"
plutil -insert CFBundlePackageType -string APPL "$APP_INFO"
plutil -insert CFBundleShortVersionString -string "$VERSION" "$APP_INFO"
plutil -insert CFBundleVersion -string "$VERSION" "$APP_INFO"
plutil -insert EdgeDiscoReleaseTag -string "$RELEASE_TAG" "$APP_INFO"
plutil -insert EdgeDiscoBuildNumber -string "$BUILD_NUMBER" "$APP_INFO"
plutil -insert LSMinimumSystemVersion -string 13.0 "$APP_INFO"
plutil -insert LSUIElement -bool true "$APP_INFO"
plutil -lint "$APP_INFO" >/dev/null
chmod 0644 "$APP_INFO"
install -m 0644 "$SCRIPT_DIR/launchd/com.edgedisco.daemon.plist" "$ROOT/Library/LaunchDaemons/com.edgedisco.daemon.plist"
install -m 0644 "$SCRIPT_DIR/launchd/com.edgedisco.agent.plist" "$ROOT/Library/LaunchAgents/com.edgedisco.agent.plist"
chmod 0755 \
    "$ROOT" \
    "$ROOT/usr" \
    "$ROOT/usr/local" \
    "$ROOT/usr/local/libexec" \
    "$ROOT/usr/local/libexec/edgedisco" \
    "$ROOT/Applications" \
    "$ROOT/Applications/EdgeDisco.app" \
    "$ROOT/Applications/EdgeDisco.app/Contents" \
    "$ROOT/Applications/EdgeDisco.app/Contents/MacOS" \
    "$ROOT/Applications/EdgeDisco.app/Contents/Resources" \
    "$ROOT/Library" \
    "$ROOT/Library/Application Support" \
    "$ROOT/Library/LaunchDaemons" \
    "$ROOT/Library/LaunchAgents"
chmod 0750 "$ROOT/Library/Application Support/EdgeDisco" \
    "$ROOT/Library/Application Support/EdgeDisco/config" \
    "$ROOT/Library/Application Support/EdgeDisco/logs"
chmod 0700 "$ROOT/Library/Application Support/EdgeDisco/data"
if command -v xattr >/dev/null 2>&1; then
    xattr -cr "$ROOT"
fi
chmod 0755 "$SCRIPT_DIR/scripts/preinstall" "$SCRIPT_DIR/scripts/postinstall"

pkgbuild --root "$ROOT" \
    --scripts "$SCRIPT_DIR/scripts" \
    --identifier com.edgedisco.pkg \
    --version "$VERSION" \
    --install-location / \
    --ownership recommended \
    --filter '(^|/)\._.*' \
    "$COMPONENT" >/dev/null
OUTPUT="$OUTPUT_DIR/EdgeDisco-$VERSION-unsigned.pkg"
rm -f "$OUTPUT"
productbuild --package "$COMPONENT" "$OUTPUT" >/dev/null
printf '%s\n' "$OUTPUT"

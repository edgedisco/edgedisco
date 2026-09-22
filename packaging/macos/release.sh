#!/bin/sh
set -eu
export COPYFILE_DISABLE=1

usage() {
    printf '%s\n' "usage: $0 [--plan] EdgeDisco-VERSION-unsigned.pkg" >&2
    exit 64
}

PLAN=0
if [ "${1:-}" = "--plan" ]; then
    PLAN=1
    shift
fi
[ "$#" -eq 1 ] || usage
UNSIGNED=$1
[ -f "$UNSIGNED" ] || { printf 'unsigned package not found: %s\n' "$UNSIGNED" >&2; exit 66; }

: "${DEVELOPER_ID_APPLICATION:?DEVELOPER_ID_APPLICATION is required}"
: "${DEVELOPER_ID_INSTALLER:?DEVELOPER_ID_INSTALLER is required}"
: "${NOTARY_PROFILE:?NOTARY_PROFILE is required}"

name=$(basename "$UNSIGNED")
case "$name" in
    EdgeDisco-*-unsigned.pkg) VERSION=${name#EdgeDisco-}; VERSION=${VERSION%-unsigned.pkg} ;;
    *) printf '%s\n' "input must be named EdgeDisco-VERSION-unsigned.pkg" >&2; exit 64 ;;
esac
case "$VERSION" in
    ''|*[!0-9A-Za-z.+-]*) printf '%s\n' "input contains an invalid version" >&2; exit 64 ;;
esac
SIGNED=$(dirname "$UNSIGNED")/EdgeDisco-$VERSION.pkg
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)

for tool in pkgutil codesign pkgbuild productbuild xcrun file find cmp wc tr xmllint plutil stat lsbom awk; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'required tool not found: %s\n' "$tool" >&2; exit 69; }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/edgedisco-release.XXXXXX")
CANDIDATE=
cleanup() {
    rm -rf "$WORK"
    if [ -n "$CANDIDATE" ]; then
        rm -f "$CANDIDATE"
    fi
}
trap cleanup EXIT HUP INT TERM
pkgutil --expand-full "$UNSIGNED" "$WORK/expanded"
COMPONENT="$WORK/expanded/EdgeDisco.pkg"
PAYLOAD="$COMPONENT/Payload"
SCRIPTS="$COMPONENT/Scripts"
PACKAGE_INFO="$COMPONENT/PackageInfo"

[ -d "$PAYLOAD" ] && [ -d "$SCRIPTS" ] && [ -f "$PACKAGE_INFO" ] || {
    printf '%s\n' "unexpected package structure" >&2
    exit 65
}
package_identifier=$(xmllint --xpath 'string(/pkg-info/@identifier)' "$PACKAGE_INFO")
package_version=$(xmllint --xpath 'string(/pkg-info/@version)' "$PACKAGE_INFO")
[ "$package_identifier" = "com.edgedisco.pkg" ] || {
    printf 'unexpected package identifier: %s\n' "$package_identifier" >&2
    exit 65
}
[ "$package_version" = "$VERSION" ] || {
    printf 'unexpected package version: expected %s, got %s\n' "$VERSION" "$package_version" >&2
    exit 65
}

binary="$PAYLOAD/usr/local/libexec/edgedisco/edgedisco"
app_binary="$PAYLOAD/Applications/EdgeDisco.app/Contents/MacOS/EdgeDiscoMenuBar"
app_info="$PAYLOAD/Applications/EdgeDisco.app/Contents/Info.plist"
daemon_plist="$PAYLOAD/Library/LaunchDaemons/com.edgedisco.daemon.plist"
agent_plist="$PAYLOAD/Library/LaunchAgents/com.edgedisco.agent.plist"
preinstall="$SCRIPTS/preinstall"
postinstall="$SCRIPTS/postinstall"
for required in "$binary" "$app_binary" "$app_info" "$daemon_plist" "$agent_plist" "$preinstall" "$postinstall"; do
    [ -f "$required" ] && [ ! -L "$required" ] || {
        printf 'required release input is missing or unsafe: %s\n' "$required" >&2
        exit 65
    }
done

payload_file_count=$(find "$PAYLOAD" -type f -print | wc -l | tr -d ' ')
payload_dir_count=$(find "$PAYLOAD" -type d -print | wc -l | tr -d ' ')
payload_object_count=$(find "$PAYLOAD" -print | wc -l | tr -d ' ')
script_file_count=$(find "$SCRIPTS" -type f -print | wc -l | tr -d ' ')
script_object_count=$(find "$SCRIPTS" -print | wc -l | tr -d ' ')
links=$(find "$PAYLOAD" "$SCRIPTS" -type l -print)
if [ "$payload_file_count" -ne 5 ] || [ "$payload_dir_count" -ne 18 ] || \
   [ "$payload_object_count" -ne 23 ] || [ "$script_file_count" -ne 2 ] || \
   [ "$script_object_count" -ne 3 ] || [ -n "$links" ]; then
    printf '%s\n' "unexpected payload or scripts in release input" >&2
    exit 65
fi
cmp -s "$daemon_plist" "$SCRIPT_DIR/launchd/com.edgedisco.daemon.plist" || {
    printf '%s\n' "unexpected LaunchDaemon content" >&2
    exit 65
}
cmp -s "$agent_plist" "$SCRIPT_DIR/launchd/com.edgedisco.agent.plist" || {
    printf '%s\n' "unexpected LaunchAgent content" >&2
    exit 65
}
cmp -s "$preinstall" "$SCRIPT_DIR/scripts/preinstall" || {
    printf '%s\n' "unexpected preinstall content" >&2
    exit 65
}
cmp -s "$postinstall" "$SCRIPT_DIR/scripts/postinstall" || {
    printf '%s\n' "unexpected postinstall content" >&2
    exit 65
}
plutil -lint "$app_info" "$daemon_plist" "$agent_plist" >/dev/null
[ "$(plutil -extract CFBundleExecutable raw -o - "$app_info")" = "EdgeDiscoMenuBar" ] || {
    printf '%s\n' "unexpected application executable in Info.plist" >&2
    exit 65
}
[ "$(plutil -extract CFBundleIdentifier raw -o - "$app_info")" = "com.edgedisco.menubar" ] || {
    printf '%s\n' "unexpected application identifier in Info.plist" >&2
    exit 65
}
[ "$(plutil -extract CFBundleVersion raw -o - "$app_info")" = "$VERSION" ] || {
    printf '%s\n' "unexpected application version in Info.plist" >&2
    exit 65
}
[ "$(plutil -extract LSUIElement raw -o - "$app_info")" = "true" ] || {
    printf '%s\n' "LSUIElement must be true in Info.plist" >&2
    exit 65
}
require_mode() {
    expected_mode=$2
    actual_mode=$(stat -f '%Lp' "$1")
    if [ "$actual_mode" != "$expected_mode" ]; then
        printf 'unexpected mode for %s: expected %s, got %s\n' "$1" "$expected_mode" "$actual_mode" >&2
        exit 65
    fi
}
require_bom_root_wheel() {
    relative_path=$1
    actual_owner=$(lsbom -p 'f?' "$COMPONENT/Bom" | awk -F '\t' -v path="./$relative_path" '$1 == path { print $2 }')
    if [ "$actual_owner" != "root/wheel" ]; then
        printf 'unexpected BOM owner for %s: expected root/wheel, got %s\n' "$relative_path" "$actual_owner" >&2
        exit 65
    fi
}
require_mode "$binary" 755
require_mode "$app_binary" 755
require_mode "$app_info" 644
require_mode "$PAYLOAD/Applications" 755
require_mode "$PAYLOAD/Applications/EdgeDisco.app" 755
require_mode "$PAYLOAD/Applications/EdgeDisco.app/Contents" 755
require_mode "$PAYLOAD/Applications/EdgeDisco.app/Contents/MacOS" 755
require_mode "$PAYLOAD/Applications/EdgeDisco.app/Contents/Resources" 755
require_mode "$PAYLOAD/usr" 755
require_mode "$PAYLOAD/usr/local" 755
require_mode "$PAYLOAD/usr/local/libexec" 755
require_mode "$PAYLOAD/usr/local/libexec/edgedisco" 755
require_mode "$PAYLOAD/Library" 755
require_mode "$PAYLOAD/Library/Application Support" 755
require_mode "$PAYLOAD/Library/LaunchDaemons" 755
require_mode "$PAYLOAD/Library/LaunchAgents" 755
require_mode "$daemon_plist" 644
require_mode "$agent_plist" 644
require_mode "$PAYLOAD/Library/Application Support/EdgeDisco" 750
require_mode "$PAYLOAD/Library/Application Support/EdgeDisco/config" 750
require_mode "$PAYLOAD/Library/Application Support/EdgeDisco/logs" 750
require_mode "$PAYLOAD/Library/Application Support/EdgeDisco/data" 700
require_mode "$preinstall" 755
require_mode "$postinstall" 755
for owned_path in \
    "Applications" \
    "Applications/EdgeDisco.app" \
    "Applications/EdgeDisco.app/Contents" \
    "Applications/EdgeDisco.app/Contents/Info.plist" \
    "Applications/EdgeDisco.app/Contents/MacOS" \
    "Applications/EdgeDisco.app/Contents/MacOS/EdgeDiscoMenuBar" \
    "Applications/EdgeDisco.app/Contents/Resources" \
    "usr" \
    "usr/local" \
    "usr/local/libexec" \
    "usr/local/libexec/edgedisco" \
    "usr/local/libexec/edgedisco/edgedisco" \
    "Library" \
    "Library/Application Support" \
    "Library/LaunchDaemons" \
    "Library/LaunchAgents" \
    "Library/LaunchDaemons/com.edgedisco.daemon.plist" \
    "Library/LaunchAgents/com.edgedisco.agent.plist" \
    "Library/Application Support/EdgeDisco" \
    "Library/Application Support/EdgeDisco/config" \
    "Library/Application Support/EdgeDisco/logs" \
    "Library/Application Support/EdgeDisco/data"
do
    require_bom_root_wheel "$owned_path"
done
case "$(file -b "$binary")" in
    *Mach-O*executable*) ;;
    *) printf '%s\n' "unexpected payload binary: native Mach-O executable required" >&2; exit 65 ;;
esac
case "$(file -b "$app_binary")" in
    *Mach-O*executable*) ;;
    *) printf '%s\n' "unexpected menu bar binary: native Mach-O executable required" >&2; exit 65 ;;
esac

if [ "$PLAN" -eq 1 ]; then
    printf '%s\n' "Validated preflight-only release plan; no identities, Keychain items, output artifacts, or network services are changed."
    printf '%s\n' "1. pkgutil --expand-full '$UNSIGNED' <temporary-directory>"
    printf '%s\n' "2. codesign --force --options runtime --timestamp --sign '$DEVELOPER_ID_APPLICATION' <payload>/usr/local/libexec/edgedisco/edgedisco"
    printf '%s\n' "3. pkgbuild the validated signed payload and trusted scripts as com.edgedisco.pkg version $VERSION"
    printf '%s\n' "4. productbuild --sign '$DEVELOPER_ID_INSTALLER' ... '<same-directory-hidden-candidate>'"
    printf '%s\n' "5. xcrun notarytool submit '<same-directory-hidden-candidate>' --keychain-profile '$NOTARY_PROFILE' --wait"
    printf '%s\n' "6. xcrun stapler staple '<same-directory-hidden-candidate>'"
    printf '%s\n' "7. atomically publish '$SIGNED' only after signature and notarization checks pass"
    exit 0
fi

security find-identity -v -p codesigning | grep -F -- "$DEVELOPER_ID_APPLICATION" >/dev/null || {
    printf '%s\n' "application signing identity is unavailable" >&2
    exit 77
}
security find-certificate -a -c "$DEVELOPER_ID_INSTALLER" >/dev/null || {
    printf '%s\n' "installer signing identity is unavailable" >&2
    exit 77
}

codesign --force --options runtime --timestamp --sign "$DEVELOPER_ID_APPLICATION" "$binary"
codesign --verify --strict --verbose=2 "$binary"
pkgbuild --root "$PAYLOAD" --scripts "$SCRIPTS" --identifier com.edgedisco.pkg \
    --version "$VERSION" --install-location / --ownership recommended \
    --filter '(^|/)\._.*' "$WORK/EdgeDisco.pkg"
CANDIDATE="$(dirname "$SIGNED")/.EdgeDisco-$VERSION.pkg.$$.candidate"
rm -f "$CANDIDATE"
productbuild --package "$WORK/EdgeDisco.pkg" --sign "$DEVELOPER_ID_INSTALLER" "$CANDIDATE"
xcrun notarytool submit "$CANDIDATE" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$CANDIDATE"
pkgutil --check-signature "$CANDIDATE"
mv -f "$CANDIDATE" "$SIGNED"
CANDIDATE=
printf '%s\n' "$SIGNED"

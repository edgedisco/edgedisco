#!/bin/sh
set -eu

DAEMON_LABEL="com.edgedisco.daemon"
AGENT_LABEL="com.edgedisco.agent"
RECEIPT="com.edgedisco.pkg"
STAGED_ROOT=${EDGEDISCO_STAGED_ROOT:-}
LIFECYCLE_LOG=${EDGEDISCO_LIFECYCLE_LOG:-}

PURGE=0
case "$#" in
    0) ;;
    1)
        case "$1" in
            --purge) PURGE=1 ;;
            --help)
                printf '%s\n' "usage: uninstall.sh [--purge] [--help]"
                exit 0
                ;;
            *) printf '%s\n' "usage: uninstall.sh [--purge] [--help]" >&2; exit 64 ;;
        esac
        ;;
    *) printf '%s\n' "usage: uninstall.sh [--purge] [--help]" >&2; exit 64 ;;
esac

if [ -n "$STAGED_ROOT" ]; then
    case "$STAGED_ROOT" in
        /*) ;;
        *) printf '%s\n' "EDGEDISCO_STAGED_ROOT must be an absolute path" >&2; exit 64 ;;
    esac
    if [ "$STAGED_ROOT" = "/" ]; then
        printf '%s\n' "EDGEDISCO_STAGED_ROOT must not be /" >&2
        exit 64
    fi
    if [ ! -d "$STAGED_ROOT" ] || [ -L "$STAGED_ROOT" ]; then
        printf '%s\n' "EDGEDISCO_STAGED_ROOT must be an existing canonical directory, not a symlink" >&2
        exit 64
    fi
    canonical_root=$(CDPATH= cd "$STAGED_ROOT" && pwd -P)
    if [ "$canonical_root" != "$STAGED_ROOT" ]; then
        printf '%s\n' "EDGEDISCO_STAGED_ROOT must be canonical" >&2
        exit 64
    fi
    ROOT=$STAGED_ROOT
    STAGED=1
else
    if [ "$(id -u)" -ne 0 ]; then
        printf '%s\n' "EdgeDisco uninstaller requires root" >&2
        exit 77
    fi
    ROOT=
    STAGED=0
fi

if [ "$STAGED" -eq 1 ] && [ -d "$ROOT/var/run" ]; then
    socket_parent=$(CDPATH= cd "$ROOT/var/run" && pwd -P)
    case "$socket_parent/" in
        "$ROOT"/*) ;;
        *) printf 'refusing socket path outside staged root: %s\n' "$socket_parent" >&2; exit 65 ;;
    esac
fi

for protected_path in \
    "$ROOT/Applications" \
    "$ROOT/Applications/EdgeDisco.app" \
    "$ROOT/Applications/EdgeDisco.app/Contents" \
    "$ROOT/Applications/EdgeDisco.app/Contents/Info.plist" \
    "$ROOT/Applications/EdgeDisco.app/Contents/MacOS" \
    "$ROOT/Applications/EdgeDisco.app/Contents/MacOS/EdgeDiscoMenuBar" \
    "$ROOT/Applications/EdgeDisco.app/Contents/Resources" \
    "$ROOT/Library" \
    "$ROOT/Library/Application Support" \
    "$ROOT/Library/Application Support/EdgeDisco" \
    "$ROOT/Library/Application Support/EdgeDisco/config" \
    "$ROOT/Library/Application Support/EdgeDisco/logs" \
    "$ROOT/Library/Application Support/EdgeDisco/data" \
    "$ROOT/Library/Application Support/EdgeDisco/data/inventory.db" \
    "$ROOT/Library/Application Support/EdgeDisco/data/inventory.db-wal" \
    "$ROOT/Library/LaunchDaemons" \
    "$ROOT/Library/LaunchDaemons/com.edgedisco.daemon.plist" \
    "$ROOT/Library/LaunchAgents" \
    "$ROOT/Library/LaunchAgents/com.edgedisco.agent.plist" \
    "$ROOT/usr" \
    "$ROOT/usr/local" \
    "$ROOT/usr/local/libexec" \
    "$ROOT/usr/local/libexec/edgedisco" \
    "$ROOT/usr/local/libexec/edgedisco/edgedisco" \
    "$ROOT/var/run/edgedisco.sock"
do
    if [ -L "$protected_path" ]; then
        printf 'refusing symlink in package-owned path: %s\n' "$protected_path" >&2
        exit 65
    fi
done

if [ "$STAGED" -eq 1 ]; then
    if [ -n "$LIFECYCLE_LOG" ]; then
        printf '%s\n' "launchctl bootout system/$DAEMON_LABEL" >> "$LIFECYCLE_LOG"
        printf '%s\n' "launchctl bootout gui/<console-uid>/$AGENT_LABEL" >> "$LIFECYCLE_LOG"
        printf '%s\n' "pkgutil --forget $RECEIPT" >> "$LIFECYCLE_LOG"
    fi
else
    /bin/launchctl bootout "system/$DAEMON_LABEL" >/dev/null 2>&1 || :
    console_uid=$(/usr/bin/stat -f '%u' /dev/console 2>/dev/null || printf '0')
    case "$console_uid" in
        ''|*[!0-9]*|0) ;;
        *) /bin/launchctl bootout "gui/$console_uid/$AGENT_LABEL" >/dev/null 2>&1 || : ;;
    esac
fi

/bin/rm -f \
    "$ROOT/usr/local/libexec/edgedisco/edgedisco" \
    "$ROOT/Library/LaunchDaemons/com.edgedisco.daemon.plist" \
    "$ROOT/Library/LaunchAgents/com.edgedisco.agent.plist" \
    "$ROOT/var/run/edgedisco.sock"
/bin/rm -rf "$ROOT/Applications/EdgeDisco.app"

if [ "$PURGE" -eq 1 ]; then
    /bin/rm -rf "$ROOT/Library/Application Support/EdgeDisco"
fi

if [ "$STAGED" -eq 0 ]; then
    /usr/sbin/pkgutil --forget "$RECEIPT" >/dev/null 2>&1 || :
fi

exit 0

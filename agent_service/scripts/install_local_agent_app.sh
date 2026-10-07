#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 <Creality Print Local Agent.app> [install path]" >&2
    exit 2
fi
SOURCE="$1"
DESTINATION="${2:-$HOME/Applications/Creality Print Local Agent.app}"
EXPECTED_NAME="Creality Print Local Agent.app"
IDENTIFIER="com.seanspiesman.crealityprint.localagent"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: macOS app installation requires macOS." >&2
    exit 2
fi
if [[ "$(basename "$SOURCE")" != "$EXPECTED_NAME" || ! -d "$SOURCE/Contents" ]]; then
    echo "Error: source must be a packaged '$EXPECTED_NAME' bundle." >&2
    exit 2
fi
if [[ "$(basename "$DESTINATION")" != "$EXPECTED_NAME" ]]; then
    echo "Error: install destination must be named '$EXPECTED_NAME'." >&2
    exit 2
fi
SOURCE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$SOURCE/Contents/Info.plist" 2>/dev/null || true)"
if [[ "$SOURCE_ID" != "$IDENTIFIER" ]]; then
    echo "Error: source has unexpected bundle identifier." >&2
    exit 2
fi
DESTINATION_ABS="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$DESTINATION")"
if [[ "$DESTINATION_ABS" == "/Applications/Creality Print.app" || -e "$DESTINATION_ABS" ]]; then
    echo "Error: refusing to replace an existing or protected app: $DESTINATION_ABS" >&2
    exit 2
fi
mkdir -p "$(dirname "$DESTINATION_ABS")"
STAGE="$(dirname "$DESTINATION_ABS")/.local-agent-install-$$.app"
if [[ -e "$STAGE" ]]; then
    echo "Error: staging path unexpectedly exists: $STAGE" >&2
    exit 2
fi
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT INT TERM
ditto "$SOURCE" "$STAGE"
codesign --verify --deep --strict "$STAGE"
mv "$STAGE" "$DESTINATION_ABS"
trap - EXIT INT TERM
if ! /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DESTINATION_ABS"; then
    echo "Error: installed app could not be registered with Launch Services: $DESTINATION_ABS" >&2
    exit 1
fi
echo "Installed separate app: $DESTINATION_ABS"

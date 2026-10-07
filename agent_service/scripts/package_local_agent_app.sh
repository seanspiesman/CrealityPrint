#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <separately-built CrealityPrint.app> <Creality Print Local Agent.app>" >&2
    exit 2
fi
SOURCE_APP="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
OUTPUT="$2"
EXPECTED_NAME="Creality Print Local Agent.app"
IDENTIFIER="com.seanspiesman.crealityprint.localagent"
SIGN_IDENTITY="${LOCAL_AGENT_SIGN_IDENTITY:--}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: macOS app packaging requires macOS." >&2
    exit 2
fi
if [[ ! -d "$SOURCE_APP/Contents" || ! -f "$SOURCE_APP/Contents/Info.plist" ]]; then
    echo "Error: source must be a built macOS app bundle." >&2
    exit 2
fi
SOURCE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$SOURCE_APP/Contents/Info.plist" 2>/dev/null || true)"
if [[ "$SOURCE_ID" != "$IDENTIFIER" ]]; then
    echo "Error: source bundle must be built with identifier $IDENTIFIER (found '$SOURCE_ID')." >&2
    exit 2
fi
if [[ "$(basename "$OUTPUT")" != "$EXPECTED_NAME" ]]; then
    echo "Error: output bundle must be named '$EXPECTED_NAME'." >&2
    exit 2
fi
if [[ -e "$OUTPUT" ]]; then
    echo "Error: refusing to replace an existing bundle: $OUTPUT" >&2
    exit 2
fi
if [[ "$SOURCE_APP" == "/Applications/Creality Print.app" ]]; then
    echo "Error: installed Creality Print cannot be used as a packaging source." >&2
    exit 2
fi

mkdir -p "$(dirname "$OUTPUT")"
OUTPUT_PARENT="$(cd "$(dirname "$OUTPUT")" && pwd)"
STAGE="$OUTPUT_PARENT/.local-agent-stage-$$.app"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT INT TERM
if [[ -e "$STAGE" ]]; then
    echo "Error: staging path unexpectedly exists: $STAGE" >&2
    exit 2
fi

ditto "$SOURCE_APP" "$STAGE"
PLIST="$STAGE/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $IDENTIFIER" "$PLIST"
/usr/libexec/PlistBuddy -c 'Set :CFBundleName Creality Print Local Agent' "$PLIST"
/usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName Creality Print Local Agent' "$PLIST"
# Avoid claiming Creality Print's file types or URL scheme in Launch Services.
/usr/libexec/PlistBuddy -c 'Delete :CFBundleURLTypes' "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Delete :CFBundleDocumentTypes' "$PLIST" 2>/dev/null || true

codesign --force --deep --sign "$SIGN_IDENTITY" --timestamp=none "$STAGE"
codesign --verify --deep --strict "$STAGE"
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$PLIST" | grep -Fx "$IDENTIFIER" >/dev/null
mv "$STAGE" "$OUTPUT"
trap - EXIT INT TERM
if [[ "$SIGN_IDENTITY" == "-" ]]; then
    echo "Created $OUTPUT (ad-hoc signed; macOS may require first-launch approval)."
else
    echo "Created $OUTPUT (signed with configured identity)."
fi

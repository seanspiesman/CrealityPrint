#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/.." && pwd)"
ARCH="$(uname -m)"
BUILD_DIR="${LOCAL_AGENT_BUILD_DIR:-$SERVICE_DIR/.runtime/build-macos-$ARCH}"
BUILD_JOBS="${LOCAL_AGENT_BUILD_JOBS:-4}"
DEPS_PREFIX="${DEPS_ENV_DIR:-$REPO_ROOT/deps/build_$ARCH/dep_$ARCH}"
DEPS_PREFIX="$DEPS_PREFIX/usr/local"
OUTPUT="${LOCAL_AGENT_APP_OUTPUT:-$SERVICE_DIR/.runtime/artifacts/Creality Print Local Agent.app}"
IDENTIFIER="com.seanspiesman.crealityprint.localagent"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: custom GUI app builds require macOS." >&2
    exit 2
fi
for tool in cmake ninja; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Error: required build tool '$tool' is not installed or not on PATH." >&2
        exit 2
    fi
done
if [[ ! -d "$DEPS_PREFIX" ]]; then
    echo "Error: Creality macOS dependency prefix is missing: $DEPS_PREFIX" >&2
    echo "Build/provision the repository's macOS dependencies or set DEPS_ENV_DIR." >&2
    exit 2
fi
if [[ -e "$OUTPUT" ]]; then
    echo "Error: output already exists; choose a new LOCAL_AGENT_APP_OUTPUT path: $OUTPUT" >&2
    exit 2
fi

mkdir -p "$BUILD_DIR" "$(dirname "$OUTPUT")"
cmake -S "$REPO_ROOT" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=11.3 \
    -DCMAKE_PREFIX_PATH="$DEPS_PREFIX" \
    -DCMAKE_INSTALL_PREFIX="$BUILD_DIR/install" \
    -DCMAKE_MACOSX_BUNDLE=ON \
    -DCREALITYPRINT_BUNDLE_IDENTIFIER="$IDENTIFIER" \
    -DCREALITY_LOCAL_AGENT=ON \
    -DCREALITYPRINT_BUILD_NUMBER=1 \
    -DPROCESS_NAME=CrealityPrint \
    -DSLIC3R_GUI=ON \
    -DSLIC3R_STATIC=ON \
    -DSLIC3R_BUILD_TESTS=OFF \
    -DSLIC3R_BUILD_SANDBOXES=OFF \
    "$@"
cmake --build "$BUILD_DIR" --config Release --target CrealityPrint --parallel "$BUILD_JOBS"

BUILT_APP="$BUILD_DIR/src/CrealityPrint.app"
if [[ ! -d "$BUILT_APP" ]]; then
    echo "Error: build completed but expected app bundle is missing: $BUILT_APP" >&2
    exit 3
fi
"$SCRIPT_DIR/package_local_agent_app.sh" "$BUILT_APP" "$OUTPUT"
echo "Packaged separate app: $OUTPUT"

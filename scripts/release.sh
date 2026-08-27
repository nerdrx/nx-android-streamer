#!/usr/bin/env bash
# Package + publish a release the NX Hub can classify and install.
# Usage: scripts/release.sh <version>   (e.g. scripts/release.sh 0.1.0)
set -euo pipefail

V=${1:?usage: scripts/release.sh <version>}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
NAME="nx-android-streamer-${V}-linux-x86_64"

nx manifest check --file "$ROOT/nx-app.json"

rm -rf "$DIST"; mkdir -p "$DIST/$NAME"
cp "$ROOT"/start.sh "$ROOT"/README.md "$ROOT"/ARCHITECTURE.md "$ROOT"/BORROWED.md "$ROOT"/LICENSE "$DIST/$NAME/"
tar -C "$DIST" -czf "$DIST/$NAME.tar.gz" "$NAME"
rm -rf "${DIST:?}/$NAME"

# APK joins the release once the Kotlin client exists
[[ -f "$ROOT/client-android/app/build/outputs/apk/release/nx-android-streamer-${V}.apk" ]] \
    && cp "$ROOT/client-android/app/build/outputs/apk/release/nx-android-streamer-${V}.apk" "$DIST/"

cd "$DIST"
for f in *.tar.gz *.apk; do [[ -f $f ]] && sha256sum "$f" > "$f.sha256"; done

gh release create "v${V}" "$DIST"/* "$ROOT/nx-app.json" \
    --title "v${V}" --generate-notes

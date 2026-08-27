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
cp "$ROOT"/start.sh "$ROOT"/README.md "$ROOT"/ARCHITECTURE.md "$ROOT"/BORROWED.md \
   "$ROOT"/CONTRIBUTING.md "$ROOT"/LICENSE "$DIST/$NAME/"
cp -r "$ROOT"/server "$ROOT"/web "$ROOT"/gui "$DIST/$NAME/"   # daemon + phone client + tray gui
find "$DIST/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +
tar -C "$DIST" -czf "$DIST/$NAME.tar.gz" "$NAME"
rm -rf "${DIST:?}/$NAME"

# APK joins the release once the Kotlin client exists
[[ -f "$ROOT/client-android/app/build/outputs/apk/release/nx-android-streamer-${V}.apk" ]] \
    && cp "$ROOT/client-android/app/build/outputs/apk/release/nx-android-streamer-${V}.apk" "$DIST/"

cd "$DIST"
for f in *.tar.gz *.apk; do [[ -f $f ]] && sha256sum "$f" > "$f.sha256"; done

# Create the release WITHOUT assets first (fast, atomic — a slow asset upload
# can't leave the release stuck as a draft), then upload assets, then flip it
# to published+latest. gh's default create-with-assets left drafts when a large
# APK upload was interrupted.
gh release create "v${V}" --title "v${V}" --generate-notes --draft
gh release upload "v${V}" "$DIST"/* "$ROOT/nx-app.json" --clobber
gh release edit "v${V}" --draft=false --latest

echo "published: $(gh release view "v${V}" --json url -q .url)"

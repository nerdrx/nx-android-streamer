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
cp -r "$ROOT"/server "$ROOT"/web "$ROOT"/gui "$ROOT"/scripts "$DIST/$NAME/"  # daemon + client + tray + first-run
find "$DIST/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +
tar -C "$DIST" -czf "$DIST/$NAME.tar.gz" "$NAME"
rm -rf "${DIST:?}/$NAME"

# The phone client. Gradle always writes app-release.apk, so take THAT and name
# it for this release — matching on a version-stamped filename meant every
# release silently shipped with no app when the local build had another name.
APK="$ROOT/client-android/app/build/outputs/apk/release/app-release.apk"
if [[ -f $APK ]]; then
    cp "$APK" "$DIST/nx-android-streamer-${V}.apk"
    # Guard the version drift that let the About screen claim 0.2.0 for four
    # releases: the APK must actually be built from this version.
    GRADLE_V=$(grep -oP 'versionName "\K[^"]+' "$ROOT/client-android/app/build.gradle" 2>/dev/null || echo "?")
    if [[ $GRADLE_V != "$V" ]]; then
        echo "WARNING: client-android versionName is $GRADLE_V but this release is $V." >&2
        echo "  Bump versionName/versionCode and rebuild, or the app will misreport itself." >&2
    fi
else
    echo "WARNING: no APK at $APK — releasing WITHOUT the phone client." >&2
    echo "  build it first: cd client-android && ./gradlew assembleRelease" >&2
fi

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

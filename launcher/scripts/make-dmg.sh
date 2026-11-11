#!/bin/sh
# Builds the macOS release: PokopiaLauncher.app with the runtime dylib and the build tools inside it, then a DMG that
# installs into /Applications. The package ships no game code; Set up lifts and compiles pkp from the player's own dump.
#
#   launcher/scripts/make-dmg.sh <play folder with pkp.ini and the dump paths> [version]
#
# Needs a production launcher build (npm run tauri build) and the runtime dylib (PKP_RT_DLL=ON, see docs/macos-port.md).
set -eu
here=$(cd "${1:?usage: make-dmg.sh <play folder> [version]}" && pwd)
repo=$(cd "$(dirname "$0")/../.." && pwd)
# the release version: the launcher's manifest, unless the caller names one (the release workflow passes the tag's)
version=${2:-$(sed -nE 's/^[[:space:]]*"version": "([^"]+)".*/\1/p' "$repo/launcher/src-tauri/tauri.conf.json" | head -1)}
app="$here/PokopiaLauncher.app"
dmg="$here/Recompia-$version-macos-arm64.dmg"

[ -x "$here/PokopiaLauncher" ] || { echo "no launcher binary at $here/PokopiaLauncher (npm run tauri build)" >&2; exit 1; }

"$repo/launcher/scripts/make-app.sh" "$here" "$version"

# The bundle carries everything the player needs: the runtime dylib, the lifter, the SDK sources and the builder. The game
# folder (pkp, pkp.ini, shader_cache, saves) is created by Set up in Application Support, never inside the bundle.
res="$app/Contents/MacOS"   # the launcher looks for tools/ next to itself
mkdir -p "$res/tools/build/sdk" "$app/Contents/Frameworks"
cp "$repo/work/build/rtb/libpkp_rt.dylib" "$app/Contents/Frameworks/libpkp_rt.dylib"
cp "$repo/game-build/release_b/pkp_build.py" "$repo/game-build/release_b/dump_hashes.json" "$res/tools/build/"
cp -R "$repo/recomp" "$res/tools/build/recomp"
find "$res/tools/build/recomp" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
# The shader pipeline: the corpus extractor and a self-contained publish of the harness (its own .NET runtime), so a
# player needs neither .NET nor a checkout.
cp "$repo/tools/shaders/ampere/extract_corpus.py" "$res/tools/"
cp -R "$repo/work/harness" "$res/tools/harness"
cp "$repo/game-build/src/pkp_stub.cpp" "$repo/game-build/src/pkp_rt_api.h" "$res/tools/build/sdk/"
# macOS has no Vulkan loader of its own: MoltenVK ships with the package (with its licence) and Set up copies it next to pkp,
# which is where volk looks for libvulkan.1.dylib.
moltenvk=$(brew --prefix molten-vk 2>/dev/null || echo /opt/homebrew/opt/molten-vk)
cp "$moltenvk/lib/libMoltenVK.dylib" "$res/tools/build/sdk/libvulkan.1.dylib"
cp "$moltenvk/LICENSE" "$res/tools/build/sdk/LICENSE-MoltenVK.txt"
# What the game reads next to pkp: the prompt icons (generated here when the work folder does not have them) and the font.
if [ ! -d "$repo/work/assets/kenney_input_prompts" ]; then
    python3 "$repo/tools/kbm_icons.py" >/dev/null
fi
mkdir -p "$res/tools/build/sdk/kbm_icons" "$res/tools/build/sdk/fonts"
cp "$repo/work/assets/kenney_input_prompts/Keyboard & Mouse/Default/"*.png "$res/tools/build/sdk/kbm_icons/"
cp "$repo/runtime/assets/fonts/DejaVuSans.ttf" "$res/tools/build/sdk/fonts/"
cp "$repo/runtime/cmake/guest_segment.py" "$res/tools/build/sdk/"
cp "$repo/work/build/rtb/libpkp_rt.dylib" "$res/tools/build/sdk/"
# No Python in the bundle: Set up fetches python-build-standalone and the lz4 and capstone wheels into tools/macos, as the
# Linux package does (host.rs prepare_tools). macOS compiles with the Apple clang of the Command Line Tools.

# The dylib is loaded from Contents/Frameworks, so the executable looks there first.
install_name_tool -add_rpath "@executable_path/../Frameworks" "$app/Contents/MacOS/PokopiaLauncher" 2>/dev/null || true
codesign --force --deep --sign - "$app" 2>/dev/null || echo "warning: codesign failed; the bundle still runs locally" >&2

# Developer ID and notarization, when the caller has them (the release workflow imports the certificate from secrets):
# CODESIGN_IDENTITY names the Developer ID Application certificate, NOTARY_PROFILE a notarytool keychain profile. Without them
# the bundle keeps the ad-hoc signature make-app.sh gave it, which macOS blocks on the first launch (the README in the image
# tells the player how to allow it).
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
    echo "signing with $CODESIGN_IDENTITY"
    # nested code first (the harness is a self-contained .NET publish with its own dylibs), then the bundle, with the hardened
    # runtime and a timestamp, which notarization requires
    find "$app" -type f \( -perm -111 -o -name '*.dylib' \) | while read -r f; do
        file "$f" | grep -q 'Mach-O' || continue
        codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$f" >/dev/null 2>&1 || echo "warning: cannot sign $f" >&2
    done
    codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$app"
    codesign --verify --deep --strict "$app"
fi

# A DMG the way Mac users expect: the app, a shortcut to /Applications, and the one paragraph Gatekeeper makes necessary.
stage=$(mktemp -d)
cp -R "$app" "$stage/"
ln -s /Applications "$stage/Applications"
cp "$repo/launcher/scripts/self-sign.sh" "$stage/Self-sign (optional).command"
cat > "$stage/How to open Recompia.txt" <<'TXT'
Recompia is not notarized by Apple, so macOS refuses to open it the first time. This is expected.

1. Drag Pokopia Launcher to the Applications folder.
2. Open it once and let macOS say it cannot be opened. Close that dialog.
3. Open System Settings, Privacy and Security, and scroll down to the Security section.
   There is a line about Pokopia Launcher being blocked: press "Open Anyway", then "Open".
4. From then on it opens like any other app.

On macOS 14 and older you can also right-click the app, choose Open, and confirm.

If System Settings shows no such line, remove the download flag in Terminal instead:

   xattr -dr com.apple.quarantine /Applications/PokopiaLauncher.app

There is also "Self-sign (optional).command" in this image: double-click it (or right-click and Open) and it removes that flag
and signs the app with a certificate it creates on this Mac. Gatekeeper still only accepts Apple-issued developer IDs by itself,
so the one-time Open Anyway above may be needed either way.
TXT
rm -f "$dmg"
hdiutil create -quiet -volname "Recompia $version" -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$dmg"
rm -rf "$stage"

# The in-place updater's package: the folder the launcher runs from (Contents/MacOS: the launcher and tools/) under
# PokopiaRecomp-<version>/, which is the layout update.rs looks for. The DMG stays the clean-install path; this zip is what
# "Update" downloads, and the updater signs the bundle again afterwards.
zip="$here/Recompia-$version-macos-arm64.zip"
stage_zip=$(mktemp -d)
mkdir -p "$stage_zip/PokopiaRecomp-$version"
cp -R "$res/." "$stage_zip/PokopiaRecomp-$version/"
rm -f "$zip"
(cd "$stage_zip" && ditto -c -k --sequesterRsrc --keepParent "PokopiaRecomp-$version" "$zip")
rm -rf "$stage_zip"
echo "built $zip"

# Notarize and staple the image: then the app opens with a plain double click. xcrun notarytool needs an Apple Developer account
# (a Developer ID Application certificate and an App Store Connect API key or an app-specific password).
if [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "notarizing $dmg"
    xcrun notarytool submit "$dmg" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$dmg"
    xcrun stapler validate "$dmg"
fi
echo "built $dmg"

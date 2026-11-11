#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Makes the app launch on this Mac without an Apple Developer account. Two things block it: the quarantine flag that macOS puts
# on everything downloaded, and a signature from no identity. This removes the flag, which is the part that lets it open, and
# signs the bundle with a certificate it creates in the login keychain ("Recompia Self-Signed"), so the signature is stable and
# verifiable. Gatekeeper still only waves Apple-issued Developer IDs through by itself: if macOS asks, allow the app once in
# System Settings, Privacy and Security.
#
#   sh self-sign.sh [path to PokopiaLauncher.app]     (default: /Applications/PokopiaLauncher.app)
#
# It asks for nothing but the keychain password when it adds the certificate. Running it again is harmless.
set -eu

app=${1:-/Applications/PokopiaLauncher.app}
name="Recompia Self-Signed"

# not installed yet: the copy beside this script (inside the image) can be signed once it has been dragged to Applications
if [ ! -d "$app" ]; then
    beside=$(cd "$(dirname "$0")" && pwd)/PokopiaLauncher.app
    if [ -d "$beside" ]; then
        echo "Drag Pokopia Launcher into Applications first, then run this again." >&2
    else
        echo "No app at $app" >&2
    fi
    echo "Pass its path: sh self-sign.sh /Applications/PokopiaLauncher.app" >&2
    exit 1
fi

# 1. The quarantine flag: this alone is what makes macOS refuse the first launch.
xattr -dr com.apple.quarantine "$app" 2>/dev/null || true
echo "removed the quarantine flag from $app"

# 2. A certificate of our own, in the login keychain, trusted for code signing.
if ! security find-identity -v -p codesigning 2>/dev/null | grep -qF "$name"; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    # /usr/bin/openssl is LibreSSL: its PKCS#12 encryption is what security(1) can verify (Homebrew's OpenSSL 3 writes AES-256,
    # which it rejects with "MAC verification failed"). The keychain import also needs a non-empty password.
    openssl=/usr/bin/openssl
    # a code signing certificate: digitalSignature key usage and the codeSigning extended key usage, which codesign requires
    "$openssl" req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$tmp/key.pem" -out "$tmp/cert.pem" -subj "/CN=$name" \
        -addext "keyUsage=critical,digitalSignature" \
        -addext "extendedKeyUsage=critical,codeSigning" >/dev/null 2>&1
    "$openssl" pkcs12 -export -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -out "$tmp/id.p12" -passout pass:recompia >/dev/null 2>&1
    security import "$tmp/id.p12" -k "$HOME/Library/Keychains/login.keychain-db" -P recompia -T /usr/bin/codesign >/dev/null
    # trust it for code signing in the login keychain (the user domain: no administrator prompt)
    if ! security add-trusted-cert -r trustRoot -p codeSign -k "$HOME/Library/Keychains/login.keychain-db" "$tmp/cert.pem" 2>/dev/null; then
        echo "note: could not trust the certificate automatically; macOS may ask once, or allow the app in System Settings" >&2
    fi
    echo "created the certificate \"$name\" in your login keychain"
else
    echo "using the existing certificate \"$name\""
fi

# 3. Python bytecode written into the bundle breaks its seal ("file added: .../__pycache__/..."), which is what macOS then
# cannot verify. An older build wrote it while running the lifter; drop it before signing.
find "$app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$app" -name '*.pyc' -delete 2>/dev/null || true

# 4. Sign the bundle: every Mach-O inside first (the harness is a self-contained .NET publish with its own dylibs), then the
# bundle itself, with the hardened runtime so the signature matches what macOS expects of a signed app.
find "$app" -type f \( -perm -111 -o -name '*.dylib' \) | while read -r f; do
    file "$f" | grep -q 'Mach-O' || continue
    codesign --force --options runtime --sign "$name" "$f" >/dev/null 2>&1 || echo "warning: cannot sign $f" >&2
done
codesign --force --options runtime --sign "$name" "$app"

# 5. Say what happened.
codesign --verify --deep --strict --verbose=2 "$app" 2>&1 | tail -2
if spctl -a -vv "$app" >/dev/null 2>&1; then
    echo "signed and accepted by Gatekeeper: $app opens with a double click"
else
    # normal for a certificate Apple did not issue: the quarantine flag is gone, so the app runs; macOS may still show its
    # dialog once, and the button below is the one-time answer to it
    echo "done. The app runs now; if macOS still asks the first time, open System Settings, Privacy and Security and press"
    echo "Open Anyway once for Pokopia Launcher."
fi

#!/bin/sh
# Build quickask.app and install it into /Applications.
#   ./build.sh              build, install to /Applications, launch
#   ./build.sh --no-install build only (result in dist/quickask.app)
#   ./build.sh --dmg        build dist/quickask.dmg to hand out to other people
set -eu
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
    echo "creating .venv"
    if command -v uv >/dev/null 2>&1; then uv venv .venv --python 3.12; else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "$PY" -r requirements-build.txt
else
    "$PY" -m pip install -q -r requirements-build.txt
fi

"$PY" make_icon.py build/icon/quickask.icns
"$PY" -m PyInstaller --noconfirm --workpath build/pyinstaller --distpath dist quickask.spec

# Re-sign the whole bundle so every nested binary carries a consistent
# signature (macOS refuses to launch otherwise). Sign with a stable identity
# when one exists: macOS ties privacy grants (Accessibility, Automation) to
# the signing identity, and an ad-hoc signature changes on every build, which
# silently revokes them. "llm-popup signing" is a self-signed certificate in
# the login keychain (named before the app became quickask); see SIGNING.md
# for how it was made and how to remove it.
IDENTITY=${QUICKASK_SIGN_IDENTITY:-llm-popup signing}
if security find-identity -v -p codesigning | grep -q "\"$IDENTITY\""; then
    codesign --force --deep --sign "$IDENTITY" dist/quickask.app
    echo "signed as \"$IDENTITY\""
else
    echo "warning: no \"$IDENTITY\" certificate; signing ad hoc (privacy grants will not survive rebuilds)" >&2
    codesign --force --deep --sign - dist/quickask.app
fi
echo "built dist/quickask.app"

if [ "${1:-}" = "--no-install" ]; then
    exit 0
fi

if [ "${1:-}" = "--dmg" ]; then
    # Disk image with the app next to an /Applications link: drag one onto the other.
    stage=build/dmg
    rm -rf "$stage" dist/quickask.dmg
    mkdir -p "$stage"
    ditto dist/quickask.app "$stage/quickask.app"
    ln -s /Applications "$stage/Applications"
    hdiutil create -quiet -volname quickask -srcfolder "$stage" -format UDZO -ov dist/quickask.dmg
    echo "built dist/quickask.dmg ($(du -h dist/quickask.dmg | cut -f1))"
    exit 0
fi

pkill -x quickask 2>/dev/null || true
rm -rf /Applications/quickask.app
ditto dist/quickask.app /Applications/quickask.app
echo "installed /Applications/quickask.app"
open /Applications/quickask.app
echo "quickask is running: look for the ? in the menu bar and press $("$PY" -c 'import quick_ask; print(quick_ask.hotkey_label())')"

#!/bin/bash
# Launches each compiled executable inside the built .app, screenshots it,
# and fails if any of them exits (crashes) within a few seconds of starting.
# Screenshots and per-tool logs land in smoke/ for upload as an artifact.

set -u

APP="installer/dist/Programmer's Pandora.app"
MACOS_DIR="$APP/Contents/MacOS"
OUT="smoke"
WAIT_SECONDS=8
TOOLS="_pandora_home androidtv_pubkey ip_monitor mac_monitor network_scanner wifi_monitor wifi_spectrum_view"

mkdir -p "$OUT"
status=0

echo "--- code signature ---"
# Anything modifying the bundle after PyInstaller signs it breaks the seal,
# and macOS then reports a downloaded copy as "damaged" - so this is fatal.
if codesign --verify --deep --strict --verbose=2 "$APP" > "$OUT/codesign.txt" 2>&1; then
    echo "PASS  bundle signature valid"
else
    echo "FAIL  bundle signature invalid:"
    tail -20 "$OUT/codesign.txt"
    status=1
fi

echo "--- bundled resources ---"
# The launcher lists tools by reading these .py files - without them it
# shows "No scripts found" even though every tool binary is present.
RESOURCES="$APP/Contents/Resources"
for required in Logo/pandora_512.png androidtv_pubkey.py ip_monitor.py mac_monitor.py network_scanner.py wifi_monitor.py wifi_spectrum_view.py; do
    if [ -f "$RESOURCES/$required" ]; then
        echo "PASS  Resources/$required present"
    else
        echo "FAIL  Resources/$required missing"
        status=1
    fi
done

for tool in $TOOLS; do
    "$MACOS_DIR/$tool" > "$OUT/$tool.log" 2>&1 &
    pid=$!
    sleep "$WAIT_SECONDS"
    screencapture -x "$OUT/$tool.png" || echo "WARN  screencapture failed for $tool"

    if kill -0 "$pid" 2>/dev/null; then
        echo "PASS  $tool still running after ${WAIT_SECONDS}s"
        kill "$pid"
        wait "$pid" 2>/dev/null
    else
        echo "FAIL  $tool exited early - log:"
        cat "$OUT/$tool.log"
        status=1
    fi
done

exit $status

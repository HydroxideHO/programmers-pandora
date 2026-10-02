# -*- mode: python ; coding: utf-8 -*-
# macOS build: compiles every top-level tool script into its own executable,
# all sharing one bundle (customtkinter/darkdetect/packaging/the embedded
# Python runtime are bundled once, not duplicated per script), then wraps
# the whole thing as "Programmer's Pandora.app". pandora_home.py is listed
# first so PyInstaller's BUNDLE() picks it as the app's main launchable
# executable (Contents/MacOS/pandora_home) - the other tools ride along as
# extra files inside the same bundle, launched as subprocesses by the
# launcher exactly like the Windows build does.
#
# Must be built ON macOS - PyInstaller does not cross-compile. See
# .github/workflows/build-macos.yml, which also generates the .icns this
# looks for from Logo/pandora_1024.png before running this spec.

import os

PROJECT_DIR = os.path.abspath(os.path.join(SPECPATH, ".."))

SCRIPTS = [
    "pandora_home.py",
    "androidtv_pubkey.py",
    "ip_monitor.py",
    "mac_monitor.py",
    "network_scanner.py",
    "wifi_monitor.py",
    "wifi_spectrum_view.py",
]

ICNS_FILE = os.path.join(PROJECT_DIR, "Logo", "pandora.icns")

COMMON_DATAS = [
    (os.path.join(PROJECT_DIR, "vendor", "customtkinter", "assets"), "customtkinter/assets"),
]

built = []
for script in SCRIPTS:
    a = Analysis(
        [os.path.join(PROJECT_DIR, script)],
        pathex=[PROJECT_DIR, os.path.join(PROJECT_DIR, "vendor")],
        datas=COMMON_DATAS,
    )
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=os.path.splitext(script)[0],
        console=False,
    )
    built.append((exe, a))

collect_args = []
for exe, a in built:
    collect_args.extend([exe, a.binaries, a.zipfiles, a.datas])

coll = COLLECT(
    *collect_args,
    strip=False,
    upx=False,
    name="ProgrammersPandora",
)

app = BUNDLE(
    coll,
    name="Programmer's Pandora.app",
    icon=ICNS_FILE if os.path.isfile(ICNS_FILE) else None,
    bundle_identifier="com.hydroxideho.programmerspandora",
    info_plist={
        "CFBundleName": "Programmer's Pandora",
        "CFBundleDisplayName": "Programmer's Pandora",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "MIT License",
    },
)

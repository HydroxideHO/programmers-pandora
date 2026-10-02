# -*- mode: python ; coding: utf-8 -*-
# macOS build: compiles every top-level tool script into its own executable,
# all sharing one bundle (customtkinter/darkdetect/packaging/the embedded
# Python runtime are bundled once, not duplicated per script), then wraps
# the whole thing as "Programmer's Pandora.app". The other tools ride along
# as extra files inside the same bundle, launched as subprocesses by the
# launcher exactly like the Windows build does.
#
# Which one PyInstaller's BUNDLE() picks as the app's actual launch target
# (Contents/MacOS/<name>, referenced by Info.plist's CFBundleExecutable)
# isn't documented for a merged multi-exe COLLECT like this one - verified
# against a real build that it's whichever compiled name sorts
# alphabetically first, not insertion order and not file size. pandora_home
# is forced to sort first via EXE_NAME_OVERRIDES below so it's always
# picked, regardless of what other tool scripts get added later.
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

EXE_NAME_OVERRIDES = {
    "pandora_home.py": "_pandora_home",
}

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
        name=EXE_NAME_OVERRIDES.get(script, os.path.splitext(script)[0]),
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

# -*- mode: python ; coding: utf-8 -*-
# Compiles every top-level tool script into its own standalone .exe, all
# sharing one dist folder (customtkinter/darkdetect/packaging/the embedded
# Python runtime are bundled once, not duplicated per script). Run via
# installer/build.bat, which invokes PyInstaller with this spec before
# handing the resulting dist/ folder to Inno Setup.

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

ICON_FILE = os.path.join(PROJECT_DIR, "Logo", "pandora.ico")

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
        icon=ICON_FILE,
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

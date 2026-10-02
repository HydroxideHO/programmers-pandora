"""
Shared theme for Programmer's Pandora
--------------------------------------
Common look-and-feel setup for the launcher and all tool scripts: wires up
the vendored customtkinter package (see vendor/) and exposes one shared
color palette and font helper so every window in this folder looks like
part of the same app.

Import `ctk` from here instead of importing customtkinter directly, so the
vendor path is guaranteed to be set up first:

    from pandora_theme import ctk, palette, font, set_window_icon

Call `set_window_icon(root)` right after creating each script's root window
to pick up the app icon - Logo/pandora.ico on Windows, Logo/pandora_512.png
on macOS (Tk's .ico support is Windows-only).

Also exposes IS_WINDOWS/IS_MAC and SUBPROCESS_FLAGS (spread into any
subprocess.run()/Popen() call that shells out to things like ping/arp/netsh,
since the flag that suppresses a console flash only exists on Windows) so
each tool script can branch on platform without re-detecting it itself.

Light/dark mode: `palette` points at whichever of light_palette/dark_palette
matches the saved preference (theme_pref.json) at the time this module is
imported. Only the launcher (pandora_home.py) can flip that preference live,
via set_theme_mode() - other tool scripts just pick up whatever was saved
the next time they're launched as their own process.
"""

import json
import os
import subprocess
import sys
import tkinter as tk

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# subprocess.CREATE_NO_WINDOW only exists on Windows (it suppresses the
# console flash from shelling out to things like netsh/arp/ping). Spread
# this into subprocess.run()/Popen() calls instead of passing creationflags
# directly, so the same call site works on both platforms:
#     subprocess.run([...], **SUBPROCESS_FLAGS)
SUBPROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}

# When frozen by PyInstaller, __file__ points inside the temp/bundle extraction
# dir, not next to the compiled .exe/.app - use sys.executable's folder instead
# so Logo/, theme_pref.json, etc. resolve to files actually shipped alongside
# it. On macOS a frozen .app's executable lives in Contents/MacOS/, two levels
# below Contents/Resources/ where PyInstaller puts bundled data by default -
# app-relative files are shipped there instead (see the .spec on macOS builds).
if getattr(sys, "frozen", False):
    if IS_MAC:
        APP_DIR = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", "Resources"))
    else:
        APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

VENDOR_DIR = os.path.join(APP_DIR, "vendor")
if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

import customtkinter as ctk  # noqa: E402  (must follow the sys.path insert)

ICON_ICO_PATH = os.path.join(APP_DIR, "Logo", "pandora.ico")
ICON_PNG_PATH = os.path.join(APP_DIR, "Logo", "pandora_512.png")
THEME_PREF_FILE = os.path.join(APP_DIR, "theme_pref.json")


def set_window_icon(window):
    # Tk's iconbitmap() only understands .ico on Windows (and is a no-op/
    # error on macOS, which wants a real image via iconphoto() instead).
    if IS_WINDOWS:
        if os.path.isfile(ICON_ICO_PATH):
            try:
                window.iconbitmap(ICON_ICO_PATH)
            except tk.TclError:
                pass
        return

    if os.path.isfile(ICON_PNG_PATH):
        try:
            photo = tk.PhotoImage(file=ICON_PNG_PATH)
            window.iconphoto(True, photo)
            window._icon_photo_ref = photo  # keep a reference - Tk drops the icon if this is garbage collected
        except tk.TclError:
            pass


ctk.set_default_color_theme("blue")

FONT_FAMILY = "Helvetica Neue" if IS_MAC else "Segoe UI"


class light_palette:
    bg = "#ECE9FF"
    surface = "#FFFFFF"
    surface_alt = "#E2DEFB"
    border = "#CFC9F5"
    accent = "#5B4FBF"
    accent_hover = "#4D43A2"
    accent_secondary = "#F4A93C"
    accent_secondary_hover = "#CF9033"
    text = "#221F33"
    text_muted = "#5B5770"
    success = "#2F9E64"
    danger = "#D14343"


class dark_palette:
    bg = "#211A3D"
    surface = "#2E2551"
    surface_alt = "#3B3067"
    border = "#4E4180"
    accent = "#F4A93C"
    accent_hover = "#D6912E"
    accent_secondary = "#8C7EF0"
    accent_secondary_hover = "#7568D6"
    text = "#F2EFFF"
    text_muted = "#B4A9DC"
    success = "#4FBE8B"
    danger = "#E5695F"


def _load_theme_mode() -> str:
    try:
        with open(THEME_PREF_FILE, "r", encoding="utf-8") as f:
            mode = json.load(f).get("mode", "light")
    except (OSError, ValueError):
        mode = "light"
    return mode if mode in ("light", "dark") else "light"


def _save_theme_mode(mode: str) -> None:
    try:
        with open(THEME_PREF_FILE, "w", encoding="utf-8") as f:
            json.dump({"mode": mode}, f)
    except OSError:
        pass


THEME_MODE = _load_theme_mode()
palette = light_palette if THEME_MODE == "light" else dark_palette
ctk.set_appearance_mode(THEME_MODE)


def set_theme_mode(mode: str) -> None:
    """Switch the active palette for this process and persist the choice.

    Only updates this module's own `palette`/THEME_MODE globals - callers
    that did `from pandora_theme import palette` keep their own binding to
    the class object that was active at their import time, so live re-theme
    only works for code that reads `pandora_theme.palette` dynamically
    (see pandora_home.py).
    """
    global THEME_MODE, palette
    if mode not in ("light", "dark"):
        return
    THEME_MODE = mode
    palette = light_palette if mode == "light" else dark_palette
    ctk.set_appearance_mode(mode)
    _save_theme_mode(mode)


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)

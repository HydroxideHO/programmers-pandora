"""
Programmer's Pandora - Home
----------------------------
Launcher UI that lists every runnable tool script in this folder and lets
you start any of them with one click.

How it works:
- Scans this directory for top-level .py files (skipping itself and shared
  helper modules like pandora_theme.py).
- Reads each script's module docstring to show a title + description (the
  description shows as a tooltip on hover, to keep the grid uncluttered).
- Launches the selected script as its own detached process - as a compiled
  sibling .exe (no Python needed) when running as a PyInstaller build, or via
  `python <script>.py` when running from source.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library.
"""

import ast
import os
import subprocess
import sys
import tkinter as tk

from tkinter import messagebox
import pandora_theme
from pandora_theme import ctk, font, set_window_icon, SUBPROCESS_FLAGS

ROOT_DIR = pandora_theme.APP_DIR
FROZEN = getattr(sys, "frozen", False)
FROZEN_EXE_SUFFIX = ".exe" if pandora_theme.IS_WINDOWS else ""
EXCLUDED_FILES = {"pandora_theme.py", "pandora_home.py"}

WINDOW_WIDTH = 960
WINDOW_HEIGHT = 540  # 16:9
GRID_COLUMNS = 3

DEFAULT_ICON = "\U0001F9F0"  # toolbox
TOOL_ICONS = {
    "ip_monitor.py": "\U0001F3AF",           # target
    "mac_monitor.py": "\U0001F3F7️",     # tag
    "network_scanner.py": "\U0001F50D",       # magnifying glass
    "wifi_monitor.py": "\U0001F4F6",          # signal bars
    "wifi_spectrum_view.py": "\U0001F4E1",    # satellite antenna
    "androidtv_pubkey.py": "\U0001F511",      # key
}

# Each glyph above has different built-in padding within its own character
# cell (e.g. the target and signal-bars glyphs happen to fill their cell
# symmetrically, but the tag/magnifying-glass/satellite glyphs sit high with
# extra invisible space below them) - centering the label alone leaves them
# visibly misaligned against each other, so nudge a few of them (dx, dy) in
# pixels to visually re-center them against the well-behaved ones.
ICON_OFFSETS = {
    "ip_monitor.py": (0, 0),
    "mac_monitor.py": (0, 7),
    "network_scanner.py": (-2, 7),
    "wifi_monitor.py": (0, 0),
    "wifi_spectrum_view.py": (-2, 11),
    "androidtv_pubkey.py": (0, 6),
}


def parse_script_info(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        docstring = ast.get_docstring(ast.parse(source))
    except (SyntaxError, OSError):
        docstring = None

    name = os.path.splitext(os.path.basename(path))[0]
    if not docstring:
        return name, ""

    lines = [line.strip() for line in docstring.strip().splitlines()]
    title = lines[0] if lines else name

    idx = 1
    while idx < len(lines) and lines[idx] and set(lines[idx]) <= {"-"}:
        idx += 1

    desc_lines = []
    for line in lines[idx:]:
        if not line:
            break
        desc_lines.append(line)

    return title, " ".join(desc_lines)


def discover_scripts():
    scripts = []
    for entry in sorted(os.listdir(ROOT_DIR)):
        path = os.path.join(ROOT_DIR, entry)
        if not entry.endswith(".py") or entry in EXCLUDED_FILES or not os.path.isfile(path):
            continue

        launch_path = path
        if FROZEN:
            exe_path = os.path.join(ROOT_DIR, os.path.splitext(entry)[0] + FROZEN_EXE_SUFFIX)
            if not os.path.isfile(exe_path):
                continue
            launch_path = exe_path

        title, description = parse_script_info(path)
        scripts.append({
            "path": launch_path,
            "file": entry,
            "title": title,
            "description": description,
            "icon": TOOL_ICONS.get(entry, DEFAULT_ICON),
            "icon_offset": ICON_OFFSETS.get(entry, (0, 0)),
        })
    return scripts


class Tooltip:
    """Small borderless popup shown near a widget on hover, e.g. a tile's
    full description - keeps the grid itself uncluttered."""

    def __init__(self, get_text):
        self.get_text = get_text
        self.popup = None

    def bind(self, widget):
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, event=None):
        text = self.get_text()
        if not text or self.popup is not None or event is None:
            return
        palette = pandora_theme.palette
        self.popup = tk.Toplevel(event.widget)
        self.popup.wm_overrideredirect(True)
        self.popup.wm_geometry(f"+{event.widget.winfo_rootx()}+{event.widget.winfo_rooty() + event.widget.winfo_height() + 6}")
        tk.Label(
            self.popup,
            text=text,
            justify="left",
            wraplength=320,
            background=palette.surface_alt,
            foreground=palette.text,
            font=(pandora_theme.FONT_FAMILY, 10),
            padx=10,
            pady=6,
            relief="solid",
            borderwidth=1,
        ).pack()

    def hide(self, event=None):
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None


class PandoraHome:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Programmer's Pandora")
        root.resizable(False, False)
        self.center_window()
        self.build_ui()

    def center_window(self):
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - WINDOW_WIDTH) // 2
        y = (screen_h - WINDOW_HEIGHT) // 2
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")

    def build_ui(self):
        palette = pandora_theme.palette
        self.root.configure(fg_color=palette.bg)

        header = ctk.CTkFrame(self.root, fg_color=palette.surface, corner_radius=0)
        header.pack(fill="x")

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x", padx=24, pady=(16, 0))
        ctk.CTkLabel(
            title_row, text="Programmer's Pandora", font=font(20, "bold"), text_color=palette.text
        ).pack(side="left")
        ctk.CTkButton(
            title_row,
            text="Refresh",
            width=90,
            fg_color=palette.surface_alt,
            hover_color=palette.border,
            text_color=palette.text,
            command=self.refresh,
        ).pack(side="right")
        self.dark_mode_var = ctk.BooleanVar(value=pandora_theme.THEME_MODE == "dark")
        ctk.CTkSwitch(
            title_row,
            text="Dark mode",
            variable=self.dark_mode_var,
            onvalue=True,
            offvalue=False,
            font=font(11),
            text_color=palette.text_muted,
            fg_color=palette.border,
            progress_color=palette.accent,
            button_color=palette.surface,
            button_hover_color=palette.surface,
            command=self.on_theme_toggle,
        ).pack(side="right", padx=(0, 12))

        ctk.CTkLabel(
            header, text="Pick a tool to launch", font=font(12), text_color=palette.text_muted
        ).pack(anchor="w", padx=24, pady=(2, 16))

        self.grid_frame = ctk.CTkScrollableFrame(self.root, fg_color=palette.bg)
        self.grid_frame.pack(fill="both", expand=True, padx=16, pady=16)
        for col in range(GRID_COLUMNS):
            self.grid_frame.grid_columnconfigure(col, weight=1, uniform="tile")

        self.status_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            self.root, textvariable=self.status_var, font=font(11), text_color=palette.text_muted
        ).pack(anchor="w", padx=24, pady=(0, 10))

        self.refresh()

    def on_theme_toggle(self):
        pandora_theme.set_theme_mode("dark" if self.dark_mode_var.get() else "light")
        self.rebuild_ui()

    def rebuild_ui(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        self.build_ui()

    def refresh(self):
        palette = pandora_theme.palette
        for widget in self.grid_frame.winfo_children():
            widget.destroy()

        scripts = discover_scripts()
        if not scripts:
            ctk.CTkLabel(
                self.grid_frame, text="No scripts found in this folder.", text_color=palette.text_muted
            ).grid(row=0, column=0, columnspan=GRID_COLUMNS, sticky="w", pady=8)
            return

        for index, script in enumerate(scripts):
            row, col = divmod(index, GRID_COLUMNS)
            self.make_tile(script, row, col)

    def make_tile(self, script: dict, row: int, col: int):
        palette = pandora_theme.palette

        tile = ctk.CTkFrame(self.grid_frame, fg_color=palette.surface, corner_radius=14, height=150)
        tile.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
        tile.grid_propagate(False)

        icon_area = ctk.CTkFrame(tile, fg_color="transparent", height=70)
        icon_area.pack(fill="x", pady=(14, 4))
        icon_area.pack_propagate(False)

        dx, dy = script["icon_offset"]
        icon_label = ctk.CTkLabel(icon_area, text=script["icon"], font=font(34), text_color=palette.accent)
        icon_label.place(relx=0.5, rely=0.5, anchor="center", x=dx, y=dy)

        title_label = ctk.CTkLabel(
            tile, text=script["title"], font=font(13, "bold"), text_color=palette.text,
            wraplength=220, justify="center",
        )
        title_label.pack(padx=12)

        def do_launch(_event=None, p=script["path"], t=script["title"]):
            self.launch(p, t)

        def on_enter(_event=None):
            tile.configure(fg_color=palette.surface_alt)

        def on_leave(_event=None):
            tile.configure(fg_color=palette.surface)

        tooltip_text = f"{script['description']}\n\n{script['file']}" if script["description"] else script["file"]
        tooltip = Tooltip(lambda t=tooltip_text: t)

        for widget in (tile, icon_area, icon_label, title_label):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", do_launch)
            widget.bind("<Enter>", on_enter, add="+")
            widget.bind("<Leave>", on_leave, add="+")
            tooltip.bind(widget)

    def launch(self, path: str, title: str):
        command = [path] if FROZEN else [sys.executable, path]
        try:
            subprocess.Popen(command, cwd=ROOT_DIR, **SUBPROCESS_FLAGS)
            self.status_var.set(f"Launched: {title}")
        except OSError as exc:
            messagebox.showerror("Launch failed", f"Could not launch {title}:\n{exc}")


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = PandoraHome(root)
    root.mainloop()

"""
WiFi Spectrum View
-------------------
Live occupancy chart of nearby WiFi networks plotted against frequency, with
reference channel markers for 2.4 GHz WiFi, 5 GHz WiFi, and Zigbee.

How it works:
- Before each read, asks Windows to actively rescan via the WLAN API's
  WlanScan() call (wlanapi.dll) - the same call the WiFi flyout/settings UI
  triggers when you open it. Without this, `netsh wlan show networks` only
  reads whatever list Windows already has cached, which can go stale for
  minutes since background scanning is heavily throttled.
- Then runs `netsh wlan show networks mode=bssid` (built into Windows) and
  parses every visible SSID/BSSID's channel and signal strength.
- Plots each network as a bump centered on its channel's center frequency,
  height set by signal percentage, alongside light gridlines marking every
  standard 2.4 GHz or 5 GHz WiFi channel.
- This is NOT a true RF spectrum analyzer - a laptop WiFi adapter can only
  report per-network channel + signal, not raw power across frequency (that
  needs SDR hardware). It's an occupancy view, the same approach tools like
  WiFi Analyzer use.
- Zigbee (802.15.4) channels 11-26 are drawn as fixed dashed reference
  markers on the 2.4 GHz view for interference troubleshooting - they are
  NOT live readings. Detecting real Zigbee traffic needs a dedicated
  802.15.4 sniffer (e.g. a CC2531/CC2652 dongle), which a WiFi adapter
  cannot do.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library (Windows only).
"""

import ctypes
from ctypes import wintypes
import re
import subprocess
import threading
import time
import queue

import tkinter as tk

from pandora_theme import ctk, palette, font, FONT_FAMILY, set_window_icon

POLL_SECONDS = 3.0
SCAN_SETTLE_SECONDS = 2.5  # time to let WlanScan() finish before reading results

# --- Channel/frequency tables -----------------------------------------

WIFI_24_CHANNELS = list(range(1, 14))  # 1-13; channel 14 is Japan-only/rare, omitted
WIFI_5_CHANNELS = [
    36, 40, 44, 48, 52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
    149, 153, 157, 161, 165,
]
ZIGBEE_CHANNELS = list(range(11, 27))  # 11-26, the worldwide 2.4 GHz Zigbee band

WIFI_24_WIDTH_MHZ = 22   # approx occupied bandwidth of a 20 MHz 2.4 GHz channel
WIFI_5_WIDTH_MHZ = 20    # conservative default; actual width (40/80/160) isn't reported by netsh
ZIGBEE_WIDTH_MHZ = 2     # 802.15.4 occupied bandwidth (channels are spaced 5 MHz apart)


def freq_24(channel: int) -> float:
    return 2412 + 5 * (channel - 1)


def freq_5(channel: int) -> float:
    return 5000 + 5 * channel


def freq_zigbee(channel: int) -> float:
    return 2405 + 5 * (channel - 11)


def channel_to_freq(channel: int):
    """Best-effort channel -> center frequency (MHz) for 2.4/5 GHz WiFi."""
    if 1 <= channel <= 13:
        return freq_24(channel), "2.4"
    if 36 <= channel <= 177:
        return freq_5(channel), "5"
    return None, None


COLOR_CYCLE = [
    "#5b8def", "#4caf7d", "#e5534b", "#e0b83a", "#b366ff",
    "#33c6c6", "#ff8a3d", "#7ee787", "#ff6ec7", "#9aa5b1",
]


def color_for_key(key: str) -> str:
    return COLOR_CYCLE[sum(key.encode("utf-8", "ignore")) % len(COLOR_CYCLE)]


def signal_to_dbm(percent: int) -> int:
    return (percent // 2) - 100


# --- Active scan trigger (Windows WLAN API) -------------------------------
# netsh only reads whatever network list Windows already has cached; it does
# not itself request a fresh over-the-air scan. Opening the WiFi flyout or
# Settings does, by calling WlanScan() - so we call the same API function
# ourselves rather than relying on the user having that window open.

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", _GUID),
        ("strInterfaceDescription", ctypes.c_wchar * 256),
        ("isState", ctypes.c_uint),
    ]


class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", ctypes.c_ulong),
        ("dwIndex", ctypes.c_ulong),
        ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1),
    ]


def trigger_active_scan() -> bool:
    try:
        wlanapi = ctypes.WinDLL("wlanapi.dll")
    except OSError:
        return False

    handle = wintypes.HANDLE()
    negotiated_version = ctypes.c_ulong()
    if wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated_version), ctypes.byref(handle)) != 0:
        return False

    try:
        p_if_list = ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)()
        if wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(p_if_list)) != 0:
            return False
        try:
            if_list = p_if_list.contents
            if if_list.dwNumberOfItems == 0:
                return False
            guid = if_list.InterfaceInfo[0].InterfaceGuid
            return wlanapi.WlanScan(handle, ctypes.byref(guid), None, None, None) == 0
        finally:
            wlanapi.WlanFreeMemory(p_if_list)
    finally:
        wlanapi.WlanCloseHandle(handle, None)


# --- Scanning ------------------------------------------------------------

def scan_networks() -> list:
    output = subprocess.run(
        ["netsh", "wlan", "show", "networks", "mode=bssid"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout

    results = []
    current_ssid = None
    current = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()

        ssid_m = re.match(r"^SSID \d+\s*:\s*(.*)$", line)
        if ssid_m:
            current_ssid = ssid_m.group(1).strip() or "(hidden)"
            continue

        bssid_m = re.match(r"^\s+BSSID \d+\s*:\s*([0-9A-Fa-f:]{17})", line)
        if bssid_m:
            if current is not None and current["channel"] is not None:
                results.append(current)
            current = {"ssid": current_ssid, "bssid": bssid_m.group(1), "signal": None, "radio": None, "channel": None}
            continue

        if current is None:
            continue

        sig_m = re.match(r"^\s+Signal\s*:\s*(\d+)%", line)
        if sig_m:
            current["signal"] = int(sig_m.group(1))
            continue

        radio_m = re.match(r"^\s+Radio type\s*:\s*(.+)$", line)
        if radio_m:
            current["radio"] = radio_m.group(1).strip()
            continue

        chan_m = re.match(r"^\s+Channel\s*:\s*(\d+)", line)
        if chan_m:
            current["channel"] = int(chan_m.group(1))
            continue

    if current is not None and current["channel"] is not None:
        results.append(current)

    return results


# --- App -------------------------------------------------------------

class WifiSpectrumViewApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("WiFi Spectrum View")
        root.geometry("880x640")
        root.minsize(680, 480)
        root.configure(fg_color=palette.bg)

        self.gui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.networks = []  # last scan results
        self.band = ctk.StringVar(value="2.4")
        self.show_zigbee = ctk.BooleanVar(value=True)

        header = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(
            header, text="WiFi Spectrum View", font=font(20, "bold"), text_color=palette.text
        ).pack(anchor="w", padx=20, pady=(16, 0))
        ctk.CTkLabel(
            header,
            text="Approximate channel occupancy from visible WiFi networks - not a true RF spectrum sweep.",
            font=font(12),
            text_color=palette.text_muted,
        ).pack(anchor="w", padx=20, pady=(2, 16))

        controls = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=10)
        controls.pack(fill="x", padx=16, pady=12)

        band_row = ctk.CTkFrame(controls, fg_color="transparent")
        band_row.pack(side="left", padx=(16, 8), pady=12)
        ctk.CTkSegmentedButton(
            band_row,
            values=["2.4", "5"],
            variable=self.band,
            command=lambda _v: self.redraw(),
            fg_color=palette.surface_alt,
            selected_color=palette.accent,
            selected_hover_color=palette.accent_hover,
            unselected_color=palette.surface_alt,
            unselected_hover_color=palette.border,
            text_color=palette.text,
        ).pack()
        ctk.CTkLabel(band_row, text="Band (GHz)", font=font(10), text_color=palette.text_muted).pack(pady=(4, 0))

        self.zigbee_check = ctk.CTkCheckBox(
            controls,
            text="Show Zigbee channel markers (11-26)",
            variable=self.show_zigbee,
            font=font(11),
            text_color=palette.text,
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            border_color=palette.border,
            command=self.redraw,
        )
        self.zigbee_check.pack(side="left", padx=16, pady=12)

        button_row = ctk.CTkFrame(controls, fg_color="transparent")
        button_row.pack(side="right", padx=16, pady=12)

        self.start_button = ctk.CTkButton(
            button_row,
            text="Start",
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            command=self.start,
        )
        self.start_button.pack(side="left", padx=(0, 8))

        self.stop_button = ctk.CTkButton(
            button_row,
            text="Stop",
            fg_color=palette.surface_alt,
            hover_color=palette.border,
            text_color=palette.text,
            state="disabled",
            command=self.stop,
        )
        self.stop_button.pack(side="left")

        status_row = ctk.CTkFrame(root, fg_color="transparent")
        status_row.pack(fill="x", padx=20, pady=(0, 8))
        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(
            status_row, textvariable=self.status_var, font=font(12, "bold"), text_color=palette.text
        ).pack(anchor="w")

        self.progress_bar = ctk.CTkProgressBar(
            status_row, mode="indeterminate", progress_color=palette.accent, height=6
        )
        self.progress_bar.pack(fill="x", pady=(6, 0))
        self.progress_bar.set(0)

        canvas_card = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=10)
        canvas_card.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self.canvas = tk.Canvas(canvas_card, bg=palette.bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=12)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Leave>", self.on_canvas_leave)

        self._plot_state = None
        self._last_mouse_xy = None

        self.list_frame = ctk.CTkScrollableFrame(root, fg_color=palette.surface, corner_radius=10, height=130)
        self.list_frame.pack(fill="x", padx=16, pady=(0, 14))

        self.root.after(150, self.poll_queue)
        self._update_zigbee_check_state()

    def _update_zigbee_check_state(self):
        self.zigbee_check.configure(state="normal" if self.band.get() == "2.4" else "disabled")

    # --- worker lifecycle ---

    def start(self):
        self.stop_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Scanning...")
        self.progress_bar.start()

        self.worker_thread = threading.Thread(
            target=self.poll_loop,
            args=(self.stop_event, self.gui_queue),
            daemon=True,
        )
        self.worker_thread.start()

    def stop(self):
        self.stop_event.set()
        self.status_var.set("Stopped.")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress_bar.stop()
        self.progress_bar.set(0)

    def poll_loop(self, stop_event: threading.Event, gui_queue: queue.Queue):
        while not stop_event.is_set():
            scanned = trigger_active_scan()

            for _ in range(int(SCAN_SETTLE_SECONDS * 10)):
                if stop_event.is_set():
                    return
                time.sleep(0.1)

            try:
                networks = scan_networks()
                gui_queue.put(("scan", {"networks": networks, "scanned": scanned}))
            except Exception as exc:
                gui_queue.put(("error", str(exc)))

            remaining = max(0.0, POLL_SECONDS - SCAN_SETTLE_SECONDS)
            for _ in range(int(remaining * 10)):
                if stop_event.is_set():
                    return
                time.sleep(0.1)

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "scan":
                    networks = payload["networks"]
                    self.networks = networks
                    n_24 = sum(1 for n in networks if channel_to_freq(n["channel"])[1] == "2.4")
                    n_5 = sum(1 for n in networks if channel_to_freq(n["channel"])[1] == "5")
                    rescan_note = "" if payload["scanned"] else "  ·  couldn't force a rescan, showing cached list"
                    self.status_var.set(
                        f"{len(networks)} network(s) visible ({n_24} on 2.4 GHz, {n_5} on 5 GHz) "
                        f"- last scan {time.strftime('%H:%M:%S')}{rescan_note}"
                    )
                    self.redraw()
                    self.refresh_list()
                elif kind == "error":
                    self.status_var.set(f"Error: {payload}")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_queue)

    # --- list panel ---

    def refresh_list(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        band = self.band.get()
        entries = [n for n in self.networks if channel_to_freq(n["channel"])[1] == band]
        entries.sort(key=lambda n: n["signal"] or 0, reverse=True)

        if not entries:
            ctk.CTkLabel(
                self.list_frame, text=f"No {band} GHz networks visible.", font=font(11), text_color=palette.text_muted
            ).pack(anchor="w", pady=4)
            return

        for n in entries:
            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            swatch = ctk.CTkFrame(row, fg_color=color_for_key(n["ssid"] + n["bssid"]), width=10, height=10, corner_radius=2)
            swatch.pack(side="left", padx=(4, 8))
            swatch.pack_propagate(False)
            dbm = signal_to_dbm(n["signal"]) if n["signal"] is not None else None
            text = f"{n['ssid']}  ·  ch {n['channel']}  ·  {n['signal']}%" + (f" ({dbm} dBm)" if dbm is not None else "")
            if n["radio"]:
                text += f"  ·  {n['radio']}"
            ctk.CTkLabel(row, text=text, font=font(11), text_color=palette.text).pack(side="left")

    # --- chart drawing ---

    def redraw(self):
        self._update_zigbee_check_state()
        self.canvas.delete("all")
        self._plot_state = None

        canvas_w = self.canvas.winfo_width() or 800
        canvas_h = self.canvas.winfo_height() or 400
        if canvas_w < 10 or canvas_h < 10:
            return

        band = self.band.get()
        margin_l, margin_r, margin_t, margin_b = 44, 20, 16, 34
        plot_w = canvas_w - margin_l - margin_r
        plot_h = canvas_h - margin_t - margin_b
        base_y = margin_t + plot_h

        if band == "2.4":
            channels = WIFI_24_CHANNELS
            freq_fn = freq_24
            width_mhz = WIFI_24_WIDTH_MHZ
            freq_min, freq_max = 2400, 2485
        else:
            channels = WIFI_5_CHANNELS
            freq_fn = freq_5
            width_mhz = WIFI_5_WIDTH_MHZ
            freq_min, freq_max = 5150, 5835

        def to_x(freq_mhz):
            return margin_l + (freq_mhz - freq_min) / (freq_max - freq_min) * plot_w

        def to_y(percent):
            return base_y - (percent / 100.0) * plot_h

        # y gridlines (signal %)
        for pct in (0, 25, 50, 75, 100):
            y = to_y(pct)
            self.canvas.create_line(margin_l, y, canvas_w - margin_r, y, fill=palette.border, width=1)
            self.canvas.create_text(
                margin_l - 6, y, text=str(pct), fill=palette.text_muted, anchor="e", font=(FONT_FAMILY, 8)
            )

        # WiFi channel gridlines + labels
        for ch in channels:
            freq = freq_fn(ch)
            x = to_x(freq)
            self.canvas.create_line(x, margin_t, x, base_y, fill=palette.border, width=1)
            self.canvas.create_text(
                x, base_y + 10, text=str(ch), fill=palette.text_muted, anchor="n", font=(FONT_FAMILY, 8)
            )
        self.canvas.create_text(
            canvas_w - margin_r, margin_t - 4, text="WiFi channel", fill=palette.text_muted,
            anchor="ne", font=(FONT_FAMILY, 8),
        )

        # Zigbee reference markers (2.4 GHz only)
        if band == "2.4" and self.show_zigbee.get():
            for ch in ZIGBEE_CHANNELS:
                freq = freq_zigbee(ch)
                if freq < freq_min or freq > freq_max:
                    continue
                x = to_x(freq)
                self.canvas.create_line(
                    x, margin_t, x, base_y, fill=palette.accent_secondary, width=1, dash=(3, 3)
                )
                self.canvas.create_text(
                    x, margin_t - 2, text=f"Z{ch}", fill=palette.accent_secondary, anchor="s", font=(FONT_FAMILY, 7)
                )

        # network bumps
        entries = [n for n in self.networks if channel_to_freq(n["channel"])[1] == band]
        hit_entries = []
        for n in entries:
            freq, _ = channel_to_freq(n["channel"])
            signal = n["signal"] or 0
            color = color_for_key(n["ssid"] + n["bssid"])
            self._draw_bump(to_x, to_y, base_y, freq, width_mhz, signal, color)
            hit_entries.append((n, freq, signal))

        self._plot_state = {
            "freq_min": freq_min,
            "freq_max": freq_max,
            "margin_l": margin_l,
            "plot_w": plot_w,
            "base_y": base_y,
            "to_y": to_y,
            "width_mhz": width_mhz,
            "entries": hit_entries,
            "canvas_w": canvas_w,
            "canvas_h": canvas_h,
        }

        if not entries:
            self.canvas.create_text(
                (margin_l + canvas_w - margin_r) / 2, (margin_t + base_y) / 2,
                text="No networks visible on this band" if self.networks or self.worker_thread else "Press Start to scan",
                fill=palette.text_muted, font=(FONT_FAMILY, 11),
            )

        if self._last_mouse_xy is not None:
            self._update_tooltip_at(*self._last_mouse_xy)

    def _draw_bump(self, to_x, to_y, base_y, center_freq, width_mhz, signal_pct, color):
        steps = 16
        points = []
        for i in range(-steps, steps + 1):
            t = i / steps  # -1 .. 1
            freq = center_freq + t * width_mhz
            shape = max(0.0, 1 - t * t)  # parabolic bump, 0 at edges
            pct = signal_pct * shape
            points.extend((to_x(freq), to_y(pct)))

        self.canvas.create_line(*points, fill=color, width=2, smooth=True, splinesteps=12)

        base_points = [to_x(center_freq - width_mhz), base_y] + points + [to_x(center_freq + width_mhz), base_y]
        self.canvas.create_polygon(*base_points, fill=color, outline="", stipple="gray25")

    # --- hover tooltip ---

    def on_canvas_motion(self, event):
        self._last_mouse_xy = (event.x, event.y)
        self._update_tooltip_at(event.x, event.y)

    def _update_tooltip_at(self, x, y):
        state = self._plot_state
        if not state or state["plot_w"] <= 0:
            self.hide_tooltip()
            return

        freq_at_x = state["freq_min"] + (x - state["margin_l"]) / state["plot_w"] * (
            state["freq_max"] - state["freq_min"]
        )
        width_mhz = state["width_mhz"]
        base_y = state["base_y"]
        to_y = state["to_y"]

        matches = []
        for n, freq, signal in state["entries"]:
            t = (freq_at_x - freq) / width_mhz
            if -1 <= t <= 1:
                pct = signal * (1 - t * t)
                y_curve = to_y(pct)
                if y_curve <= y <= base_y:
                    matches.append((n, pct))

        if not matches:
            self.hide_tooltip()
            return

        matches.sort(key=lambda m: m[1], reverse=True)

        max_shown = 6
        lines = []
        for n, _pct in matches[:max_shown]:
            dbm = signal_to_dbm(n["signal"]) if n["signal"] is not None else None
            detail = f"ch {n['channel']} - {n['signal']}%" + (f" ({dbm} dBm)" if dbm is not None else "")
            if len(matches) == 1 and n["radio"]:
                detail += f"  ·  {n['radio']}"
            lines.append((n["ssid"], True))
            lines.append((detail, False))
        if len(matches) > max_shown:
            lines.append((f"+{len(matches) - max_shown} more", False))

        self.show_tooltip(x, y, lines, state["canvas_w"], state["canvas_h"])

    def on_canvas_leave(self, event):
        self._last_mouse_xy = None
        self.hide_tooltip()

    def hide_tooltip(self):
        self.canvas.delete("tooltip")

    def show_tooltip(self, x, y, lines, canvas_w, canvas_h):
        self.canvas.delete("tooltip")
        pad = 6
        line_h = 13
        total_h = len(lines) * line_h + pad * 2

        box_x = x + 14
        box_y = y - total_h - 10
        if box_y < pad:
            box_y = y + 14
        box_y = max(pad, min(box_y, canvas_h - total_h - pad))

        text_ids = []
        for i, (text, bold) in enumerate(lines):
            weight = "bold" if bold else "normal"
            tid = self.canvas.create_text(
                box_x + pad, box_y + pad + i * line_h, text=text, anchor="nw",
                fill=palette.text, font=(FONT_FAMILY, 9, weight), tags="tooltip",
            )
            text_ids.append(tid)

        bbox = self.canvas.bbox("tooltip")
        if not bbox:
            return
        x0, y0, x1, y1 = bbox

        if x1 + pad > canvas_w:
            shift = (x - 14) - (x1 + pad)
            self.canvas.move("tooltip", shift, 0)
            bbox = self.canvas.bbox("tooltip")
            x0, y0, x1, y1 = bbox

        rect = self.canvas.create_rectangle(
            x0 - pad, y0 - pad, x1 + pad, y1 + pad,
            fill=palette.surface_alt, outline=palette.border, tags="tooltip",
        )
        self.canvas.tag_lower(rect, text_ids[0])


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = WifiSpectrumViewApp(root)
    root.mainloop()

"""
WiFi Signal Monitor
-------------------
Simple GUI tool that live-polls the currently connected WiFi interface and
displays the access point's BSSID (MAC address), SSID, and signal strength.

How it works:
- Repeatedly runs `netsh wlan show interfaces` (built into Windows).
- Parses the SSID, BSSID, signal percentage, radio type, and channel from
  its output.
- Converts signal percentage to an approximate dBm value for reference.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library (Windows only).
"""

import re
import subprocess
import threading
import time
import queue

from pandora_theme import ctk, palette, font, set_window_icon

POLL_SECONDS = 1.0

FIELD_PATTERNS = {
    "ssid": re.compile(r"^\s*SSID\s*:\s*(.+?)\s*$", re.MULTILINE),
    "bssid": re.compile(r"^\s*AP BSSID\s*:\s*(.+?)\s*$", re.MULTILINE),
    "signal": re.compile(r"^\s*Signal\s*:\s*(\d+)%", re.MULTILINE),
    "rssi": re.compile(r"^\s*Rssi\s*:\s*(-?\d+)", re.MULTILINE),
    "radio": re.compile(r"^\s*Radio type\s*:\s*(.+?)\s*$", re.MULTILINE),
    "channel": re.compile(r"^\s*Channel\s*:\s*(\d+)", re.MULTILINE),
    "state": re.compile(r"^\s*State\s*:\s*(.+?)\s*$", re.MULTILINE),
}


def signal_percent_to_dbm(percent: int) -> int:
    return (percent // 2) - 100


def get_wlan_status() -> dict:
    output = subprocess.run(
        ["netsh", "wlan", "show", "interfaces"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout

    info = {}
    for key, pattern in FIELD_PATTERNS.items():
        match = pattern.search(output)
        info[key] = match.group(1) if match else None

    if info["signal"] is not None:
        info["signal"] = int(info["signal"])

    if info["rssi"] is not None:
        info["dbm"] = int(info["rssi"])
    elif info["signal"] is not None:
        info["dbm"] = signal_percent_to_dbm(info["signal"])
    else:
        info["dbm"] = None

    return info


class WifiMonitorApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("WiFi Signal Monitor")
        root.geometry("380x420")
        root.resizable(False, False)
        root.configure(fg_color=palette.bg)

        self.stop_event = threading.Event()
        self.worker_thread = None
        self.gui_queue = queue.Queue()

        card = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=12)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            card, text="WiFi Signal Monitor", font=font(16, "bold"), text_color=palette.text
        ).pack(anchor="w", padx=20, pady=(20, 16))

        self.signal_bar = ctk.CTkProgressBar(card, progress_color=palette.accent)
        self.signal_bar.set(0)
        self.signal_bar.pack(fill="x", padx=20, pady=(0, 16))

        rows = [
            ("state", "State"),
            ("ssid", "SSID"),
            ("bssid", "AP MAC (BSSID)"),
            ("signal", "Signal"),
            ("dbm", "Approx. dBm"),
            ("radio", "Radio type"),
            ("channel", "Channel"),
        ]

        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=20)
        stats.grid_columnconfigure(1, weight=1)

        self.labels = {}
        for i, (key, label_text) in enumerate(rows):
            ctk.CTkLabel(
                stats, text=label_text, font=font(11), text_color=palette.text_muted
            ).grid(row=i, column=0, sticky="w", pady=4)
            var = ctk.StringVar(value="--")
            ctk.CTkLabel(
                stats, textvariable=var, font=font(12, "bold"), text_color=palette.text
            ).grid(row=i, column=1, sticky="w", pady=4, padx=(10, 0))
            self.labels[key] = var

        button_row = ctk.CTkFrame(card, fg_color="transparent")
        button_row.pack(fill="x", padx=20, pady=(16, 0))

        self.start_button = ctk.CTkButton(
            button_row,
            text="Start",
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            command=self.start,
        )
        self.start_button.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.stop_button = ctk.CTkButton(
            button_row,
            text="Stop",
            fg_color=palette.surface_alt,
            hover_color=palette.border,
            text_color=palette.text,
            state="disabled",
            command=self.stop,
        )
        self.stop_button.pack(side="left", expand=True, fill="x", padx=(6, 0))

        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(
            card, textvariable=self.status_var, font=font(11), text_color=palette.text_muted
        ).pack(anchor="w", padx=20, pady=(16, 20))

        self.root.after(200, self.poll_queue)
        self.start()

    def start(self):
        self.stop_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Monitoring...")

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

    def poll_loop(self, stop_event: threading.Event, gui_queue: queue.Queue):
        while not stop_event.is_set():
            try:
                info = get_wlan_status()
                gui_queue.put(("update", info))
            except Exception as exc:
                gui_queue.put(("error", str(exc)))

            for _ in range(int(POLL_SECONDS * 10)):
                if stop_event.is_set():
                    return
                time.sleep(0.1)

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "update":
                    if payload.get("state") and "connected" in payload["state"].lower():
                        self.labels["state"].set(payload["state"])
                        self.labels["ssid"].set(payload["ssid"] or "--")
                        self.labels["bssid"].set(payload["bssid"] or "--")
                        self.labels["signal"].set(
                            f"{payload['signal']}%" if payload["signal"] is not None else "--"
                        )
                        self.labels["dbm"].set(
                            f"{payload['dbm']} dBm" if payload["dbm"] is not None else "--"
                        )
                        self.labels["radio"].set(payload["radio"] or "--")
                        self.labels["channel"].set(payload["channel"] or "--")
                        self.signal_bar.set((payload["signal"] or 0) / 100)
                    else:
                        self.labels["state"].set(payload.get("state") or "Not connected")
                        for key in ("ssid", "bssid", "signal", "dbm", "radio", "channel"):
                            self.labels[key].set("--")
                        self.signal_bar.set(0)
                elif kind == "error":
                    self.status_var.set(f"Error: {payload}")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_queue)


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = WifiMonitorApp(root)
    root.mainloop()

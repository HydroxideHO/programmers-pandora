"""
MAC Address Network Monitor
----------------------------
Simple GUI tool that scans your local subnet until a specific MAC address
comes online, then shows its current IP address in a success popup.

How it works:
- Repeatedly pings every host in your local /24 subnet (this forces the OS
  to populate its ARP cache with IP<->MAC mappings, even if the target
  blocks ICMP echo replies).
- Reads the system ARP table (`arp -a`) and looks for the target MAC.
- When found, shows the IP in a popup and updates the status label.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library (no scapy/npcap needed). Works on
Windows and macOS - `ping`/`arp` are both present on each, just with
different flags and output formats, which this handles per-platform.
"""

import re
import socket
import subprocess
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor

from tkinter import messagebox
from pandora_theme import ctk, palette, font, set_window_icon, IS_MAC, SUBPROCESS_FLAGS

PING_TIMEOUT_MS = 300
MAX_WORKERS = 100
SWEEP_PAUSE_SECONDS = 2


def normalize_mac(mac: str) -> str:
    # Split on separators and re-pad each octet rather than just stripping
    # punctuation, since macOS's arp -a omits leading zeros on octets (e.g.
    # "8:0:20:1:2:3") - stripping alone would under-count hex digits and
    # fail to match against a properly zero-padded target MAC.
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return "".join(p.zfill(2).lower() for p in parts)
    return re.sub(r"[^0-9a-fA-F]", "", mac).lower()


def is_valid_mac(mac: str) -> bool:
    return len(normalize_mac(mac)) == 12


def get_local_subnet_prefix() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


def ping(ip: str) -> None:
    if IS_MAC:
        cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT_MS), ip]
    else:
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **SUBPROCESS_FLAGS)


def sweep_subnet(prefix: str) -> None:
    targets = [f"{prefix}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(ping, targets))


def get_arp_table() -> dict:
    output = subprocess.run(["arp", "-a"], capture_output=True, text=True, **SUBPROCESS_FLAGS).stdout

    mapping = {}
    if IS_MAC:
        # e.g. "? (192.168.1.1) at ac:de:48:0:11:22 on en0 ifscope [ethernet]"
        entry_re = re.compile(
            r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})"
        )
        for match in entry_re.finditer(output):
            mapping[normalize_mac(match.group(2))] = match.group(1)
    else:
        ip_re = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$")
        mac_re = re.compile(r"^([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}$")
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and ip_re.match(parts[0]) and mac_re.match(parts[1]):
                mapping[normalize_mac(parts[1])] = parts[0]

    return mapping


class MacMonitorApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("MAC Address Monitor")
        root.geometry("420x300")
        root.resizable(False, False)
        root.configure(fg_color=palette.bg)

        self.stop_event = threading.Event()
        self.worker_thread = None
        self.gui_queue = queue.Queue()

        card = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=12)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            card, text="MAC Address Monitor", font=font(16, "bold"), text_color=palette.text
        ).pack(anchor="w", padx=20, pady=(20, 0))
        ctk.CTkLabel(
            card,
            text="Scans your subnet until this device comes online.",
            font=font(11),
            text_color=palette.text_muted,
        ).pack(anchor="w", padx=20, pady=(2, 16))

        ctk.CTkLabel(
            card, text="Target MAC Address", font=font(11), text_color=palette.text_muted
        ).pack(anchor="w", padx=20)
        self.mac_entry = ctk.CTkEntry(card, font=font(13))
        self.mac_entry.pack(fill="x", padx=20, pady=(4, 16))
        self.mac_entry.insert(0, "AA:BB:CC:DD:EE:FF")

        button_row = ctk.CTkFrame(card, fg_color="transparent")
        button_row.pack(fill="x", padx=20)

        self.start_button = ctk.CTkButton(
            button_row,
            text="Start Monitoring",
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

    def start(self):
        mac = self.mac_entry.get().strip()
        if not is_valid_mac(mac):
            messagebox.showerror("Invalid MAC", "Enter a valid MAC address, e.g. AA:BB:CC:DD:EE:FF")
            return

        self.stop_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.mac_entry.configure(state="disabled")
        self.status_var.set("Starting scan...")

        self.worker_thread = threading.Thread(
            target=self.scan_loop,
            args=(normalize_mac(mac), self.stop_event, self.gui_queue),
            daemon=True,
        )
        self.worker_thread.start()

    def stop(self):
        self.stop_event.set()
        self.status_var.set("Stopped.")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.mac_entry.configure(state="normal")

    def scan_loop(self, target_mac: str, stop_event: threading.Event, gui_queue: queue.Queue):
        try:
            prefix = get_local_subnet_prefix()
        except OSError:
            gui_queue.put(("error", "Could not determine local network. Are you connected?"))
            return

        while not stop_event.is_set():
            gui_queue.put(("status", f"Scanning {prefix}.0/24 for {target_mac}..."))
            sweep_subnet(prefix)

            if stop_event.is_set():
                return

            arp = get_arp_table()
            if target_mac in arp:
                gui_queue.put(("found", (target_mac, arp[target_mac])))
                return

            for _ in range(SWEEP_PAUSE_SECONDS * 10):
                if stop_event.is_set():
                    return
                time.sleep(0.1)

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "found":
                    mac, ip = payload
                    self.status_var.set(f"Found! {mac} -> {ip}")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.mac_entry.configure(state="normal")
                    messagebox.showinfo(
                        "Device Online",
                        f"Target device is online!\n\nMAC: {mac}\nIP: {ip}",
                    )
                elif kind == "error":
                    self.status_var.set(f"Error: {payload}")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.mac_entry.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_queue)


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = MacMonitorApp(root)
    root.mainloop()

"""
IP Address Monitor
-------------------
Simple GUI tool that watches a specific IP address until it comes online,
then shows its current MAC address. You give it an IP, it tells you the MAC.

How it works:
- Repeatedly pings the target IP (this forces the OS to populate its ARP
  cache with the IP<->MAC mapping, even if the target blocks ICMP echo
  replies but still answers ARP).
- Reads the system ARP table (`arp -a`) and looks for the target IP.
- When found, shows the MAC in a popup and updates the status label.
- ARP only works for devices on your local subnet, so if the target IP
  isn't in your local /24, a warning is shown before starting.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library (no scapy/npcap needed). Works on
Windows and macOS - `ping`/`arp` are both present on each, just with
different flags and output formats, which this handles per-platform.
"""

import ipaddress
import re
import socket
import subprocess
import threading
import time
import queue

from tkinter import messagebox
from pandora_theme import ctk, palette, font, set_window_icon, IS_MAC, SUBPROCESS_FLAGS

PING_TIMEOUT_MS = 300
POLL_PAUSE_SECONDS = 2


def normalize_mac(mac: str) -> str:
    # Split on separators and re-pad each octet rather than just stripping
    # punctuation, since macOS's arp -a omits leading zeros on octets (e.g.
    # "8:0:20:1:2:3") - stripping alone would misjudge how many hex digits
    # there are and skip normalizing those.
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return ":".join(p.zfill(2).lower() for p in parts)
    return mac


def is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def get_local_subnet_prefix() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


def ping(ip: str) -> None:
    # -n/-w (count/timeout-ms) on Windows vs -c/-W (count/timeout-ms) on
    # macOS - both happen to take the timeout in milliseconds, just under
    # different flags.
    if IS_MAC:
        cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT_MS), ip]
    else:
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **SUBPROCESS_FLAGS)


def get_arp_table() -> dict:
    output = subprocess.run(["arp", "-a"], capture_output=True, text=True, **SUBPROCESS_FLAGS).stdout

    mapping = {}
    if IS_MAC:
        # e.g. "? (192.168.1.1) at ac:de:48:0:11:22 on en0 ifscope [ethernet]"
        # (unresolved entries show "(incomplete)" instead of a MAC, which
        # this pattern simply won't match, same as skipping them).
        entry_re = re.compile(
            r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})"
        )
        for match in entry_re.finditer(output):
            mapping[match.group(1)] = normalize_mac(match.group(2))
    else:
        ip_re = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$")
        mac_re = re.compile(r"^([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}$")
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and ip_re.match(parts[0]) and mac_re.match(parts[1]):
                mapping[parts[0]] = normalize_mac(parts[1])

    return mapping


class IpMonitorApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("IP Address Monitor")
        root.geometry("420x300")
        root.resizable(False, False)
        root.configure(fg_color=palette.bg)

        self.stop_event = threading.Event()
        self.worker_thread = None
        self.gui_queue = queue.Queue()

        card = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=12)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            card, text="IP Address Monitor", font=font(16, "bold"), text_color=palette.text
        ).pack(anchor="w", padx=20, pady=(20, 0))
        ctk.CTkLabel(
            card,
            text="Watches this IP until it comes online, then shows its MAC.",
            font=font(11),
            text_color=palette.text_muted,
        ).pack(anchor="w", padx=20, pady=(2, 16))

        ctk.CTkLabel(
            card, text="Target IP Address", font=font(11), text_color=palette.text_muted
        ).pack(anchor="w", padx=20)
        self.ip_entry = ctk.CTkEntry(card, font=font(13))
        self.ip_entry.pack(fill="x", padx=20, pady=(4, 16))
        self.ip_entry.insert(0, "192.168.1.1")

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
        ip = self.ip_entry.get().strip()
        if not is_valid_ip(ip):
            messagebox.showerror("Invalid IP", "Enter a valid IP address, e.g. 192.168.1.1")
            return

        try:
            prefix = get_local_subnet_prefix()
        except OSError:
            prefix = None

        if prefix and not ip.startswith(prefix + "."):
            proceed = messagebox.askyesno(
                "Different subnet",
                f"{ip} doesn't look like it's on your local network ({prefix}.0/24).\n\n"
                "ARP only reports MAC addresses for devices on your local subnet, so this "
                "may never resolve, or may show the MAC of your gateway instead.\n\n"
                "Start anyway?",
            )
            if not proceed:
                return

        self.stop_event = threading.Event()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.ip_entry.configure(state="disabled")
        self.status_var.set("Starting scan...")

        self.worker_thread = threading.Thread(
            target=self.scan_loop,
            args=(ip, self.stop_event, self.gui_queue),
            daemon=True,
        )
        self.worker_thread.start()

    def stop(self):
        self.stop_event.set()
        self.status_var.set("Stopped.")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.ip_entry.configure(state="normal")

    def scan_loop(self, target_ip: str, stop_event: threading.Event, gui_queue: queue.Queue):
        while not stop_event.is_set():
            gui_queue.put(("status", f"Pinging {target_ip}..."))
            ping(target_ip)

            if stop_event.is_set():
                return

            arp = get_arp_table()
            if target_ip in arp:
                gui_queue.put(("found", (target_ip, arp[target_ip])))
                return

            gui_queue.put(("status", f"Waiting for {target_ip} to respond..."))
            for _ in range(POLL_PAUSE_SECONDS * 10):
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
                    ip, mac = payload
                    self.status_var.set(f"Found! {ip} -> {mac}")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.ip_entry.configure(state="normal")
                    messagebox.showinfo(
                        "Device Online",
                        f"Target device is online!\n\nIP: {ip}\nMAC: {mac}",
                    )
                elif kind == "error":
                    self.status_var.set(f"Error: {payload}")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.ip_entry.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_queue)


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = IpMonitorApp(root)
    root.mainloop()

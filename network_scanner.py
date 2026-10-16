"""
Network Scanner
----------------
GUI tool that scans a chosen network adapter's subnet (or a custom subnet
you type in) and lists every device that responds, with its IP, MAC,
hostname, and manufacturer.

How it works:
- Reads `ipconfig /all` (Windows) or `ifconfig`/`networksetup` (macOS) to
  list your network adapters and their IPv4 address/subnet mask.
- Pick an adapter (auto-fills its subnet as CIDR) or type your own CIDR.
- Pings every host in that subnet to populate the OS ARP cache, then reads
  the ARP table (`arp -a`) to map IP -> MAC for anything that answered.
  Each device appears in the table immediately once found.
- Hostname, manufacturer, and web-server checks then run at the same time
  in the background, filling in each device's row as results come in
  rather than waiting for everything to finish.
- Reverse-DNS resolves each IP for a hostname (best effort; many devices
  won't have one).
- Looks up each MAC's manufacturer from a small built-in table of common
  vendor prefixes; anything not in that table is looked up online via the
  free api.macvendors.com service (requires internet; sends that MAC to a
  third-party service). Successful online lookups are cached to
  vendor_cache.json next to this script, so the same manufacturer prefix
  is never looked up online twice.
- Probes each device on common web ports (80, 443, 8080, 8443) to flag
  ones that look like they're hosting a web page (e.g. router/printer/
  camera admin UIs). Check "Only show devices with a web page" to filter
  the results down to just those.
- For each device with a web server, reads the page's <title> (from the
  same URL double-clicking the row opens) into the Web Page column, so you
  can see what you're about to open. Certificate checks are skipped since
  device admin pages are almost always self-signed.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library. Works on Windows and macOS.
"""

import html
import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

from pandora_theme import ctk, palette, font, FONT_FAMILY, set_window_icon, STATE_DIR, IS_MAC, SUBPROCESS_FLAGS

PING_TIMEOUT_MS = 300
MAX_WORKERS = 100
MAX_HOSTS = 1024
VENDOR_API_URL = "https://api.macvendors.com/{}"
VENDOR_API_TIMEOUT = 2
VENDOR_REQUEST_DELAY = 1.1  # stay under the free API's ~1 req/sec rate limit
VENDOR_RATE_LIMIT_RETRY_DELAY = 2.0
VENDOR_CACHE_FILE = os.path.join(STATE_DIR, "vendor_cache.json")
WEB_PORTS = (80, 443, 8080, 8443)
WEB_CHECK_TIMEOUT = 0.5

# Small set of high-confidence prefixes seen constantly on dev/home networks
# (mostly virtualization). Anything else falls through to the online API.
LOCAL_OUI_VENDORS = {
    "005056": "VMware",
    "000c29": "VMware",
    "080027": "Oracle VirtualBox",
    "00155d": "Microsoft Hyper-V",
    "b827eb": "Raspberry Pi Foundation",
    "dca632": "Raspberry Pi Foundation",
    "e45f01": "Raspberry Pi Foundation",
}


def normalize_mac(mac: str) -> str:
    # Split on separators and re-pad each octet rather than just stripping
    # punctuation, since macOS's arp -a omits leading zeros on octets (e.g.
    # "8:0:20:1:2:3") - stripping alone would misjudge how many hex digits
    # there are and skip normalizing those.
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return ":".join(p.zfill(2).lower() for p in parts)
    return mac


def _hex_netmask_to_dotted(hex_mask: str) -> str:
    value = int(hex_mask, 16)
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _mac_hardware_port_names() -> dict:
    """Maps device names (en0, en1, ...) to their friendly macOS hardware
    port names (Wi-Fi, Ethernet, ...), the same role Windows' "Ethernet
    adapter Ethernet"/"Wireless LAN adapter Wi-Fi" headers serve."""
    output = subprocess.run(
        ["networksetup", "-listallhardwareports"], capture_output=True, text=True, **SUBPROCESS_FLAGS
    ).stdout

    names = {}
    pending_port = None
    for line in output.splitlines():
        port_match = re.match(r"Hardware Port:\s*(.+)$", line)
        if port_match:
            pending_port = port_match.group(1).strip()
            continue
        device_match = re.match(r"Device:\s*(\S+)$", line)
        if device_match and pending_port:
            names[device_match.group(1)] = pending_port
            pending_port = None
    return names


def get_adapters_mac() -> list:
    output = subprocess.run(["ifconfig"], capture_output=True, text=True, **SUBPROCESS_FLAGS).stdout
    hardware_names = _mac_hardware_port_names()

    iface_re = re.compile(r"^(\S+):\s+flags=", re.MULTILINE)
    headers = list(iface_re.finditer(output))

    adapters = []
    for i, header in enumerate(headers):
        device = header.group(1)
        if device == "lo0":
            continue
        start = header.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(output)
        block = output[start:end]

        inet_match = re.search(r"\binet (\d{1,3}(?:\.\d{1,3}){3}) netmask (0x[0-9a-fA-F]+)", block)
        if not inet_match:
            continue

        adapters.append({
            "name": hardware_names.get(device, device),
            "ip": inet_match.group(1),
            "mask": _hex_netmask_to_dotted(inet_match.group(2)),
        })

    return adapters


def get_adapters_windows() -> list:
    output = subprocess.run(
        ["ipconfig", "/all"], capture_output=True, text=True, **SUBPROCESS_FLAGS
    ).stdout

    header_re = re.compile(r"^(\S.*adapter.*):\s*$", re.MULTILINE)
    headers = list(header_re.finditer(output))

    adapters = []
    for i, header in enumerate(headers):
        start = header.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(output)
        block = output[start:end]

        ip_match = re.search(r"IPv4 Address[.\s]*:\s*([\d.]+)", block)
        mask_match = re.search(r"Subnet Mask[.\s]*:\s*([\d.]+)", block)
        if not (ip_match and mask_match):
            continue

        adapters.append({
            "name": header.group(1).strip(),
            "ip": ip_match.group(1),
            "mask": mask_match.group(1),
        })

    return adapters


def get_adapters() -> list:
    return get_adapters_mac() if IS_MAC else get_adapters_windows()


ETHERNET_ADAPTER_RE = re.compile(r"^Ethernet adapter Ethernet(\s+\d+)?$")
WIFI_ADAPTER_RE = re.compile(r"^Wireless LAN adapter Wi-Fi(\s+\d+)?$")


def default_adapter_index(adapters: list) -> int:
    if IS_MAC:
        for i, adapter in enumerate(adapters):
            if "Ethernet" in adapter["name"]:
                return i
        for i, adapter in enumerate(adapters):
            if adapter["name"] == "Wi-Fi":
                return i
        return 0

    for i, adapter in enumerate(adapters):
        if ETHERNET_ADAPTER_RE.match(adapter["name"]):
            return i
    for i, adapter in enumerate(adapters):
        if WIFI_ADAPTER_RE.match(adapter["name"]):
            return i
    return 0


def ping(ip: str) -> bool:
    if IS_MAC:
        cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT_MS), ip]
    else:
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **SUBPROCESS_FLAGS)
    return result.returncode == 0


def sweep_subnet(hosts: list, gui_queue) -> dict:
    results = {}
    total = len(hosts)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(ping, ip): ip for ip in hosts}
        for completed, future in enumerate(as_completed(futures), start=1):
            ip = futures[future]
            results[ip] = future.result()
            gui_queue.put(("progress", ("Pinging hosts", completed, total)))
    return results


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


def resolve_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return ""


def resolve_hostnames(ips: list, gui_queue) -> None:
    total = len(ips)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(resolve_hostname, ip): ip for ip in ips}
        for completed, future in enumerate(as_completed(futures), start=1):
            ip = futures[future]
            gui_queue.put(("field", (ip, "hostname", future.result())))
            gui_queue.put(("progress", ("Resolving hostnames", completed, total)))


def check_port_open(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=WEB_CHECK_TIMEOUT):
            return True
    except OSError:
        return False


TITLE_FETCH_TIMEOUT = 4
TITLE_MAX_BYTES = 65536  # <title> is in <head>; no need to download whole pages
TITLE_MAX_LENGTH = 80
TITLE_MAX_REDIRECT_HOPS = 2
TITLE_UNREADABLE = "(couldn't read page)"
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.IGNORECASE)
# Device UIs often land on a stub page that just forwards to the real one,
# either with <meta http-equiv="refresh"> or a one-line script.
REDIRECT_RES = [
    re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+url=["']?([^"'>\s]+)""", re.IGNORECASE),
    re.compile(r"""(?:window\.|document\.|top\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']"""),
    re.compile(r"""location\.(?:replace|assign)\(\s*["']([^"']+)["']"""),
]


def _title_tls_context() -> ssl.SSLContext:
    # Device admin pages are almost always self-signed, and older devices
    # use ciphers/key sizes modern defaults reject - but this only reads a
    # page title and sends nothing, so accept whatever they offer.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
        context.minimum_version = ssl.TLSVersion.TLSv1
    except (ssl.SSLError, ValueError):
        pass
    return context


def _decode_page(body: bytes, header_charset) -> str:
    meta_match = META_CHARSET_RE.search(body)
    candidates = [header_charset, meta_match.group(1).decode("ascii") if meta_match else None, "utf-8"]
    for charset in candidates:
        if not charset:
            continue
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("cp1252", errors="replace")  # undeclared legacy pages are usually Windows-1252


def _read_page(url: str, context: ssl.SSLContext):
    """Returns (status, final_url, text). Error statuses are returned, not
    raised, since login/error pages still often have a useful title."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Programmer's Pandora)"})
    try:
        resp = urllib.request.urlopen(request, timeout=TITLE_FETCH_TIMEOUT, context=context)
    except urllib.error.HTTPError as exc:
        resp = exc
    with resp:
        status = getattr(resp, "status", None) or resp.code
        try:
            body = resp.read(TITLE_MAX_BYTES)
        except http.client.IncompleteRead as exc:  # device closed the connection early
            body = exc.partial
        return status, resp.url, _decode_page(body, resp.headers.get_content_charset())


def fetch_page_title(url: str) -> str:
    """Returns the page's <title>; "" if it has none; "(no title, HTTP n)"
    for an untitled error page; TITLE_UNREADABLE if it couldn't be fetched."""
    context = _title_tls_context()
    status = None
    try:
        for _ in range(TITLE_MAX_REDIRECT_HOPS + 1):
            status, final_url, text = _read_page(url, context)
            match = TITLE_RE.search(text)
            title = " ".join(html.unescape(match.group(1)).split()) if match else ""
            if title:
                return title[:TITLE_MAX_LENGTH]
            target = next((m.group(1) for regex in REDIRECT_RES if (m := regex.search(text))), None)
            if not target:
                break
            url = urllib.parse.urljoin(final_url, html.unescape(target))
    except (OSError, ValueError, http.client.HTTPException):
        return TITLE_UNREADABLE

    return f"(no title, HTTP {status})" if status and status >= 400 else ""


def resolve_web_servers(ips: list, gui_queue) -> None:
    pending_ports = {ip: set(WEB_PORTS) for ip in ips}
    open_ports = {ip: [] for ip in ips}
    pairs = [(ip, port) for ip in ips for port in WEB_PORTS]
    total = len(ips)
    devices_done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_port_open, ip, port): (ip, port) for ip, port in pairs}
        for future in as_completed(futures):
            ip, port = futures[future]
            if future.result():
                open_ports[ip].append(port)
            pending_ports[ip].discard(port)
            if not pending_ports[ip]:
                devices_done += 1
                gui_queue.put(("field", (ip, "web", sorted(open_ports[ip]))))
                gui_queue.put(("progress", ("Checking for web servers", devices_done, total)))

    # Read each detected page's <title> from the same URL double-clicking
    # the row opens, so the table shows what that click leads to.
    urls = {ip: build_web_url(ip, ports) for ip, ports in open_ports.items() if ports}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_page_title, url): ip for ip, url in urls.items()}
        for completed, future in enumerate(as_completed(futures), start=1):
            gui_queue.put(("field", (futures[future], "title", future.result())))
            gui_queue.put(("progress", ("Reading web page titles", completed, len(urls))))


def build_web_url(ip: str, ports: list):
    if 443 in ports:
        return f"https://{ip}"
    if 80 in ports:
        return f"http://{ip}"
    if 8443 in ports:
        return f"https://{ip}:8443"
    if 8080 in ports:
        return f"http://{ip}:8080"
    return None


def lookup_vendor_online(mac: str) -> str:
    try:
        with urllib.request.urlopen(VENDOR_API_URL.format(mac), timeout=VENDOR_API_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore").strip()
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            return ""
    except OSError:
        return ""

    # Rate-limited: wait once and retry before giving up on this device.
    time.sleep(VENDOR_RATE_LIMIT_RETRY_DELAY)
    try:
        with urllib.request.urlopen(VENDOR_API_URL.format(mac), timeout=VENDOR_API_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore").strip()
    except OSError:
        return ""


def load_vendor_cache() -> dict:
    try:
        with open(VENDOR_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_vendor_cache(cache: dict) -> None:
    try:
        with open(VENDOR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except OSError:
        pass


def resolve_vendors(ip_by_mac: dict, gui_queue) -> None:
    macs_by_oui = {}
    unique_ouis = []
    for mac in ip_by_mac:
        oui = mac.replace(":", "")[:6]
        if oui not in macs_by_oui:
            unique_ouis.append(oui)
        macs_by_oui.setdefault(oui, []).append(mac)

    disk_cache = load_vendor_cache()
    to_look_up = [
        oui for oui in unique_ouis if oui not in LOCAL_OUI_VENDORS and oui not in disk_cache
    ]
    total = len(to_look_up)

    looked_up = 0
    cache_dirty = False
    for oui in unique_ouis:
        if oui in LOCAL_OUI_VENDORS:
            vendor = LOCAL_OUI_VENDORS[oui]
        elif oui in disk_cache:
            vendor = disk_cache[oui]
        else:
            if looked_up > 0:
                time.sleep(VENDOR_REQUEST_DELAY)
            looked_up += 1
            gui_queue.put(("progress", ("Looking up manufacturers", looked_up, total)))

            vendor = lookup_vendor_online(macs_by_oui[oui][0])
            if vendor:
                disk_cache[oui] = vendor
                cache_dirty = True

        for mac in macs_by_oui[oui]:
            gui_queue.put(("field", (ip_by_mac[mac], "vendor", vendor)))

    if cache_dirty:
        save_vendor_cache(disk_cache)


def configure_treeview_style() -> str:
    style = ttk.Style()
    style.theme_use("clam")

    style.configure(
        "Pandora.Treeview",
        background=palette.surface,
        fieldbackground=palette.surface,
        foreground=palette.text,
        bordercolor=palette.surface,
        borderwidth=0,
        rowheight=26,
        font=(FONT_FAMILY, 10),
    )
    style.map(
        "Pandora.Treeview",
        background=[("selected", palette.accent)],
        foreground=[("selected", "#ffffff")],
    )
    style.configure(
        "Pandora.Treeview.Heading",
        background=palette.surface_alt,
        foreground=palette.text,
        borderwidth=0,
        relief="flat",
        font=(FONT_FAMILY, 10, "bold"),
    )
    style.map("Pandora.Treeview.Heading", background=[("active", palette.surface_alt)])

    style.configure(
        "Pandora.Vertical.TScrollbar",
        background=palette.surface_alt,
        troughcolor=palette.surface,
        bordercolor=palette.surface,
        arrowcolor=palette.text_muted,
        relief="flat",
    )
    return "Pandora.Treeview"


COLUMN_LABELS = {
    "ip": "IP Address",
    "mac": "MAC Address",
    "hostname": "Hostname",
    "vendor": "Manufacturer",
    "web": "Web Server",
    "page": "Web Page",
}

SORT_KEYS = {
    "ip": lambda ip, device: ipaddress.ip_address(ip),
    "mac": lambda ip, device: device["mac"] or "",
    "hostname": lambda ip, device: (device["hostname"] or "").lower(),
    "vendor": lambda ip, device: (device["vendor"] or "").lower(),
    "web": lambda ip, device: tuple(device["web"]),
    # Real titles first; "(no title)"-style placeholders and unread pages after.
    "page": lambda ip, device: (not device["title"] or device["title"].startswith("("), (device["title"] or "").lower()),
}


class NetworkScannerApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Network Scanner")
        root.geometry("1000x600")
        root.minsize(820, 420)
        root.configure(fg_color=palette.bg)

        self.adapters = []
        self.adapter_labels = []
        self.sort_column = None
        self.sort_reverse = False
        self.devices = {}
        self.tree_items = {}
        self.gui_queue = queue.Queue()

        header = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(
            header, text="Network Scanner", font=font(20, "bold"), text_color=palette.text
        ).pack(anchor="w", padx=20, pady=(16, 0))
        ctk.CTkLabel(
            header,
            text="Find devices on your network, with hostname, manufacturer, and web page detection.",
            font=font(12),
            text_color=palette.text_muted,
        ).pack(anchor="w", padx=20, pady=(2, 16))

        controls = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=10)
        controls.pack(fill="x", padx=16, pady=12)
        controls.grid_columnconfigure(1, weight=1)

        def field_label(text, row):
            ctk.CTkLabel(controls, text=text, font=font(11), text_color=palette.text_muted).grid(
                row=row, column=0, sticky="w", padx=(16, 10), pady=8
            )

        field_label("Network Adapter", 0)
        self.adapter_combo = ctk.CTkComboBox(
            controls,
            values=[],
            font=font(12),
            dropdown_font=font(12),
            fg_color=palette.surface_alt,
            border_color=palette.border,
            button_color=palette.surface_alt,
            button_hover_color=palette.border,
            dropdown_fg_color=palette.surface_alt,
            command=self.on_adapter_selected,
        )
        self.adapter_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=8)
        ctk.CTkButton(
            controls,
            text="Refresh",
            width=90,
            fg_color=palette.surface_alt,
            hover_color=palette.border,
            text_color=palette.text,
            command=self.refresh_adapters,
        ).grid(row=0, column=2, sticky="e", padx=(0, 16), pady=8)

        field_label("Subnet (CIDR)", 1)
        self.subnet_entry = ctk.CTkEntry(
            controls,
            font=font(12),
            fg_color=palette.surface_alt,
            border_color=palette.border,
            placeholder_text=f"Any CIDR size, e.g. 10.0.0.0/16 - up to {MAX_HOSTS} hosts",
        )
        self.subnet_entry.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=8)
        self.scan_button = ctk.CTkButton(
            controls,
            text="Scan",
            width=90,
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            command=self.start_scan,
        )
        self.scan_button.grid(row=1, column=2, sticky="e", padx=(0, 16), pady=8)

        field_label("Search", 2)
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.apply_filter())
        self.search_entry = ctk.CTkEntry(
            controls,
            textvariable=self.search_var,
            font=font(12),
            fg_color=palette.surface_alt,
            border_color=palette.border,
            placeholder_text="Filter by IP, MAC, hostname, or manufacturer...",
        )
        self.search_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=8)

        self.web_only_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            controls,
            text="Only show devices with a web page",
            variable=self.web_only_var,
            font=font(11),
            text_color=palette.text_muted,
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            border_color=palette.border,
            command=self.apply_filter,
        ).grid(row=3, column=1, columnspan=2, sticky="w", padx=(0, 16), pady=(0, 12))

        status_row = ctk.CTkFrame(root, fg_color="transparent")
        status_row.pack(fill="x", padx=20, pady=(0, 8))
        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(
            status_row, textvariable=self.status_var, font=font(11), text_color=palette.text_muted
        ).pack(anchor="w")
        self.progress_bar = ctk.CTkProgressBar(status_row, progress_color=palette.accent)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", pady=(6, 0))

        table_frame = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=10)
        table_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        tree_style = configure_treeview_style()
        self.tree = ttk.Treeview(
            table_frame,
            columns=tuple(COLUMN_LABELS),
            show="headings",
            style=tree_style,
        )
        for col in COLUMN_LABELS:
            self.tree.heading(col, text=COLUMN_LABELS[col], command=lambda c=col: self.sort_by(c))
        self.tree.column("ip", width=130)
        self.tree.column("mac", width=140)
        self.tree.column("hostname", width=170)
        self.tree.column("vendor", width=160)
        self.tree.column("web", width=90)
        self.tree.column("page", width=200)

        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview, style="Pandora.Vertical.TScrollbar"
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        scrollbar.pack(side="right", fill="y", padx=(0, 12), pady=12)

        self.tree.bind("<Double-1>", self.on_row_double_click)
        self.tree.bind("<Button-3>", self.on_row_right_click)

        self.root.after(100, self.poll_queue)
        self.refresh_adapters()

    def refresh_adapters(self):
        self.adapters = get_adapters()
        self.adapter_labels = [f"{a['name']}  ({a['ip']})" for a in self.adapters]
        self.adapter_combo.configure(values=self.adapter_labels)
        if self.adapter_labels:
            index = default_adapter_index(self.adapters)
            self.adapter_combo.set(self.adapter_labels[index])
            self.on_adapter_selected(self.adapter_labels[index])
        else:
            self.status_var.set("No active adapters found.")

    def on_adapter_selected(self, label: str):
        if label not in self.adapter_labels:
            return
        adapter = self.adapters[self.adapter_labels.index(label)]
        network = ipaddress.ip_network(f"{adapter['ip']}/{adapter['mask']}", strict=False)
        self.subnet_entry.delete(0, tk.END)
        self.subnet_entry.insert(0, str(network))

    def passes_filter(self, ip: str) -> bool:
        device = self.devices[ip]
        if self.web_only_var.get() and not device["web"]:
            return False
        term = self.search_var.get().strip().lower()
        if not term:
            return True
        haystack = " ".join(
            str(v) for v in (
                ip, device["mac"], device["hostname"], device["vendor"], self.format_web(ip), self.format_page(ip)
            )
        ).lower()
        return term in haystack

    def format_web(self, ip: str) -> str:
        return ", ".join(str(p) for p in self.devices[ip]["web"])

    def format_page(self, ip: str) -> str:
        # None = not read yet (blank); "" = read, but the page has no title.
        title = self.devices[ip]["title"]
        return "(no title)" if title == "" else (title or "")

    def refresh_row(self, ip: str):
        device = self.devices[ip]
        values = (
            ip, device["mac"], device["hostname"], device["vendor"], self.format_web(ip), self.format_page(ip)
        )
        item_id = self.tree_items.get(ip)

        if self.passes_filter(ip):
            if item_id:
                self.tree.item(item_id, values=values)
            else:
                self.tree_items[ip] = self.tree.insert("", tk.END, values=values)
        elif item_id:
            self.tree.delete(item_id)
            del self.tree_items[ip]

    def apply_filter(self):
        for ip in self.devices:
            self.refresh_row(ip)
        self._apply_sort_order()

    def sort_by(self, column: str):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self._apply_sort_order()
        self._update_heading_labels()

    def _apply_sort_order(self):
        if not self.sort_column:
            return
        key_fn = SORT_KEYS[self.sort_column]
        ips = sorted(self.tree_items, key=lambda ip: key_fn(ip, self.devices[ip]), reverse=self.sort_reverse)
        for index, ip in enumerate(ips):
            self.tree.move(self.tree_items[ip], "", index)

    def _update_heading_labels(self):
        for col, label in COLUMN_LABELS.items():
            suffix = ""
            if col == self.sort_column:
                suffix = "  ▼" if self.sort_reverse else "  ▲"
            self.tree.heading(col, text=label + suffix)

    def ip_for_row(self, item_id: str) -> str:
        return self.tree.item(item_id, "values")[0]

    def open_web_page(self, ip: str):
        device = self.devices.get(ip)
        url = build_web_url(ip, device["web"]) if device else None
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("No web page detected", f"No web server was found on {ip}.")

    def copy_to_clipboard(self, value: str, label: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update()
        self.status_var.set(f"Copied {label} to clipboard: {value}")

    def on_row_double_click(self, event):
        item_id = self.tree.identify_row(event.y)
        if item_id:
            self.open_web_page(self.ip_for_row(item_id))

    def on_row_right_click(self, event):
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        self.tree.selection_set(item_id)
        ip = self.ip_for_row(item_id)
        mac = self.devices.get(ip, {}).get("mac", "")

        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=palette.surface_alt,
            fg=palette.text,
            activebackground=palette.accent,
            activeforeground="#ffffff",
            bd=0,
        )
        menu.add_command(label="Copy IP address", command=lambda: self.copy_to_clipboard(ip, "IP address"))
        menu.add_command(
            label="Copy MAC address",
            command=lambda: self.copy_to_clipboard(mac, "MAC address"),
            state="normal" if mac else "disabled",
        )
        menu.add_separator()
        menu.add_command(label="Open web page", command=lambda: self.open_web_page(ip))
        menu.tk_popup(event.x_root, event.y_root)

    def start_scan(self):
        cidr = self.subnet_entry.get().strip()
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            messagebox.showerror("Invalid subnet", f"'{cidr}' is not a valid subnet, e.g. 192.168.1.0/24")
            return

        hosts = [str(ip) for ip in network.hosts()]
        if len(hosts) > MAX_HOSTS:
            messagebox.showerror(
                "Subnet too large",
                f"{cidr} has {len(hosts)} addresses to ping, which would take a while. "
                f"Any CIDR size works here (/16, /22, /28, ...) as long as it's {MAX_HOSTS} "
                "hosts or fewer - e.g. a /22 or smaller, or split a larger range into a few "
                "smaller scans.",
            )
            return

        self.scan_button.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.tree_items = {}
        self.devices = {}
        self.progress_bar.set(0)
        self.status_var.set(f"Scanning {network} ({len(hosts)} hosts)...")

        threading.Thread(target=self.scan_worker, args=(hosts, network), daemon=True).start()

    def scan_worker(self, hosts: list, network: ipaddress.IPv4Network):
        sweep_subnet(hosts, self.gui_queue)
        arp = get_arp_table()

        found = [(ipaddress.ip_address(ip), ip, mac) for ip, mac in arp.items() if ip in set(hosts)]
        found.sort(key=lambda d: d[0])

        ips = [ip for _, ip, _ in found]
        ip_by_mac = {mac: ip for _, ip, mac in found}
        self.gui_queue.put(("devices", [(ip, mac) for _, ip, mac in found]))

        # Hostname, manufacturer, and web-server checks are independent I/O,
        # so they run at the same time instead of one after another.
        workers = [
            threading.Thread(target=resolve_hostnames, args=(ips, self.gui_queue), daemon=True),
            threading.Thread(target=resolve_vendors, args=(ip_by_mac, self.gui_queue), daemon=True),
            threading.Thread(target=resolve_web_servers, args=(ips, self.gui_queue), daemon=True),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.gui_queue.put(("scan_complete", len(found)))

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "progress":
                    label, completed, total = payload
                    self.status_var.set(f"{label}... {completed}/{total}")
                    self.progress_bar.set(completed / total if total else 0)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "devices":
                    for ip, mac in payload:
                        self.devices[ip] = {"mac": mac, "hostname": "", "vendor": "", "web": [], "title": None}
                        self.refresh_row(ip)
                elif kind == "field":
                    ip, field, value = payload
                    if ip in self.devices:
                        self.devices[ip][field] = value
                        self.refresh_row(ip)
                elif kind == "scan_complete":
                    self.status_var.set(f"Found {payload} device(s) online.")
                    self.progress_bar.set(1)
                    self.scan_button.configure(state="normal")
        except queue.Empty:
            pass
        self._apply_sort_order()
        self.root.after(100, self.poll_queue)


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = NetworkScannerApp(root)
    root.mainloop()
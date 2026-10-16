"""Runs each tool's macOS-specific code paths against the CI runner's real
system commands (arp, ifconfig, networksetup, system_profiler, airport) -
the parsing was only ever unit-tested against sample text before this.

The runner has no Wi-Fi hardware, so the WiFi checks only verify those
paths fail gracefully rather than crash; everything else must pass.
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pandora_theme  # noqa: E402
import ip_monitor  # noqa: E402
import mac_monitor  # noqa: E402
import network_scanner  # noqa: E402
import wifi_monitor  # noqa: E402
import wifi_spectrum_view  # noqa: E402

failures = []


def check(name, fn):
    try:
        result = fn()
        print(f"PASS  {name}: {result!r}"[:1500])
    except Exception:
        failures.append(name)
        print(f"FAIL  {name}")
        traceback.print_exc()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def adapters():
    found = network_scanner.get_adapters()
    require(found, "get_adapters() found no adapters")
    require(all(a["ip"] and a["mask"] for a in found), "adapter missing ip/mask")
    return found


def ping_loopback():
    require(network_scanner.ping("127.0.0.1"), "ping to 127.0.0.1 failed - ping flags likely wrong for macOS")
    ip_monitor.ping("127.0.0.1")
    mac_monitor.ping("127.0.0.1")
    return True


def arp_tables():
    by_ip = network_scanner.get_arp_table()
    require(by_ip == ip_monitor.get_arp_table(), "ip_monitor and network_scanner disagree on arp table")
    # Keyed by MAC, so entries sharing one (e.g. broadcast) collapse - only
    # check every MAC it found also appears in the IP-keyed table.
    by_mac = mac_monitor.get_arp_table()
    seen_macs = {mac.replace(":", "") for mac in by_ip.values()}
    require(set(by_mac) <= seen_macs, "mac_monitor found MACs network_scanner didn't")
    return by_ip


def wlan_status():
    info = wifi_monitor.get_wlan_status()
    require(isinstance(info, dict) and "ssid" in info, "unexpected wifi_monitor result shape")
    return info


def wifi_scan():
    try:
        return wifi_spectrum_view.scan_networks()
    except RuntimeError as exc:
        return f"(graceful, airport unavailable) {exc}"


print(f"IS_MAC={pandora_theme.IS_MAC} APP_DIR={pandora_theme.APP_DIR} FONT={pandora_theme.FONT_FAMILY}")
require(pandora_theme.IS_MAC, "IS_MAC is False on a macOS runner")

check("network_scanner.get_adapters", adapters)
check("ping 127.0.0.1 (all three tools)", ping_loopback)
check("arp -a parsing (all three tools)", arp_tables)
check("wifi_monitor.get_wlan_status", wlan_status)
check("wifi_spectrum_view.scan_networks", wifi_scan)

if failures:
    print(f"\n{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("\nAll source-level checks passed.")

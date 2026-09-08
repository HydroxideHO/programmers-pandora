# Programmer's Pandora

A small toolkit of Windows GUI utilities for home-network troubleshooting and
device setup, launched from one home screen. Built with Python and
[customtkinter](https://github.com/TomSchimansky/CustomTkinter) (vendored, so
nothing extra to install).

## Tools

- **IP Address Monitor** - watches an IP until it comes online, then shows its MAC.
- **MAC Address Network Monitor** - watches your subnet until a MAC address comes online, then shows its IP.
- **Network Scanner** - scans a subnet and lists every device found, with hostname, manufacturer, and web-server detection. Sortable, filterable, copy IP/MAC from the right-click menu.
- **WiFi Signal Monitor** - live-polls your current WiFi connection's SSID, signal strength, and channel.
- **WiFi Spectrum View** - occupancy chart of nearby WiFi networks by channel, with 2.4/5 GHz and Zigbee channel markers.
- **Android TV Public Key Extractor** - pulls a device's RSA public key (modulus + exponent) from its TLS certificate, formatted for pairing-handshake driver properties.

Each tool is a standalone script - the launcher (`pandora_home.py`) just
discovers and runs them, so adding a new one is as simple as dropping another
`.py` file in the root with a module docstring.

## Installing

Grab the latest installer from the [Releases](../../releases) page and run
it - no Python required, everything's bundled. It installs to
`%LocalAppData%\Programmer's Pandora` with a Start Menu shortcut.

## Running from source

Requires Python 3.9+ on Windows (uses `tkinter`, included with the standard
installer from python.org).

```
python pandora_home.py
```

## Building the installer yourself

```
installer\build.bat
```

Compiles every tool with PyInstaller, then packages the result with
[Inno Setup](https://jrsoftware.org/isinfo.php) into
`installer\Output\ProgrammersPandora-Setup.exe`.

## License

MIT - see [LICENSE](LICENSE).

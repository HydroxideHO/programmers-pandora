"""
Android TV Public Key Extractor
---------------------------------
Connects to an Android TV / Google Cast remote-pairing service over TLS and
pulls out its RSA public key (modulus + exponent), formatted for pasting
straight into a driver's "Device Public Key Modulus"/"Exponent" properties.

How it works:
- Opens a raw TLS connection to the given host:port (default 6467, the
  standard Android TV remote-pairing port) without verifying the
  certificate - same as `openssl s_client`, since the device's cert is
  normally self-signed and we just want to read the public key it presents,
  not validate a trust chain.
- Reads the certificate's DER bytes directly off the socket via Python's
  built-in ssl module (no external `openssl` binary needed - handy since
  Windows doesn't ship one).
- Locates the RSA public key inside the certificate by finding the
  rsaEncryption OID (1.2.840.113549.1.1.1) and decoding the ASN.1/DER
  SubjectPublicKeyInfo that follows it - a small hand-rolled parser, since
  doing this "properly" would need the third-party `cryptography` package,
  which isn't part of the Python standard library.
- Formats the modulus as continuous uppercase hex with no separators (the
  same format `openssl x509 -noout -modulus` prints after its "Modulus="
  prefix is stripped), and the exponent as plain decimal - verified to
  match openssl's output byte-for-byte against a locally generated test
  certificate.

Uses the vendored customtkinter package in vendor/ for its UI, plus the
rest of the Python standard library (no scapy/openssl/cryptography needed).
"""

import queue
import socket
import ssl
import threading

from pandora_theme import ctk, palette, font, set_window_icon

DEFAULT_PORT = 6467
CONNECT_TIMEOUT = 5.0

RSA_ENCRYPTION_OID = bytes.fromhex("06092a864886f70d010101")  # 1.2.840.113549.1.1.1


def read_der_length(data: bytes, offset: int):
    first = data[offset]
    if first < 0x80:
        return first, offset + 1
    num_bytes = first & 0x7F
    length = int.from_bytes(data[offset + 1:offset + 1 + num_bytes], "big")
    return length, offset + 1 + num_bytes


def extract_rsa_modulus_exponent(der_cert: bytes):
    idx = der_cert.find(RSA_ENCRYPTION_OID)
    if idx == -1:
        raise ValueError("This certificate's public key isn't RSA (no rsaEncryption OID found).")

    pos = idx + len(RSA_ENCRYPTION_OID)
    if der_cert[pos:pos + 2] == b"\x05\x00":  # AlgorithmIdentifier's trailing NULL param
        pos += 2

    if der_cert[pos] != 0x03:  # BIT STRING
        raise ValueError("Unexpected certificate structure (no BIT STRING after RSA algorithm ID).")
    pos += 1
    bitstr_len, pos = read_der_length(der_cert, pos)
    pos += 1  # skip the "unused bits" byte (0 for a byte-aligned key)
    rsa_seq = der_cert[pos:pos + bitstr_len - 1]

    if rsa_seq[0] != 0x30:  # SEQUENCE
        raise ValueError("Unexpected certificate structure (no RSAPublicKey SEQUENCE).")
    _seq_len, p = read_der_length(rsa_seq, 1)

    if rsa_seq[p] != 0x02:  # INTEGER (modulus)
        raise ValueError("Unexpected certificate structure (no modulus INTEGER).")
    mod_len, p2 = read_der_length(rsa_seq, p + 1)
    modulus_bytes = rsa_seq[p2:p2 + mod_len]
    p3 = p2 + mod_len

    if rsa_seq[p3] != 0x02:  # INTEGER (exponent)
        raise ValueError("Unexpected certificate structure (no exponent INTEGER).")
    exp_len, p4 = read_der_length(rsa_seq, p3 + 1)
    exponent_bytes = rsa_seq[p4:p4 + exp_len]

    # DER left-pads an INTEGER with one 0x00 byte when the true high bit
    # would otherwise look like a sign bit - that byte isn't part of the
    # modulus's value, so openssl's -modulus output strips it; match that.
    if len(modulus_bytes) > 1 and modulus_bytes[0] == 0x00:
        modulus_bytes = modulus_bytes[1:]
    modulus_hex = modulus_bytes.hex().upper()

    exponent_int = int.from_bytes(exponent_bytes, "big")
    return modulus_hex, exponent_int


def fetch_der_certificate(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bytes:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls_sock:
            return tls_sock.getpeercert(binary_form=True)


class AndroidTvPubkeyApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        root.title("Android TV Public Key Extractor")
        root.geometry("560x400")
        root.resizable(False, False)
        root.configure(fg_color=palette.bg)

        self.gui_queue = queue.Queue()

        card = ctk.CTkFrame(root, fg_color=palette.surface, corner_radius=12)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            card, text="Android TV Public Key Extractor", font=font(16, "bold"), text_color=palette.text
        ).pack(anchor="w", padx=20, pady=(20, 0))
        ctk.CTkLabel(
            card,
            text="Reads the device's RSA public key straight from its TLS certificate - no openssl, no third-party site.",
            font=font(11),
            text_color=palette.text_muted,
            wraplength=480,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(2, 16))

        field_row = ctk.CTkFrame(card, fg_color="transparent")
        field_row.pack(fill="x", padx=20)

        ctk.CTkLabel(field_row, text="Device IP", font=font(11), text_color=palette.text_muted).grid(
            row=0, column=0, sticky="w"
        )
        self.host_entry = ctk.CTkEntry(
            field_row, font=font(12), fg_color=palette.surface_alt, border_color=palette.border, width=280
        )
        self.host_entry.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.host_entry.insert(0, "192.168.1.244")

        ctk.CTkLabel(field_row, text="Port", font=font(11), text_color=palette.text_muted).grid(
            row=0, column=1, sticky="w", padx=(16, 0)
        )
        self.port_entry = ctk.CTkEntry(
            field_row, font=font(12), fg_color=palette.surface_alt, border_color=palette.border, width=80
        )
        self.port_entry.grid(row=1, column=1, sticky="w", padx=(16, 0), pady=(2, 0))
        self.port_entry.insert(0, str(DEFAULT_PORT))

        self.fetch_button = ctk.CTkButton(
            field_row,
            text="Fetch Public Key",
            fg_color=palette.accent,
            hover_color=palette.accent_hover,
            command=self.start_fetch,
        )
        self.fetch_button.grid(row=1, column=2, sticky="w", padx=(16, 0), pady=(2, 0))

        self.status_var = ctk.StringVar(value="Idle")
        ctk.CTkLabel(
            card, textvariable=self.status_var, font=font(11), text_color=palette.text_muted, wraplength=480,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(14, 6))

        ctk.CTkLabel(card, text="Modulus", font=font(11), text_color=palette.text_muted).pack(
            anchor="w", padx=20, pady=(6, 0)
        )
        modulus_row = ctk.CTkFrame(card, fg_color="transparent")
        modulus_row.pack(fill="x", padx=20, pady=(2, 0))
        self.modulus_entry = ctk.CTkEntry(
            modulus_row, font=("Consolas", 11), fg_color=palette.surface_alt, border_color=palette.border
        )
        self.modulus_entry.pack(side="left", fill="x", expand=True)
        self.copy_modulus_button = ctk.CTkButton(
            modulus_row,
            text="Copy",
            width=70,
            state="disabled",
            fg_color=palette.surface_alt,
            hover_color=palette.border,
            text_color=palette.text,
            command=lambda: self.copy_field(self.modulus_entry, "Modulus"),
        )
        self.copy_modulus_button.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(card, text="Exponent", font=font(11), text_color=palette.text_muted).pack(
            anchor="w", padx=20, pady=(12, 0)
        )
        exponent_row = ctk.CTkFrame(card, fg_color="transparent")
        exponent_row.pack(fill="x", padx=20, pady=(2, 0))
        self.exponent_entry = ctk.CTkEntry(
            exponent_row, font=("Consolas", 11), fg_color=palette.surface_alt, border_color=palette.border, width=140
        )
        self.exponent_entry.pack(side="left")
        self.copy_exponent_button = ctk.CTkButton(
            exponent_row,
            text="Copy",
            width=70,
            state="disabled",
            fg_color=palette.surface_alt,
            hover_color=palette.border,
            text_color=palette.text,
            command=lambda: self.copy_field(self.exponent_entry, "Exponent"),
        )
        self.copy_exponent_button.pack(side="left", padx=(8, 0))

        self.root.after(100, self.poll_queue)

    def start_fetch(self):
        host = self.host_entry.get().strip()
        port_text = self.port_entry.get().strip()
        if not host:
            self.status_var.set("Enter the device's IP address first.")
            return
        try:
            port = int(port_text)
        except ValueError:
            self.status_var.set(f"'{port_text}' isn't a valid port number.")
            return

        self.fetch_button.configure(state="disabled")
        self.copy_modulus_button.configure(state="disabled")
        self.copy_exponent_button.configure(state="disabled")
        self._set_field(self.modulus_entry, "")
        self._set_field(self.exponent_entry, "")
        self.status_var.set(f"Connecting to {host}:{port}...")

        threading.Thread(target=self.fetch_worker, args=(host, port), daemon=True).start()

    def fetch_worker(self, host: str, port: int):
        try:
            der_cert = fetch_der_certificate(host, port)
            modulus_hex, exponent_int = extract_rsa_modulus_exponent(der_cert)
            self.gui_queue.put(("ok", (modulus_hex, exponent_int, host, port)))
        except (OSError, ssl.SSLError, ValueError) as exc:
            self.gui_queue.put(("error", str(exc)))

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "ok":
                    modulus_hex, exponent_int, host, port = payload
                    self._set_field(self.modulus_entry, modulus_hex)
                    self._set_field(self.exponent_entry, str(exponent_int))
                    self.copy_modulus_button.configure(state="normal")
                    self.copy_exponent_button.configure(state="normal")
                    self.status_var.set(f"Fetched RSA public key from {host}:{port}.")
                elif kind == "error":
                    self.status_var.set(f"Error: {payload}")
                self.fetch_button.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    @staticmethod
    def _set_field(entry: ctk.CTkEntry, value: str):
        entry.delete(0, "end")
        entry.insert(0, value)

    def copy_field(self, entry: ctk.CTkEntry, label: str):
        value = entry.get()
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update()
        self.status_var.set(f"Copied {label} to clipboard.")


if __name__ == "__main__":
    root = ctk.CTk()
    set_window_icon(root)
    app = AndroidTvPubkeyApp(root)
    root.mainloop()

"""USB-link networking daemon for ai-hud.

When the device is plugged into a PC over USB, this daemon makes the link
"plug-and-play":

  1. Mini DHCP server (RFC 2131 subset) on usb0:67
     -- offers the PC a static IP so it can talk to the device without any
        manual network configuration.
  2. Mini mDNS responder (RFC 6762 subset) on 224.0.0.251:5353
     -- answers `ai-hud.local` A-record queries with the device's usb0 IP,
        so the user can open http://ai-hud.local/ in any browser.

Bound exclusively to usb0 via SO_BINDTODEVICE so we never leak DHCP/mDNS
onto the wired/wireless networks served by eth0.

No third-party dependencies (Python stdlib only).
"""

import os
import socket
import struct
import sys
import threading
import time


# ---------------------------------------------------------------------------
# Defaults (overridable via env vars when run from S60_usb_netd)
# ---------------------------------------------------------------------------

USB_IFACE = os.environ.get("USB_IFACE", "usb0")
DEVICE_IP = os.environ.get("DEVICE_IP", "172.32.0.93")
CLIENT_IP = os.environ.get("CLIENT_IP", "172.32.0.100")
NETMASK   = os.environ.get("NETMASK",   "255.255.0.0")
HOSTNAME  = os.environ.get("HOSTNAME",  "ai-hud")
LEASE_SEC = int(os.environ.get("LEASE_SEC", "86400"))   # 24h

# DHCP magic cookie + message type codes
_DHCP_MAGIC = b"\x63\x82\x53\x63"
_DHCP_DISCOVER = 1
_DHCP_OFFER    = 2
_DHCP_REQUEST  = 3
_DHCP_ACK      = 5
_DHCP_NAK      = 6

# mDNS constants
_MDNS_ADDR = "224.0.0.251"
_MDNS_PORT = 5353
_DNS_TYPE_A   = 1
_DNS_TYPE_PTR = 12
_DNS_CLASS_IN = 1


# Linux setsockopt level/name (not always exported by Python's socket module).
_SOL_SOCKET = socket.SOL_SOCKET
_SO_BINDTODEVICE = 25


def _log(msg):
    print(f"[usb_netd] {msg}", flush=True)


def _ip_to_bytes(ip):
    return socket.inet_aton(ip)


def _bytes_to_ip(b):
    return socket.inet_ntoa(b)


def _bind_to_iface(sock, iface):
    """Pin the socket to a specific interface so DHCP/mDNS only talk on usb0.

    SO_BINDTODEVICE requires CAP_NET_RAW (root). The HUD process already runs
    as root, so this is fine on the device.
    """
    try:
        sock.setsockopt(_SOL_SOCKET, _SO_BINDTODEVICE, iface.encode() + b"\x00")
    except OSError as e:
        _log(f"WARN: SO_BINDTODEVICE({iface}) failed: {e} -- daemon may "
             f"leak onto other interfaces; root required.")


# ---------------------------------------------------------------------------
# DHCP server
# ---------------------------------------------------------------------------

class _DHCPServer:
    """Single-client DHCP server: always hands out CLIENT_IP, no pool logic."""

    def __init__(self, iface=USB_IFACE, server_ip=DEVICE_IP,
                 client_ip=CLIENT_IP, netmask=NETMASK, lease=LEASE_SEC):
        self.iface = iface
        self.server_ip = server_ip
        self.client_ip = client_ip
        self.netmask = netmask
        self.lease = lease
        self._sock = None

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        _bind_to_iface(s, self.iface)
        s.bind(("0.0.0.0", 67))
        self._sock = s

    def _build_reply(self, req, msg_type):
        """Construct a DHCP OFFER/ACK reply mirroring the request's xid/chaddr."""
        # Request layout (RFC 2131):
        #   op(1) htype(1) hlen(1) hops(1)
        #   xid(4) secs(2) flags(2)
        #   ciaddr(4) yiaddr(4) siaddr(4) giaddr(4)
        #   chaddr(16) sname(64) file(128)
        #   magic(4) options(var)
        xid    = req[4:8]
        flags  = req[10:12]
        chaddr = req[28:44]

        # Reply header (op=2 = BOOTREPLY)
        out = bytearray(240)
        out[0] = 2          # op = BOOTREPLY
        out[1] = 1          # htype = Ethernet
        out[2] = 6          # hlen = 6
        out[3] = 0          # hops
        out[4:8]   = xid
        out[8:10]  = b"\x00\x00"             # secs
        out[10:12] = flags
        out[12:16] = b"\x00\x00\x00\x00"      # ciaddr
        out[16:20] = _ip_to_bytes(self.client_ip)  # yiaddr -- offered IP
        out[20:24] = _ip_to_bytes(self.server_ip)  # siaddr -- next server
        out[24:28] = b"\x00\x00\x00\x00"      # giaddr
        out[28:44] = chaddr
        out[236:240] = _DHCP_MAGIC

        # Options
        opts = bytearray()
        opts += bytes([53, 1, msg_type])                            # DHCP message type
        opts += bytes([54, 4]) + _ip_to_bytes(self.server_ip)       # server identifier
        opts += bytes([51, 4]) + struct.pack("!I", self.lease)      # lease time
        opts += bytes([1, 4])  + _ip_to_bytes(self.netmask)         # subnet mask
        opts += bytes([3, 4])  + _ip_to_bytes(self.server_ip)       # router
        opts += bytes([6, 4])  + _ip_to_bytes(self.server_ip)       # DNS (us)
        opts += bytes([255])                                        # end
        return bytes(out) + bytes(opts)

    def _parse_msg_type(self, pkt):
        """Walk DHCP options to find the message type (option 53)."""
        if len(pkt) < 240 or pkt[236:240] != _DHCP_MAGIC:
            return None
        i = 240
        while i < len(pkt):
            t = pkt[i]
            if t == 0:        # pad
                i += 1
                continue
            if t == 255:      # end
                return None
            if i + 1 >= len(pkt):
                return None
            ln = pkt[i + 1]
            val = pkt[i + 2:i + 2 + ln]
            if t == 53 and ln == 1:
                return val[0]
            i += 2 + ln
        return None

    def serve_forever(self):
        self._open()
        _log(f"DHCP listening on {self.iface}:67 -> offers {self.client_ip}")
        while True:
            try:
                pkt, _src = self._sock.recvfrom(2048)
            except OSError as e:
                _log(f"DHCP recv error: {e}")
                time.sleep(1)
                continue
            if len(pkt) < 240 or pkt[0] != 1:   # op must be BOOTREQUEST
                continue
            mtype = self._parse_msg_type(pkt)
            if mtype == _DHCP_DISCOVER:
                reply = self._build_reply(pkt, _DHCP_OFFER)
                # Broadcast: client has no IP yet.
                self._sock.sendto(reply, ("255.255.255.255", 68))
            elif mtype == _DHCP_REQUEST:
                reply = self._build_reply(pkt, _DHCP_ACK)
                self._sock.sendto(reply, ("255.255.255.255", 68))


# ---------------------------------------------------------------------------
# mDNS responder
# ---------------------------------------------------------------------------

class _MDNSResponder:
    """Tiny responder for one hostname: <HOSTNAME>.local -> DEVICE_IP."""

    def __init__(self, iface=USB_IFACE, host=HOSTNAME, ip=DEVICE_IP):
        self.iface = iface
        self.host = host.lower()
        self.ip = ip
        self._sock = None

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind to the mDNS port; we'll join the group below.
        s.bind(("", _MDNS_PORT))

        # Join multicast group on the USB interface so we only listen there.
        try:
            iface_ip = _get_iface_ip(self.iface)
            mreq = socket.inet_aton(_MDNS_ADDR) + socket.inet_aton(iface_ip)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                         socket.inet_aton(iface_ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError as e:
            _log(f"mDNS: IP_ADD_MEMBERSHIP({self.iface}) failed: {e}")
        self._sock = s

    def _encode_name(self, name):
        """Encode 'ai-hud.local' as DNS labels."""
        out = bytearray()
        for label in name.split("."):
            b = label.encode("ascii")
            out.append(len(b))
            out += b
        out.append(0)
        return bytes(out)

    def _parse_question(self, pkt):
        """Return (qname_lower, qtype) for the first question, or None."""
        if len(pkt) < 12:
            return None
        qdcount = struct.unpack("!H", pkt[4:6])[0]
        if qdcount < 1:
            return None
        i = 12
        labels = []
        while i < len(pkt):
            ln = pkt[i]
            if ln == 0:
                i += 1
                break
            if ln & 0xC0:    # pointer -- skip (not used for outgoing mDNS queries)
                return None
            i += 1
            labels.append(pkt[i:i + ln].decode("ascii", errors="ignore"))
            i += ln
        if i + 4 > len(pkt):
            return None
        qtype = struct.unpack("!H", pkt[i:i + 2])[0]
        # qclass at pkt[i+2:i+4]; ignore
        return ".".join(labels).lower(), qtype

    def _build_response(self, qname, transaction_id=b"\x00\x00"):
        """Build a single A-record response for our hostname."""
        flags = 0x8400        # standard query response + authoritative
        header = struct.pack("!HHHHHH", 0, flags, 0, 1, 0, 0)
        name = self._encode_name(qname)
        # type=A, class=IN | cache-flush (top bit set per RFC 6762)
        rdata = _ip_to_bytes(self.ip)
        answer = name + struct.pack("!HHIH", _DNS_TYPE_A, _DNS_CLASS_IN | 0x8000,
                                    120, 4) + rdata
        return header + answer

    def serve_forever(self):
        self._open()
        target = f"{self.host}.local"
        _log(f"mDNS listening on {self.iface}:5353 -> {target} = {self.ip}")
        while True:
            try:
                pkt, src = self._sock.recvfrom(2048)
            except OSError as e:
                _log(f"mDNS recv error: {e}")
                time.sleep(1)
                continue
            # Only respond to queries from the USB peer (cheap filter).
            if not src[0].startswith("172.32.") and src[0] != "0.0.0.0":
                continue
            parsed = self._parse_question(pkt)
            if parsed is None:
                continue
            qname, qtype = parsed
            if qtype != _DNS_TYPE_A or qname != target:
                continue
            try:
                self._sock.sendto(self._build_response(qname),
                                  (_MDNS_ADDR, _MDNS_PORT))
            except OSError as e:
                _log(f"mDNS send error: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_iface_ip(iface):
    """Return the IPv4 address bound to a given interface, or '' if none."""
    # Read /proc/net/fib_trie isn't trivial; use a UDP socket trick that
    # asks the kernel which source IP routes to the multicast group via iface.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _bind_to_iface(s, iface)
        s.connect(("224.0.0.251", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except OSError:
        pass
    # Fallback: scrape `ip -o -4 addr show <iface>`
    try:
        import subprocess
        out = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show", iface], timeout=2).decode()
        # e.g. "3: usb0    inet 172.32.0.93/16 brd ..."
        for tok in out.split():
            if "." in tok and "/" in tok:
                return tok.split("/")[0]
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Wait briefly for usb0 to have an IP (init order can race startup).
    for _ in range(20):
        if _get_iface_ip(USB_IFACE):
            break
        time.sleep(0.5)
    else:
        _log(f"FATAL: {USB_IFACE} has no IPv4 after 10s, exiting")
        sys.exit(1)

    dhcp = _DHCPServer()
    mdns = _MDNSResponder()

    threads = [
        threading.Thread(target=dhcp.serve_forever, name="dhcp", daemon=True),
        threading.Thread(target=mdns.serve_forever, name="mdns", daemon=True),
    ]
    for t in threads:
        t.start()

    _log("ready")
    # Block forever; daemon threads will keep running.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()

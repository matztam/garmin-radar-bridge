"""
garmin-radar-bridge — Step 1: CDM heartbeat + HD status reports

Emulates a Garmin GMR 18/24 HD radar on the local network so that a
Garmin plotter discovers a "virtual radar" without any real spoke data.

Protocol sources: MarineYachtRadar/mayara-server src/lib/brand/garmin/

Run as root (or with CAP_NET_ADMIN) so multicast sockets can bind.

Usage:
    sudo python3 bridge.py [--iface <ip>]

    --iface   Local IP address on the Garmin marine subnet (172.16.x.x).
              Defaults to auto-detection of the first 172.16/12 address.
"""

import argparse
import socket
import struct
import time
import threading
import logging

log = logging.getLogger("gmr-bridge")

# ── GMN wire constants ────────────────────────────────────────────────────────

CDM_HEARTBEAT_GROUP = "239.254.2.2"
CDM_HEARTBEAT_PORT  = 50050

REPORT_GROUP = "239.254.2.0"
REPORT_PORT  = 50100   # HD: both spokes AND status live here
COMMAND_PORT = 50101   # unicast commands from plotter → radar

# GMN header: [u32 LE msg_id][u32 LE payload_len]
GMN_HEADER = struct.Struct("<II")

# CDM heartbeat message id
MSG_CDM_HEARTBEAT = 0x038e

# HD status / report message ids (radar → MFD)
MSG_HD_STATE        = 0x02a5  # radar state, range, gain …
MSG_HD_SCANNER_ID   = 0x02a6  # scanner identity / version
MSG_HD_CAPABILITY   = 0x02ae  # capability report
MSG_HD_ROTATION_SPEED = 0x02ab  # RPM × 100

# HD settings composite report (msg 0x02A7) — sent in response to 0x02D3
MSG_HD_SETTINGS     = 0x02a7

# HD state codes (msg 0x02A5)
HD_STATE_STANDBY    = 3
HD_STATE_TRANSMIT   = 4

# Product IDs (from mayara discovery.rs)
PRODUCT_ID_GMR_18_HD = 0x01fd  # confirmed in mayara discovery.rs
PRODUCT_ID_GMR_24_HD = 0x0195

# ── CDM heartbeat (30 bytes) ──────────────────────────────────────────────────

def build_cdm_heartbeat(seq: int, syc_group_id: int = 6) -> bytes:
    """Build a CDM V2 heartbeat packet (msg 0x038e) with one radar service.

    Mirrors the layout captured from a real GMR xHD in mayara's test data:
      [GMN header 8B][version+pad+product_id+sim+subtype+syc+const 8B]
      [service_count 1B][pad 3B][service 8B][tail 6B]
    Total: 8 + 8 + 1 + 3 + 8 + 6 = 34 bytes payload, 42 bytes total.
    """
    # 1 service entry: class=1 inst=0 ver=2 reserved=0 id=0x08d40aa0
    # (service ID observed in mayara's captured xHD heartbeat)
    service = struct.pack("<BBH", 1, 0, 2) + struct.pack("<I", 0x08d40aa0)

    payload = bytearray()
    payload += struct.pack("<B", 2)                     # version_marker
    payload += b'\x00'                                  # pad
    payload += struct.pack("<H", PRODUCT_ID_GMR_18_HD)  # product_id
    payload += b'\x00'                                  # simulator_mode
    payload += struct.pack("<B", 5)                     # product_subtype
    payload += struct.pack("<B", syc_group_id)
    payload += b'\x01'                                  # constant
    payload += struct.pack("<B", 1)                     # service_count
    payload += b'\x00\x00\x00'                          # pad
    payload += service                                  # 8 bytes
    payload += b'\x01\x04'                              # tail tag type+len
    payload += struct.pack("<I", seq)                   # sequence

    header = GMN_HEADER.pack(MSG_CDM_HEARTBEAT, len(payload))
    return header + bytes(payload)


# ── HD state / settings packet layout (shared by 0x02A5 and 0x02A7) ─────────
#
# From radar_pi GarminHDReceive.cpp rad_response_pkt struct:
#   Byte offsets are from the start of the full GMN packet (header included).
#
#   [0..4]   msg_id      (GMN header)
#   [4..8]   pay_len=40  (GMN header — payload only, NOT counting this header)
#   [8..10]  scanner_state (u16 LE)  3=standby, 4=transmit
#   [10..12] warmup (u16 LE)
#   [12..16] range_meters (u32 LE, wire value = meters - 1)
#   [16]     gain_level
#   [17]     gain_mode (0=manual, 1=auto)
#   [18..20] fill_1 (u16)
#   [20]     sea_clutter_level
#   [21]     sea_clutter_mode
#   [22..24] fill_2 (u16)
#   [24]     rain_clutter_level
#   [25..28] fill_3[3]
#   [28..30] dome_offset (i16 LE, bearing alignment in degrees)
#   [30]     FTC_mode
#   [31]     crosstalk_onoff
#   [32..36] fill_4[2] (u16[2])
#   [36]     timed_transmit_mode
#   [37]     timed_transmit_transmit
#   [38]     timed_transmit_standby
#   [39]     fill_5
#   [40]     dome_speed (scan_speed: 0=normal, 1=slow)
#   [41..48] fill_6[7]
# Total: 8-byte header + 40-byte payload = 48 bytes on the wire.

_HD_PAYLOAD_SIZE = 40

def _build_hd_payload(state: int, range_m: int) -> bytearray:
    """Build the 40-byte payload common to 0x02A5 and 0x02A7."""
    p = bytearray(_HD_PAYLOAD_SIZE)
    struct.pack_into("<H", p, 0, state)           # scanner_state
    struct.pack_into("<H", p, 2, 0)               # warmup
    struct.pack_into("<I", p, 4, range_m - 1)     # range (meters − 1 on wire)
    p[8]  = 50                                    # gain_level
    p[9]  = 1                                     # gain_mode (1 = auto)
    # p[12] = 0  sea_clutter_level (already 0)
    # p[13] = 0  sea_clutter_mode  (already 0)
    # p[16] = 0  rain_clutter_level (already 0)
    struct.pack_into("<h", p, 20, 0)              # dome_offset (bearing align)
    # p[22] = 0  FTC_mode          (already 0)
    # p[23] = 0  crosstalk_onoff   (already 0)
    # p[32] = 0  dome_speed        (already 0)
    return p


def build_hd_state(state: int = HD_STATE_STANDBY, range_m: int = 1852) -> bytes:
    p = _build_hd_payload(state, range_m)
    header = GMN_HEADER.pack(MSG_HD_STATE, _HD_PAYLOAD_SIZE)
    return header + bytes(p)


# ── HD capability report (msg 0x02AE) ────────────────────────────────────────
#
# Minimal capability report: just signals that this is a legacy HD device
# (not xHD), so the plotter uses the 0x02xx protocol.
# Layout: u16 LE capability_flags (0 = no extra caps)

def build_hd_capability() -> bytes:
    payload = struct.pack("<H", 0)
    header  = GMN_HEADER.pack(MSG_HD_CAPABILITY, len(payload))
    return header + payload


# ── HD scanner ID (msg 0x02A6) ───────────────────────────────────────────────
#
# Identifies the scanner to the MFD. Minimal layout:
#   u16 LE  product_id
#   u8[8]   version string (ASCII, null-padded)

def build_hd_scanner_id() -> bytes:
    # product_id (2B) + software_version (8B ASCII null-padded)
    # Version string must match what the plotter expects for GMR 18 HD.
    # "4.00    " is a plausible live firmware version; "1.0     " may be
    # rejected as too old / incompatible.
    version = b"4.00    "   # 8 bytes
    payload = struct.pack("<H", PRODUCT_ID_GMR_18_HD) + version
    header  = GMN_HEADER.pack(MSG_HD_SCANNER_ID, len(payload))
    return header + payload


# ── HD rotation speed (msg 0x02AB) ───────────────────────────────────────────
#
# RPM × 100 as u16 LE. Typical GMR 18 HD normal speed: 2400 RPM × 100

def build_hd_rotation_speed(rpm: int = 24) -> bytes:
    payload = struct.pack("<H", rpm * 100)
    header  = GMN_HEADER.pack(MSG_HD_ROTATION_SPEED, len(payload))
    return header + payload


# ── Network helpers ───────────────────────────────────────────────────────────

def make_multicast_sender(local_ip: str, group: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(local_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    # Bind to the same port we send on — a real radar sends from port 50100
    # on the report stream and port 50050 on the CDM heartbeat stream.
    sock.bind((local_ip, port))
    sock.connect((group, port))
    return sock


def detect_garmin_ip() -> str | None:
    """Return first local IP in the 172.16.0.0/12 subnet."""
    import ipaddress
    garmin_net = ipaddress.IPv4Network("172.16.0.0/12")
    for iface_ip in _local_ips():
        try:
            if ipaddress.IPv4Address(iface_ip) in garmin_net:
                return iface_ip
        except ValueError:
            pass
    return None


def _local_ips():
    import subprocess, re
    out = subprocess.run(["ip", "-4", "addr"], capture_output=True, text=True).stdout
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_heartbeat(sock_cdm: socket.socket, stop: threading.Event, syc_group_id: int = 6):
    """Send CDM heartbeat: 1 s for first 30 ticks, then 5 s."""
    seq = 0
    while not stop.is_set():
        pkt = build_cdm_heartbeat(seq, syc_group_id)
        try:
            sock_cdm.send(pkt)
            log.debug("CDM heartbeat seq=%d syc_group_id=%d", seq, syc_group_id)
        except OSError as e:
            log.warning("CDM send failed: %s", e)
        seq += 1
        interval = 1.0 if seq <= 30 else 5.0
        stop.wait(interval)


def run_status(sock_report: socket.socket, stop: threading.Event):
    """Send HD status reports every second."""
    pkts = [
        build_hd_scanner_id(),
        build_hd_capability(),
        build_hd_rotation_speed(),
        build_hd_state(HD_STATE_STANDBY),
    ]
    while not stop.is_set():
        for pkt in pkts:
            try:
                sock_report.send(pkt)
            except OSError as e:
                log.warning("Report send failed: %s", e)
        stop.wait(1.0)


def build_hd_settings(state: int = HD_STATE_STANDBY, range_m: int = 1852) -> bytes:
    """Build a 0x02A7 settings report — same 40-byte layout as 0x02A5."""
    p = _build_hd_payload(state, range_m)
    header = GMN_HEADER.pack(MSG_HD_SETTINGS, _HD_PAYLOAD_SIZE)
    return header + bytes(p)


def run_command_listener(local_ip: str, sock_report: socket.socket, stop: threading.Event):
    """Listen on UDP port 50101 for commands from the plotter and respond."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((local_ip, COMMAND_PORT))
    sock.settimeout(1.0)
    log.info("Listening for commands on %s:%d", local_ip, COMMAND_PORT)

    _ee_count = 0  # throttle 0x02EE logging

    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
            if len(data) >= 8:
                msg_id, pay_len = struct.unpack('<II', data[:8])

                if msg_id == 0x02ee:
                    _ee_count += 1
                    if _ee_count <= 3 or _ee_count % 100 == 0:
                        log.info("CMD 0x02ee from %s (count=%d)", addr, _ee_count)
                else:
                    log.info("CMD from %s: msg=%#06x pay_len=%d raw=%s",
                             addr, msg_id, pay_len, data.hex())

                # 0x02D2 — request scanner ID → reply with 0x02A6
                if msg_id == 0x02d2:
                    sock.sendto(build_hd_scanner_id(), addr)
                    log.debug("Replied scanner ID to %s", addr)

                # 0x02D3 — request settings → reply with 0x02A7 + 0x02A5
                elif msg_id == 0x02d3:
                    sock.sendto(build_hd_settings(), addr)
                    sock.sendto(build_hd_state(), addr)
                    log.debug("Replied settings to %s", addr)

                # 0x02EE — request settings report (CMD_HD_REQUEST_SETTINGS)
                # Reply with settings only — NOT scanner ID (0x02A6), since
                # sending 0x02A6 after 0x02EE creates a feedback loop where
                # each scanner-ID triggers another 0x02EE flood.
                elif msg_id == 0x02ee:
                    sock.sendto(build_hd_settings(), addr)
                    sock.sendto(build_hd_state(), addr)
                    log.debug("Replied settings (0x02ee) to %s", addr)

                else:
                    log.info("Unknown CMD %#06x from %s", msg_id, addr)
            else:
                log.info("Short CMD from %s: raw=%s", addr, data.hex())
        except socket.timeout:
            continue
        except OSError as e:
            log.warning("Command listener error: %s", e)
    sock.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iface", help="Local IP on the Garmin subnet (172.16.x.x)")
    args = parser.parse_args()

    local_ip = args.iface or detect_garmin_ip()
    if not local_ip:
        log.error("No IP address in 172.16.0.0/12 found. "
                  "Assign one to your network interface or pass --iface.")
        raise SystemExit(1)

    log.info("Using local IP: %s", local_ip)
    log.info("CDM heartbeat  → %s:%d", CDM_HEARTBEAT_GROUP, CDM_HEARTBEAT_PORT)
    log.info("Status reports → %s:%d", REPORT_GROUP, REPORT_PORT)

    sock_cdm    = make_multicast_sender(local_ip, CDM_HEARTBEAT_GROUP, CDM_HEARTBEAT_PORT)
    sock_report = make_multicast_sender(local_ip, REPORT_GROUP, REPORT_PORT)

    stop = threading.Event()
    threads = [
        threading.Thread(target=run_heartbeat,        args=(sock_cdm, stop),           daemon=True, name="cdm"),
        threading.Thread(target=run_status,            args=(sock_report, stop),        daemon=True, name="status"),
        threading.Thread(target=run_command_listener,  args=(local_ip, sock_report, stop), daemon=True, name="cmd"),
    ]
    for t in threads:
        t.start()

    log.info("Running — press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping…")
        stop.set()
        for t in threads:
            t.join(timeout=2)
        sock_cdm.close()
        sock_report.close()
        log.info("Stopped")


if __name__ == "__main__":
    main()

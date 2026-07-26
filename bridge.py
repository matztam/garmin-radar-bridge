"""
garmin-radar-bridge — GMR xHD emulator

Emulates a Garmin GMR xHD radar on the local network so that a Garmin plotter
discovers a "virtual radar" without any real spoke data (Step 1).  Once the
plotter shows the radar as usable, Step 2 will bridge Furuno spoke data from
mayara-server into the xHD spoke stream.

Protocol: enhanced (0x09xx message IDs), product_id=0x06d0 (GMR xHD).
Discovery: CDM heartbeat (0x038e) + capability bitmap (0x09B1) + range table (0x09B2).
Status stream: 239.254.2.0:50100 — settings broadcast ~1/s each.
Spoke stream:  239.254.2.0:50102 — (Step 2, not yet implemented).
Commands:      unicast UDP to <radar_ip>:50101.

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
REPORT_PORT  = 50100   # settings/status reports
DATA_PORT    = 50102   # spoke data (xHD uses separate port)
COMMAND_PORT = 50101   # unicast commands from plotter → radar

# GMN header: [u32 LE msg_id][u32 LE payload_len]
GMN_HEADER = struct.Struct("<II")

# CDM heartbeat
MSG_CDM_HEARTBEAT = 0x038e

# xHD enhanced-protocol message IDs (radar → MFD, bidirectional)
MSG_TRANSMIT_MODE         = 0x0919  # 0=standby, 1=transmit
MSG_TRANSMIT_MODE_CURRENT = 0x0918
MSG_SCANNER_STATE         = 0x0992  # state codes below
MSG_STATE_CHANGE          = 0x0993  # ms until next state change
MSG_RPM_MODE              = 0x0916  # 0=normal, 1=slow (wire: value×2)
MSG_RANGE_A               = 0x091e  # meters (no off-by-one, unlike HD)
MSG_RANGE_MODE            = 0x091c  # 0=single, 1=dual
MSG_RANGE_A_GAIN_MODE     = 0x0924  # 0=manual, 2=auto
MSG_RANGE_A_GAIN          = 0x0925  # uint16 LE, 0..10000 (percent×100)
MSG_RANGE_A_RAIN_MODE     = 0x0933  # 0=off, 1=on
MSG_RANGE_A_RAIN_GAIN     = 0x0934  # uint16 LE, gain×100
MSG_RANGE_A_SEA_MODE      = 0x0939  # 0=off, 1=manual, 2=auto
MSG_RANGE_A_SEA_GAIN      = 0x093a  # uint16 LE, gain×100
MSG_RANGE_A_SEA_STATE     = 0x093b  # 0=calm, 1=moderate, 2=rough
MSG_DITHER_MODE           = 0x091b  # interference rejection
MSG_NOISE_BLANKER         = 0x0932
MSG_BEARING_ALIGNMENT     = 0x0930  # i32 LE, degrees×32
MSG_NO_TX_ZONE_1_MODE     = 0x093f  # 0=off, 1=on
MSG_NO_TX_ZONE_1_START    = 0x0940  # i32 LE, degrees×32
MSG_NO_TX_ZONE_1_STOP     = 0x0941  # i32 LE, degrees×32
MSG_SENTRY_MODE           = 0x0942  # 0=off, 1=on
MSG_SENTRY_STANDBY_TIME   = 0x0943  # uint16 LE, seconds
MSG_SENTRY_TRANSMIT_TIME  = 0x0944  # uint16 LE, seconds
MSG_AFC_MODE              = 0x0920  # 0=manual, 1=auto
MSG_AFC_SETTING           = 0x0921
MSG_AFC_COARSE            = 0x0922
MSG_AFC_PROGRESS          = 0x099a
MSG_SPOKE_TOTAL           = 0x0917  # total spokes (2400 typical)
MSG_MAX_RANGE             = 0x099f  # meters
MSG_INPUT_VOLTAGE         = 0x09a3
MSG_TRANSMIT_POWER        = 0x09a2
MSG_SCAN_TYPE             = 0x0911  # 1=single range
MSG_SCAN_TYPE_B           = 0x0912  # 0=normal
MSG_SCAN_TYPE_C           = 0x0913  # 0=normal
MSG_CAPABILITY            = 0x09b1  # 48-byte capability bitmap
MSG_RANGE_TABLE           = 0x09b2  # range list
# Antenna / timing values seen in real xHD captures
MSG_ANTENNA_HEIGHT        = 0x0928  # u16 LE, cm
MSG_ANTENNA_FORWARD       = 0x0929  # u16 LE, cm
MSG_ANTENNA_STARBOARD     = 0x092a  # u16 LE, cm
MSG_ANTENNA_POWER         = 0x092b  # u16 LE
MSG_RANGE_A_SEC           = 0x091f  # u32 LE, secondary range (m)
MSG_TRIGGER_PERIOD        = 0x0994  # u32 LE
MSG_TRIGGER_DELAY         = 0x0995  # u32 LE
MSG_TRIGGER_PERIOD_B      = 0x0996  # u32 LE
MSG_TUNE_FINE             = 0x0951  # u8
MSG_TUNE_COARSE           = 0x0952  # u8
MSG_TUNE_MODE             = 0x0953  # u8
MSG_STATUS_099C           = 0x099c  # u8 (seen=1 in transmit)
MSG_STATUS_099D           = 0x099d  # u8 (seen=1 in transmit)
MSG_STATUS_099E           = 0x099e  # u8 (seen=0 in transmit)
MSG_SPOKE_DATA            = 0x0998

# xHD scanner state codes (msg 0x0992)
STATE_STANDBY    = 3
STATE_TRANSMIT   = 5

# Product ID
PRODUCT_ID_XHD = 0x06d0  # GMR xHD

# ── Garmin xHD range table (nautical) ────────────────────────────────────────
# From mayara range_table.rs test data (captured from real xHD)
XHD_RANGES_M = [
    232,    # ~1/8 NM
    463,    # 1/4 NM
    926,    # 1/2 NM
    1389,   # 3/4 NM
    1852,   # 1 NM
    2778,   # 1.5 NM
    3704,   # 2 NM
    5556,   # 3 NM
    7408,   # 4 NM
    11112,  # 6 NM
    14816,  # 8 NM
    22224,  # 12 NM
    29632,  # 16 NM
    44448,  # 24 NM
    66672,  # 36 NM
    88896,  # 48 NM
]

# ── CDM heartbeat (26-byte payload, 34 bytes total) ──────────────────────────

def build_cdm_heartbeat(seq: int, syc_group_id: int = 6) -> bytes:
    """CDM V2 heartbeat for GMR xHD.  Mirrors the captured xHD body from
    mayara's discovery.rs test data (SAMPLE_BODY constant).
    """
    # service: class=1 inst=0 ver=2 rsv=0 id=0x08d40aa0
    service = struct.pack("<BBH", 1, 0, 2) + struct.pack("<I", 0x08d40aa0)

    payload = bytearray()
    payload += struct.pack("<B", 2)                    # version_marker = 2
    payload += b'\x00'                                 # pad
    payload += struct.pack("<H", PRODUCT_ID_XHD)       # product_id = 0x06d0
    payload += b'\x00'                                 # simulator_mode
    payload += struct.pack("<B", 5)                    # product_subtype = 5
    payload += struct.pack("<B", syc_group_id)         # syc_group_id
    payload += b'\x01'                                 # constant = 1
    payload += struct.pack("<B", 1)                    # service_count = 1
    payload += b'\x00\x00\x00'                        # pad
    payload += service                                 # 8 bytes
    payload += b'\x01\x04'                            # tail tag=1 len=4
    payload += struct.pack("<I", seq)                  # sequence

    header = GMN_HEADER.pack(MSG_CDM_HEARTBEAT, len(payload))
    return header + bytes(payload)


# ── xHD capability bitmap (0x09B1, 48 bytes total) ───────────────────────────
#
# From mayara capabilities.rs SAMPLE_0X09B1_BODY — the actual bytes captured
# from a real GMR xHD.  We send this verbatim; the plotter reads it to decide
# which controls to show.

_XHD_CAP_BODY = bytes([
    # 8-byte message header prefix (included in the payload slice)
    0x01, 0x00, 0x30, 0x00, 0x9d, 0x00, 0x0a, 0x00,
    # word 0 (bits 0–63):   almost all set
    0xdf, 0xfe, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    # word 1 (bits 64–127): all set
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    # word 2 (bits 128–191): lower bits set
    0xfd, 0xff, 0xff, 0x07, 0x00, 0x00, 0x00, 0x00,
    # word 3 (bits 192–255): zero
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # word 4 (bits 256–319): zero
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

def build_capability() -> bytes:
    """Build 0x09B1 capability bitmap (48-byte payload)."""
    header = GMN_HEADER.pack(MSG_CAPABILITY, len(_XHD_CAP_BODY))
    return header + _XHD_CAP_BODY


# ── xHD range table (0x09B2) ─────────────────────────────────────────────────

def build_range_table() -> bytes:
    """Build 0x09B2 range table: version=1, length, count, then u32 meters."""
    count = len(XHD_RANGES_M)
    body_len = 8 + count * 4  # header(version+length+count=8) + ranges
    payload = bytearray()
    payload += struct.pack("<H", 1)          # version = 1
    payload += struct.pack("<H", body_len)   # length (includes this header)
    payload += struct.pack("<I", count)      # count
    for m in XHD_RANGES_M:
        payload += struct.pack("<I", m)
    header = GMN_HEADER.pack(MSG_RANGE_TABLE, len(payload))
    return header + bytes(payload)


# ── Single-value helpers ──────────────────────────────────────────────────────

def _pkt_u8(msg_id: int, value: int) -> bytes:
    """Build a 9-byte packet: GMN header + 1-byte payload."""
    return GMN_HEADER.pack(msg_id, 1) + struct.pack("<B", value)

def _pkt_u16(msg_id: int, value: int) -> bytes:
    """Build a 10-byte packet: GMN header + u16 LE payload."""
    return GMN_HEADER.pack(msg_id, 2) + struct.pack("<H", value)

def _pkt_u32(msg_id: int, value: int) -> bytes:
    """Build a 12-byte packet: GMN header + u32 LE payload."""
    return GMN_HEADER.pack(msg_id, 4) + struct.pack("<I", value)

def _pkt_i32(msg_id: int, value: int) -> bytes:
    """Build a 12-byte packet: GMN header + i32 LE payload."""
    return GMN_HEADER.pack(msg_id, 4) + struct.pack("<i", value)


# ── Status broadcast packets ──────────────────────────────────────────────────

def build_status_packets(range_m: int = 3704) -> list[bytes]:
    """Return the full set of status packets broadcast each second.

    State is TRANSMIT (5) to make the plotter show the radar in Marine Network.
    Spoke data (0x0998) is sent separately on port 50102.
    """
    return [
        # Scanner state: TRANSMIT
        _pkt_u8(MSG_SCANNER_STATE, STATE_TRANSMIT),
        _pkt_u32(MSG_STATE_CHANGE, 0),
        _pkt_u8(MSG_TRANSMIT_MODE, 1),
        _pkt_u8(MSG_TRANSMIT_MODE_CURRENT, 1),
        # Scan type
        _pkt_u8(MSG_SCAN_TYPE, 1),    # 1=single range
        _pkt_u8(MSG_SCAN_TYPE_B, 0),
        _pkt_u8(MSG_SCAN_TYPE_C, 0),
        # Range mode: single
        _pkt_u8(MSG_RANGE_MODE, 0),
        # Range A
        _pkt_u32(MSG_RANGE_A, range_m),
        _pkt_u32(MSG_RANGE_A_SEC, 926),  # secondary range (unused, 1/2 NM)
        # Gain: auto
        _pkt_u8(MSG_RANGE_A_GAIN_MODE, 2),
        _pkt_u16(MSG_RANGE_A_GAIN, 5000),
        # Sea clutter: off
        _pkt_u8(MSG_RANGE_A_SEA_MODE, 0),
        _pkt_u16(MSG_RANGE_A_SEA_GAIN, 0),
        _pkt_u8(MSG_RANGE_A_SEA_STATE, 0),
        # Rain clutter: off
        _pkt_u8(MSG_RANGE_A_RAIN_MODE, 0),
        _pkt_u16(MSG_RANGE_A_RAIN_GAIN, 0),
        # Interference rejection: off
        _pkt_u8(MSG_DITHER_MODE, 0),
        _pkt_u8(MSG_NOISE_BLANKER, 0),
        # Bearing alignment: 0°
        _pkt_i32(MSG_BEARING_ALIGNMENT, 0),
        # No-TX zone 1: off
        _pkt_u8(MSG_NO_TX_ZONE_1_MODE, 0),
        # Sentry mode: off
        _pkt_u8(MSG_SENTRY_MODE, 0),
        _pkt_u16(MSG_SENTRY_STANDBY_TIME, 0),
        _pkt_u16(MSG_SENTRY_TRANSMIT_TIME, 0),
        # AFC: auto
        _pkt_u8(MSG_AFC_MODE, 1),
        # RPM mode: normal
        _pkt_u8(MSG_RPM_MODE, 0),
        # Antenna position (from real xHD capture: 0x0dac=3500cm, 0x05dc=1500cm)
        _pkt_u16(MSG_ANTENNA_HEIGHT, 350),      # 3.5 m
        _pkt_u16(MSG_ANTENNA_FORWARD, 150),     # 1.5 m
        _pkt_u16(MSG_ANTENNA_STARBOARD, 0),
        _pkt_u16(MSG_ANTENNA_POWER, 0x2134),    # from capture
        # Trigger timing (from real xHD capture)
        _pkt_u32(MSG_TRIGGER_PERIOD,   0x000009b4),
        _pkt_u32(MSG_TRIGGER_DELAY,    0x00001710),
        _pkt_u32(MSG_TRIGGER_PERIOD_B, 0x000016f8),
        # Tune
        _pkt_u8(MSG_TUNE_FINE, 2),
        _pkt_u8(MSG_TUNE_COARSE, 0),
        _pkt_u8(MSG_TUNE_MODE, 0),
        # Status flags seen in transmit mode
        _pkt_u8(MSG_STATUS_099C, 1),
        _pkt_u8(MSG_STATUS_099D, 1),
        _pkt_u8(MSG_STATUS_099E, 0),
        # Max range + spoke total
        _pkt_u32(MSG_MAX_RANGE, max(XHD_RANGES_M)),
        _pkt_u32(MSG_SPOKE_TOTAL, 2400),
        # Input voltage
        _pkt_u16(MSG_INPUT_VOLTAGE, 120),
        # Capability bitmap + range table
        build_capability(),
        build_range_table(),
    ]


# ── Spoke builder ────────────────────────────────────────────────────────────
#
# xHD spoke wire format (0x0998):
#   GMN header (8 bytes): msg_id=0x0998, pay_len
#   Spoke header (20 bytes):
#     +00  u16  spoke_number  (always 1 in captures; plotter uses angle instead)
#     +02  u16  flags         (0x02d3 in captures)
#     +04  u32  angle_raw     (0..65535 = 0..360°, i.e. angle_raw/65536*360)
#     +08  u32  range_m       (current range in meters)
#     +12  u32  unk1          (0x00001047 in captures — leave fixed)
#     +16  u16  unk2          (0x0000)
#     +18  u16  data_len      (number of sample bytes that follow the 8-byte sub-header)
#   Sub-header (8 bytes):
#     +00  u8   encode = 0x08
#     +01  u8   spare  = 0x01
#     +02  u16  sample_count  (== data_len)
#     +04  u32  zero
#   Sample data: data_len bytes, 8-bit intensity (0=no echo, 255=max)
#
# Angle encoding: 65536 units per full revolution.
# Real xHD sends 8192 spokes/rev (step=8 raw units = 0.044°).
# We use the same step so the plotter gets a full-resolution sweep.

# Real xHD sends 8192 spokes/rev (angle step=8).  We send at 24 rpm = 0.4 rev/s.
# At 8192 spokes/rev that would be 3277 spokes/s — far too fast for Python.
# Instead we send every 5 ms (200/s) with angle step scaled to complete a full
# revolution in 200 × 0.005 = 1.0 s (= 60 rpm).  The plotter only cares that
# angles advance monotonically and cover 0..65535 without gaps.
# Real xHD: 8192 spokes/rev, angle step=8, ~24 rpm → ~3277 spokes/s
# We batch BURST_SIZE spokes per sleep cycle to approximate this with Python.
SPOKE_RANGE_M     = 3704   # must match range in spoke packets
SAMPLES_PER_SPOKE = 695    # matches real xHD at 3704m (2 NM)

# Spoke header offsets within the UDP payload
_ANGLE_OFFSET = 12   # u16 angle (radar_line.angle)
_RANGE_OFFSET = 16   # u32 range_meters


def _build_spoke_header(samples: int, range_m: int) -> bytes:
    """Build 36-byte spoke header matching struct radar_line from GarminxHDReceive.cpp.

    struct radar_line (pragma pack 1):
      [0]  u32 packet_type       = 0x0998
      [4]  u32 len1              = pay_len (total payload after 8-byte GMN header)
      [8]  u16 fill_1            = 0
      [10] u16 scan_length       = samples  (video byte count)
      [12] u16 angle             = 0        (patched per spoke)
      [14] u16 fill_2            = 0
      [16] u32 range_meters      = range_m
      [20] u32 display_meters    = range_m
      [24] u8  a_b_range         = 0
      [25] u8  dual_range        = 0
      [26] u16 scan_length_bytes_s = samples
      [28] u16 fills_4           = 0
      [30] u32 scan_length_bytes_i = samples
      [34] u16 fills_5           = 0
      [36+] line_data[]
    """
    pay_len = 36 - 8 + samples   # header fields after GMN header (28 bytes) + samples
    h = bytearray(36)
    struct.pack_into("<I", h,  0, MSG_SPOKE_DATA)  # packet_type
    struct.pack_into("<I", h,  4, pay_len)          # len1
    struct.pack_into("<H", h,  8, 1)                # fill_1 = 1 (constant in all real captures)
    struct.pack_into("<H", h, 10, 0x02d3)           # scan_length (constant 0x02d3 in all real captures)
    struct.pack_into("<H", h, 12, 0)                # angle (patched per spoke)
    struct.pack_into("<H", h, 14, 0)                # fill_2
    struct.pack_into("<I", h, 16, range_m)          # range_meters
    struct.pack_into("<I", h, 20, round(range_m * 4167 / 3704))  # display_meters (scaled like real xHD)
    h[24] = 0                                       # a_b_range
    h[25] = 0                                       # dual_range
    struct.pack_into("<H", h, 26, samples)          # scan_length_bytes_s
    struct.pack_into("<H", h, 28, 0x0108)           # fills_4 = encode(0x08)+spare(0x01) in LE
    struct.pack_into("<I", h, 30, samples)          # scan_length_bytes_i
    struct.pack_into("<H", h, 34, 0)                # fills_5
    return bytes(h)


_SPOKE_HEADER = _build_spoke_header(SAMPLES_PER_SPOKE, SPOKE_RANGE_M)

# Sanity check
assert len(_SPOKE_HEADER) == 36
assert struct.unpack_from("<I", _SPOKE_HEADER, 4)[0] == 28 + SAMPLES_PER_SPOKE


def _make_spoke_pkt(angle_raw: int) -> bytes:
    samples = bytearray(SAMPLES_PER_SPOKE)
    # Bright ring at 25% range
    r = SAMPLES_PER_SPOKE // 4
    for i in range(r - 3, r + 3):
        if 0 <= i < SAMPLES_PER_SPOKE:
            samples[i] = 0xff
    # Bright line every 30° (every 5461 angle units out of 65536)
    if (angle_raw % 5461) < (ANGLE_STEP + 1):
        for i in range(SAMPLES_PER_SPOKE):
            samples[i] = max(samples[i], 0xc0)
    pkt = bytearray(_SPOKE_HEADER) + samples
    struct.pack_into("<H", pkt, _ANGLE_OFFSET, angle_raw & 0xffff)
    return bytes(pkt)


def _load_pcap_spokes(fname: str, max_angle_range: int = 65536) -> list[tuple[float, bytes]]:
    """Load all 0x0998 spoke packets from a pcap, preserving relative timing."""
    result = []
    try:
        with open(fname, 'rb') as f:
            struct.unpack('<IHHiIII', f.read(24))
            while True:
                hdr = f.read(16)
                if len(hdr) < 16: break
                ts_s, ts_us, cap_len, orig_len = struct.unpack('<IIII', hdr)
                data = f.read(cap_len)
                if len(data) < 34: continue
                ihl = (data[14] & 0x0f) * 4
                if data[23] != 17: continue
                udp_off = 14 + ihl
                if len(data) < udp_off + 8: continue
                payload = data[udp_off+8:]
                if len(payload) < 8: continue
                mid = struct.unpack_from('<I', payload, 0)[0]
                if mid != 0x0998: continue
                ts = ts_s + ts_us / 1e6
                angle = struct.unpack_from('<H', payload, 12)[0]
                if result:
                    first_angle = struct.unpack_from('<H', result[0][1], 12)[0]
                    if (angle - first_angle) % 65536 >= max_angle_range:
                        break
                result.append((ts, bytes(payload)))
    except FileNotFoundError:
        pass
    return result


def _make_spoke_pkt(angle_raw: int) -> bytes:
    """Build one xHD spoke packet with all-zero samples (empty sea)."""
    pkt = bytearray(_SPOKE_HEADER) + bytearray(SAMPLES_PER_SPOKE)
    struct.pack_into("<H", pkt, _ANGLE_OFFSET, angle_raw & 0xffff)
    return bytes(pkt)


MAYARA_URL = "ws://172.16.254.150:6502/signalk/v2/api/vessels/self/radars/furfe07/spokes"

# xHD: 1440 spokes/rev, angle step=8, range 0..11512
XHD_SPOKES_PER_REV = 1440
XHD_ANGLE_MAX      = XHD_SPOKES_PER_REV * 8   # 11520 (exclusive)


def _furuno_to_xhd_spoke(src_angle: int, src_spokes_per_rev: int,
                          src_samples: bytes, src_range_m: int) -> bytes:
    """Convert a mayara protobuf spoke to an xHD UDP packet.

    src_angle is in [0..src_spokes_per_rev), bow-relative (0=ahead).
    xHD angle is in 1/8° units, [0..11512], also bow-relative.
    Samples are resampled to SAMPLES_PER_SPOKE via nearest-neighbour.
    """
    # Map angle: src units → xHD units (0..11512)
    xhd_angle = round(src_angle * XHD_ANGLE_MAX / src_spokes_per_rev) % XHD_ANGLE_MAX
    xhd_angle = (xhd_angle // 8) * 8   # quantize to step=8

    # Resample spoke data to SAMPLES_PER_SPOKE via nearest-neighbour
    n_src = len(src_samples)
    if n_src == 0:
        samples = bytearray(SAMPLES_PER_SPOKE)
    elif n_src == SAMPLES_PER_SPOKE:
        samples = bytearray(src_samples)
    else:
        samples = bytearray(SAMPLES_PER_SPOKE)
        for i in range(SAMPLES_PER_SPOKE):
            j = round(i * n_src / SAMPLES_PER_SPOKE)
            samples[i] = src_samples[min(j, n_src - 1)]

    # Build xHD range to match what we report in status
    range_m = SPOKE_RANGE_M   # keep fixed for now; TODO: track plotter range

    pkt = bytearray(_SPOKE_HEADER) + samples
    struct.pack_into('<H', pkt, _ANGLE_OFFSET, xhd_angle)
    # Patch range fields to match current range
    struct.pack_into('<I', pkt, _RANGE_OFFSET, range_m)
    struct.pack_into('<I', pkt, _RANGE_OFFSET + 4, round(range_m * 4167 / 3704))
    return bytes(pkt)


def run_spokes(sock_data: socket.socket, stop: threading.Event):
    """Bridge Furuno spokes from mayara WebSocket → xHD multicast.

    Falls back to synthetic test pattern if mayara is unavailable.
    """
    import importlib, sys, queue

    # Try to import websockets; graceful fallback if missing
    try:
        import websockets.sync.client as ws_sync
    except ImportError:
        log.warning("websockets not installed — using synthetic spokes (pip install websockets)")
        ws_sync = None

    try:
        import RadarMessage_pb2 as pb
    except ImportError:
        log.warning("RadarMessage_pb2 not found — using synthetic spokes")
        pb = None

    if ws_sync is None or pb is None:
        _run_synthetic_spokes(sock_data, stop)
        return

    # Queue for passing spokes from WebSocket thread to sender loop
    spoke_q: queue.Queue = queue.Queue(maxsize=512)

    def ws_reader():
        while not stop.is_set():
            try:
                with ws_sync.connect(MAYARA_URL, max_size=2**20) as ws:
                    log.info("Connected to mayara: %s", MAYARA_URL)
                    while not stop.is_set():
                        try:
                            raw = ws.recv(timeout=5.0)
                        except TimeoutError:
                            continue
                        if not isinstance(raw, (bytes, bytearray)):
                            continue
                        msg = pb.RadarMessage()
                        msg.ParseFromString(raw)
                        for spoke in msg.spokes:
                            if not stop.is_set():
                                try:
                                    spoke_q.put_nowait(spoke)
                                except queue.Full:
                                    pass   # drop oldest implicitly
            except Exception as e:
                if not stop.is_set():
                    log.warning("mayara WS error: %s — reconnecting in 3s", e)
                    stop.wait(3.0)

    reader = threading.Thread(target=ws_reader, daemon=True, name="ws-reader")
    reader.start()

    spokes_per_rev = None
    log.info("Waiting for mayara spokes…")

    while not stop.is_set():
        try:
            spoke = spoke_q.get(timeout=1.0)
        except queue.Empty:
            continue

        if spokes_per_rev is None:
            spokes_per_rev = 8192   # Furuno DRS: angles 0..8191
            log.info("Using spokes_per_rev=%d (Furuno DRS)", spokes_per_rev)

        pkt = _furuno_to_xhd_spoke(
            src_angle=spoke.angle,
            src_spokes_per_rev=spokes_per_rev,
            src_samples=spoke.data,
            src_range_m=spoke.range,
        )
        try:
            sock_data.send(pkt)
        except OSError as e:
            log.warning("Spoke send failed: %s", e)
        # Small inter-spoke gap to avoid flooding the plotter
        time.sleep(0.0005)


def _run_synthetic_spokes(sock_data: socket.socket, stop: threading.Event):
    """Fallback: synthetic ring + heading mark when mayara is unavailable."""
    pcap = _load_pcap_spokes('/tmp/garmin_long.pcap')
    if not pcap:
        log.error("No pcap found and mayara unavailable — no spoke data")
        return

    real_angles = [struct.unpack_from('<H', p, 12)[0] for _, p in pcap]
    wrap_idxs   = [i for i in range(1, len(real_angles)) if real_angles[i] < real_angles[i-1]]
    w0, w1      = wrap_idxs[0], wrap_idxs[1]
    one_rev     = pcap[w0:w1]
    t0_rev      = pcap[w0][0]
    one_times   = [ts - t0_rev for ts, _ in one_rev]
    rev_dur     = one_times[-1]
    log.info("Synthetic spokes: %d spokes/rev, %.2fs", len(one_rev), rev_dur)

    ring_samples = bytearray(SAMPLES_PER_SPOKE)
    r = SAMPLES_PER_SPOKE // 4
    for j in range(r - 2, r + 3):
        if 0 <= j < SAMPLES_PER_SPOKE:
            ring_samples[j] = 0xff
    heading_samples = bytearray(SAMPLES_PER_SPOKE)
    for j in range(SAMPLES_PER_SPOKE):
        heading_samples[j] = 0xff

    while not stop.is_set():
        wall_start = time.monotonic()
        for i, (_, payload) in enumerate(one_rev):
            if stop.is_set():
                break
            target = wall_start + one_times[i]
            sleep  = target - time.monotonic()
            if sleep > 0.0005:
                time.sleep(sleep)
            angle = (i * 8) % 11520
            pkt   = bytearray(payload)
            struct.pack_into('<H', pkt, 12, angle)
            pkt[36:36 + SAMPLES_PER_SPOKE] = heading_samples if angle < 200 else ring_samples
            try:
                sock_data.send(bytes(pkt))
            except OSError as e:
                log.warning("Spoke send failed: %s", e)
        elapsed = time.monotonic() - wall_start
        if elapsed < rev_dur:
            stop.wait(rev_dur - elapsed)


# ── Network helpers ───────────────────────────────────────────────────────────

def make_multicast_sender(local_ip: str, group: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(local_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.bind((local_ip, port))
    sock.connect((group, port))
    return sock


def detect_garmin_ip() -> str | None:
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


# ── Main threads ──────────────────────────────────────────────────────────────

def run_heartbeat(sock_cdm: socket.socket, stop: threading.Event, syc_group_id: int = 6):
    """Send CDM heartbeat: every 1 s for first 30 ticks, then every 5 s."""
    seq = 0
    while not stop.is_set():
        try:
            sock_cdm.send(build_cdm_heartbeat(seq, syc_group_id))
            log.debug("CDM heartbeat seq=%d", seq)
        except OSError as e:
            log.warning("CDM send failed: %s", e)
        seq += 1
        stop.wait(1.0 if seq <= 30 else 5.0)


def run_status(sock_report: socket.socket, stop: threading.Event):
    """Broadcast xHD status packets every 5 seconds (minimal load)."""
    pkts = build_status_packets()
    while not stop.is_set():
        for pkt in pkts:
            try:
                sock_report.send(pkt)
            except OSError as e:
                log.warning("Report send failed: %s", e)
        stop.wait(1.0)


def run_command_listener(local_ip: str, sock_report: socket.socket, stop: threading.Event):
    """Listen on UDP port 50101 for commands from the plotter and respond."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((local_ip, COMMAND_PORT))
    sock.settimeout(1.0)
    log.info("Listening for commands on %s:%d", local_ip, COMMAND_PORT)

    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
            if len(data) < 8:
                continue
            msg_id, pay_len = struct.unpack('<II', data[:8])
            log.info("CMD from %s: msg=0x%04x pay_len=%d", addr, msg_id, pay_len)

            # Most xHD commands: the plotter sends a value, we echo it back on the
            # report stream so the plotter sees its command was accepted.
            if msg_id == MSG_TRANSMIT_MODE:
                val = data[8] if len(data) > 8 else 0
                sock.sendto(_pkt_u8(MSG_TRANSMIT_MODE, val), addr)
                sock.sendto(_pkt_u8(MSG_SCANNER_STATE,
                                    STATE_TRANSMIT if val else STATE_STANDBY), addr)
            elif msg_id == MSG_RANGE_A:
                if len(data) >= 12:
                    meters = struct.unpack("<I", data[8:12])[0]
                    sock.sendto(_pkt_u32(MSG_RANGE_A, meters), addr)
            else:
                # For all other commands, just log — the status broadcast will
                # keep the plotter in sync.
                log.debug("Unhandled CMD 0x%04x", msg_id)

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
    sock_data   = make_multicast_sender(local_ip, REPORT_GROUP, DATA_PORT)

    stop = threading.Event()
    threads = [
        threading.Thread(target=run_heartbeat,       args=(sock_cdm, stop),              daemon=True, name="cdm"),
        threading.Thread(target=run_status,           args=(sock_report, stop),           daemon=True, name="status"),
        threading.Thread(target=run_spokes,           args=(sock_data, stop),             daemon=True, name="spokes"),
        threading.Thread(target=run_command_listener, args=(local_ip, sock_report, stop), daemon=True, name="cmd"),
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
        sock_data.close()
        log.info("Stopped")


if __name__ == "__main__":
    main()

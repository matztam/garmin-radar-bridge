# garmin-radar-bridge

Emulates a Garmin GMR xHD radar on the local network so that a Garmin chartplotter discovers and displays a virtual radar image. Intended as a bridge to display Furuno spoke data (via [mayara-server](https://github.com/MarineYachtRadar/mayara-server)) on a Garmin plotter.

## Status

- [x] **Step 1** — Virtual xHD radar discovered and displaying synthetic spoke data
- [ ] **Step 2** — Bridge real Furuno spoke data from mayara-server into the xHD stream

## Requirements

- Python 3.10+
- A network interface with an IP address in `172.16.0.0/12` (the Garmin marine subnet)
- The Garmin chartplotter on the same subnet

## Usage

```bash
sudo python3 bridge.py [--iface <local-ip>]
```

`--iface` is optional — the script auto-detects the first IP in `172.16.0.0/12`.

The plotter should show the radar as "Xmit On" in Marine Network and display a rotating sweep with a synthetic ring pattern.

## Protocol

The xHD enhanced protocol uses `0x09xx` message IDs, multicast over the Garmin Marine Network (`172.16.0.0/12`).

| Channel | Address | Port | Purpose |
|---------|---------|------|---------|
| CDM heartbeat | 239.254.2.2 | 50050 | Device discovery (`0x038e`) |
| Status reports | 239.254.2.0 | 50100 | Settings/state broadcast |
| Spoke data | 239.254.2.0 | 50102 | Sweep data (`0x0998`) |
| Commands | `<radar_ip>` | 50101 | Unicast commands from plotter |

### Spoke format (msg `0x0998`)

```c
struct radar_line {          // pragma pack(1)
    uint32_t packet_type;   // +0   0x0998
    uint32_t len1;          // +4   payload length
    uint16_t fill_1;        // +8   = 1 (constant)
    uint16_t scan_length;   // +10  = 0x02d3 (constant)
    uint16_t angle;         // +12  spoke angle, 1/8° units (0..11512)
    uint16_t fill_2;        // +14  = 0
    uint32_t range_meters;  // +16  current range
    uint32_t display_meters;// +20  display range
    uint8_t  a_b_range;     // +24  = 0
    uint8_t  dual_range;    // +25  = 0
    uint16_t scan_length_bytes_s; // +26  sample count
    uint16_t fills_4;       // +28  = 0x0108
    uint32_t scan_length_bytes_i; // +30  sample count (redundant)
    uint16_t fills_5;       // +34  = 0
    uint8_t  line_data[];   // +36  8-bit intensity samples
};
```

Key facts confirmed by pcap analysis and the [mayara-server protocol docs](https://github.com/MarineYachtRadar/mayara-server):

- **1440 spokes/revolution**, angle step = 8, range 0..11512 (not 0..65535!)
- **695 samples/spoke** at 2 NM range (3704 m)
- `fill_1` must be **1** (not 0)
- `fills_4` must be **0x0108** (encode=0x08, spare=0x01 in little-endian)
- `scan_length` is always **0x02d3** regardless of sample count
- Spoke timing: bursts of 5–7 packets at ~0 ms spacing, then ~9.7 ms gap

### CDM heartbeat

Sent to `239.254.2.2:50050`, msg `0x038e`, every 1 s for the first 30 ticks then every 5 s. Identifies as GMR xHD (`product_id=0x06d0`, `syc_group_id=6`).

## What caused crashes (lessons learned)

The Garmin plotter crashes silently when:

- Spoke angles exceed **11512** (valid range is 0..11512, not 0..65535)
- Range in spoke packets does not match range in status reports
- `fill_1` at byte 8 is 0 instead of 1
- `fills_4` at byte 28 is 0x0000 instead of 0x0108

The plotter freezes (without crashing) when it only receives a partial revolution and never sees the full 0..11512 angle range.

## Sources

- [MarineYachtRadar/mayara-server](https://github.com/MarineYachtRadar/mayara-server) — Rust radar server, Garmin protocol source
- [douwefokkema/radar_pi](https://github.com/douwefokkema/radar_pi) — OpenCPN plugin, `GarminxHDReceive.cpp` defines `struct radar_line`
- Garmin xHD pcap from radar_pi repository (`garmin_xhd.pcap.gz`)
- Mayara developer protocol documentation (garmin.zip, covering xHD and Fantom Pro)

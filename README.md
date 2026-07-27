# garmin-radar-bridge

Bridges Furuno radar spoke data (via [mayara-server](https://github.com/MarineYachtRadar/mayara-server)) to a Garmin GPSMAP chartplotter. The bridge emulates a Garmin GMR xHD radar on the local network: the plotter discovers it, displays it in Marine Network, and renders the Furuno radar image — including the chart overlay with correct scale.

## Status

- [x] Step 1 — Virtual xHD radar discovered and displaying synthetic spoke data
- [x] Step 2 — Real Furuno spoke data bridged from mayara-server, chart overlay correct
- [x] Step 3 — Plotter controls (range, gain, sea/rain clutter, transmit) forwarded to Furuno via mayara

## Hardware setup

```
Furuno DRS ──── (Ethernet) ──── Switch ──── mayara-server (Linux host)
                                  │
                                  └──── Garmin GPSMAP (chartplotter)
```

The bridge runs on the same Linux host as mayara-server. It connects to mayara via WebSocket and relays the spoke data as a Garmin xHD radar stream onto the local network.

## Requirements

- Python 3.10+
- `websockets` and `protobuf` Python packages
- A network interface with an IP in `172.16.0.0/12` (the Garmin Marine Network subnet)
- The Garmin chartplotter on the same subnet
- mayara-server running and connected to the Furuno radar

```bash
pip install websockets protobuf
```

The protobuf class `RadarMessage_pb2.py` must be in the working directory (generated from `RadarMessage.proto`, included in this repository).

## Usage

```bash
sudo PYTHONPATH=/path/to/site-packages:/path/to/bridge python3 bridge.py [--iface <local-ip>]
```

`--iface` is optional — the script auto-detects the first IP in `172.16.0.0/12`.

`sudo` is required for multicast socket binding. If websockets/protobuf are installed in a user environment (e.g. platformio), pass that path explicitly via `PYTHONPATH`.

Example:
```bash
sudo PYTHONPATH=/home/user/.platformio/penv/lib/python3.13/site-packages:/home/user/garmin-radar-bridge \
    python3 bridge.py --iface 172.16.254.217
```

The plotter should show the radar as "Xmit On" in Marine Network within a few seconds and display a rotating sweep.

## Network notes

All spoke data is sent via UDP multicast to `239.254.2.0:50102`. Multicast TTL is set to 1 so packets stay in the local network segment and do not reach routers beyond the local switch.

The Furuno radar itself also sends multicast (to deliver spokes to mayara-server). If your switch does not perform IGMP snooping, all radar multicast is flooded to every port — including Wi-Fi access points — which can saturate the wireless network (~6 Mbit/s of radar data). Enable IGMP snooping on your switch to confine the traffic to the ports that actually need it.

---

## Protocol

### Garmin Marine Network (GMN)

GMN uses UDP multicast in the `172.16.0.0/12` subnet. All packets share a common 8-byte header:

```
[u32 LE msg_id][u32 LE payload_len]
```

The xHD enhanced protocol uses `0x09xx` message IDs. The bridge identifies as a GMR xHD radar (`product_id=0x06d0`, `syc_group_id=6`).

| Channel | Multicast group | Port | Direction | Purpose |
|---------|----------------|------|-----------|---------|
| CDM heartbeat | 239.254.2.2 | 50050 | radar → all | Device discovery (`0x038e`) |
| Status reports | 239.254.2.0 | 50100 | radar → all | Settings/state broadcast |
| Spoke data | 239.254.2.0 | 50102 | radar → all | Sweep data (`0x0998`) |
| Commands | `<radar_ip>` | 50101 | plotter → radar | Unicast control messages |

### Discovery sequence

1. The bridge sends a **CDM heartbeat** (`0x038e`) every 1 s for the first 30 seconds, then every 5 s.
2. The plotter responds by sending commands on port 50101 (unicast to the bridge IP).
3. The bridge answers the capability query with a **0x09B1 capability bitmap** (48 bytes, copied from a real xHD capture) and a **0x09B2 range table**.
4. The plotter shows the radar as "Xmit On" in Marine Network.

### Status stream (port 50100)

Broadcast every second. Contains ~40 individual packets covering all radar settings:

| Message | ID | Value |
|---------|----|-------|
| Scanner state | `0x0992` | 5 = TRANSMIT, 3 = STANDBY |
| Transmit mode | `0x0919` | 1 = on, 0 = off |
| Range A | `0x091e` | current range in meters |
| Gain mode | `0x0924` | 0 = manual, 2 = auto |
| Gain | `0x0925` | uint16, percent×100 |
| Sea clutter mode | `0x0939` | 0 = off, 1 = manual, 2 = auto |
| Sea clutter gain | `0x093a` | uint16, level×100 |
| Rain clutter mode | `0x0933` | 0 = off, 1 = on |
| Rain clutter gain | `0x0934` | uint16, level×100 |
| Bearing alignment | `0x0930` | i32 LE, degrees×32 |
| … | … | … |

The bridge reads the initial state of all controls from mayara at startup so the plotter shows the correct values immediately.

The range value in the status stream must always match `range_meters` in the spoke packets. A mismatch causes the plotter to crash or freeze.

### Command handling (port 50101)

The plotter sends unicast UDP commands to `<bridge_ip>:50101`. The bridge:

1. Echoes the command back on the status stream so the plotter sees it confirmed
2. Forwards it to mayara via REST `PUT /controls/{name}`

| Command | Wire ID | Forwarded to mayara as |
|---------|---------|------------------------|
| Transmit on/off | `0x0919` | `PUT /controls/power` `{"value": "transmit"/"standby"}` |
| Range | `0x091e` | `PUT /controls/range` `{"value": <meters>}` |
| Gain mode | `0x0924` | `PUT /controls/gain` `{"auto": true/false}` |
| Gain level | `0x0925` | `PUT /controls/gain` `{"auto": false, "value": <0-100>}` |
| Sea clutter mode | `0x0939` | `PUT /controls/sea` `{"auto": true/false, "value": ...}` |
| Sea clutter gain | `0x093a` | `PUT /controls/sea` `{"auto": false, "value": <0-100>}` |
| Rain clutter off | `0x0933` val=0 | `PUT /controls/rain` `{"auto": true}` (Furuno has auto, Garmin does not) |
| Rain clutter on | `0x0933` val=1 | `PUT /controls/rain` `{"auto": false, "value": 50}` |
| Rain clutter gain | `0x0934` | `PUT /controls/rain` `{"auto": false, "value": <0-100>}` |

**Range command quirk:** The Furuno's internal range steps differ from the xHD range table. `spoke.range` in the WebSocket stream carries the Furuno's internal display range, which is approximately 1.782× the xHD range set via mayara. The bridge converts back via this ratio to keep `_current_range_m` consistent with the status stream.

### Spoke format (msg `0x0998`, port 50102)

Each spoke is one UDP packet. The payload is the GMN header followed by `struct radar_line` (from `GarminxHDReceive.cpp` in the radar_pi plugin):

```c
struct radar_line {          // pragma pack(1)
    uint32_t packet_type;        // +0   0x0998
    uint32_t len1;               // +4   payload length (28 + sample_count)
    uint16_t fill_1;             // +8   = 1  (must not be 0!)
    uint16_t scan_length;        // +10  = 0x02d3  (constant in all captures)
    uint16_t angle;              // +12  spoke angle in 1/8° units, range 0..11512
    uint16_t fill_2;             // +14  = 0
    uint32_t range_meters;       // +16  nearest xHD range table value
    uint32_t display_meters;     // +20  actual range (used for image scaling)
    uint8_t  a_b_range;          // +24  = 0
    uint8_t  dual_range;         // +25  = 0
    uint16_t scan_length_bytes_s;// +26  = sample_count
    uint16_t fills_4;            // +28  = 0x0108  (must not be 0x0000!)
    uint32_t scan_length_bytes_i;// +30  = sample_count
    uint16_t fills_5;            // +34  = 0
    uint8_t  line_data[];        // +36  8-bit intensity samples (0=no echo, 255=max)
};
```

**Critical constraints** (plotter crashes or freezes if violated):

- `angle` must be in **0..11512** (= 1440 spokes × 8). Values > 11512 crash the plotter immediately.
- `fill_1` at byte +8 must be **1**, not 0.
- `fills_4` at byte +28 must be **0x0108**, not 0x0000.
- `range_meters` must be a value from the xHD range table and must match the status stream.

**Angle encoding:** 1/8° units, bow-relative (0 = straight ahead). One full revolution covers angles 0..11512 in steps of 8, giving 1440 unique spoke positions.

**Sample encoding:** 8-bit grayscale per range bin. At 2 NM (3704 m) range, 695 samples/spoke. The sample count varies with range but the bridge always sends 695 samples (the Furuno data is resampled to fit).

**`range_meters` vs `display_meters`:** The plotter uses `range_meters` to select the matching entry in the range table (for the UI range display) and `display_meters` to scale the radar image onto the chart. To get a correct overlay, `range_meters` must be the nearest xHD table value (e.g. 14816 m for a Furuno range of 13202 m), while `display_meters` must be the actual Furuno range (13202 m). Using the same value for both causes the radar image to be scaled incorrectly on the chart.

### xHD range table

The plotter only accepts ranges from this fixed table (in meters):

```
232, 463, 926, 1389, 1852, 2778, 3704, 5556,
7408, 11112, 14816, 22224, 29632, 44448, 66672, 88896
```

---

## Furuno → xHD conversion

### Spoke angle

The xHD protocol uses **1/8° units** for angles, bow-relative (0 = straight ahead). One full revolution spans 0..11519, but valid spoke angles are **0..11512** in steps of 8 — giving exactly 1440 unique spoke positions per revolution (11520 / 8 = 1440). Angles above 11512 crash the plotter immediately.

The Furuno DRS sends 8192 spokes per revolution, with angles 0..8191 (bow-relative). The conversion to xHD angle units:

```python
xhd_angle = round(src_angle * 11520 / 8192) % 11520
xhd_angle = (xhd_angle // 8) * 8   # quantize to step=8
```

Because the Furuno has more spokes per revolution (8192) than the xHD supports (1440), multiple Furuno spokes map to the same xHD angle. The plotter receives more updates per position than a real xHD would send, but this is harmless — the plotter simply overwrites the previous value for that angle.

### Sample resampling

The Furuno sends 1024 samples/spoke; the xHD stream uses 695 samples/spoke. Nearest-neighbour resampling:

```python
for i in range(695):
    j = round(i * 1024 / 695)
    samples[i] = src_samples[min(j, 1023)]
```

### Range mapping

```python
range_meters  = nearest_xhd_range(furuno_range_m)   # for UI display
display_meters = furuno_range_m                       # for image scaling
```

### Data path

```
mayara-server ──WebSocket──► ws_reader thread ──Queue──► sender loop ──UDP multicast──► plotter
                (protobuf RadarMessage)        (8192)
```

The WebSocket receiver and UDP sender run in separate threads connected by a `queue.Queue(maxsize=8192)`. The queue size matches the mayara batch size (256 spokes per WebSocket message × several messages in flight). If the queue is too small, spokes are dropped and the image shows missing sectors.

mayara delivers spokes as protobuf `RadarMessage` with repeated `Spoke` fields:

```protobuf
message Spoke {
    uint32 angle  = 1;   // 0..8191, bow-relative
    uint32 range  = 2;   // meters
    bytes  data   = 3;   // 1024 8-bit intensity samples
}
```

---

## What causes crashes and freezes

| Symptom | Cause |
|---------|-------|
| Immediate crash on start | Spoke angle > 11512 |
| Crash after a few seconds | `range_meters` in spokes doesn't match status stream |
| Crash after a few seconds | `fill_1` = 0 instead of 1 |
| Crash after a few seconds | `fills_4` = 0x0000 instead of 0x0108 |
| Freeze (no crash) | Only partial revolution received, never completes 0..11512 |
| Missing sectors (torn image) | Spoke queue too small, spokes dropped under load |
| Chart overlay misaligned | `display_meters` = `range_meters` instead of actual Furuno range |
| Wi-Fi collapse | Multicast TTL > 1 without IGMP snooping — all Wi-Fi clients receive radar traffic |

---

## Sources

- [MarineYachtRadar/mayara-server](https://github.com/MarineYachtRadar/mayara-server) — Rust radar server, Garmin xHD protocol implementation and documentation
- [douwefokkema/radar_pi](https://github.com/douwefokkema/radar_pi) — OpenCPN plugin, `GarminxHDReceive.cpp` defines `struct radar_line`
- Garmin xHD packet captures — used for protocol verification and deriving exact field values

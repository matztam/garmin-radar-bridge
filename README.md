# garmin-radar-bridge

PoC: emulate a Garmin GMR 18/24 HD radar on the local network so that a Garmin plotter discovers and displays radar data bridged from a Furuno via [mayara-server](https://github.com/MarineYachtRadar/mayara-server).

## Status

- [x] **Step 1** — CDM heartbeat + HD status reports: plotter discovers the virtual radar
- [ ] **Step 2** — HD spoke stream: plotter displays radar image from Furuno data

## Requirements

- Python 3.10+
- A network interface with an IP address in `172.16.0.0/12` (the Garmin marine subnet)
- The Garmin plotter must be on the same subnet

## Step 1: plotter discovery

```bash
sudo python3 bridge.py [--iface <local-ip>]
```

`--iface` is optional — the script auto-detects the first IP in `172.16.0.0/12`.

The script sends:
- **CDM heartbeat** (`0x038e`) to `239.254.2.2:50050` every 1 s (first 30), then every 5 s
- **HD state** (`0x02a5`) to `239.254.2.0:50100` every 1 s — reports standby state
- **HD scanner ID** (`0x02a6`) — identifies the device as GMR 18 HD
- **HD capability** (`0x02ae`) — signals legacy HD protocol
- **HD rotation speed** (`0x02ab`)

If the plotter shows a radar icon or "searching for radar" prompt, Step 1 is successful.

## Protocol notes

Sources: [MarineYachtRadar/mayara-server](https://github.com/MarineYachtRadar/mayara-server) `src/lib/brand/garmin/`

| Layer | Details |
|-------|---------|
| Network | UDP multicast, Garmin marine subnet `172.16.0.0/12` |
| Heartbeat | `239.254.2.2:50050`, msg `0x038e`, 30 bytes |
| Reports/spokes | `239.254.2.0:50100`, legacy HD protocol (`0x02xx` msg IDs) |
| Commands | Unicast UDP to `<radar_ip>:50101` |
| Spoke geometry | 720 spokes/rev, 1-bit binary samples, 4 spokes/packet |

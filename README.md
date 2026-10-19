# gpsd-replay

Replay recorded NMEA logs or synthetic GPX routes as live GPS streams, with a browser-based control UI and a live map.

## Features

- **Two source modes**: raw NMEA log replay or synthetic route generated from a GPX file
- **Three sink modes**: gpsd JSON protocol over TCP, raw NMEA over TCP, and/or PTY virtual serial device
- **Web UI**: browser-based control panel with live map, position, speed, file picker, and session info
- **Playback controls**: speed multiplier, loop, pause/resume, seek by frame, skip forward/back
- **File picker**: point at a directory of replay files and switch between them without restarting
- **Zero runtime dependencies**: pure Python stdlib (3.10+)
- **Docker support**: single-container deployment

<img width="1702" height="806" alt="image" src="https://github.com/user-attachments/assets/9660a185-51a9-4e4b-81c9-5456d4c8c1c4" />

## Installation

### Local

```bash
pip install -e .
```

### Docker

```bash
docker build -t gpsd-replay .
```

## Quick Start

### Local

```bash
# Replay an NMEA log at real-time speed, web UI on http://localhost:8765
gpsd-replay nmea examples/national_mall_dc_drive.nmea

# Replay at 2× with looping, expose a directory of files in the UI picker
gpsd-replay nmea examples/national_mall_dc_drive.nmea --speed 2.0 --loop --file-dir examples/

# Synthetic route from GPX at 15 m/s
gpsd-replay gpx my_route.gpx --gpx-speed 15 --loop

# Raw NMEA TCP server on port 2948 (in addition to the default gpsd JSON on 2947)
gpsd-replay nmea examples/national_mall_dc_drive.nmea --nmea-port 2948
```

### Docker

```bash
# Mount a directory of replay files, expose gpsd JSON and web UI ports
docker run --rm \
  -v /path/to/your/nmea/files:/data:ro \
  -p 2947:2947 \
  -p 8765:8765 \
  gpsd-replay nmea /data/your_file.nmea --loop --file-dir /data

# Also expose raw NMEA on port 2948
docker run --rm \
  -v /path/to/your/nmea/files:/data:ro \
  -p 2947:2947 \
  -p 2948:2948 \
  -p 8765:8765 \
  gpsd-replay nmea /data/your_file.nmea --loop --file-dir /data --nmea-port 2948
```

Open **http://localhost:8765** for the control UI.

## Using with gpsd

### Direct TCP (simplest)

The gpsd JSON TCP server (port 2947) speaks the native gpsd protocol. Point gpsd clients directly at it:

```bash
gpspipe -w -h localhost -p 2947
cgps -s localhost:2947
```

### Feeding a host gpsd instance

To have a real gpsd instance consume the replay (e.g. so other gpsd clients on the system work normally), use the raw NMEA TCP server with `socat` to create a PTY that gpsd treats as a serial device:

```bash
# Terminal 1 — start replay with raw NMEA enabled
gpsd-replay nmea examples/national_mall_dc_drive.nmea --loop --nmea-port 2948

# Terminal 2 — bridge raw NMEA TCP to a PTY on the host
socat PTY,link=/tmp/gps0,raw,echo=0 TCP:localhost:2948

# Terminal 3 — point gpsd at the PTY
gpsd -G -N -n /tmp/gps0
```

The same pattern works when replay is running in Docker — just change `TCP:localhost:2948` to match the exposed port.

> **Note:** Running `gpsd` as root may fail with `Permission denied` on the PTY due to Linux mount namespace isolation between user sessions. Run gpsd without `sudo` (works when using ports > 1024) or use `nsenter` to place it in the same namespace as the socat process.

### PTY sink (local only)

The built-in PTY sink works for local (non-Docker) use:

```bash
gpsd-replay nmea examples/national_mall_dc_drive.nmea --no-tcp --pty --pty-path /tmp/gps0
gpsd -G -N -n /tmp/gps0
```

> PTY output from inside a Docker container is not accessible on the host — use the raw NMEA TCP + socat approach instead.

## Web UI

The web UI is available at `http://localhost:8765` (or whatever `--ui-port` is set to).

| Panel | Description |
|---|---|
| **File** | Switch between replay files in `--file-dir` without restarting |
| **Position** | Live lat/lon, timestamp from the replay, speed in knots/mph/kph/m/s, bearing, altitude |
| **Playback** | Play/pause/stop, progress bar (clickable to seek), speed multiplier, skip forward/back by N frames |
| **Map** | Live position marker and path history on an OpenStreetMap tile layer. Toggle follow mode or clear the path |
| **Session** | Shows all active configuration: source, ports, PTY path, file dir, speed, loop, live-time |
| **Event Log** | Recent playback events |

## CLI Reference

```
gpsd-replay nmea <file.nmea> [OPTIONS]
gpsd-replay gpx  <file.gpx>  [OPTIONS]

Source options (gpx only):
  --gpx-speed M_PER_S     Synthetic travel speed in m/s (default: 10)
  --gpx-interval SECS     Fix interval in seconds (default: 1.0)

Playback:
  --speed FLOAT           Playback speed multiplier (default: 1.0)
  --loop                  Loop playback at end of file
  --live-time             Replace replay timestamps with current wall-clock time

Sinks:
  --tcp / --no-tcp        Enable/disable gpsd JSON TCP server (default: enabled)
  --tcp-port INT          gpsd JSON TCP port (default: 2947)
  --tcp-host ADDR         TCP bind address (default: 0.0.0.0)
  --nmea-port INT         Enable raw NMEA TCP server on this port (disabled by default)
  --pty / --no-pty        Enable/disable PTY sink (default: disabled)
  --pty-path PATH         PTY symlink path (default: /tmp/gpsd-replay-pty)

Web UI:
  --web-ui / --no-web-ui  Enable/disable web UI (default: enabled)
  --ui-port INT           Web UI port (default: 8765)
  --file-dir DIR          Directory of replay files to expose in the UI file picker

General:
  -v, --verbose           Enable debug logging
```

## Architecture

```
Source (NMEALogSource / GPXRouteSource)
    │
    ▼
PlaybackEngine  ←── play/pause/seek/speed/loop controls
    │                              ▲
    ├──► TCPServerSink             │
    │    (gpsd JSON, :2947)        │
    │                          WebUIServer
    ├──► NMEARawServerSink         (http://localhost:8765)
    │    (raw NMEA, :2948)
    │
    └──► PTYSink
         (/dev/pts/N → symlink)
```

## Connecting as a Python client

```python
import socket, json

s = socket.create_connection(("localhost", 2947))
s.sendall(b'?WATCH={"enable":true,"json":true}\n')

while True:
    data = s.recv(4096).decode()
    for line in data.splitlines():
        try:
            msg = json.loads(line)
            if msg.get("class") == "TPV":
                print(f"Lat: {msg['lat']}, Lon: {msg['lon']}, Speed: {msg['speed']} m/s")
        except json.JSONDecodeError:
            pass
```

Or use the [`gpsd` Python package](https://pypi.org/project/gpsd/):

```python
import gpsd
gpsd.connect(host="localhost", port=2947)
fix = gpsd.get_current()
print(fix.lat, fix.lon)
```

## Example Files

`examples/national_mall_dc_drive.nmea` is a synthetic NMEA log of a round-trip drive along the National Mall in Washington DC (Lincoln Memorial → US Capitol → Lincoln Memorial) at ~25 mph. It is generated from public coordinates and contains no real device data.

To create your own NMEA recordings from a live GPS device:

```bash
# Capture from a running gpsd instance
gpsmon -n -l my_recording.nmea localhost:2947

# Capture directly from a serial GPS receiver
gpsmon -n -l my_recording.nmea /dev/ttyUSB0
```

> **Privacy note:** NMEA logs embed precise coordinates and timestamps. Avoid committing personal recordings to version control — add `*.nmea` to `.gitignore` to prevent accidents.

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

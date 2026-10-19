#!/usr/bin/env python3
"""
gpsd_to_kml.py - Stream live GPS from gpsd to a KML file for Google Earth.

Connects to a gpsd server, reads TPV fixes, and
continuously rewrites a KML file. A companion network_link.kml is written
once at startup — open that file in Google Earth to start live tracking.

Usage
-----
  python tools/gpsd_to_kml.py                       # localhost:2947
  python tools/gpsd_to_kml.py --port 2448           # gpsd-replay port
  python tools/gpsd_to_kml.py --follow              # keep Google Earth camera on position
  python tools/gpsd_to_kml.py --output /tmp/gps.kml --max-track 200

Google Earth setup
------------------
  File → Add → Network Link
    URL: file:///path/to/live.kml
    Refresh: Periodic, every 1 second

  Or just double-click the generated <name>_network_link.kml.
"""

import argparse
import json
import signal
import socket
import sys
import time
from pathlib import Path
from typing import List, Tuple


# ---------------------------------------------------------------------------
# gpsd connection
# ---------------------------------------------------------------------------

def _connect(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    sock.settimeout(5.0)
    return sock


def _tpv_stream(sock: socket.socket):
    """Yield parsed TPV dicts with a valid 2D+ fix from a gpsd socket."""
    buf = b""
    # Give the server a moment to send VERSION, then start watching
    time.sleep(0.1)
    sock.sendall(b'?WATCH={"enable":true,"json":true};\n')

    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            continue

        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("class") == "TPV" and msg.get("mode", 0) >= 2:
                yield msg


# ---------------------------------------------------------------------------
# KML helpers
# ---------------------------------------------------------------------------

_KML_NS = 'xmlns="http://www.opengis.net/kml/2.2"'
_HEADER = f'<?xml version="1.0" encoding="UTF-8"?>\n<kml {_KML_NS}>'
_FOOTER = "</kml>"


def _coord(lon: float, lat: float, alt: float) -> str:
    return f"{lon:.7f},{lat:.7f},{alt:.1f}"


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file then rename — avoids Google Earth reading a partial file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_live_kml(
    path: Path,
    lat: float,
    lon: float,
    alt: float,
    heading: float,
    speed_mps: float,
    timestamp: str,
    track: List[Tuple[float, float, float]],
    follow: bool,
) -> None:
    speed_kn = speed_mps * 1.94384
    pos_coord = _coord(lon, lat, alt)

    look_at = ""
    if follow:
        look_at = f"""
    <LookAt>
      <longitude>{lon:.7f}</longitude>
      <latitude>{lat:.7f}</latitude>
      <altitude>{alt:.1f}</altitude>
      <heading>{heading:.1f}</heading>
      <tilt>45</tilt>
      <range>800</range>
      <altitudeMode>relativeToGround</altitudeMode>
    </LookAt>"""

    track_placemark = ""
    if len(track) >= 2:
        coords = " ".join(_coord(lo, la, al) for la, lo, al in track)
        track_placemark = f"""
    <Placemark>
      <name>Track</name>
      <styleUrl>#trackStyle</styleUrl>
      <LineString>
        <tessellate>1</tessellate>
        <altitudeMode>clampToGround</altitudeMode>
        <coordinates>{coords}</coordinates>
      </LineString>
    </Placemark>"""

    kml = f"""{_HEADER}
  <Document>
    <name>GPS Live</name>{look_at}
    <Style id="posStyle">
      <IconStyle>
        <color>ff00ff00</color>
        <scale>1.2</scale>
        <heading>{heading:.1f}</heading>
        <Icon><href>http://maps.google.com/mapfiles/kml/shapes/arrow.png</href></Icon>
        <hotSpot x="0.5" y="0.5" xunits="fraction" yunits="fraction"/>
      </IconStyle>
      <LabelStyle><scale>0.9</scale></LabelStyle>
    </Style>
    <Style id="trackStyle">
      <LineStyle>
        <color>ffff0000</color>
        <width>3</width>
      </LineStyle>
    </Style>
    <Placemark>
      <name>{timestamp}</name>
      <description>Speed: {speed_kn:.1f} kn  Heading: {heading:.0f}°  Alt: {alt:.0f} m</description>
      <styleUrl>#posStyle</styleUrl>
      <Point>
        <altitudeMode>clampToGround</altitudeMode>
        <coordinates>{pos_coord}</coordinates>
      </Point>
    </Placemark>{track_placemark}
  </Document>
{_FOOTER}"""

    _write_atomic(path, kml)


def write_network_link_kml(nl_path: Path, live_path: Path, interval: float) -> None:
    live_uri = live_path.resolve().as_uri()
    kml = f"""{_HEADER}
  <NetworkLink>
    <name>GPS Live Feed</name>
    <open>1</open>
    <Link>
      <href>{live_uri}</href>
      <refreshMode>onInterval</refreshMode>
      <refreshInterval>{interval:.1f}</refreshInterval>
    </Link>
  </NetworkLink>
{_FOOTER}"""
    nl_path.write_text(kml, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stream live GPS from gpsd to a KML file for Google Earth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", default="localhost", help="gpsd host (default: localhost)")
    parser.add_argument("--port", type=int, default=2947, help="gpsd port (default: 2947)")
    parser.add_argument("--output", default="live.kml", metavar="FILE",
                        help="KML output path (default: live.kml)")
    parser.add_argument("--max-track", type=int, default=500, metavar="N",
                        help="Max track points to retain (default: 500)")
    parser.add_argument("--follow", action="store_true",
                        help="Move Google Earth camera to follow the position on each update")
    args = parser.parse_args()

    live_path = Path(args.output).resolve()
    nl_path = live_path.with_name(live_path.stem + "_network_link.kml")

    print(f"Connecting to gpsd at {args.host}:{args.port} ...")
    try:
        sock = _connect(args.host, args.port)
    except OSError as e:
        print(f"Cannot connect to gpsd: {e}", file=sys.stderr)
        sys.exit(1)

    write_network_link_kml(nl_path, live_path, interval=1.0)
    print(f"Network link KML : {nl_path}")
    print(f"Live KML         : {live_path}")
    print(f"Open {nl_path.name} in Google Earth to start tracking.")
    print("Waiting for fix  ...")

    track: List[Tuple[float, float, float]] = []
    count = 0

    def _shutdown(_sig, _frame):
        print(f"\nStopped after {count} fixes.")
        sock.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for tpv in _tpv_stream(sock):
        lat = tpv.get("lat", 0.0)
        lon = tpv.get("lon", 0.0)
        alt = tpv.get("alt", 0.0)
        heading = tpv.get("track", 0.0)
        speed = tpv.get("speed", 0.0)
        timestamp = tpv.get("time", "")

        track.append((lat, lon, alt))
        if len(track) > args.max_track:
            track = track[-args.max_track:]

        write_live_kml(
            live_path,
            lat=lat, lon=lon, alt=alt,
            heading=heading,
            speed_mps=speed,
            timestamp=timestamp,
            track=track,
            follow=args.follow,
        )

        count += 1
        if count == 1:
            print(f"First fix: {lat:.6f}, {lon:.6f}  alt={alt:.0f} m")
        elif count % 30 == 0:
            print(f"  {count} fixes  {lat:.6f}, {lon:.6f}")


if __name__ == "__main__":
    main()

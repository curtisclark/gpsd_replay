"""
gpsd_proto.py - gpsd JSON protocol messages (subset used for replay).

Reference: https://gpsd.gitlab.io/gpsd/gpsd_json.html

All messages use compact JSON (no spaces after separators) to match the
real gpsd wire format, which libgps clients rely on for class dispatch.
"""

import json
import datetime
from typing import Optional

_J = json.dumps  # shorthand; always call with separators=(',', ':')


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(',', ':'))


WATCH_MSG = _dumps({"class": "DEVICES", "devices": []}) + "\r\n"

VERSION_MSG = _dumps({
    "class": "VERSION",
    "release": "3.25",
    "rev": "gpsd-replay-0.1",
    "proto_major": 3,
    "proto_minor": 16,
}) + "\r\n"

WATCH_RESPONSE = _dumps({
    "class": "WATCH",
    "enable": True,
    "json": True,
    "nmea": False,
    "raw": 0,
    "scaled": False,
    "timing": False,
}) + "\r\n"

DEVICES_MSG = _dumps({
    "class": "DEVICES",
    "devices": [
        {
            "class": "DEVICE",
            "path": "/dev/pts/0",
            "driver": "NMEA0183",
            "activated": "2024-01-01T00:00:00.000Z",
            "flags": 1,
            "native": 0,
            "bps": 4800,
            "parity": "N",
            "stopbits": 1,
            "cycle": 1.00,
            "mincycle": 1.00,
        }
    ],
}) + "\r\n"


def tpv(
    lat: float,
    lon: float,
    alt: float = 0.0,
    speed: float = 0.0,  # m/s
    track: float = 0.0,
    ts: Optional[datetime.datetime] = None,
    mode: int = 3,
    device: str = "/dev/pts/0",
) -> str:
    """Build a TPV (time-position-velocity) JSON message."""
    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    msg = {
        "class": "TPV",
        "device": device,
        "mode": mode,
        "status": 1,
        "time": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "leapseconds": 18,
        "lat": round(lat, 8),
        "lon": round(lon, 8),
        "alt": round(alt, 2),
        "altHAE": round(alt, 2),
        "altMSL": round(alt, 2),
        "climb": 0.0,
        "speed": round(speed, 3),
        "track": round(track, 2),
        "ept": 0.005,
        "epx": 5.0,
        "epy": 5.0,
        "epv": 10.0,
        "eps": 0.01,
        "epc": 0.01,
    }
    return _dumps(msg) + "\r\n"


def sky(satellites: list = None, hdop: float = 1.0, vdop: float = 1.5, pdop: float = 1.8) -> str:
    """Build a SKY (satellite info) JSON message.

    satellites: list of dicts with keys PRN, el, az, ss (from parse_gpgsv).
                If None or empty, an empty satellites list is emitted.
    """
    sats = []
    for s in (satellites or []):
        sats.append({
            "PRN":    s["PRN"],
            "az":     round(s["az"]),
            "el":     round(s["el"]),
            "ss":     round(s["ss"]),
            "used":   s["ss"] > 0,
            "gnssid": 0,
            "svid":   s["PRN"],
        })
    msg = {
        "class": "SKY",
        "hdop": hdop,
        "vdop": vdop,
        "pdop": pdop,
        "satellites": sats,
    }
    return _dumps(msg) + "\r\n"


def parse_watch_cmd(data: str) -> Optional[dict]:
    """Try to parse a ?WATCH= command from a gpsd client."""
    data = data.strip()
    if data.startswith("?WATCH="):
        try:
            return json.loads(data[7:])
        except Exception:
            return {}
    if data == "?WATCH;":
        return {}
    return None

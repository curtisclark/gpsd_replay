#!/usr/bin/env python3
"""
Convert CSV files with GPS data to NMEA format.

CSV format:
- Header rows (anything not starting with FIELDS or REC) are ignored
- FIELDS row defines column headings (comma-separated, first token is "FIELDS")
- REC rows define data records (comma-separated, first token is "REC")

Required columns: SYS_DATE_TIME, GPS_LOCK, LAT, LON, ALT
  GPS_LOCK values: 0=no fix, 1=2D fix, 2=3D fix

Outputs GPGGA and GPRMC sentences for each record.
Speed and heading are computed from consecutive points.
"""

import argparse
import math
import sys
from datetime import datetime
from typing import Optional


# --- Datetime parsing ---

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%m/%d/%Y %H:%M:%S.%f",
]


def parse_datetime(dt_str: str) -> Optional[datetime]:
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue
    return None


# --- Coordinate conversion ---

def decimal_to_nmea_lat(lat: float) -> tuple[str, str]:
    """Decimal degrees → NMEA (DDMM.MMMM, N/S)."""
    hemi = "N" if lat >= 0 else "S"
    lat = abs(lat)
    deg = int(lat)
    minutes = (lat - deg) * 60
    return f"{deg:02d}{minutes:07.4f}", hemi


def decimal_to_nmea_lon(lon: float) -> tuple[str, str]:
    """Decimal degrees → NMEA (DDDMM.MMMM, E/W)."""
    hemi = "E" if lon >= 0 else "W"
    lon = abs(lon)
    deg = int(lon)
    minutes = (lon - deg) * 60
    return f"{deg:03d}{minutes:07.4f}", hemi


# --- NMEA utilities ---

def nmea_checksum(body: str) -> str:
    """XOR checksum of characters between $ and * (exclusive)."""
    result = 0
    for c in body:
        result ^= ord(c)
    return f"{result:02X}"


def nmea_sentence(body: str) -> str:
    return f"${body}*{nmea_checksum(body)}"


# --- Geodetic calculations ---

def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two decimal-degree points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def true_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing in degrees [0, 360) from point 1 to point 2."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# --- NMEA sentence builders ---

def make_gpgga(dt: datetime, lat: float, lon: float, alt: float, fix_quality: int) -> str:
    """
    $GPGGA – GPS Fix Data
    fix_quality: 0=invalid, 1=GPS, 2=DGPS
    """
    nmea_lat, lat_hemi = decimal_to_nmea_lat(lat)
    nmea_lon, lon_hemi = decimal_to_nmea_lon(lon)
    time_str = dt.strftime("%H%M%S.00")
    num_sats = 8 if fix_quality > 0 else 0
    hdop = 1.0 if fix_quality > 0 else 99.9
    body = (
        f"GPGGA,{time_str},"
        f"{nmea_lat},{lat_hemi},"
        f"{nmea_lon},{lon_hemi},"
        f"{fix_quality},{num_sats:02d},{hdop:.1f},"
        f"{alt:.1f},M,"
        f"0.0,M,,"
    )
    return nmea_sentence(body)


def make_gprmc(
    dt: datetime,
    lat: float,
    lon: float,
    speed_knots: float,
    heading: float,
    valid: bool,
) -> str:
    """
    $GPRMC – Recommended Minimum Specific GPS Data
    valid: True → status A (active), False → status V (void)
    """
    nmea_lat, lat_hemi = decimal_to_nmea_lat(lat)
    nmea_lon, lon_hemi = decimal_to_nmea_lon(lon)
    status = "A" if valid else "V"
    time_str = dt.strftime("%H%M%S.00")
    date_str = dt.strftime("%d%m%y")
    body = (
        f"GPRMC,{time_str},{status},"
        f"{nmea_lat},{lat_hemi},"
        f"{nmea_lon},{lon_hemi},"
        f"{speed_knots:.2f},{heading:.2f},"
        f"{date_str},,"
    )
    return nmea_sentence(body)


# --- CSV parsing ---

def parse_csv(filepath: str):
    """
    Yield each REC row as a dict keyed by the FIELDS header.
    Skips all lines that do not start with FIELDS or REC.
    """
    fields: Optional[list[str]] = None
    with open(filepath, "r") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue

            if line.upper().startswith("FIELDS"):
                parts = [p.strip() for p in line.split(",")]
                fields = parts[1:]  # drop the "FIELDS" token
            elif line.upper().startswith("REC") and fields is not None:
                parts = [p.strip() for p in line.split(",")]
                values = parts[1:]  # drop the "REC" token
                yield dict(zip(fields, values))


# --- Conversion logic ---

def convert(filepath: str, output=sys.stdout) -> None:
    rows = list(parse_csv(filepath))

    prev_dt: Optional[datetime] = None
    prev_lat: Optional[float] = None
    prev_lon: Optional[float] = None
    speed_knots = 0.0
    heading = 0.0

    for row in rows:
        dt = parse_datetime(row.get("SYS_DATE_TIME", ""))
        if dt is None:
            print(f"# WARNING: unparseable timestamp: {row.get('SYS_DATE_TIME')}", file=output)
            continue

        try:
            lat = float(row["LAT"])
            lon = float(row["LON"])
            alt = float(row.get("ALT", 0))
            lock = int(row.get("GPS_LOCK", 0))
        except (KeyError, ValueError) as exc:
            print(f"# WARNING: skipping row – {exc}", file=output)
            continue

        valid = lock > 0
        # GPGGA fix quality: 0=invalid, 1=GPS fix (covers both 2D and 3D)
        fix_quality = 0 if lock == 0 else 1

        # Compute speed/heading from previous valid point
        if prev_dt is not None and prev_lat is not None and prev_lon is not None:
            dt_sec = (dt - prev_dt).total_seconds()
            if dt_sec > 0:
                dist_m = haversine_meters(prev_lat, prev_lon, lat, lon)
                speed_knots = (dist_m / dt_sec) * 1.94384  # m/s → knots
                if dist_m > 0.5:  # update heading only when meaningfully displaced
                    heading = true_bearing(prev_lat, prev_lon, lat, lon)

        print(make_gpgga(dt, lat, lon, alt, fix_quality), file=output)
        print(make_gprmc(dt, lat, lon, speed_knots, heading, valid), file=output)

        prev_dt = dt
        prev_lat = lat
        prev_lon = lon


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert GPS CSV logs to NMEA sentences (GPGGA + GPRMC)."
    )
    parser.add_argument("input", help="Input CSV file")
    parser.add_argument(
        "-o", "--output", help="Output file (default: stdout)"
    )
    args = parser.parse_args()

    if args.output:
        with open(args.output, "w") as fh:
            convert(args.input, output=fh)
    else:
        convert(args.input)


if __name__ == "__main__":
    main()

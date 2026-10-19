"""
nmea.py - NMEA 0183 parsing and generation utilities.
"""

import re
import math
import datetime
from typing import Optional


def checksum(sentence: str) -> str:
    """Compute NMEA checksum (XOR of bytes between $ and *)."""
    cs = 0
    for ch in sentence:
        cs ^= ord(ch)
    return f"{cs:02X}"


def wrap(sentence: str) -> str:
    """Wrap a raw sentence (without $ or checksum) into a full NMEA line."""
    return f"${sentence}*{checksum(sentence)}\r\n"


def verify(line: str) -> bool:
    """Return True if NMEA checksum is valid."""
    m = re.match(r"^\$([^*]+)\*([0-9A-Fa-f]{2})", line.strip())
    if not m:
        return False
    return checksum(m.group(1)).upper() == m.group(2).upper()


def decdeg_to_nmea(deg: float, is_lat: bool) -> tuple[str, str]:
    """Convert decimal degrees to NMEA ddmm.mmmmm / dddmm.mmmmm format."""
    if is_lat:
        hemi = "N" if deg >= 0 else "S"
        d = abs(deg)
        dd = int(d)
        mm = (d - dd) * 60
        return f"{dd:02d}{mm:08.5f}", hemi
    else:
        hemi = "E" if deg >= 0 else "W"
        d = abs(deg)
        dd = int(d)
        mm = (d - dd) * 60
        return f"{dd:03d}{mm:08.5f}", hemi


def nmea_to_decdeg(val: str, hemi: str) -> float:
    """Convert NMEA lat/lon string + hemisphere to decimal degrees."""
    dot = val.index(".")
    deg = int(val[: dot - 2])
    minutes = float(val[dot - 2 :])
    result = deg + minutes / 60.0
    if hemi in ("S", "W"):
        result = -result
    return result


def make_gprmc(
    lat: float,
    lon: float,
    speed_knots: float = 0.0,
    bearing: float = 0.0,
    ts: Optional[datetime.datetime] = None,
) -> str:
    """Generate a $GPRMC sentence."""
    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    time_str = ts.strftime("%H%M%S.00")
    date_str = ts.strftime("%d%m%y")
    lat_s, lat_h = decdeg_to_nmea(lat, True)
    lon_s, lon_h = decdeg_to_nmea(lon, False)
    body = f"GPRMC,{time_str},A,{lat_s},{lat_h},{lon_s},{lon_h},{speed_knots:.2f},{bearing:.2f},{date_str},,"
    return wrap(body)


def make_gpgga(
    lat: float,
    lon: float,
    altitude_m: float = 0.0,
    num_sats: int = 8,
    hdop: float = 1.0,
    ts: Optional[datetime.datetime] = None,
) -> str:
    """Generate a $GPGGA sentence."""
    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    time_str = ts.strftime("%H%M%S.00")
    lat_s, lat_h = decdeg_to_nmea(lat, True)
    lon_s, lon_h = decdeg_to_nmea(lon, False)
    body = (
        f"GPGGA,{time_str},{lat_s},{lat_h},{lon_s},{lon_h},"
        f"1,{num_sats:02d},{hdop:.1f},{altitude_m:.1f},M,0.0,M,,"
    )
    return wrap(body)


def parse_gprmc(line: str) -> Optional[dict]:
    """Parse $GPRMC into a dict or None."""
    line = line.strip()
    if not line.startswith("$GPRMC") and not line.startswith("$GNRMC"):
        return None
    parts = line.split("*")[0].lstrip("$").split(",")
    if len(parts) < 10 or parts[2] != "A":
        return None
    try:
        t = parts[1]
        d = parts[9]
        dt = datetime.datetime.strptime(d + t[:6], "%d%m%y%H%M%S")
        lat = nmea_to_decdeg(parts[3], parts[4])
        lon = nmea_to_decdeg(parts[5], parts[6])
        speed_knots = float(parts[7]) if parts[7] else 0.0
        bearing = float(parts[8]) if parts[8] else 0.0
        return {"lat": lat, "lon": lon, "speed_knots": speed_knots, "bearing": bearing, "ts": dt}
    except Exception:
        return None


def parse_gpgga(line: str) -> Optional[dict]:
    """Parse $GPGGA into a dict or None."""
    line = line.strip()
    if not line.startswith("$GPGGA") and not line.startswith("$GNGGA"):
        return None
    parts = line.split("*")[0].lstrip("$").split(",")
    if len(parts) < 10 or parts[6] == "0":
        return None
    try:
        lat = nmea_to_decdeg(parts[2], parts[3])
        lon = nmea_to_decdeg(parts[4], parts[5])
        alt = float(parts[9]) if parts[9] else 0.0
        geoid_sep = float(parts[11]) if len(parts) > 11 and parts[11] else None
        result: dict = {"lat": lat, "lon": lon, "altitude_m": alt}
        if geoid_sep is not None:
            result["geoid_sep"] = geoid_sep
        return result
    except Exception:
        return None


def parse_pashr_pos(line: str) -> Optional[dict]:
    """Parse a $PASHR,POS sentence into a dict or None.

    Field layout (Ashtech/Trimble):
      $PASHR,POS,mode,nsats,hhmmss.ss,lat,NS,lon,EW,alt_msl,track,sog_kn,e_vel,n_vel,
                 PDOP,HDOP,VDOP,TDOP,...*cs

    Altitude is MSL in metres.  Track may be '????' when stationary.
    Solution mode 0 = autonomous GPS (valid), not "no fix".
    Returns time-of-day only (no date); caller should merge with a date source if needed.
    """
    line = line.strip()
    if not line.startswith("$PASHR,POS"):
        return None
    parts = line.split("*")[0].lstrip("$").split(",")
    # Minimum required: up to SOG (index 11)
    if len(parts) < 12:
        return None
    try:
        t = parts[4]
        ts = datetime.datetime.strptime(t[:6], "%H%M%S").replace(
            microsecond=int(float("0." + (t.split(".")[1] if "." in t else "0")) * 1e6)
        )
        lat = nmea_to_decdeg(parts[5], parts[6])
        lon = nmea_to_decdeg(parts[7], parts[8])
        alt_msl = float(parts[9])
        speed_knots = float(parts[11]) if parts[11] else 0.0
        result: dict = {
            "lat": lat,
            "lon": lon,
            "altitude_m": alt_msl,
            "speed_knots": speed_knots,
            "ts": ts,
        }
        try:
            result["bearing"] = float(parts[10])
        except ValueError:
            pass  # omit key when track is '????' (stationary / unknown)
        if len(parts) > 15:
            try:
                result["hdop"] = float(parts[15])
            except ValueError:
                pass
        return result
    except Exception:
        return None


def parse_gpgsv(line: str) -> list:
    """Parse a $GPGSV sentence, returning a list of satellite dicts."""
    line = line.strip()
    if not line[1:3] in ("GP", "GN", "GL", "GA", "GB") or "GSV" not in line[:7]:
        return []
    parts = line.split("*")[0].lstrip("$").split(",")
    if len(parts) < 4:
        return []
    sats = []
    i = 4
    while i < len(parts):
        prn_s = parts[i] if i < len(parts) else ""
        el_s  = parts[i + 1] if i + 1 < len(parts) else ""
        az_s  = parts[i + 2] if i + 2 < len(parts) else ""
        snr_s = parts[i + 3] if i + 3 < len(parts) else ""
        if prn_s:
            try:
                sats.append({
                    "PRN": int(prn_s),
                    "el":  float(el_s)  if el_s  else 0.0,
                    "az":  float(az_s)  if az_s  else 0.0,
                    "ss":  float(snr_s) if snr_s else 0.0,
                })
            except ValueError:
                pass
        i += 4
    return sats


def haversine_bearing(lat1, lon1, lat2, lon2) -> float:
    """Compute initial bearing from point 1 to point 2 in degrees."""
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def haversine_distance(lat1, lon1, lat2, lon2) -> float:
    """Return distance in meters between two points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

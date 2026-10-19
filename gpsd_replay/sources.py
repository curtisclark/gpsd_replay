"""
sources.py - GPS data sources for gpsd-replay.

Provides:
  - NMEALogSource  : replays a recorded NMEA log file with accurate timing
  - GPXRouteSource : generates synthetic NMEA from a GPX waypoint file
"""

import re
import time
import math
import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

from .nmea import (
    make_gprmc,
    make_gpgga,
    parse_gprmc,
    parse_gpgga,
    parse_pashr_pos,
    haversine_bearing,
    haversine_distance,
)


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

@dataclass
class GPSFrame:
    """One logical GPS fix, with associated NMEA sentences to emit."""
    ts: datetime.datetime
    lat: float
    lon: float
    alt: float = 0.0          # MSL altitude (metres)
    alt_hae: Optional[float] = None  # Height Above Ellipsoid (MSL + geoid sep); None if unknown
    speed_knots: float = 0.0
    bearing: float = 0.0
    sentences: List[str] = field(default_factory=list)  # raw NMEA lines (already wrapped)


# ---------------------------------------------------------------------------
# NMEA Log Source
# ---------------------------------------------------------------------------

class NMEALogSource:
    """
    Reads a raw NMEA log file and yields GPSFrames preserving the original
    inter-sentence timing derived from $GPRMC timestamps.

    Lines that have no parseable RMC are still re-emitted as passthrough
    sentences attached to the most recent frame.
    """

    def __init__(self, path: str):
        self.path = path
        self._frames: List[GPSFrame] = []
        self._loaded = False

    def _load(self):
        frames: List[GPSFrame] = []
        pending: List[str] = []
        last_frame: Optional[GPSFrame] = None
        ref_date: Optional[datetime.date] = None  # date learned from first RMC

        with open(self.path, "r", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or not line.startswith("$"):
                    continue

                rmc = parse_gprmc(line)
                if rmc:
                    ref_date = rmc["ts"].date()
                    if last_frame and pending:
                        last_frame.sentences.extend(pending)
                        pending = []
                    frame = GPSFrame(
                        ts=rmc["ts"],
                        lat=rmc["lat"],
                        lon=rmc["lon"],
                        speed_knots=rmc["speed_knots"],
                        bearing=rmc["bearing"],
                        sentences=[line + "\r\n"],
                    )
                    frames.append(frame)
                    last_frame = frame
                    continue

                # pashr = parse_pashr_pos(line)
                # if pashr:
                #     if last_frame is not None:
                #         # Refine existing frame: PASHR,POS carries higher-precision
                #         # lat/lon than the GPSD-truncated RMC/GGA sentences.
                #         last_frame.lat = pashr["lat"]
                #         last_frame.lon = pashr["lon"]
                #         last_frame.speed_knots = pashr["speed_knots"]
                #         if "bearing" in pashr:
                #             last_frame.bearing = pashr["bearing"]
                #         if last_frame.alt == 0.0:
                #             last_frame.alt = pashr["altitude_m"]
                #         last_frame.sentences.append(line + "\r\n")
                #     else:
                #         # No RMC seen yet — use PASHR,POS as the frame anchor.
                #         tod = pashr["ts"]
                #         date = ref_date or datetime.date.today()
                #         ts = tod.replace(
                #             year=date.year, month=date.month, day=date.day
                #         )
                #         frame = GPSFrame(
                #             ts=ts,
                #             lat=pashr["lat"],
                #             lon=pashr["lon"],
                #             alt=pashr["altitude_m"],
                #             speed_knots=pashr["speed_knots"],
                #             bearing=pashr.get("bearing", 0.0),
                #             sentences=pending + [line + "\r\n"],
                #         )
                #         pending = []
                #         frames.append(frame)
                #         last_frame = frame
                #     continue

                if last_frame is not None:
                    gga = parse_gpgga(line)
                    if gga is not None:
                        last_frame.alt = gga["altitude_m"]
                        if "geoid_sep" in gga:
                            last_frame.alt_hae = gga["altitude_m"] + gga["geoid_sep"]
                    last_frame.sentences.append(line + "\r\n")
                else:
                    pending.append(line + "\r\n")

        self._frames = frames
        self._loaded = True

    def frames(self) -> List[GPSFrame]:
        if not self._loaded:
            self._load()
        return self._frames

    def __len__(self):
        return len(self.frames())

    def __repr__(self):
        return f"NMEALogSource({self.path!r}, {len(self)} frames)"


# ---------------------------------------------------------------------------
# GPX Route Source
# ---------------------------------------------------------------------------

_GPX_NS = {
    "gpx1": "http://www.topografix.com/GPX/1/1",
    "gpx0": "http://www.topografix.com/GPX/1/0",
}

@dataclass
class Waypoint:
    lat: float
    lon: float
    alt: float = 0.0
    name: str = ""
    ts: Optional[datetime.datetime] = None


def _parse_gpx(path: str) -> List[Waypoint]:
    """Parse GPX file, returning a list of Waypoints from tracks, routes, or waypoints."""
    tree = ET.parse(path)
    root = tree.getroot()
    tag = root.tag  # e.g. {http://...}gpx

    ns_uri = ""
    m = re.match(r"\{([^}]+)\}", tag)
    if m:
        ns_uri = m.group(1)
    ns = {"g": ns_uri} if ns_uri else {}

    def find_pts(search_tag):
        pts = []
        if ns:
            for pt in root.iter(f"{{{ns_uri}}}{search_tag}"):
                lat = float(pt.attrib.get("lat", 0))
                lon = float(pt.attrib.get("lon", 0))
                ele_el = pt.find(f"{{{ns_uri}}}ele")
                alt = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
                name_el = pt.find(f"{{{ns_uri}}}name")
                name = name_el.text if name_el is not None else ""
                time_el = pt.find(f"{{{ns_uri}}}time")
                ts = None
                if time_el is not None and time_el.text:
                    try:
                        ts = datetime.datetime.fromisoformat(
                            time_el.text.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except ValueError:
                        pass
                pts.append(Waypoint(lat=lat, lon=lon, alt=alt, name=name, ts=ts))
        else:
            for pt in root.iter(search_tag):
                lat = float(pt.attrib.get("lat", 0))
                lon = float(pt.attrib.get("lon", 0))
                pts.append(Waypoint(lat=lat, lon=lon))
        return pts

    # Prefer trkpt > rtept > wpt
    pts = find_pts("trkpt")
    if not pts:
        pts = find_pts("rtept")
    if not pts:
        pts = find_pts("wpt")
    return pts


class GPXRouteSource:
    """
    Generates synthetic NMEA GPSFrames by interpolating along a GPX route.

    Parameters
    ----------
    path        : GPX file path
    speed_mps   : Constant speed in m/s for interpolation (used when GPX has no timestamps)
    interval_s  : Fix interval in seconds (default 1.0)
    start_ts    : Override start time (default: now)
    """

    def __init__(
        self,
        path: str,
        speed_mps: float = 10.0,
        interval_s: float = 1.0,
        start_ts: Optional[datetime.datetime] = None,
    ):
        self.path = path
        self.speed_mps = speed_mps
        self.interval_s = interval_s
        self.start_ts = start_ts or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self._frames: List[GPSFrame] = []
        self._loaded = False

    def _load(self):
        waypoints = _parse_gpx(self.path)
        if len(waypoints) < 2:
            raise ValueError(f"GPX file must have at least 2 points, got {len(waypoints)}")

        frames: List[GPSFrame] = []
        ts = self.start_ts
        dt = datetime.timedelta(seconds=self.interval_s)

        # Walk each segment between consecutive waypoints
        for i in range(len(waypoints) - 1):
            wp_a = waypoints[i]
            wp_b = waypoints[i + 1]
            dist = haversine_distance(wp_a.lat, wp_a.lon, wp_b.lat, wp_b.lon)
            bearing = haversine_bearing(wp_a.lat, wp_a.lon, wp_b.lat, wp_b.lon)

            if dist < 0.1:
                continue  # skip duplicate points

            travel_time_s = dist / self.speed_mps
            steps = max(1, int(travel_time_s / self.interval_s))

            for step in range(steps):
                frac = step / steps
                lat = wp_a.lat + frac * (wp_b.lat - wp_a.lat)
                lon = wp_a.lon + frac * (wp_b.lon - wp_a.lon)
                alt = wp_a.alt + frac * (wp_b.alt - wp_a.alt)
                speed_knots = self.speed_mps * 1.94384

                rmc = make_gprmc(lat, lon, speed_knots, bearing, ts)
                gga = make_gpgga(lat, lon, alt, ts=ts)

                frame = GPSFrame(
                    ts=ts,
                    lat=lat,
                    lon=lon,
                    alt=alt,
                    speed_knots=speed_knots,
                    bearing=bearing,
                    sentences=[rmc, gga],
                )
                frames.append(frame)
                ts += dt

        self._frames = frames
        self._loaded = True

    def frames(self) -> List[GPSFrame]:
        if not self._loaded:
            self._load()
        return self._frames

    def __len__(self):
        return len(self.frames())

    def __repr__(self):
        return f"GPXRouteSource({self.path!r}, {len(self)} frames)"

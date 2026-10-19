"""
tests/test_gpsd_replay.py
"""

import datetime
import math
import pytest
import tempfile
import os

from gpsd_replay.nmea import (
    checksum, wrap, verify, decdeg_to_nmea, nmea_to_decdeg,
    make_gprmc, make_gpgga, parse_gprmc, haversine_distance, haversine_bearing,
)
from gpsd_replay.gpsd_proto import tpv, sky, parse_watch_cmd
from gpsd_replay.sources import NMEALogSource, GPXRouteSource
from gpsd_replay.engine import PlaybackEngine, State


# ---------------------------------------------------------------------------
# NMEA helpers
# ---------------------------------------------------------------------------

class TestNMEA:
    def test_checksum_known(self):
        # Known NMEA checksum: GPRMC has a defined CS
        body = "GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
        cs = checksum(body)
        assert len(cs) == 2
        assert cs.isalnum()

    def test_wrap_verify(self):
        sentence = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
        line = wrap(sentence)
        assert line.startswith("$")
        assert "*" in line
        assert verify(line.strip())

    def test_decdeg_roundtrip(self):
        for lat in [0.0, 51.5074, -33.8688, 90.0, -90.0]:
            s, h = decdeg_to_nmea(lat, True)
            result = nmea_to_decdeg(s, h)
            assert abs(result - lat) < 1e-5, f"lat {lat} roundtrip failed: {result}"

        for lon in [0.0, -122.4194, 139.6917, 180.0, -180.0]:
            s, h = decdeg_to_nmea(lon, False)
            result = nmea_to_decdeg(s, h)
            assert abs(result - lon) < 1e-5, f"lon {lon} roundtrip failed: {result}"

    def test_make_gprmc_parseable(self):
        ts = datetime.datetime(2024, 3, 15, 10, 30, 0)
        line = make_gprmc(51.5, -0.1, speed_knots=5.0, bearing=270.0, ts=ts)
        assert verify(line.strip())
        parsed = parse_gprmc(line)
        assert parsed is not None
        assert abs(parsed["lat"] - 51.5) < 1e-4
        assert abs(parsed["lon"] - (-0.1)) < 1e-4
        assert abs(parsed["speed_knots"] - 5.0) < 0.01
        assert abs(parsed["bearing"] - 270.0) < 0.01

    def test_make_gpgga_valid(self):
        line = make_gpgga(37.7749, -122.4194, altitude_m=30.0)
        assert verify(line.strip())

    def test_haversine_distance(self):
        # London to Paris ≈ 340 km
        d = haversine_distance(51.5074, -0.1278, 48.8566, 2.3522)
        assert 330_000 < d < 350_000

    def test_haversine_bearing(self):
        # Due East from origin
        b = haversine_bearing(0.0, 0.0, 0.0, 1.0)
        assert abs(b - 90.0) < 1.0


# ---------------------------------------------------------------------------
# gpsd protocol
# ---------------------------------------------------------------------------

class TestGpsdProto:
    def test_tpv_valid_json(self):
        import json
        ts = datetime.datetime(2024, 1, 1, 12, 0, 0)
        msg = tpv(lat=51.5, lon=-0.1, alt=10.0, speed=5.0, track=180.0, ts=ts)
        obj = json.loads(msg.strip())
        assert obj["class"] == "TPV"
        assert obj["lat"] == 51.5
        assert obj["lon"] == -0.1

    def test_sky_valid_json(self):
        import json
        msg = sky(num_sats=6)
        obj = json.loads(msg.strip())
        assert obj["class"] == "SKY"
        assert len(obj["satellites"]) == 6

    def test_parse_watch_cmd(self):
        assert parse_watch_cmd("?WATCH;") == {}
        result = parse_watch_cmd('?WATCH={"enable":true,"json":true}')
        assert result["enable"] is True


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

SAMPLE_NMEA = """\
$GPRMC,120000.00,A,5130.00000,N,00006.00000,W,0.000,0.00,150324,,*1C
$GPGGA,120000.00,5130.00000,N,00006.00000,W,1,08,1.0,50.0,M,0.0,M,,*46
$GPRMC,120001.00,A,5130.00100,N,00005.99900,W,1.000,45.00,150324,,*27
$GPGGA,120001.00,5130.00100,N,00005.99900,W,1,08,1.0,50.0,M,0.0,M,,*62
$GPRMC,120002.00,A,5130.00200,N,00005.99800,W,1.000,45.00,150324,,*27
"""

SAMPLE_GPX = """\
<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="51.5" lon="-0.1"><ele>10</ele></trkpt>
    <trkpt lat="51.51" lon="-0.09"><ele>12</ele></trkpt>
    <trkpt lat="51.52" lon="-0.08"><ele>15</ele></trkpt>
  </trkseg></trk>
</gpx>
"""


class TestNMEALogSource:
    def _make_file(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".nmea", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_load_frames(self):
        path = self._make_file(SAMPLE_NMEA)
        try:
            src = NMEALogSource(path)
            frames = src.frames()
            assert len(frames) == 3
            assert abs(frames[0].lat - 51.5) < 0.01
        finally:
            os.unlink(path)

    def test_sentences_attached(self):
        path = self._make_file(SAMPLE_NMEA)
        try:
            src = NMEALogSource(path)
            frames = src.frames()
            # Each frame should have at least the RMC line
            for f in frames:
                assert len(f.sentences) >= 1
                assert any("GPRMC" in s for s in f.sentences)
        finally:
            os.unlink(path)


class TestGPXRouteSource:
    def _make_file(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".gpx", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_load_frames(self):
        path = self._make_file(SAMPLE_GPX)
        try:
            src = GPXRouteSource(path, speed_mps=10.0, interval_s=1.0)
            frames = src.frames()
            assert len(frames) > 2
            # First frame near first waypoint
            assert abs(frames[0].lat - 51.5) < 0.01
        finally:
            os.unlink(path)

    def test_sentences_generated(self):
        path = self._make_file(SAMPLE_GPX)
        try:
            src = GPXRouteSource(path, speed_mps=10.0)
            for f in src.frames():
                assert len(f.sentences) == 2  # RMC + GGA
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TestPlaybackEngine:
    def _make_frames(self, n=5):
        from gpsd_replay.sources import GPSFrame
        base_ts = datetime.datetime(2024, 1, 1, 0, 0, 0)
        frames = []
        for i in range(n):
            frames.append(GPSFrame(
                ts=base_ts + datetime.timedelta(seconds=i),
                lat=51.5 + i * 0.001,
                lon=-0.1,
                sentences=[f"$FRAME{i}\r\n"],
            ))
        return frames

    def test_play_stop(self):
        import time
        frames = self._make_frames(3)
        received = []
        engine = PlaybackEngine(frames, speed=100.0)
        engine.add_sink(lambda f: received.append(f))
        engine.play()
        time.sleep(0.3)
        engine.stop()
        assert len(received) == 3

    def test_pause_resume(self):
        import time
        frames = self._make_frames(10)
        received = []
        engine = PlaybackEngine(frames, speed=100.0)
        engine.add_sink(lambda f: received.append(f))
        engine.play()
        time.sleep(0.05)
        engine.pause()
        count_at_pause = len(received)
        time.sleep(0.1)
        assert len(received) == count_at_pause  # frozen while paused
        engine.resume()
        time.sleep(0.3)
        engine.stop()
        assert len(received) > count_at_pause

    def test_seek(self):
        import time
        frames = self._make_frames(20)
        received = []
        engine = PlaybackEngine(frames, speed=100.0)
        engine.add_sink(lambda f: received.append(f))
        engine.play()
        time.sleep(0.05)
        engine.seek(15)
        time.sleep(0.2)
        engine.stop()
        # Verify a frame near index 15 was received
        lats = [f.lat for f in received]
        assert any(lat > 51.514 for lat in lats)

    def test_loop(self):
        import time
        frames = self._make_frames(3)
        received = []
        engine = PlaybackEngine(frames, speed=1000.0, loop=True)
        engine.add_sink(lambda f: received.append(f))
        engine.play()
        time.sleep(0.2)
        engine.stop()
        assert len(received) > 3  # looped

    def test_status(self):
        frames = self._make_frames(5)
        engine = PlaybackEngine(frames, speed=1.0, loop=False)
        st = engine.status
        assert st.state == State.STOPPED
        assert st.total_frames == 5

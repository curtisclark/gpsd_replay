"""
engine.py - Playback engine for gpsd-replay.

The PlaybackEngine iterates over GPSFrames and emits them to registered
sinks at the correct wall-clock rate (adjusted by speed multiplier).
It supports pause/resume, seek, loop, and live speed changes.
"""

import time
import datetime
import threading
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, List, Optional

from .sources import GPSFrame

logger = logging.getLogger(__name__)


class State(Enum):
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()
    MANUAL = auto()


# Sink: callable that receives (frame: GPSFrame) → None
FrameSink = Callable[[GPSFrame], None]


@dataclass
class PlaybackStatus:
    state: State = State.STOPPED
    frame_index: int = 0
    total_frames: int = 0
    speed: float = 1.0
    loop: bool = False
    current_lat: float = 0.0
    current_lon: float = 0.0
    elapsed_s: float = 0.0


class PlaybackEngine:
    """
    Thread-safe GPS playback engine.

    Usage
    -----
    engine = PlaybackEngine(frames, speed=1.0, loop=True)
    engine.add_sink(my_callback)
    engine.play()
    ...
    engine.pause()
    engine.seek(100)        # jump to frame index
    engine.set_speed(2.0)
    engine.stop()
    """

    def __init__(
        self,
        frames: List[GPSFrame],
        speed: float = 1.0,
        loop: bool = False,
        live_time: bool = False,
    ):
        self._frames = frames
        self._speed = speed
        self._loop = loop
        self._live_time = live_time
        self._sinks: List[FrameSink] = []
        self._state = State.STOPPED
        self._index = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._manual_frame: Optional[GPSFrame] = None
        self._start_wall: float = 0.0
        self._elapsed_s: float = 0.0

    # ------------------------------------------------------------------
    # Sink management
    # ------------------------------------------------------------------

    def add_sink(self, sink: FrameSink):
        with self._lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: FrameSink):
        with self._lock:
            self._sinks = [s for s in self._sinks if s is not sink]

    # ------------------------------------------------------------------
    # Playback control (thread-safe)
    # ------------------------------------------------------------------

    def play(self):
        with self._cond:
            if self._state == State.PLAYING:
                return
            if self._state == State.PAUSED:
                self._state = State.PLAYING
                self._cond.notify_all()
                return
            if self._state == State.MANUAL:
                self._manual_frame = None
                self._state = State.STOPPED
                self._cond.notify_all()
            # STOPPED → start fresh
            self._state = State.PLAYING
            self._index = 0
            self._thread = threading.Thread(target=self._run, daemon=True, name="gpsd-replay-engine")
            self._thread.start()

    def pause(self):
        with self._cond:
            if self._state == State.PLAYING:
                self._state = State.PAUSED
                self._cond.notify_all()

    def resume(self):
        self.play()

    def stop(self):
        with self._cond:
            self._state = State.STOPPED
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def seek(self, index: int):
        """Seek to a specific frame index (clamped to valid range)."""
        with self._cond:
            self._index = max(0, min(index, len(self._frames) - 1))
            self._cond.notify_all()

    def set_speed(self, speed: float):
        """Set playback speed multiplier (> 0)."""
        with self._cond:
            self._speed = max(0.01, float(speed))

    def set_loop(self, loop: bool):
        with self._cond:
            self._loop = loop

    def load_frames(self, frames: List[GPSFrame]):
        """Replace current frames and reset to stopped state."""
        with self._cond:
            self._state = State.STOPPED
            self._manual_frame = None
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            self._frames = frames
            self._index = 0
            self._elapsed_s = 0.0

    def set_manual(self, frame: GPSFrame):
        """Pin a static GPS frame and emit it continuously at ~1 Hz."""
        with self._cond:
            self._state = State.STOPPED
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            self._manual_frame = frame
            self._state = State.MANUAL
        self._thread = threading.Thread(
            target=self._manual_run, daemon=True, name="gpsd-replay-manual"
        )
        self._thread.start()
        logger.info("Manual mode: %.6f, %.6f", frame.lat, frame.lon)

    def clear_manual(self):
        """Exit manual mode and return to stopped state."""
        with self._cond:
            self._manual_frame = None
            self._state = State.STOPPED
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def current_frame(self) -> Optional[GPSFrame]:
        with self._lock:
            if self._state == State.MANUAL and self._manual_frame is not None:
                return self._manual_frame
            if self._frames and 0 <= self._index < len(self._frames):
                return self._frames[self._index]
            return None

    @property
    def status(self) -> PlaybackStatus:
        with self._lock:
            if self._state == State.MANUAL and self._manual_frame is not None:
                f = self._manual_frame
            else:
                f = self._frames[self._index] if self._frames else None
            return PlaybackStatus(
                state=self._state,
                frame_index=self._index,
                total_frames=len(self._frames),
                speed=self._speed,
                loop=self._loop,
                current_lat=f.lat if f else 0.0,
                current_lon=f.lon if f else 0.0,
                elapsed_s=self._elapsed_s,
            )

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _manual_run(self):
        from .nmea import make_gprmc, make_gpgga
        logger.info("Manual emit loop started")
        while True:
            with self._lock:
                if self._state != State.MANUAL or self._manual_frame is None:
                    break
                tmpl = self._manual_frame
                sinks = list(self._sinks)

            now = datetime.datetime.utcnow()
            frame = GPSFrame(
                ts=now,
                lat=tmpl.lat,
                lon=tmpl.lon,
                alt=tmpl.alt,
                alt_hae=tmpl.alt_hae,
                speed_knots=tmpl.speed_knots,
                bearing=tmpl.bearing,
                sentences=[
                    make_gprmc(tmpl.lat, tmpl.lon, tmpl.speed_knots, tmpl.bearing, now),
                    make_gpgga(tmpl.lat, tmpl.lon, tmpl.alt, ts=now),
                ],
            )
            for sink in sinks:
                try:
                    sink(frame)
                except Exception as e:
                    logger.warning("Sink error: %s", e)

            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with self._lock:
                    if self._state != State.MANUAL:
                        logger.info("Manual emit loop stopped")
                        return
                time.sleep(min(0.05, remaining))

        logger.info("Manual emit loop stopped")

    def _run(self):
        logger.info("Playback engine started, %d frames", len(self._frames))
        while True:
            with self._cond:
                # Wait while paused
                while self._state == State.PAUSED:
                    self._cond.wait()

                if self._state == State.STOPPED:
                    break

                index = self._index
                speed = self._speed

            if index >= len(self._frames):
                with self._cond:
                    if self._loop:
                        self._index = 0
                        logger.info("Loop: restarting from frame 0")
                        continue
                    else:
                        self._state = State.STOPPED
                        logger.info("Playback complete")
                        break

            frame = self._frames[index]
            next_index = index + 1

            # Compute delay using original timestamps (before any live-time override)
            delay_s = 0.0
            if next_index < len(self._frames):
                next_frame = self._frames[next_index]
                delta = (next_frame.ts - frame.ts).total_seconds()
                delay_s = max(0.0, delta) / speed

            if self._live_time:
                from .nmea import make_gprmc, make_gpgga
                now = datetime.datetime.utcnow()
                frame = GPSFrame(
                    ts=now,
                    lat=frame.lat,
                    lon=frame.lon,
                    alt=frame.alt,
                    speed_knots=frame.speed_knots,
                    bearing=frame.bearing,
                    sentences=[
                        make_gprmc(frame.lat, frame.lon, frame.speed_knots, frame.bearing, now),
                        make_gpgga(frame.lat, frame.lon, frame.alt, ts=now),
                    ],
                )

            # Emit to sinks
            sinks_snapshot = []
            with self._lock:
                sinks_snapshot = list(self._sinks)

            for sink in sinks_snapshot:
                try:
                    sink(frame)
                except Exception as e:
                    logger.warning("Sink error: %s", e)

            with self._lock:
                self._index = next_index
                self._elapsed_s += delay_s

            # Sleep in small increments to remain responsive to pause/stop/seek
            if delay_s > 0:
                deadline = time.monotonic() + delay_s
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    with self._cond:
                        if self._state != State.PLAYING:
                            break
                        if self._index != next_index:  # seek happened
                            break
                    time.sleep(min(0.05, remaining))

        logger.info("Playback engine stopped")

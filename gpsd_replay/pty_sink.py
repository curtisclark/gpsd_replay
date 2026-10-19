"""
pty_sink.py - PTY (pseudo-terminal) sink for raw NMEA output.

Creates a PTY pair. The slave side is symlinked to a predictable path
(e.g. /tmp/gpsd-replay-pty) so that gpsd or other serial consumers can
connect to it directly.

Usage:
    sink = PTYSink(symlink="/tmp/gps0")
    sink.start()
    print(sink.slave_path)   # e.g. /dev/pts/4
    sink(frame)              # write NMEA sentences
    sink.stop()
"""

import fcntl
import os
import pty
import tty
import logging
import threading
from typing import Optional

from .sources import GPSFrame

logger = logging.getLogger(__name__)


class PTYSink:
    """
    Writes raw NMEA sentences to a PTY slave device.
    The master side is kept open internally; the slave path is exposed
    for use as a serial GPS device.
    """

    def __init__(self, symlink: Optional[str] = "/tmp/gpsd-replay-pty"):
        self.symlink = symlink
        self._master_fd: Optional[int] = None
        self._slave_fd: Optional[int] = None
        self._slave_path: Optional[str] = None
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        master_fd, slave_fd = pty.openpty()
        # Set raw mode so we don't add extra line processing
        try:
            tty.setraw(slave_fd)
        except Exception:
            pass

        # Set master non-blocking so writes drop silently when no reader
        fcntl.fcntl(master_fd, fcntl.F_SETFL, os.O_NONBLOCK)

        self._master_fd = master_fd
        self._slave_fd = slave_fd
        self._slave_path = os.ttyname(slave_fd)
        try:
            os.chmod(self._slave_path, 0o666)
        except OSError as e:
            logger.warning("Could not chmod PTY slave %s: %s", self._slave_path, e)
        self._running = True

        if self.symlink:
            try:
                if os.path.exists(self.symlink) or os.path.islink(self.symlink):
                    os.unlink(self.symlink)
                os.symlink(self._slave_path, self.symlink)
                logger.info("PTY symlink: %s → %s", self.symlink, self._slave_path)
            except PermissionError:
                logger.warning("Could not create symlink %s (permission denied)", self.symlink)

        logger.info("PTY device ready: %s", self._slave_path)

    def stop(self):
        self._running = False
        for fd in (self._master_fd, self._slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
        self._master_fd = None
        self._slave_fd = None

        if self.symlink:
            try:
                if os.path.islink(self.symlink):
                    os.unlink(self.symlink)
            except Exception:
                pass

    @property
    def slave_path(self) -> Optional[str]:
        return self._slave_path

    # ------------------------------------------------------------------
    # Sink interface (called by engine)
    # ------------------------------------------------------------------

    def __call__(self, frame: GPSFrame):
        """Write NMEA sentences for this frame to the PTY master."""
        if not self._running or self._master_fd is None:
            return
        data = "".join(frame.sentences).encode("ascii", errors="replace")
        with self._lock:
            try:
                os.write(self._master_fd, data)
            except OSError as e:
                logger.debug("PTY write error: %s", e)

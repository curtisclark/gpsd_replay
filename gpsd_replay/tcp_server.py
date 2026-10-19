"""
tcp_server.py - gpsd-protocol TCP server sink.

Listens on port 2947 (or configurable), speaks the gpsd JSON protocol.
Clients connect, send ?WATCH={"enable":true,"json":true}, and receive
TPV + SKY messages for each GPS fix.

gpsmon additionally requests nmea:true in the WATCH command, which causes
raw NMEA sentences to be streamed alongside the JSON messages.
"""

import logging
import socket
import threading
from typing import Optional

from .sources import GPSFrame
from .gpsd_proto import VERSION_MSG, WATCH_RESPONSE, DEVICES_MSG, parse_watch_cmd, tpv, sky
from .nmea import parse_gpgsv

logger = logging.getLogger(__name__)

GPSD_PORT = 2947


class TCPServerSink:
    """
    A gpsd JSON TCP server that accepts multiple clients.
    Implements enough of the gpsd protocol for standard clients
    (gpspipe, cgps, gpsmon, Python gps module, etc.)
    """

    def __init__(self, host: str = "0.0.0.0", port: int = GPSD_PORT):
        self.host = host
        self.port = port
        # Each entry: (conn, addr, flags) where flags is a mutable dict
        self._clients: list = []
        self._clients_lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(10)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="gpsd-tcp-accept")
        self._thread.start()
        logger.info("gpsd TCP server listening on %s:%d", self.host, self.port)

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        with self._clients_lock:
            for conn, _addr, _flags in self._clients:
                try:
                    conn.close()
                except Exception:
                    pass
            self._clients.clear()

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._server.accept()
                logger.info("Client connected: %s", addr)
                t = threading.Thread(
                    target=self._client_handler,
                    args=(conn, addr),
                    daemon=True,
                    name=f"gpsd-client-{addr[0]}:{addr[1]}",
                )
                t.start()
            except Exception as e:
                if self._running:
                    logger.warning("Accept error: %s", e)

    def _client_handler(self, conn: socket.socket, addr):
        """Handle one gpsd client connection."""
        flags = {"nmea": False}
        buf = b""

        try:
            conn.sendall(VERSION_MSG.encode())
            conn.settimeout(1.0)
            with self._clients_lock:
                self._clients.append((conn, addr, flags))

            while True:
                try:
                    data = conn.recv(256)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf or b";" in buf:
                        for sep in (b"\n", b";"):
                            if sep in buf:
                                line, buf = buf.split(sep, 1)
                                self._handle_cmd(conn, line.decode(errors="replace"), addr, flags)
                                break
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            logger.info("Client disconnected: %s", addr)
            with self._clients_lock:
                self._clients = [(c, a, f) for c, a, f in self._clients if c is not conn]
            try:
                conn.close()
            except Exception:
                pass

    def _handle_cmd(self, conn: socket.socket, cmd: str, addr, flags: dict):
        cmd = cmd.strip()
        if not cmd:
            return
        logger.debug("CMD from %s: %r", addr, cmd)
        if cmd.startswith("?WATCH"):
            params = parse_watch_cmd(cmd) or {}
            flags["nmea"] = bool(params.get("nmea", False))
            conn.sendall(WATCH_RESPONSE.encode())
            conn.sendall(DEVICES_MSG.encode())
        elif cmd.startswith("?VERSION"):
            conn.sendall(VERSION_MSG.encode())
        elif cmd.startswith("?DEVICES"):
            conn.sendall(DEVICES_MSG.encode())

    # ------------------------------------------------------------------
    # Sink interface (called by engine)
    # ------------------------------------------------------------------

    def __call__(self, frame: GPSFrame):
        """Emit a GPS frame to all connected clients as TPV + SKY (+ NMEA if requested)."""
        speed_mps = frame.speed_knots * 0.514444

        satellites = []
        for sentence in frame.sentences:
            satellites.extend(parse_gpgsv(sentence))

        json_payload = (
            tpv(
                lat=frame.lat,
                lon=frame.lon,
                alt=frame.alt,
                speed=speed_mps,
                track=frame.bearing,
                ts=frame.ts,
            )
            + sky(satellites=satellites or None)
        ).encode()

        nmea_payload = "".join(frame.sentences).encode() if frame.sentences else b""

        dead: list = []
        with self._clients_lock:
            clients_copy = list(self._clients)

        for conn, addr, flags in clients_copy:
            try:
                conn.sendall(json_payload)
                if flags.get("nmea") and nmea_payload:
                    conn.sendall(nmea_payload)
            except Exception as e:
                logger.debug("Write error to %s: %s", addr, e)
                dead.append(conn)

        if dead:
            with self._clients_lock:
                self._clients = [(c, a, f) for c, a, f in self._clients if c not in dead]

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)


class NMEARawServerSink:
    """
    A plain TCP server that streams raw NMEA sentences to connected clients.
    No gpsd protocol — clients receive exactly what a serial GPS would send.
    Intended for use with socat to feed a PTY or any NMEA consumer.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 2948):
        self.host = host
        self.port = port
        self._clients: list = []
        self._clients_lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(10)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="nmea-raw-accept")
        self._thread.start()
        logger.info("NMEA raw TCP server listening on %s:%d", self.host, self.port)

    def stop(self):
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        with self._clients_lock:
            for conn in self._clients:
                try:
                    conn.close()
                except Exception:
                    pass
            self._clients.clear()

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._server.accept()
                logger.info("NMEA client connected: %s", addr)
                with self._clients_lock:
                    self._clients.append(conn)
            except Exception as e:
                if self._running:
                    logger.warning("NMEA accept error: %s", e)

    def __call__(self, frame: GPSFrame):
        if not frame.sentences:
            return
        data = "".join(frame.sentences).encode("ascii", errors="replace")
        dead = []
        with self._clients_lock:
            clients_copy = list(self._clients)
        for conn in clients_copy:
            try:
                conn.sendall(data)
            except Exception:
                dead.append(conn)
        if dead:
            with self._clients_lock:
                self._clients = [c for c in self._clients if c not in dead]

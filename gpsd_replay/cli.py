"""
cli.py - Command-line interface for gpsd-replay.

Examples
--------
# Replay an NMEA log at 2x speed, loop, with web UI:
  gpsd-replay nmea track.nmea --speed 2.0 --loop --web-ui

# Replay a GPX route at 15 m/s synthetic speed:
  gpsd-replay gpx route.gpx --gpx-speed 15 --loop

# Use both TCP and PTY sinks:
  gpsd-replay nmea track.nmea --tcp --pty --pty-path /tmp/gps0

# Disable TCP, use PTY only, no web UI:
  gpsd-replay gpx route.gpx --no-tcp --pty
"""

import argparse
import logging
import os
import signal
import sys
import time

logger = logging.getLogger("gpsd_replay")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpsd-replay",
        description="Replay NMEA logs or GPX routes as live GPS streams.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="source_type", required=True)

    # --- nmea sub-command ---
    nmea_p = sub.add_parser("nmea", help="Replay a raw NMEA log file")
    nmea_p.add_argument("file", help="Path to NMEA log file")

    # --- gpx sub-command ---
    gpx_p = sub.add_parser("gpx", help="Replay a GPX route as synthetic NMEA")
    gpx_p.add_argument("file", help="Path to GPX file")
    gpx_p.add_argument(
        "--gpx-speed", type=float, default=10.0, metavar="M_PER_S",
        help="Constant travel speed in m/s for synthetic route (default: 10)",
    )
    gpx_p.add_argument(
        "--gpx-interval", type=float, default=1.0, metavar="SECONDS",
        help="Fix interval in seconds for synthetic route (default: 1.0)",
    )

    # Shared options (added to both sub-parsers)
    for p in (nmea_p, gpx_p):
        p.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier (default: 1.0)")
        p.add_argument("--loop", action="store_true", help="Loop playback when end is reached")
        p.add_argument("--live-time", action="store_true", help="Override timestamps in playback with current wall-clock time")

        # Sinks
        tcp_grp = p.add_mutually_exclusive_group()
        tcp_grp.add_argument("--tcp", dest="tcp", action="store_true", default=True, help="Enable gpsd TCP server (default)")
        tcp_grp.add_argument("--no-tcp", dest="tcp", action="store_false", help="Disable TCP server")

        p.add_argument("--tcp-port", type=int, default=2947, help="TCP port (default: 2947)")
        p.add_argument("--tcp-host", default="0.0.0.0", help="TCP bind address (default: 0.0.0.0)")
        p.add_argument("--nmea-port", type=int, default=None, metavar="PORT",
                       help="Enable raw NMEA TCP server on this port (e.g. 2948)")

        pty_grp = p.add_mutually_exclusive_group()
        pty_grp.add_argument("--pty", dest="pty", action="store_true", default=False, help="Enable PTY sink")
        pty_grp.add_argument("--no-pty", dest="pty", action="store_false")

        p.add_argument("--pty-path", default="/tmp/gpsd-replay-pty", help="PTY symlink path (default: /tmp/gpsd-replay-pty)")

        # Web UI
        ui_grp = p.add_mutually_exclusive_group()
        ui_grp.add_argument("--web-ui", dest="web_ui", action="store_true", default=True, help="Enable web control UI (default)")
        ui_grp.add_argument("--no-web-ui", dest="web_ui", action="store_false")

        p.add_argument("--ui-port", type=int, default=8765, help="Web UI port (default: 8765)")
        p.add_argument("--file-dir", default=None, metavar="DIR",
                       help="Directory of replay files to expose in the web UI file picker")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- Load source ---
    from .sources import NMEALogSource, GPXRouteSource

    if args.source_type == "nmea":
        logger.info("Loading NMEA log: %s", args.file)
        source = NMEALogSource(args.file)
        frames = source.frames()
    else:
        logger.info("Loading GPX route: %s (%.1f m/s)", args.file, args.gpx_speed)
        source = GPXRouteSource(
            path=args.file,
            speed_mps=args.gpx_speed,
            interval_s=args.gpx_interval,
        )
        frames = source.frames()

    if not frames:
        logger.error("No GPS frames loaded. Exiting.")
        sys.exit(1)

    logger.info("Loaded %d frames", len(frames))

    # --- Build engine ---
    from .engine import PlaybackEngine
    engine = PlaybackEngine(frames, speed=args.speed, loop=args.loop, live_time=args.live_time)

    # --- Attach sinks ---
    sinks = []

    if args.tcp:
        from .tcp_server import TCPServerSink
        tcp = TCPServerSink(host=args.tcp_host, port=args.tcp_port)
        tcp.start()
        engine.add_sink(tcp)
        sinks.append(tcp)
        logger.info("gpsd TCP server: %s:%d", args.tcp_host, args.tcp_port)

    if args.nmea_port:
        from .tcp_server import NMEARawServerSink
        nmea_raw = NMEARawServerSink(host=args.tcp_host, port=args.nmea_port)
        nmea_raw.start()
        engine.add_sink(nmea_raw)
        sinks.append(nmea_raw)
        logger.info("NMEA raw TCP server: %s:%d", args.tcp_host, args.nmea_port)

    if args.pty:
        from .pty_sink import PTYSink
        import sys as _sys
        if _sys.platform == "win32":
            logger.warning("PTY not supported on Windows, skipping")
        else:
            pty_sink = PTYSink(symlink=args.pty_path)
            pty_sink.start()
            engine.add_sink(pty_sink)
            sinks.append(pty_sink)
            logger.info("PTY device: %s (→ %s)", pty_sink.slave_path, args.pty_path)

    # --- Web UI ---
    if args.web_ui:
        from .web_ui import WebUIServer

        def _make_loader():
            _src_type = args.source_type
            _gpx_speed = getattr(args, "gpx_speed", 10.0)
            _gpx_interval = getattr(args, "gpx_interval", 1.0)
            def _loader(path):
                if _src_type == "nmea":
                    return NMEALogSource(path).frames()
                return GPXRouteSource(
                    path=path, speed_mps=_gpx_speed, interval_s=_gpx_interval
                ).frames()
            return _loader

        _file_dir = getattr(args, "file_dir", None)
        if _file_dir:
            import os as _os
            if _os.path.isdir(_file_dir):
                _files = [f for f in _os.listdir(_file_dir) if _os.path.isfile(_os.path.join(_file_dir, f))]
                logger.info("File dir: %s (%d files: %s)", _file_dir, len(_files), ", ".join(sorted(_files)))
            else:
                logger.warning("File dir does not exist or is not a directory: %s", _file_dir)
        else:
            logger.info("File dir: not set (no file picker in UI)")

        ui = WebUIServer(
            port=args.ui_port,
            file_dir=_file_dir,
            loader=_make_loader() if _file_dir else None,
            config={
                "source_type": args.source_type,
                "file": os.path.basename(args.file),
                "tcp": args.tcp,
                "tcp_host": args.tcp_host,
                "tcp_port": args.tcp_port,
                "nmea_port": getattr(args, "nmea_port", None),
                "pty": args.pty,
                "pty_path": args.pty_path,
                "ui_port": args.ui_port,
                "file_dir": _file_dir,
                "speed": args.speed,
                "loop": args.loop,
                "live_time": args.live_time,
            },
        )
        ui.attach_engine(engine)
        ui._current_file = os.path.basename(args.file)
        ui.start()
        logger.info("Web UI: http://localhost:%d", args.ui_port)
    else:
        ui = None

    # --- Signal handling ---
    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        engine.stop()
        for s in sinks:
            try:
                s.stop()
            except Exception:
                pass
        if ui:
            ui.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # --- Start playback ---
    engine.play()
    logger.info(
        "Playback started at %.1f× speed%s%s",
        args.speed,
        " (loop)" if args.loop else "",
        " (live-time)" if args.live_time else "",
    )

    # Keep main thread alive
    try:
        while True:
            time.sleep(0.5)
            st = engine.status
            if st.state.name == "STOPPED" and not args.loop:
                logger.info("Playback finished.")
                break
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        for s in sinks:
            try:
                s.stop()
            except Exception:
                pass
        if ui:
            ui.stop()


if __name__ == "__main__":
    main()

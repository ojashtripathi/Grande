"""Entry point: ``grande`` / ``python -m grande``.

Picks a free loopback port, starts a threaded WSGI server and opens the browser.
Binding to 127.0.0.1 keeps Windows Firewall quiet and makes the app unreachable
from the rest of the network.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__


def _free_port(preferred: int | None = None) -> int:
    """Bind port 0 and let the OS pick, so there is no scan/TOCTOU race."""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open_browser_when_ready(url: str, port: int, timeout: float = 20.0) -> None:
    """Poll the port rather than sleeping a fixed amount, then open the browser once."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grande",
        description=f"{APP_NAME} — browse, pivot and export tables too big for Excel. Runs entirely offline.",
    )
    parser.add_argument("file", nargs="?", help="Optional data file to open on startup.")
    parser.add_argument("--port", type=int, default=None, help="Port to serve on (default: any free port).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Directory for the dataset cache (default: a per-user cache directory).",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    # Imported late so --help and --version stay instant even on a cold DuckDB import.
    from .web.server import create_app

    port = _free_port(args.port)
    # A per-run token. The UI reads it from the page; cross-origin pages that guess
    # the port still cannot drive the API. See web/server.py.
    token = secrets.token_urlsafe(24)

    try:
        app = create_app(workspace=args.workspace, token=token, initial_file=args.file)
    except Exception as exc:
        # Launched by double-clicking a .bat, a traceback is noise to the person
        # reading it. Say what went wrong in one line.
        print(f"\n  {APP_NAME} could not start.\n", file=sys.stderr)
        print(f"  {exc}\n", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/?t={token}"

    if not args.no_browser and os.environ.get("GRANDE_NO_BROWSER") != "1":
        threading.Thread(target=_open_browser_when_ready, args=(url, port), daemon=True).start()

    # Flushed explicitly: launched from a .bat or a double-click, stdout is a
    # pipe and Python buffers it, so without this the window stays blank and
    # looks stuck while the server is in fact already running.
    print(f"\n  {APP_NAME} {__version__}", flush=True)
    print(f"  Open:  {url}", flush=True)
    print("  Stop:  press Ctrl+C in this window\n", flush=True)

    from waitress import serve

    try:
        serve(
            app,
            host="127.0.0.1",
            port=port,
            threads=8,
            # Long-running exports stream progress; do not let the server time them out.
            channel_timeout=3600,
            ident=APP_NAME,
        )
    except KeyboardInterrupt:
        print("\n  Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

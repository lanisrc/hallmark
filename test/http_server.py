"""Serve a directory over loopback HTTP, with scripted faults, for tests.

Unlike ``mock_server.MockServer``, this is a real server: requests,
urllib3 and Hallmark's HTTP backend exercise their actual network code,
including status handling, short reads, resets and timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import struct
import threading
import time
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True)
class Status:
    """Respond with an HTTP status and an empty body."""
    code: int
    headers: tuple = ()


@dataclass(frozen=True)
class ResetAfter:
    """Announce the full length, send ``nbytes``, then reset the connection."""
    nbytes: int


@dataclass(frozen=True)
class ShortBody:
    """Announce the full length, send ``nbytes``, then close cleanly."""
    nbytes: int


@dataclass(frozen=True)
class NoLengthTruncate:
    """Omit Content-Length, send ``nbytes``, then close cleanly."""
    nbytes: int


@dataclass(frozen=True)
class BitFlip:
    """Serve the file with one byte inverted at ``offset``."""
    offset: int = 0


@dataclass(frozen=True)
class Stall:
    """Wait before responding, or after the headers when ``after_headers``."""
    seconds: float
    after_headers: bool = False


@dataclass(frozen=True)
class SlowDrip:
    """Send the body in ``chunk``-byte pieces ``delay`` seconds apart."""
    chunk: int = 1024
    delay: float = 0.05


@dataclass(frozen=True)
class Redirect:
    """Redirect to ``location`` (an absolute or server-relative URL)."""
    location: str
    code: int = 302


@dataclass(frozen=True)
class NotAListing:
    """Serve a directory as an ordinary page without listing markers."""


@dataclass
class _Rule:
    pattern: str
    fault: object
    method: str
    remaining: int | None


@dataclass
class FaultPlan:
    """Faults to apply to matching request paths, consumed per request.

    Paths are relative to the served root, without a leading slash;
    directories end with ``/``. Rules are keyed by path rather than by a
    global request count so concurrent download workers stay deterministic.
    """
    _rules: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, pattern, fault, *, times=1, method="GET"):
        """Apply ``fault`` to the next ``times`` matches (None: every match)."""
        with self._lock:
            self._rules.append(_Rule(pattern, fault, method, times))

    def clear(self):
        with self._lock:
            self._rules.clear()

    def take(self, method, path):
        with self._lock:
            for rule in self._rules:
                if (rule.method in {method, "*"} and rule.remaining != 0
                        and fnmatch.fnmatchcase(path, rule.pattern)):
                    if rule.remaining is not None:
                        rule.remaining -= 1
                    return rule.fault
        return None


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _listing(style, request_path, children):
    """Render ``children`` as (name, is_directory, size) in a server's style."""
    title = html.escape(request_path)
    if style == "python":
        items = "".join(
            f'<li><a href="{quote(name)}{"/" if is_dir else ""}">'
            f'{html.escape(name)}{"/" if is_dir else ""}</a></li>\n'
            for name, is_dir, _ in children)
        return (f'<!DOCTYPE HTML>\n<html lang="en"><head><meta charset="utf-8">'
                f"<title>Directory listing for {title}</title></head>\n<body>\n"
                f"<h1>Directory listing for {title}</h1>\n<hr>\n<ul>\n{items}"
                "</ul>\n<hr>\n</body>\n</html>\n")
    if style == "apache":
        stamp = time.gmtime(1759600000)
        date = (f"{stamp.tm_mday:02d}-{_MONTHS[stamp.tm_mon - 1]}-{stamp.tm_year} "
                f"{stamp.tm_hour:02d}:{stamp.tm_min:02d}")
        rows = "".join(
            f'<a href="{quote(name)}{"/" if is_dir else ""}">'
            f'{html.escape(name)}{"/" if is_dir else ""}</a>'
            f'    {date}  {"-" if is_dir else size}\n'
            for name, is_dir, size in children)
        return (f"<html><head><title>Index of {title}</title></head><body>\n"
                f"<h1>Index of {title}</h1><pre>"
                '<a href="?C=N;O=D">Name</a>  <a href="?C=M;O=A">Last modified</a>'
                '  <a href="?C=S;O=A">Size</a>\n<hr>'
                '<a href="../">Parent Directory</a>\n'
                f"{rows}<hr></pre></body></html>\n")
    if style == "cyverse":
        rows = "".join(
            f'<tr class="object {"collection" if is_dir else "data-object"}">'
            f'<td class="name"><a href="{quote(name)}{"/" if is_dir else ""}">'
            f"{html.escape(name)}</a></td>"
            f'<td class="size">{"" if is_dir else size}</td></tr>'
            for name, is_dir, size in children)
        return f"<html><body><table><tbody>{rows}</tbody></table></body></html>"
    raise ValueError(f"Unknown listing style {style!r}")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Idle keep-alive connections end so the server can shut down promptly.
    timeout = 5

    def log_message(self, format, *args):
        pass

    def do_HEAD(self):
        self._respond(head=True)

    def do_GET(self):
        self._respond(head=False)

    def _respond(self, head):
        server = self.server.owner
        request_path = unquote(urlsplit(self.path).path, errors="strict")
        relative = request_path.lstrip("/")
        with server._lock:
            server.requests.append((self.command, relative))
        fault = server.faults.take(self.command, relative)
        if isinstance(fault, Status):
            return self._send_empty(fault.code, dict(fault.headers))
        if isinstance(fault, Redirect):
            return self._send_empty(fault.code, {"Location": fault.location})
        if isinstance(fault, Stall) and not fault.after_headers:
            server.release.wait(fault.seconds)

        target = (server.root / relative).resolve()
        if server.root.resolve() not in (target, *target.parents):
            return self._send_empty(404)
        if target.is_dir():
            if not request_path.endswith("/"):
                return self._send_empty(301, {"Location": request_path + "/"})
            if isinstance(fault, NotAListing):
                body = b"<html><body><p>Welcome</p></body></html>"
            else:
                children = sorted(
                    (child.name, child.is_dir(),
                     None if child.is_dir() else child.stat().st_size)
                    for child in target.iterdir())
                body = _listing(server.listing_style, request_path,
                                children).encode("utf-8")
            return self._send_body(body, "text/html; charset=utf-8", head, fault)
        if not target.is_file():
            return self._send_empty(404)
        body = target.read_bytes()
        if isinstance(fault, BitFlip) and body:
            index = min(fault.offset, len(body) - 1)
            body = body[:index] + bytes([body[index] ^ 0xFF]) + body[index + 1:]
        return self._send_body(body, "application/octet-stream", head, fault)

    def _send_empty(self, code, headers=None):
        self.send_response(code)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_body(self, body, content_type, head, fault):
        server = self.server.owner
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if isinstance(fault, NoLengthTruncate):
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if head:
            return
        if isinstance(fault, Stall) and fault.after_headers:
            self.wfile.flush()
            server.release.wait(fault.seconds)
        if isinstance(fault, (ResetAfter, ShortBody, NoLengthTruncate)):
            self.wfile.write(body[:fault.nbytes])
            self.wfile.flush()
            self.close_connection = True
            if isinstance(fault, ResetAfter):
                # A zero linger timeout makes close() send RST instead of FIN.
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            return
        if isinstance(fault, SlowDrip):
            for start in range(0, len(body), fault.chunk):
                self.wfile.write(body[start:start + fault.chunk])
                self.wfile.flush()
                if server.release.wait(fault.delay):
                    break
            return
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Clients that abandon faulted transfers are expected; stay quiet.
        pass


class DatasetHTTPServer:
    """A loopback HTTP server for a directory, with faults and a request log.

    Args:
        root: Directory to serve.
        listing_style: ``"python"`` (http.server), ``"apache"`` (with sizes)
            or ``"cyverse"`` (CyVerse WebDAV table rows with sizes).
    """

    def __init__(self, root, listing_style="python"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.listing_style = listing_style
        self.faults = FaultPlan()
        self.requests = []
        # Set at shutdown so stalled and dripping responses end promptly.
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def port(self):
        return self._httpd.server_address[1]

    def url(self, path=""):
        """Return the URL for ``path`` relative to the served root."""
        return f"http://127.0.0.1:{self.port}/" + quote(path, safe="/")

    def gets(self, prefix=""):
        """Paths requested with GET, in order, optionally under ``prefix``."""
        with self._lock:
            return [path for method, path in self.requests
                    if method == "GET" and path.startswith(prefix)]

    def payload_gets(self):
        """GET requests for files rather than directory listings."""
        return [path for path in self.gets()
                if path and not path.endswith("/")
                and (self.root / path).is_file()]

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self.release.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


def write_file(root, relative, data):
    """Write ``data`` (bytes or text) at ``root/relative``, creating folders."""
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    os.utime(path, (1759600000, 1759600000))
    return path

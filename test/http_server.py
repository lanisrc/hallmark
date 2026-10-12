"""A small loopback HTTP server that serves files, indexes and redirects."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


class LocalServer:
    """
    Serve ``files`` beneath ``/data/`` on 127.0.0.1, recording each request.

    Attributes:
        files (dict[str, bytes]): Paths beneath ``/data/`` and their bytes.
        redirects (dict[str, str]): Paths answered with a 302 to a URL.
        authorization (str, optional): Required ``Authorization`` header.
        requests (list[tuple[str, str, str | None]]): Method, path and the
            ``Authorization`` header of every request received.
        handlers (dict[str, callable]): Extra handlers by HTTP method, called
            as ``handler(request_handler, path)``.
    """

    def __init__(self):
        self.files = {}
        self.redirects = {}
        self.authorization = None
        self.requests = []
        self.handlers = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, method):
                path = unquote(urlsplit(self.path).path)
                server.requests.append(
                    (method, path, self.headers.get("Authorization")))
                if (server.authorization is not None
                        and self.headers.get("Authorization") != server.authorization):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="data"')
                    self.end_headers()
                    return
                if path in server.redirects:
                    self.send_response(302)
                    self.send_header("Location", server.redirects[path])
                    self.end_headers()
                    return
                if method in server.handlers:
                    server.handlers[method](self, path)
                    return
                relative = path[len("/data/"):] if path.startswith("/data/") else None
                if relative is None:
                    self.send_error(404)
                    return
                if relative in server.files:
                    self._send(server.files[relative])
                    return
                names = sorted(name[len(relative):].split("/")[0]
                               + ("/" if "/" in name[len(relative):] else "")
                               for name in server.files
                               if name.startswith(relative)
                               and (relative == "" or relative.endswith("/")))
                if not names:
                    self.send_error(404)
                    return
                links = "".join(f'<a href="{name}">{name}</a>'
                                for name in dict.fromkeys(names))
                self._send(f"<h1>Index of {path}</h1>{links}".encode(), "text/html")

            def _send(self, body, content_type="application/octet-stream"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._serve("GET")

            def do_PROPFIND(self):
                self._serve("PROPFIND")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/data/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

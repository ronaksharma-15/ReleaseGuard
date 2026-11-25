"""Two independent checkout servers behind a loopback-only weighted HTTP router."""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from urllib.parse import urlsplit

from checkout import start_checkout


def forward_request(url, method, path, body=None, workload=None):
    target = urlsplit(url)
    connection = HTTPConnection(target.hostname, target.port, timeout=4)
    headers = {"Content-Type": "application/json"}
    if workload:
        headers["X-ReleaseGuard-Workload"] = workload
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader("X-Release-Version"), response.read(65536)
    finally:
        connection.close()


def address(server):
    return f"http://127.0.0.1:{server.server_port}"


def close_server(server):
    if server:
        server.shutdown()
        server.server_close()


class LocalRuntime:
    def __init__(self):
        self._lock = Lock()
        self._stable = self._candidate = self._router = None
        self._percent = self._position = 0

    def initialize(self, stable):
        self._stable = start_checkout("127.0.0.1", 0, stable["version"], stable["scenario"])
        runtime = self

        class Router(BaseHTTPRequestHandler):
            def forward(self):
                if (self.command, self.path) not in (("GET", "/health"), ("POST", "/quote")):
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 <= length <= 65536:
                        self.send_error(413)
                        return
                    body = self.rfile.read(length) if length else None
                    status, version, data = forward_request(
                        runtime.select_url(), self.command, self.path, body,
                        self.headers.get("X-ReleaseGuard-Workload"),
                    )
                except (OSError, ValueError):
                    status, version, data = 502, None, b'{"error":"Checkout unavailable"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if version:
                    self.send_header("X-Release-Version", version)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = forward

            def log_message(self, *_):
                pass

        class WeightedServer(ThreadingHTTPServer):
            request_queue_size = 64

        self._router = WeightedServer(("127.0.0.1", 0), Router)
        Thread(target=self._router.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def stable_url(self):
        return address(self._stable)

    @property
    def candidate_url(self):
        return address(self._candidate)

    @property
    def router_url(self):
        return address(self._router)

    def select_url(self):
        with self._lock:
            self._position += self._percent
            if self._position >= 100:
                self._position -= 100
                return self.candidate_url
            return self.stable_url

    def route(self, percent):
        if percent not in (0, 10, 50, 100):
            raise ValueError("Unsupported traffic percentage")
        with self._lock:
            if percent and self._candidate is None:
                raise RuntimeError("Candidate is unavailable")
            self._percent, self._position = percent, 0

    def prepare(self, release):
        close_server(self._candidate)
        self._candidate = start_checkout(
            "127.0.0.1", 0, release["candidate_version"], release["scenario"]
        )

    def promote(self):
        with self._lock:
            if self._percent != 100:
                raise RuntimeError("Candidate has not received full traffic")
            self._stable, self._candidate = self._candidate, self._stable
            self._percent, self._position = 0, 0

    def close(self):
        for server in (self._router, self._candidate, self._stable):
            close_server(server)

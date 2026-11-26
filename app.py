"""ReleaseGuard dashboard and API. Run 'python app.py' for the offline demo."""

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import urlsplit

from controller import APIError, BASE, Controller
from local_runtime import forward_request


def json_reply(status, data):
    return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, (
        json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
    )


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def authorize(controller, headers, client):
    origin, host = headers.get("origin"), headers.get("host", "")
    if origin and (urlsplit(origin).scheme not in ("http", "https") or urlsplit(origin).netloc != host):
        raise APIError(403, "Open the dashboard on this controller to launch a release")
    if controller.mode == "local":
        if not loopback(client) or not loopback(urlsplit("//" + host).hostname or ""):
            raise APIError(403, "The demo launcher accepts loopback connections only")
        if headers.get("x-releaseguard-ui") != "1":
            raise APIError(403, "Supply X-ReleaseGuard-UI: 1 from the local dashboard")
    else:
        expected = os.getenv("RG_DEPLOY_TOKEN", "")
        supplied = headers.get("authorization", "")
        if not expected or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
            raise APIError(401, "A deployment token is required")


def dispatch(controller, method, target, raw_headers, body, client, *, public_demo=False):
    headers = {k.lower(): v for k, v in raw_headers.items()}
    path = urlsplit(target).path
    try:
        if public_demo and method != "GET":
            raise APIError(403, "The public demo is read-only")
        if method == "GET" and path == "/health":
            return json_reply(200, {"ok": True, "mode": controller.mode})
        if method == "GET" and path == "/api/state":
            state = controller.state()
            if public_demo:
                state["public_demo"] = True
            return json_reply(200, state)
        if method == "GET" and (m := re.fullmatch(r"/api/releases/([a-z0-9]+)", path)):
            record = controller.store.get(m[1])
            if record is None:
                raise APIError(404, "Release not found")
            return json_reply(200, record)
        if method == "POST" and path == "/quote":
            status, version, data = forward_request(controller.runtime.router_url, "POST", "/quote", body)
            response_headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
            if version:
                response_headers["X-Release-Version"] = version
            return status, response_headers, data
        if method == "POST" and path.startswith("/api/"):
            authorize(controller, headers, client)
            if path == "/api/releases":
                if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                    raise APIError(415, "Send a JSON object with Content-Type application/json")
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, "Invalid JSON")
                if not isinstance(data, dict):
                    raise APIError(400, "Expected a release object")
                release, created = controller.submit(data)
                return json_reply(202 if created else 200, release)
            if m := re.fullmatch(r"/api/releases/([a-z0-9]+)/recover", path):
                return json_reply(202, controller.retry_recovery(m[1]))
        if method == "GET" and not path.startswith("/api/"):
            root = (BASE / "web").resolve()
            file = (root / (path.lstrip("/") or "index.html")).resolve()
            if file.is_relative_to(root) and file.is_file():
                kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                return 200, {"Content-Type": kind, "Cache-Control": "no-cache"}, file.read_bytes()
        raise APIError(404, "Resource not found")
    except APIError as exc:
        return json_reply(exc.status, {"error": str(exc)})
    except (ConnectionError, TimeoutError, OSError):
        return json_reply(503, {"error": "Service unavailable. Check controller and checkout readiness."})
    except ValueError:
        return json_reply(400, {"error": "Malformed request"})


class Handler(BaseHTTPRequestHandler):
    def handle_api(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= length <= 65536:
            self.send_error(413)
            return
        status, headers, body = dispatch(self.server.controller, self.command, self.path, self.headers,
                                         self.rfile.read(length) if length else b"", self.client_address[0],
                                         public_demo=self.server.public_demo)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    do_GET = do_POST = handle_api

    def log_message(self, *_):
        pass


def prepare_public_demo(controller):
    """Record the three synthetic runs for visitors to replay."""
    for scenario, expected in (("wrong_total", "blocked"), ("slow", "recovered"), ("healthy", "promoted")):
        release, created = controller.submit({"scenario": scenario, "request_id": f"public-demo-{scenario}"})
        if created:
            controller.thread.join()
            release = controller.store.get(release["id"])
        if release["status"] != expected:
            raise RuntimeError(f"Public demo scenario {scenario} ended as {release['status']}, expected {expected}")


def main():
    parser = argparse.ArgumentParser(description="ReleaseGuard local dashboard")
    parser.add_argument("--port", type=int, default=int(os.getenv("RG_PORT", 8787)))
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    parser.add_argument("--public-demo", action="store_true", help="serve recorded synthetic runs read-only")
    args = parser.parse_args()
    host = os.getenv("RG_BIND", "127.0.0.1")
    with ThreadingHTTPServer((host, args.port), Handler) as server:
        server.public_demo = args.public_demo
        if args.public_demo:
            # Create a separate history so only synthetic public runs are exposed.
            from tempfile import TemporaryDirectory
            with TemporaryDirectory(prefix="releaseguard-public-") as directory:
                serve(server, directory, args.open)
        else:
            serve(server, args.data_dir, args.open)


def serve(server, data_dir, open_browser):
    server.controller = Controller(data_dir, mode="local" if server.public_demo else None)
    try:
        if server.public_demo:
            prepare_public_demo(server.controller)
        print(f"ReleaseGuard is ready at http://{server.server_address[0]}:{server.server_port}", flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{server.server_port}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.close()


if __name__ == "__main__":
    main()

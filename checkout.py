"""Stateless checkout service used by the ReleaseGuard demonstration."""

import json
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


SCENARIOS = {"healthy", "slow", "wrong_total"}
SLOW_DELAY_SECONDS = 0.42


def _number(value):
    if type(value) not in (int, float):
        raise ValueError("Prices and percentages must be numbers.")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Prices and percentages must be finite.")
    return number


def _rupees(paise):
    return paise // 100 if paise % 100 == 0 else paise / 100


def quote(payload, scenario="healthy"):
    """Calculate in paise and round the discount to the nearest paisa."""
    if not isinstance(payload, dict):
        raise ValueError("The request must contain a JSON object.")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("Supply between 1 and 50 items.")
    subtotal = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each item must contain unit_price and quantity.")
        price = _number(item.get("unit_price"))
        quantity = item.get("quantity")
        if not 0 <= price <= 1_000_000 or price * 100 != (price * 100).to_integral_value():
            raise ValueError("Use a non-negative unit price up to INR 1,000,000 with at most two decimal places.")
        if type(quantity) is not int or not 1 <= quantity <= 100:
            raise ValueError("Quantity must be an integer between 1 and 100.")
        subtotal += int(price * 100) * quantity

    percentage = _number(payload.get("discount_percent", 0))
    if not 0 <= percentage <= 100:
        raise ValueError("Discount percentage must be between 0 and 100.")
    discount = int((subtotal * percentage / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    total = subtotal - discount
    if scenario == "wrong_total" and percentage == 10:
        total *= 2
    return {"currency": "INR", "subtotal": _rupees(subtotal), "discount": _rupees(discount), "total": _rupees(total)}


class CheckoutHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, status, data):
        body = json.dumps(data, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Release-Version", self.server.version)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok", "version": self.server.version})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint."})

    def do_POST(self):
        if urlsplit(self.path).path != "/quote":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 16_384:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Quote exceeds 16 KiB."})
                return
            if size <= 0:
                raise ValueError("Supply a JSON request body.")
            payload = json.loads(self.rfile.read(size))
            result = quote(payload, self.server.scenario)
        except (ValueError, InvalidOperation, RecursionError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid quote. Check the items, unit prices, quantities and discount."})
            return
        if self.server.scenario == "slow" and self.headers.get("X-ReleaseGuard-Workload") == "synthetic":
            time.sleep(SLOW_DELAY_SECONDS)
        self._json(HTTPStatus.OK, result)


class CheckoutServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, version, scenario):
        if not isinstance(version, str) or not re.fullmatch(r"[!-~]{1,512}", version):
            raise ValueError("VERSION must be 1–512 printable ASCII characters without spaces.")
        if scenario not in SCENARIOS:
            raise ValueError("Unknown checkout scenario.")
        self.version = version
        self.scenario = scenario
        super().__init__(address, CheckoutHandler)


def start_checkout(host, port, version, scenario):
    server = CheckoutServer((host, port), version, scenario)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server


if __name__ == "__main__":
    with CheckoutServer(
        (os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8001"))),
        os.getenv("VERSION", "checkout-local"), os.getenv("SCENARIO", "healthy")
    ) as checkout_server:
        checkout_server.serve_forever()

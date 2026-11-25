import http.client
import json
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checkout import quote, start_checkout
from checks import measure_window, run_fixtures


class CheckoutChecksTest(unittest.TestCase):
    def setUp(self):
        self.servers = ExitStack()
        self.addCleanup(self.servers.close)
        self.stable = self.start("stable-v1")

    def start(self, version, scenario="healthy"):
        service = start_checkout("127.0.0.1", 0, version, scenario)
        self.servers.callback(service.server_close)
        self.servers.callback(service.shutdown)
        return f"http://127.0.0.1:{service.server_port}"

    def test_behavior_gate_and_business_regression(self):
        healthy = self.start("v2")
        good = run_fixtures(self.stable, healthy)
        self.assertEqual(good["passed"], 24)
        self.assertEqual(good["failed"], 0)
        self.assertGreater(good["rules_checked"], good["total"])
        bad = run_fixtures(self.stable, self.start("bad", "wrong_total"))
        mismatch = bad["failures"][0]
        self.assertEqual(mismatch["candidate"]["http_status"], 200)
        self.assertEqual(mismatch["expected"]["total"], 450)
        self.assertEqual(mismatch["actual"]["total"], 900)
        self.assertEqual(mismatch["side"], "candidate")
        self.assertIn("total equals 450", mismatch["rule"])
        self.assertEqual(bad["passed"] + bad["failed"], bad["total"])

    def test_recovery_requires_stable_version_and_behavior(self):
        wrong_route = run_fixtures(self.stable, self.start("v2"), critical=True, expected_version="stable-v1")
        self.assertEqual(wrong_route["failed"], 1)
        self.assertGreater(wrong_route["skipped"], 0)
        self.assertFalse(wrong_route["complete"])
        recovered = run_fixtures(self.stable, self.stable, critical=True, expected_version="stable-v1")
        self.assertEqual(recovered["passed"], 6)
        bad_baseline = run_fixtures(self.start("broken", "wrong_total"), self.stable, critical=True)
        self.assertEqual(bad_baseline["failures"][0]["side"], "stable")

    def test_slow_build_passes_behavior_then_breaches_routed_latency(self):
        slow = self.start("v2", "slow")
        gate = run_fixtures(self.stable, slow)
        self.assertEqual(gate["failed"], 0)
        window = measure_window(slow, self.stable, "stable-v1", "v2",
                                {"window_requests": 3, "concurrency": 3, "candidate_p95_limit_ms": 200})
        self.assertEqual(window["candidate"]["routed_count"], 3)
        self.assertEqual(window["stable"]["routed_count"], 0)
        self.assertEqual(window["stable"]["baseline_count"], 3)
        self.assertEqual(window["unknown_count"], 0)
        self.assertEqual(window["candidate"]["errors"], 0)
        self.assertGreater(window["candidate"]["p95_ms"], 200)
        self.assertIsNotNone(window["first_breach_at"])

    def test_unattributed_responses_do_not_count_as_candidate_traffic(self):
        class UnlabelledHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        router = ThreadingHTTPServer(("127.0.0.1", 0), UnlabelledHandler)
        threading.Thread(target=router.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.servers.callback(router.server_close)
        self.servers.callback(router.shutdown)
        window = measure_window(f"http://127.0.0.1:{router.server_port}", self.stable,
                                "stable-v1", "v2", {"window_requests": 3})
        self.assertEqual(window["unknown_count"], 3)
        self.assertEqual(window["candidate"]["count"], 0)
        self.assertIsNone(window["candidate"]["p95_ms"])
        self.assertIsNone(window["candidate"]["error_rate"])
        self.assertEqual(window["stable"]["baseline_count"], 3)

    def test_invalid_quote_and_header_validation(self):
        with self.assertRaises(ValueError):
            start_checkout("127.0.0.1", 0, "v2\r\nInjected: x", "healthy")
        for price in (True, -1, 1.001, float("nan")):
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    quote({"items": [{"unit_price": price, "quantity": 1}]})
        connection = http.client.HTTPConnection(self.stable.split("//")[1], timeout=2)
        self.addCleanup(connection.close)
        connection.request("POST", "/quote", json.dumps({"items": []}))
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader("X-Release-Version"), "stable-v1")


if __name__ == "__main__":
    unittest.main()

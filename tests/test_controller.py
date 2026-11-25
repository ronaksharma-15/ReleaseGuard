import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import dispatch
from controller import APIError, Controller


class ReleasePolicyTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = Controller(self.directory.name, mode="local", policy={"window_seconds": 0.25})
        self.addCleanup(lambda: self.controller.close())

    def run_demo(self, scenario, key):
        record, created = self.controller.submit({"scenario": scenario, "request_id": key})
        self.assertTrue(created)
        self.controller.thread.join(30)
        self.assertFalse(self.controller.thread.is_alive())
        return self.controller.store.get(record["id"])

    def test_three_decisions_with_real_http_and_persisted_history(self):
        baseline = self.controller.state()["stable"]
        bad = self.run_demo("wrong_total", "prevent")
        self.assertEqual((bad["status"], bad["traffic"], len(bad["windows"])), ("blocked", 0, 0))
        failure = bad["behavior"]["failures"][0]
        self.assertEqual((failure["expected"]["total"], failure["actual"]["total"]), (450, 900))
        self.assertEqual(failure["candidate"]["http_status"], 200)
        slow = self.run_demo("slow", "recover")
        self.assertEqual((slow["status"], slow["traffic"]), ("recovered", 0))
        self.assertEqual([w["verdict"] for w in slow["windows"]], ["breach", "breach"])
        self.assertEqual(slow["recovery"]["passed"], slow["recovery"]["total"])
        self.assertEqual(self.controller.state()["stable"], baseline)
        good = self.run_demo("healthy", "release")
        self.assertEqual((good["status"], good["traffic"]), ("promoted", 100))
        self.assertEqual([w["stage"] for w in good["windows"]], [10, 50, 100, 100])
        self.assertEqual(self.controller.state()["stable"]["version"], good["candidate_version"])
        for w in good["windows"]:
            self.assertEqual(w["routed_requests"], w["candidate"]["count"] + w["stable"]["routed_count"])
        self.controller.close()
        self.controller = Controller(self.directory.name, mode="local")
        self.assertEqual(len(self.controller.state()["releases"]), 3)
        self.assertEqual(self.controller.state()["stable"]["version"], good["candidate_version"])
        status, headers, data = dispatch(self.controller, "POST", "/quote", {}, json.dumps(failure["input"]).encode(),
                                         "127.0.0.1")
        self.assertEqual((status, headers["X-Release-Version"], json.loads(data)["total"]),
                         (200, good["candidate_version"], 450))

    def test_duplicate_and_concurrent_trigger(self):
        data = {"scenario": "healthy", "request_id": "same-push"}
        first, _ = self.controller.submit(data)
        second, created = self.controller.submit(data)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(created)
        with self.assertRaises(APIError) as err:
            self.controller.submit({**data, "request_id": "overlapping"})
        self.assertEqual(err.exception.status, 409)
        with self.assertRaises(APIError):
            self.controller.submit({**data, "scenario": "slow"})

    def test_incomplete_metrics_pause_then_stable_recovery(self):
        self.controller.policy.update(min_candidate_requests=1000, max_wait_seconds=0.7)
        row = self.run_demo("healthy", "sparse")
        self.assertEqual(row["status"], "inconclusive")
        self.assertIn("paused", [e["status"] for e in row["events"]])
        self.assertTrue(all(w["stage"] == 10 and w["verdict"] == "insufficient" for w in row["windows"]))
        self.assertGreater(row["recovery"]["passed"], 0)

    def test_failed_verification_requires_operator_recheck(self):
        from checks import run_fixtures

        def unavailable(*args, **kwargs):
            if kwargs.get("critical"):
                return {"total": 6, "passed": 0, "failed": 1, "complete": False}
            return run_fixtures(*args, **kwargs)

        with patch("controller.checks.run_fixtures", side_effect=unavailable):
            row = self.run_demo("slow", "recovery-failure")
        self.assertEqual(row["status"], "needs_attention")
        with self.assertRaises(APIError):
            self.controller.submit({"scenario": "healthy", "request_id": "too-soon"})
        self.controller.retry_recovery(row["id"])
        self.controller.thread.join(10)
        self.assertEqual(self.controller.store.get(row["id"])["status"], "recovered")

    def test_launch_requires_local_same_origin_and_valid_input(self):
        headers = {"Host": "127.0.0.1:8787", "Content-Type": "application/json", "X-ReleaseGuard-UI": "1"}
        for changes, body, status in [
            ({"Origin": "https://different.example"}, b"{}", 403),
            ({"Host": "different.example"}, b"{}", 403),
            ({"X-ReleaseGuard-UI": ""}, b"{}", 403),
            ({}, b"[]", 400),
            ({}, b"{}", 400),
        ]:
            result = dispatch(self.controller, "POST", "/api/releases", {**headers, **changes}, body, "127.0.0.1")
            self.assertEqual(result[0], status)

    def test_restart_verifies_stable_before_closing_interrupted_canary(self):
        row = self.run_demo("wrong_total", "before-interruption")
        self.controller.event(row, "canary", "Test checkpoint during a canary.", target=10)
        self.controller.close()
        self.controller = Controller(self.directory.name, mode="local")
        restored = self.controller.store.get(row["id"])
        self.assertEqual((restored["status"], restored["traffic"]), ("recovered", 0))
        self.assertEqual(restored["recovery"]["sample"]["candidate"]["version"], row["stable_version"])
        self.assertIsNone(self.controller.state()["active_id"])

    def test_promotion_write_failure_routes_to_previous_stable(self):
        write = self.controller.store.write

        def fail_promotion(release, **settings):
            if "stable" in settings:
                raise sqlite3.OperationalError("Simulated commit failure")
            return write(release, **settings)

        stable = self.controller.state()["stable"]
        with patch.object(self.controller.store, "write", side_effect=fail_promotion):
            row = self.run_demo("healthy", "commit-failure")
        self.assertEqual(row["status"], "recovered")
        self.assertEqual(row["recovery"]["sample"]["candidate"]["version"], stable["version"])
        self.assertEqual(self.controller.state()["stable"], stable)
        self.assertNotIn("promoted", [event["status"] for event in row["events"]])


if __name__ == "__main__":
    unittest.main()

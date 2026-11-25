import unittest
from unittest.mock import patch

from deployment import host_agent
from deployment.runtime import ComposeRuntime


class RouteTests(unittest.TestCase):
    def test_only_exposed_slots_enter_nginx(self):
        for stable, other in (("blue", "green"), ("green", "blue")):
            for pct in (0, 10, 50, 100):
                config = host_agent.nginx_config(stable, pct)
                self.assertEqual(set(host_agent.serving(config)),
                                 {slot for slot, value in [(stable, 100 - pct), (other, pct)] if value})
                self.assertIn("proxy_next_upstream off", config)
                if pct == 10:
                    self.assertIn(f"{other}:8001 weight=10", config)
                    self.assertIn(f"{stable}:8001 weight=90", config)

    def test_host_restricts_artifacts_and_routes(self):
        repo = "ghcr.io/team/guard/checkout"
        digest = repo + "@sha256:" + "e" * 64
        host_agent.validate_artifact(repo, digest, "release-abc", "healthy")
        for invalid in (repo + ":latest", digest + ".x", "untrusted/" + digest):
            with self.assertRaises(ValueError):
                host_agent.validate_artifact(repo, invalid, "release-abc", "healthy")
        for pct in (-1, 99, True):
            with self.assertRaises(ValueError):
                host_agent.weights("blue", pct)

    @patch("deployment.runtime.verify_version")
    @patch("deployment.runtime.ask_agent")
    def test_restart_loads_sqlite_identity_even_after_slot_swap(self, agent, verify):
        old = {"digest": "old", "version": "v1"}
        new = {"digest": "new", "version": "v2"}
        agent.return_value = {"slots": {"blue": old, "green": new}, "serving": ["green"]}
        runtime = ComposeRuntime()
        runtime.initialize(old)
        agent.assert_called_with(op="route", stable="blue", percent=0)
        verify.assert_called_with(runtime.router_url, "v1")
        runtime.prepare({"candidate_version": "v2", "candidate_digest": "new", "scenario": "healthy"})
        runtime.promote()
        self.assertEqual(runtime.stable_slot, "green")
        runtime.route(0)
        verify.assert_called_with(runtime.router_url, "v2")

    @patch("deployment.runtime.ask_agent")
    def test_missing_stable_never_replaces_a_serving_slot(self, agent):
        agent.return_value = {"slots": {"blue": None, "green": None}, "serving": ["blue", "green"]}
        with self.assertRaisesRegex(RuntimeError, "both slots"):
            ComposeRuntime().initialize({"version": "old", "digest": "required"})
        agent.assert_called_once_with(op="slots")

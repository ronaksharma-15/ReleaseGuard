"""Release policy, durable evidence, and serialized release execution."""

import hashlib
import json
import math
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from threading import Lock, Thread
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import checks
from local_runtime import LocalRuntime


BASE = Path(__file__).resolve().parent
CHECKOUT_SOURCE = (BASE / "checkout.py").read_bytes()
ACTIVE = {"queued", "validating", "canary", "paused", "rolling_back"}
SCENARIOS = {"healthy", "wrong_total", "slow"}
POLICY = {
    "stages": [10, 50, 100], "window_requests": 60, "concurrency": 3,
    "window_seconds": 1.0, "min_candidate_requests": 4, "max_wait_seconds": 15,
    "consecutive_breach_windows": 2, "candidate_error_rate_limit": 0.02,
    "candidate_p95_limit_ms": 200, "request_timeout_seconds": 3,
    "full_traffic_healthy_windows": 2,
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def elapsed_ms(start, end):
    if not start:
        return None
    return max(0, round((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                         datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000))


def fingerprint(scenario):
    return "sha256:" + hashlib.sha256(CHECKOUT_SOURCE + b"\0" + scenario.encode()).hexdigest()


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "history.sqlite3"
        self.lease = (directory / "controller.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lease.seek(0)
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lease.close()
            raise RuntimeError("Another controller is using this history directory") from exc
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS releases
                  (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
            """)

    def read(self, query, *parameters):
        with closing(sqlite3.connect(self.db)) as db:
            return [json.loads(row[0]) for row in db.execute(query, parameters)]

    def setting(self, key):
        return next(iter(self.read("SELECT value FROM settings WHERE key=?", key)), None)

    def history(self, limit=50):
        return self.read("SELECT data FROM releases ORDER BY rowid DESC LIMIT ?", limit)

    def get(self, release_id):
        return next(iter(self.read("SELECT data FROM releases WHERE id=?", release_id)), None)

    def duplicate(self, request_id):
        return next(iter(self.read("SELECT data FROM releases WHERE request_id=?", request_id)), None)

    def write(self, release=None, **settings):
        with closing(sqlite3.connect(self.db)) as db, db:
            if release is not None:
                db.execute("""INSERT INTO releases(id,request_id,data) VALUES(?,?,?)
                              ON CONFLICT(id) DO UPDATE SET data=excluded.data""",
                           (release["id"], release["request_id"], json.dumps(release, allow_nan=False)))
            for key, value in settings.items():
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))


def check_ready(url, version, timeout=8):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urlopen(url + "/health", timeout=1.5) as response:
                if response.status == 200 and response.headers.get("X-Release-Version") == version:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Readiness probe did not confirm the requested version")
        time.sleep(0.15)


class Controller:
    def __init__(self, directory=None, mode=None, runtime=None, policy=None):
        self.mode = mode or os.getenv("RG_MODE", "local")
        if self.mode not in ("local", "compose"):
            raise ValueError("RG_MODE must be local or compose")
        self.store = Store(directory or os.getenv("RG_DATA_DIR", BASE / ".releaseguard"))
        self.lock = Lock()
        self.policy = {**POLICY, **(policy or {})}
        self.thread = None
        try:
            if self.store.setting("mode") not in (None, self.mode):
                raise RuntimeError("Select a separate history directory for each runtime mode")
            if self.mode == "compose":
                from deployment.runtime import ComposeRuntime
                self.runtime = runtime or ComposeRuntime()
                initial = {"version": os.environ["RG_STABLE_VERSION"],
                           "digest": os.environ["RG_STABLE_DIGEST"]}
            else:
                self.runtime = runtime or LocalRuntime()
                digest = fingerprint("healthy")
                initial = {"version": "baseline-" + digest[7:14], "digest": digest}
            baseline_commit = os.getenv("RG_STABLE_COMMIT") if self.mode == "compose" else None
            stable = self.store.setting("stable") or {**initial, "scenario": "healthy", "commit_sha": baseline_commit}
            if self.mode == "local" and stable["digest"] != fingerprint(stable["scenario"]):
                raise RuntimeError("Checkout source differs from this demo's stable fingerprint. "
                                   "Restore the recorded source or select a new --data-dir for the changed checkout.")
            self.store.write(stable=stable, mode=self.mode)
            self.runtime.initialize(stable)
            check_ready(self.runtime.router_url, stable["version"])
            latest = self.store.history(1)
            if latest and latest[0]["status"] in ACTIVE:
                self.rollback(latest[0], "Controller restarted during the release.")
        except Exception:
            if getattr(self, "runtime", None) and hasattr(self.runtime, "close"):
                self.runtime.close()
            self.store.lease.close()
            raise

    def state(self):
        with self.lock:
            history = self.store.history()
            return {"releases": history, "stable": self.store.setting("stable"),
                    "active_id": next((r["id"] for r in history if r["status"] in ACTIVE), None),
                    "mode": self.mode, "policy": self.policy}

    def submit(self, data):
        scenario = data.get("scenario", "healthy")
        key = data.get("request_id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", key):
            raise APIError(400, "Supply a request_id of 1–128 letters, digits or . _ : / -")
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise APIError(400, "Unknown checkout scenario")
        commit = None
        if self.mode == "compose":
            repository = os.getenv("RG_IMAGE_REPOSITORY", "")
            digest, commit = data.get("candidate_digest"), data.get("commit_sha")
            if not repository or not isinstance(digest, str) or not re.fullmatch(
                    re.escape(repository) + r"@sha256:[0-9a-f]{64}", digest):
                raise APIError(400, "Candidate must be an immutable digest from the configured repository")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
                raise APIError(400, "Supply the full Git commit SHA")
        else:
            digest = fingerprint(scenario)
        with self.lock:
            existing = self.store.duplicate(key)
            if existing:
                if (scenario, digest, commit) != (existing["scenario"], existing["candidate_digest"],
                                                 existing["commit_sha"]):
                    raise APIError(409, "This request_id already identifies a different release")
                return existing, False
            latest = self.store.history(1)
            if latest and latest[0]["status"] in ACTIVE | {"needs_attention"}:
                raise APIError(409, "Resolve the current release before starting another")
            stable = self.store.setting("stable")
            release_id = uuid4().hex[:12]
            at = now()
            release = {
                "id": release_id, "request_id": key, "scenario": scenario, "commit_sha": commit,
                "candidate_version": "release-" + release_id, "candidate_digest": digest,
                "stable_version": stable["version"], "stable_digest": stable["digest"],
                "created_at": at, "updated_at": at, "traffic": 0, "behavior": None, "recovery": None,
                "windows": [], "events": [], "detection_ms": None, "recovery_ms": None,
                "policy": self.policy,
            }
            self.event(release, "queued", "Candidate queued. Current stable version recorded.")
            self.thread = Thread(target=self.run_release, args=(release,), name="release-worker", daemon=True)
            result = self.store.get(release_id)
            self.thread.start()
            return result, True

    def event(self, r, status, message, **metadata):
        at = now()
        r.update(status=status, summary=message, updated_at=at)
        r["events"].append({"at": at, "status": status, "message": message, **metadata})
        self.store.write(r)

    def run_release(self, r):
        try:
            self.event(r, "validating", "Starting the candidate with zero routed traffic.")
            self.runtime.prepare(r)
            check_ready(self.runtime.candidate_url, r["candidate_version"])
            check_ready(self.runtime.stable_url, r["stable_version"])
            r["behavior"] = checks.run_fixtures(
                self.runtime.stable_url, self.runtime.candidate_url, expected_version=r["candidate_version"],
                expected_stable_version=r["stable_version"],
            )
            if not self.checks_pass(r["behavior"]):
                self.event(r, "blocked", "Behavior gate blocked promotion. Candidate received no routed traffic.")
                return
            self.event(r, "validating", f'All {r["behavior"]["total"]} behavior fixtures passed.')
            for stage in self.policy["stages"]:
                self.event(r, "canary", f"Requesting {stage}% candidate traffic.", target=stage)
                self.runtime.route(stage)
                r["traffic"] = stage
                self.event(r, "canary", f"Routing {stage}% to the candidate. Measuring synthetic requests.")
                if not self.observe_stage(r, stage):
                    return
            check_ready(self.runtime.router_url, r["candidate_version"])
            self.event(r, "canary", "Full-traffic observation passed. Committing the candidate as stable.")
            self.runtime.promote()
            new_stable = {"version": r["candidate_version"], "digest": r["candidate_digest"],
                          "scenario": r["scenario"], "commit_sha": r["commit_sha"]}
            at = now()
            msg = "Candidate promoted after behavior checks and full-traffic observation passed."
            r.update(status="promoted", summary=msg, updated_at=at)
            r["events"].append({"at": at, "status": "promoted", "message": msg})
            with self.lock:
                self.store.write(r, stable=new_stable)
        except Exception as exc:
            self.rollback(r, f"Release interrupted ({type(exc).__name__}).")

    @staticmethod
    def checks_pass(result):
        return bool(result.get("total") and result.get("passed") == result["total"]
                    and result.get("failed") == 0 and result.get("complete", True))

    def observe_stage(self, r, stage):
        p = self.policy
        deadline = time.monotonic() + p["max_wait_seconds"]
        failures = successes = 0
        first_failure = None
        required = p["full_traffic_healthy_windows"] if stage == 100 else 1
        while time.monotonic() < deadline:
            started = now()
            w = checks.measure_window(self.runtime.router_url, self.runtime.stable_url,
                                      r["stable_version"], r["candidate_version"],
                                      {**p, "deadline_monotonic": deadline})
            c, s = w["candidate"], w["stable"]
            measured = all(isinstance(sample.get(k), (int, float)) and math.isfinite(sample[k])
                           for sample in (c, s) for k in ("p95_ms", "error_rate"))
            enough = (measured and not w.get("unknown_count", 0) and not w.get("baseline_unknown_count", 0)
                      and c["count"] >= p["min_candidate_requests"] and s["count"] > 0)
            breaches = []
            if enough:
                if c["p95_ms"] > p["candidate_p95_limit_ms"]:
                    breaches.append("p95 latency")
                if c["error_rate"] > p["candidate_error_rate_limit"]:
                    breaches.append("error rate")
            verdict = "insufficient" if not enough else "breach" if breaches else "pass"
            w.update(stage=stage, started_at=started, at=now(), verdict=verdict, breached_rules=breaches)
            r["windows"].append(w)
            if not enough:
                failures = successes = 0
                first_failure = None
                self.event(r, "paused", f"Paused at {stage}%. Window has insufficient attributable measurements.")
            elif breaches:
                successes = 0
                failures += 1
                first_failure = first_failure or w.get("first_breach_at") or started
                reason = " and ".join(breaches)
                self.event(r, "canary", f"Candidate {reason} exceeded policy at {stage}% ({failures} consecutive).")
                if failures >= p["consecutive_breach_windows"]:
                    r["detection_ms"] = elapsed_ms(first_failure, now())
                    self.rollback(r, f"Canary stopped at {stage}% after {failures} consecutive breach windows.")
                    return False
            else:
                failures = 0
                first_failure = None
                successes += 1
                self.event(r, "canary", f'Window passed at {stage}% with {c["count"]} candidate requests.')
                if successes >= required and time.monotonic() <= deadline:
                    return True
        self.rollback(r, f"Observation timed out at {stage}%. Promotion is inconclusive.", inconclusive=True)
        return False

    def rollback(self, r, reason, inconclusive=False):
        started = now()
        self.event(r, "rolling_back", reason + " Restoring the recorded stable version.", target=0)
        try:
            self.runtime.route(0)
            r["traffic"] = 0
            self.event(r, "rolling_back", "Stable route applied. Checking critical fixtures through the router.")
            result = checks.run_fixtures(self.runtime.stable_url, self.runtime.router_url, critical=True,
                                         expected_version=r["stable_version"])
            r["recovery"] = result
            if not self.checks_pass(result):
                self.event(r, "needs_attention", "Stable recovery checks failed. Operator review required.")
                return
            r["recovery_ms"] = elapsed_ms(started, now())
            state = "inconclusive" if inconclusive else "recovered"
            self.event(r, state, reason + f' Stable traffic verified with {result["passed"]} critical fixtures.')
        except Exception as exc:
            self.event(r, "needs_attention", f"Could not verify stable routing or recovery ({type(exc).__name__}).")

    def retry_recovery(self, release_id):
        with self.lock:
            latest = self.store.history(1)
            if not latest or latest[0]["id"] != release_id or latest[0]["status"] != "needs_attention":
                raise APIError(409, "Only the latest release awaiting operator attention can retry recovery")
            r = latest[0]
            self.event(r, "rolling_back", "Operator requested another stable routing and recovery check.")
            self.thread = Thread(target=self.rollback, args=(r, "Operator retried recovery."), daemon=True)
            self.thread.start()
            return self.store.get(release_id)

    def close(self):
        if self.thread:
            self.thread.join()
        if hasattr(self.runtime, "close"):
            self.runtime.close()
        self.store.lease.close()

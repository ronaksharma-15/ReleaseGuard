"""Behavior fixtures and version-attributed HTTP measurement for ReleaseGuard."""

import http.client
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit


# These expected values are examples of the contract, independent of checkout.py.
# Each row gives name, (unit price in INR, quantity) pairs, percentage discount,
# and the expected subtotal, discount amount and final total.
_CASES = [
    ("Standard discount", [(500, 1)], 10, 500, 50, 450),
    ("Full-price basket", [(125, 2)], 0, 250, 0, 250),
    ("Free sample", [(0, 1)], 10, 0, 0, 0),
    ("Complimentary basket", [(149, 1)], 100, 149, 149, 0),
    ("Mixed basket", [(100, 2), (50, 3)], 20, 350, 70, 280),
    ("Round half a paisa up", [(1.05, 1)], 10, 1.05, 0.11, 0.94),
    ("Smallest unit", [(0.01, 1)], 0, 0.01, 0, 0.01),
    ("Small price, full discount", [(0.01, 1)], 100, 0.01, 0.01, 0),
    ("Twenty-five percent", [(100, 1)], 25, 100, 25, 75),
    ("Quantity multiplies subtotal", [(25, 4)], 5, 100, 5, 95),
    ("Discounted pair", [(250, 2)], 10, 500, 50, 450),
    ("Free item with purchase", [(0, 3), (80, 1)], 5, 80, 4, 76),
    ("Maximum line quantity", [(2.5, 100)], 4, 250, 10, 240),
    ("Fractional discount", [(400, 1)], 2.5, 400, 10, 390),
    ("Nearly full discount", [(200, 2)], 99, 400, 396, 4),
    ("Half-price basket", [(19.99, 2)], 50, 39.98, 19.99, 19.99),
    ("Repeating decimal price", [(33.33, 3)], 10, 99.99, 10, 89.99),
    ("Three different items", [(15, 2), (30, 1), (20, 2)], 15, 100, 15, 85),
    ("Round discount down", [(0.04, 1)], 10, 0.04, 0, 0.04),
    ("Two paise at half price", [(0.02, 1)], 50, 0.02, 0.01, 0.01),
    ("No discount on decimals", [(7.75, 4)], 0, 31, 0, 31),
    ("Fractional percentage on bundle", [(25, 8)], 12.5, 200, 25, 175),
    ("Large amount", [(100_000, 10)], 10, 1_000_000, 100_000, 900_000),
    ("Mixed quantities", [(12.5, 4), (5, 10)], 30, 100, 30, 70),
]


def _fixture(case):
    name, prices, percent, subtotal, discount, total = case
    return {
        "name": name,
        "input": {"items": [{"unit_price": price, "quantity": quantity} for price, quantity in prices],
                  "discount_percent": percent},
        "expected": {"currency": "INR", "subtotal": subtotal, "discount": discount, "total": total},
    }


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _request(base_url, payload, workload, timeout):
    url = urlsplit(base_url)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("Probe endpoint requires an HTTP or HTTPS base URL.")
    constructor = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = constructor(url.hostname, url.port, timeout=timeout)
    started = time.perf_counter()
    status, version, body = 0, None, None
    try:
        connection.request(
            "POST", url.path.rstrip("/") + "/quote", json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json", "X-ReleaseGuard-Workload": workload},
        )
        response = connection.getresponse()
        status = response.status
        version = response.getheader("X-Release-Version")
        raw = response.read(16_385)
        if len(raw) <= 16_384:
            body = json.loads(raw)
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        pass
    finally:
        connection.close()
    return {"status": status, "version": version, "body": body, "observed_at": _utc(),
            "latency_ms": (time.perf_counter() - started) * 1000}


def _money(value):
    if type(value) is int:
        return value >= 0
    return type(value) is float and math.isfinite(value) and value >= 0


def _assess(result, expected, expected_version):
    body = result["body"] if isinstance(result["body"], dict) else {}
    fields = {key: body[key] for key in expected if key in body}
    rules = {
        "HTTP status is 200": result["status"] == 200,
        "response identifies the release": bool(result["version"]),
        "all required quote fields are present": set(fields) == set(expected),
    }
    if expected_version is not None:
        rules["response version matches " + expected_version] = result["version"] == expected_version
    for key, value in expected.items():
        actual = fields.get(key)
        numeric = isinstance(value, (int, float))
        rules[f"{key} equals {value}"] = (not numeric or _money(actual)) and actual == value
    valid_amounts = all(_money(fields.get(key)) for key in ("total", "subtotal", "discount"))
    rules["quote amounts are non-negative"] = valid_amounts
    rules["discount stays within subtotal"] = valid_amounts and fields["discount"] <= fields["subtotal"]
    rules["total equals subtotal minus discount"] = valid_amounts and (
        Decimal(str(fields["subtotal"])) - Decimal(str(fields["discount"])) == Decimal(str(fields["total"]))
    )
    return fields, [name for name, passed in rules.items() if not passed], len(rules)


def run_fixtures(stable_url, candidate_url, critical=False, expected_version=None, expected_stable_version=None):
    """Replay bounded, read-only cases; critical recovery can assert routed identity."""
    cases = _CASES[:6] if critical else _CASES
    result = {"total": len(cases), "passed": 0, "failed": 0, "skipped": 0, "rules_checked": 0,
              "failures": [], "sample": None, "complete": True}
    deadline = time.monotonic() + 15
    for case in cases:
        fixture = _fixture(case)
        violations = []
        observations = {}
        unavailable = False
        for side, url, version in (
            ("stable", stable_url, expected_stable_version or (expected_version if critical else None)),
            ("candidate", candidate_url, expected_version),
        ):
            remaining = deadline - time.monotonic()
            response = (_request(url, fixture["input"], "behavior", min(1.5, remaining))
                        if remaining > 0 else {"status": 0, "version": None, "body": None})
            fields, broken, count = _assess(response, fixture["expected"], version)
            observations[side] = {"values": fields, "http_status": response["status"], "version": response["version"]}
            result["rules_checked"] += count
            if broken:
                violations.append({"side": side, "rules": broken})
            if not response["version"] or response["status"] in (502, 503, 504) or (version and response["version"] != version):
                unavailable = True
        comparison = {**fixture, "stable": observations["stable"], "candidate": observations["candidate"]}
        if result["sample"] is None:
            result["sample"] = comparison
        if violations:
            side = violations[0]["side"]
            result["failures"].append({**comparison, "side": side, "rule": violations[0]["rules"][0],
                                       "actual": observations[side]["values"], "violations": violations})
            result["failed"] += 1
        else:
            result["passed"] += 1
        if unavailable or time.monotonic() >= deadline:
            result["skipped"] = result["total"] - result["passed"] - result["failed"]
            result["complete"] = result["skipped"] == 0
            # An expired run cannot be interpreted as fully passing.
            if result["skipped"] and not result["failed"]:
                pending = _fixture(cases[result["passed"]])
                result["failures"].append({**pending, "actual": None, "rule": "behavior gate deadline exceeded"})
                result["failed"] += 1
                result["skipped"] -= 1
            break
    return result


def _stats(samples, baseline_count=0):
    errors = sum(r["status"] != 200 or not isinstance(r["body"], dict) for r in samples)
    count = len(samples)
    timings = sorted(r["latency_ms"] for r in samples)
    return {"count": count, "routed_count": count - baseline_count, "baseline_count": baseline_count,
            "errors": errors, "error_rate": errors / count if count else None,
            "p95_ms": round(timings[math.ceil(count * 0.95) - 1], 2) if count else None}


def measure_window(router_url, stable_url, stable_version, candidate_version, policy):
    """Finish one complete synthetic window before the caller evaluates any limit."""
    if stable_version == candidate_version:
        raise ValueError("Stable and candidate must have distinct release identities.")
    count = max(1, min(2_000, int(policy.get("window_requests", 60))))
    concurrency = max(1, min(16, int(policy.get("concurrency", 3))))
    timeout = max(0.1, min(5.0, float(policy.get("request_timeout_seconds", 2))))
    seconds = max(0, min(120, float(policy.get("window_seconds", 0))))
    limit_ms = float(policy.get("candidate_p95_limit_ms", 200))
    payload = _fixture(_CASES[0])["input"]
    started_at, start = _utc(), time.monotonic()

    def send(index):
        # Spread request starts through the configured window. Slow responses
        # naturally limit concurrency and may extend the final observation.
        delay = start + seconds * index / max(1, count - 1) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return _request(router_url, payload, "synthetic", timeout)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        routed = list(pool.map(send, range(count)))
        stable = [r for r in routed if r["version"] == stable_version]
        candidate = [r for r in routed if r["version"] == candidate_version]
        unknown = count - len(candidate) - len(stable)
        baseline_count = baseline_unknown = 0
        if not stable:
            # Full rollout has no stable traffic. Keep comparison probes out of exposure.
            baseline = list(pool.map(lambda _: _request(stable_url, payload, "synthetic", timeout),
                                     range(min(count, 6))))
            stable = [r for r in baseline if r["version"] == stable_version]
            baseline_count = len(stable)
            baseline_unknown = len(baseline) - baseline_count
    breaches = [r["observed_at"] for r in candidate if r["status"] != 200
                or not isinstance(r["body"], dict) or r["latency_ms"] > limit_ms]
    return {"started_at": started_at, "finished_at": _utc(), "duration_ms": round((time.monotonic() - start) * 1000),
            "workload": "synthetic", "routed_requests": count, "unknown_count": unknown,
            "baseline_unknown_count": baseline_unknown, "candidate": _stats(candidate),
            "stable": _stats(stable, baseline_count), "first_breach_at": min(breaches, default=None)}

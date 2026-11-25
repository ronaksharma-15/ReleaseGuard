"""Submit one immutable checkout image and wait for its recorded decision."""

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def run():
    base = os.getenv("RG_CONTROLLER_URL", "http://127.0.0.1:8787").rstrip("/")
    token = os.environ["RG_DEPLOY_TOKEN"]
    digest = os.environ["RG_CANDIDATE_DIGEST"]
    commit = os.environ["RG_COMMIT_SHA"]
    scenario = os.getenv("RG_SCENARIO", "healthy")
    expected = os.getenv("RG_EXPECTED_STATUS", "promoted")
    # A retried job observing the same source and artifact gets the same decision.
    key = os.getenv("RG_REQUEST_ID", f"{scenario}:{commit}:{digest.rsplit(':', 1)[-1]}")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    deadline = time.monotonic() + 45
    while True:
        try:
            with urlopen(f"{base}/health", timeout=3) as response:
                if response.status == 200:
                    break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Controller did not become ready.")
            time.sleep(1)
    body = json.dumps({"request_id": key, "commit_sha": commit,
                       "candidate_digest": digest, "scenario": scenario}).encode()
    with urlopen(Request(f"{base}/api/releases", body, headers), timeout=15) as response:
        release = json.load(response)
    last_status = None
    deadline = time.monotonic() + 600
    while True:
        status = release["status"]
        if status != last_status:
            print(f'{release["id"]}  {status}  {release["summary"]}', flush=True)
            last_status = status
        if status in {"promoted", "blocked", "recovered", "inconclusive", "needs_attention"}:
            return 0 if status == expected else 1
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for the decision. Check release history on the VM.")
        time.sleep(1)
        with urlopen(Request(f'{base}/api/releases/{release["id"]}', headers=headers), timeout=10) as response:
            release = json.load(response)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except HTTPError as exc:
        print(f"Controller returned HTTP {exc.code}: {exc.read(1024).decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, ValueError, OSError, RuntimeError, URLError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)

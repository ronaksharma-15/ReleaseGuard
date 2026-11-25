# ReleaseGuard

**Behavior checks and verified recovery for CI/CD**  
Team Ravenrebels · RavenX Cloud PS-3

A checkout can respond with HTTP 200 and still charge the wrong amount. ReleaseGuard sends the same baskets to the stable and candidate versions, measures candidate traffic during rollout, and verifies the stable checkout after rollback. Its dashboard keeps the requests, measurements and decisions together in a SQLite release history.

## Run the hackathon demo

To use another laptop, download the `hackathon/releaseguard-demo` branch from GitHub or run these commands.

```sh
git clone --branch hackathon/releaseguard-demo https://github.com/ronaksharma-15/ReleaseGuard.git
cd ReleaseGuard
```

On Windows, double-click **start-demo.cmd**. On Windows, macOS or Linux with **Python 3.10+**, you can also run the following in the project folder.

```sh
python app.py --open
```

Open **http://127.0.0.1:8787/**. The local demo uses two real checkout HTTP services, a weighted router and synthetic requests. Python’s standard library runs the entire local backend. No cloud account, Docker installation or API key is required. Press Ctrl+C in the launch terminal to stop it. The next launch reopens the same history.

### Three-minute judging walkthrough

| Run | What to show the judges |
| --- | --- |
| **Prevent** | Open the failed standard-discount fixture. A ₹500 basket with a 10% discount should cost ₹450. Both versions return HTTP 200. The candidate returns ₹900. It receives zero routed traffic. |
| **Recover** | All behavior fixtures pass. The candidate introduces a delay on synthetic load requests. At the 10% canary, inspect the sample counts and candidate p95 across two consecutive breach windows. Follow the router back to stable and show the six critical checkout checks. |
| **Release** | Run the healthy candidate. Watch 10%, 50% and 100% traffic, including a second healthy observation window at 100%. Compare the newly recorded stable identity with the earlier runs in history. |

Use the on-screen measurements when presenting detection and recovery times. Latency, sample counts on NGINX, and timing depend on the machine and workload. Only the synthetic quote endpoint is exercised. No orders or payments are created.

## How decisions work

- **Behavior.** Twenty-four fixed baskets cover amounts, discount bounds, rounding and response structure. Both services must match the expected version and contract. Each failed case records the input, both responses and the broken rule.
- **Rollout.** Each demo window schedules 60 requests with up to three concurrent clients over one second. The candidate needs at least four identified requests, with p95 at or below 200 ms and error rate at or below 2%. Two consecutive breach windows trigger rollback. Missing attribution or insufficient samples pause the stage. After a 15-second stage budget the controller restores stable traffic and records an inconclusive decision.
- **Verification.** The controller checks six critical baskets through the router and confirms the response version. If verification fails, the release requires operator attention and further releases are held. Retry recovery after restoring service. At 100% candidate traffic, direct stable comparison samples are recorded separately from routed exposure.
- **Evidence.** SQLite stores version identities, request keys, expected and observed fixture values, thresholds, traffic-change events, request counts and timestamps. Retrying a request key reuses the original release. One controller and one release at a time can use a history directory. After an interrupted rollout, startup reinstates the recorded stable version and checks recovery.

## Local and cloud runtimes

| | Local demo | Linux VM |
| --- | --- | --- |
| Controller and history | Python HTTP server, SQLite | FastAPI, SQLite on a Compose volume |
| Checkout routing | In-process weighted HTTP router and two localhost HTTP services | NGINX and two Docker Compose checkout slots |
| Artifact identity | SHA-256 of checkout source and scenario, plus per-release version | Git commit and approved repository image pinned by digest |
| Release trigger | Dashboard scenario buttons | Authenticated CI request over an SSH tunnel |

The GitHub Actions workflow runs tests and exercises Docker, NGINX and all three decisions on a Linux runner. On an approved main-branch run it can publish the checkout image to GHCR. VM delivery requires a configured Linux host and deployment secrets. The hosted controller exposes a read-only dashboard for judges through an SSH tunnel. See [single-VM setup and CI](deployment/README.md).

## Run the tests

```sh
python -m unittest discover -s tests -v
```

The tests cover wrong prices, routing identity, missing samples, repeated triggers, failed recovery, promotion after rollback and SQLite persistence. The real-HTTP tests need permission to connect to localhost.

## Inspect the running service

- `GET /api/state` returns the stable version, policy, active release and recent history.
- `GET /api/releases/{id}` returns one release and its recorded evidence.
- `POST /api/releases` starts a release. The local launcher sends a same-origin request with `X-ReleaseGuard-UI: 1`, a scenario and a unique request ID. VM triggers use the deployment token, commit SHA and immutable digest.
- `POST /api/releases/{id}/recover` retries a release awaiting recovery verification using the same authorization.
- `POST /quote` serves the routed checkout. For example, send `{"items":[{"unit_price":500,"quantity":1}],"discount_percent":10}` as JSON and inspect the `X-Release-Version` header.

Local evidence is stored under `.releaseguard/`, excluded from Git. Use `python app.py --port 8788 --data-dir demo-history` for an independent rehearsal. The prototype covers a single stateless application. Database changes, external side effects and change-aware policies are outside this rollout.

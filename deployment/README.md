# Run ReleaseGuard on one cloud VM

## Free hosted demo on Render

In the [Render dashboard](https://dashboard.render.com/), choose **New → Blueprint**, connect the GitHub repository, select `main`, and deploy the service defined in `render.yaml`. Its Free Web Service runs the Python demo. It records fresh Prevent, Recover, and Release results when the process starts, so visitors can replay all three after a restart. Release controls are available in the offline launcher and VM deployment.

Once the Render URL works, open **Settings → Custom Domains**, add the hostname assigned by your event, and copy Render's DNS record instructions to the domain provider. If the event controls DNS for your hostname, send the record details to the organizers. Verify the hostname in Render and open it over HTTPS. [Render custom domain guide](https://render.com/docs/custom-domains)

Render Free sleeps after 15 minutes without traffic and loses local SQLite data on restart. The demo regenerates its sample runs on wake. Wait for the dashboard before presenting. [Render Free limits](https://render.com/docs/free)

The rest of this guide covers the Docker Compose VM setup, including optional deployments from GitHub Actions.

The Linux runtime keeps two stateless checkout containers and an NGINX router ready on one x86-64 VM. A FastAPI controller saves decisions in SQLite. The controller requests fixed slot and routing operations through a Unix socket. Docker access stays with a small host service. Use the local launcher in the project README for an offline presentation.

## Run the demo with GitHub alone

Push a branch named `hackathon/releaseguard-demo` to a repository with Actions enabled. Open the **ReleaseGuard** run in Actions to see the verification job exercise all three scenarios on a temporary Linux runner. The job summary shows the decisions, and the **releaseguard-evidence** artifact contains measured windows, fixtures and timestamps for 14 days. The runner shuts down when the job finishes.

## Publish the first image

1. Put the project in a GitHub repository with a `main` branch and GitHub Actions enabled. Leave the repository variable `RG_DEPLOY_ENABLED` unset during bootstrap.
2. Push to `main`. The workflow runs the unit suite, builds a disposable Linux stack and exercises the three demo paths. A successful check allows the publish job to push the checkout image to GHCR.
3. Copy the full commit SHA and `ghcr.io/owner/repo/checkout@sha256:…` from the publish job summary. Use a known-good commit for this initial stable release.

## Bootstrap the VM

Use an x86-64 Linux VM with systemd, Python 3.10+, Git, Docker Engine, the Docker Compose v2 plugin with `--wait` support, and outbound registry access. Permit SSH from your deployment runner. Open port 8080 only to the networks that need to exercise the demo checkout. Port 8787 is bound to VM loopback.

Clone the trusted project to `/opt/releaseguard` as root. Check out the same commit used for the initial release. Only the VM administrator should be able to write this directory or edit the systemd service, since the helper starts Compose services from it. For a private GHCR package, give the VM a read-packages token and run `sudo docker login ghcr.io` with the appropriate GitHub username.

Create a root-owned mode-0600 file at `/opt/releaseguard/.env`. Replace every example value. Generate the token with `openssl rand -hex 32`.

```dotenv
RG_IMAGE_REPOSITORY=ghcr.io/owner/repo/checkout
RG_STABLE_DIGEST=ghcr.io/owner/repo/checkout@sha256:REPLACE_WITH_64_HEX_DIGEST
RG_STABLE_VERSION=baseline-REPLACE_WITH_SHORT_SHA
RG_STABLE_COMMIT=REPLACE_WITH_FULL_COMMIT_SHA
RG_DEPLOY_TOKEN=REPLACE_WITH_GENERATED_TOKEN
```

Start the helper and stack from a root shell. The helper creates the initial NGINX configuration and a private file containing slot assignments.

```sh
cd /opt/releaseguard
install -m 644 deployment/releaseguard-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now releaseguard-agent
while [ ! -S /run/releaseguard/agent.sock ]; do
  systemctl is-active --quiet releaseguard-agent || exit 1
  sleep 1
done
docker compose --env-file .env --env-file /var/lib/releaseguard-agent/slots.env up -d --build
curl -fsS --retry 15 --retry-all-errors --retry-delay 1 http://127.0.0.1:8787/health
curl -isS http://127.0.0.1:8080/quote -H 'Content-Type: application/json' \
  -d '{"items":[{"unit_price":500,"quantity":1}],"discount_percent":10}'
```

Check that the response reports the initial version in `X-Release-Version`, currency INR and a total of 450. Use both `--env-file` arguments on subsequent Compose commands so restarts use the last selected slot images. View the dashboard on your laptop through an SSH tunnel.

```sh
ssh -NT -L 127.0.0.1:8787:127.0.0.1:8787 user@your-vm
```

Open [the dashboard](http://127.0.0.1:8787). Run the local demo on a different port if both dashboards are open.

## Enable push-to-VM deployment

In GitHub, create the environment `hackathon-vm`. Limit it to `main`. Protect the branch and configure the environment with the following settings.

| Location | Name | Value |
| --- | --- | --- |
| Environment variable | `RG_VM_HOST`, `RG_VM_USER` | VM SSH hostname and a dedicated forwarding user |
| Environment secret | `RG_SSH_KEY` | Private SSH key for the forwarding user |
| Environment secret | `RG_KNOWN_HOSTS` | Verified OpenSSH known-hosts entry matching the hostname |
| Environment secret | `RG_DEPLOY_TOKEN` | Same token as the controller |
| Repository variable | `RG_DEPLOY_ENABLED` | `true` |

The CI user only needs SSH forwarding to `127.0.0.1:8787`. Keep this account outside the Docker and sudo groups. Its authorized-key entry can use `restrict,port-forwarding,permitopen="127.0.0.1:8787",command="/bin/false"`. Confirm the host-key fingerprint through your provider's VM console before storing the known-hosts entry.

A push to `main` now tests, builds, publishes and submits the digest with its commit. CI waits for the terminal decision. A failed build, blocked release, rollback or need for operator attention keeps the deployment job from reporting success. Pull requests run verification on hosted runners. The controller and host helper are infrastructure code installed during bootstrap. When those files change, update the trusted VM checkout and rebuild the controller during a maintenance window.

## Demonstrate the other decisions on the VM

From the project directory in a trusted VM root shell, load the configured token and select a published checkout artifact. This example reuses the initial image, with each candidate's scenario selected explicitly.

```sh
set -a
. ./.env
set +a
export RG_CANDIDATE_DIGEST="$RG_STABLE_DIGEST" RG_COMMIT_SHA="$RG_STABLE_COMMIT"
RG_SCENARIO=wrong_total RG_EXPECTED_STATUS=blocked python3 deployment/trigger.py
RG_SCENARIO=slow RG_EXPECTED_STATUS=recovered python3 deployment/trigger.py
RG_SCENARIO=healthy RG_EXPECTED_STATUS=promoted python3 deployment/trigger.py
```

The trigger reuses the existing result for identical scenario, digest and commit. Supply a fresh `RG_REQUEST_ID` for a new rehearsal. Candidate and stable measurements in the dashboard come from generated HTTP requests. Checkout images must implement the project's quote and version-header contract.

## Inspect the VM

```sh
journalctl -u releaseguard-agent -n 50 --no-pager
docker compose --env-file .env --env-file /var/lib/releaseguard-agent/slots.env ps
docker compose --env-file .env --env-file /var/lib/releaseguard-agent/slots.env logs --tail 50 controller router
```

The host helper checks the immutable image reference, slot readiness and NGINX generation. The controller independently verifies the routed version at the 0% and 100% boundaries. SQLite records the accepted version and reconciles routing after a controller restart. The previous slot stays available until the next candidate preparation. Preserve the controller volume and `/var/lib/releaseguard-agent` when restarting the demo. VM charges depend on your provider, storage and runtime. Stop unused resources after the event.

Workflow references include [GitHub's Docker publishing guide](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images), [Compose environment precedence](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/), [NGINX upstream weights](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) and [NGINX configuration reloads](https://nginx.org/en/docs/control.html).

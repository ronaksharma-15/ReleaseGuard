#!/usr/bin/env bash
# Runs only on an ephemeral GitHub-hosted Linux runner.
set -eu -o pipefail
: "${GITHUB_WORKSPACE:?}" "${GITHUB_SHA:?}"
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
sudo mkdir /opt/releaseguard
sudo cp -a "$GITHUB_WORKSPACE/." /opt/releaseguard/
cd /opt/releaseguard
docker run -d --name ci-registry -p 127.0.0.1:5000:5000 registry:2
repo=localhost:5000/releaseguard/checkout
docker build --target checkout -t "$repo:ci" .
docker push "$repo:ci"
export RG_CANDIDATE_DIGEST
RG_CANDIDATE_DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$repo:ci")
export RG_COMMIT_SHA="$GITHUB_SHA"
export RG_DEPLOY_TOKEN
RG_DEPLOY_TOKEN=$(openssl rand -hex 32)
printf '%s\n' "RG_DEPLOY_TOKEN=$RG_DEPLOY_TOKEN" "RG_IMAGE_REPOSITORY=$repo" \
  "RG_STABLE_DIGEST=$RG_CANDIDATE_DIGEST" "RG_STABLE_VERSION=ci-baseline" \
  "RG_STABLE_COMMIT=$GITHUB_SHA" | sudo tee .env >/dev/null
sudo chmod 600 .env
sudo install -m 644 deployment/releaseguard-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start releaseguard-agent
for i in {1..50}; do test -S /run/releaseguard/agent.sock && break; sleep .2; done
stack=(sudo docker compose --env-file .env --env-file /var/lib/releaseguard-agent/slots.env)
trap 'if [ "$?" -ne 0 ]; then "${stack[@]}" logs --tail 30 controller router; sudo journalctl -u releaseguard-agent -n 30 --no-pager; fi' EXIT
"${stack[@]}" up -d --build
RG_SCENARIO=wrong_total RG_EXPECTED_STATUS=blocked python3 deployment/trigger.py
RG_SCENARIO=slow RG_EXPECTED_STATUS=recovered python3 deployment/trigger.py
RG_SCENARIO=healthy RG_EXPECTED_STATUS=promoted python3 deployment/trigger.py
before=$(curl -fsS http://127.0.0.1:8080/health | jq -er .version)
"${stack[@]}" restart controller
python3 deployment/trigger.py
after=$(curl -fsS http://127.0.0.1:8080/health | jq -er .version)
test "$before" = "$after"

#!/usr/bin/env bash
# Deploys a release whose three images are already in ECR: points the app, ingest and
# eval functions at them, waits until /health reports the new version, and starts a
# post-deploy canary. Used by make release (scripts/release.sh) and by the GitHub
# pipeline's deploy job (.github/workflows/ci.yml), so both deploy the same way.
#
#   scripts/deploy.sh <tag> <registry> <site url>
set -euo pipefail
tag=$1
registry=$2
site=${3%/}
region=ap-southeast-2

for name in app ingest eval; do
  aws lambda update-function-code --region "$region" --function-name "policy-desk-$name" \
    --image-uri "$registry/policy-desk-$name:$tag" >/dev/null
done
for name in app ingest eval; do
  aws lambda wait function-updated-v2 --region "$region" --function-name "policy-desk-$name"
done
echo "The three functions run $tag."

live=""
for _ in $(seq 1 24); do
  live=$(curl -sf "$site/health" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("version") or "")' || true)
  [ "$live" = "$tag" ] && break
  sleep 5
done
if [ "$live" != "$tag" ]; then
  echo "Deployed, but $site/health reports ${live:-nothing}, not $tag. See docs/runbook.md." >&2
  exit 1
fi
echo "$site/health reports $tag."

# The release's note (RELEASE_NOTE, from the publish), for the status page's releases.
payload=$(RELEASE_NOTE="${RELEASE_NOTE:-}" python3 -c '
import json, os
event = {"suite": "canary", "kind": "post-deploy"}
if os.environ["RELEASE_NOTE"].strip():
    event["note"] = os.environ["RELEASE_NOTE"].strip()
print(json.dumps(event))')
aws lambda invoke --region "$region" --function-name policy-desk-eval --invocation-type Event \
  --payload "$payload" --cli-binary-format raw-in-base64-out \
  /dev/null >/dev/null
echo "A post-deploy canary has started; it's recorded under runs/post-deploy/."

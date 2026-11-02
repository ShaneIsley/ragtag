#!/usr/bin/env bash
# Build and push the three images for the current commit, then, once you confirm,
# deploy them with scripts/deploy.sh: point the three functions at the new images, wait
# until /health reports the new version, and start a post-deploy canary. The GitHub
# pipeline's deploy job uses the same script (.github/workflows/ci.yml). Terraform sets a function's image
# only when it creates it; infrastructure changes still go through plan and apply.
#
#   make release                  asks before deploying
#   RELEASE_YES=1 make release    doesn't ask
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Commit or stash your changes first: the images are tagged with the commit." >&2
  exit 1
fi

# Ship only answer files that a passing full release run has tested (evals/records.py).
# RELEASE_OVERRIDE="why" skips the check; the reason is recorded.
if [ -n "${RELEASE_OVERRIDE:-}" ]; then
  uv run python -m evals.records release-override "$RELEASE_OVERRIDE"
elif ! uv run python -m evals.records release-check; then
  exit 1
fi

sha=$(git rev-parse --short=12 HEAD)
region=ap-southeast-2
app_repo=$(terraform -chdir=infra output -raw ecr_app_repository)
ingest_repo=$(terraform -chdir=infra output -raw ecr_ingest_repository)
if ! eval_repo=$(terraform -chdir=infra output -raw ecr_eval_repository 2>/dev/null); then
  echo "The evaluation image's repository doesn't exist yet. Create it first:" >&2
  echo "  terraform -chdir=infra apply -target='aws_ecr_repository.this[\"eval\"]'" >&2
  exit 1
fi
registry=${app_repo%%/*}

pushed() {
  aws ecr describe-images --region "$region" --repository-name "${1#*/}" \
    --image-ids imageTag="$sha" >/dev/null 2>&1
}

echo "Release $sha"
aws ecr get-login-password --region "$region" |
  podman login --username AWS --password-stdin "$registry" >/dev/null

for pair in "app:$app_repo" "ingest:$ingest_repo" "eval:$eval_repo"; do
  name=${pair%%:*}
  repo=${pair#*:}
  if pushed "$repo"; then
    echo "The $name image for $sha is already in ECR; skipping it."
    continue
  fi
  podman build --platform linux/arm64 --build-arg APP_VERSION="$sha" \
    -t "$repo:$sha" -f "Dockerfile.$name" .
  podman push "$repo:$sha"
done

# Kept current, so rebuilding from nothing creates the functions with this release.
sed -i.bak "s/^image_tag = \".*\"\$/image_tag = \"$sha\"/" infra/terraform.tfvars
rm -f infra/terraform.tfvars.bak

echo
if [ -z "${RELEASE_YES:-}" ]; then
  read -r -p "The images for $sha are in ECR. Deploy them to production now? [y/N] " answer
  case "$answer" in
    y | Y) ;;
    *)
      echo "Not deployed. Run make release again to deploy these images."
      exit 0
      ;;
  esac
fi

scripts/deploy.sh "$sha" "$registry" "$(terraform -chdir=infra output -raw app_url)"
echo "Released $sha. Follow the canary with: aws logs tail /aws/lambda/policy-desk-eval --follow"

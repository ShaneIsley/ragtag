# Infrastructure

Terraform for everything Policy Desk runs on, in AWS (Sydney) and Azure (Australia East), and the links between them.

## What it builds

| File | Builds |
|---|---|
| `lambda.tf` | Three ECR repositories; the app and the weekly ingest job as Lambda functions (container images, ARM64); the app's function URL; the weekly schedule; the log groups of all three functions |
| `eval.tf` | The scheduled evaluation: a Lambda function from `Dockerfile.eval` that runs the gate against production (nightly canary; weekly full run and Bedrock standby check after the ingest), an S3 bucket for every run's record and results, and an alarm when a run fails |
| `edge.tf` | CloudFront for HTTPS, with origin access control so only CloudFront can call the app's function URL |
| `data.tf` | DynamoDB for flagged answers, which expire after 90 days; Secrets Manager for every key |
| `iam.tf` | Least-privilege roles for each function, the scheduler, and GitHub Actions over OIDC (one CI role, one deploy role) |
| `observability.tf` | Transaction Search for spans; alarms for app errors, throttling, slow responses, a failed ingest, mostly declined questions, failovers and a blocked answer; a CloudWatch dashboard; monthly budget alerts for AWS and for the Azure resource group |
| `azure.tf` | Azure OpenAI with chat and embedding deployments, Azure AI Search (Free tier), and the Entra ID app registration for the admin view |

There is no VPC: neither function needs to be inside a network, so there are no subnets, security groups, NAT gateway or load balancer to secure or pay for.

## How the pieces connect

- **Browser to app.** The browser connects over HTTPS to CloudFront. CloudFront signs each request (SigV4, origin access control) and sends it to the app's Lambda function URL, which accepts only requests signed for this distribution. For a POST the page sends the body's SHA-256 in `x-amz-content-sha256`, which signing needs. Inside the function, the Lambda Web Adapter passes the request to the FastAPI app, so the same image runs locally and on Lambda.
- **App to models and search.** Terraform copies the Azure keys into AWS Secrets Manager. The function's environment holds only the secrets' ARNs; the app reads the values once at start-up with its own role (`app/settings.py`), so no key is in code, in GitHub or in the function's configuration.
- **Failover model.** The app calls Bedrock with its role, scoped to one inference profile.
- **Flagged answers.** These go to DynamoDB through the app's role.
- **Traces and metrics.** Lambda sends its own request traces to X-Ray, and the app sends its OpenTelemetry spans to X-Ray's OTLP endpoint, signed with the app's role. CloudWatch Transaction Search, an account-wide setting this configuration turns on for the region, stores both as structured logs in `aws/spans` for 30 days, searchable by trace ID. The page shows each answer's trace ID. The dashboard's answer panels query those spans, and metric filters on them feed three alarms: most questions declined, repeated failovers, and a blocked answer. Logs go to CloudWatch.
- **Admin sign-in.** The admin view signs in through the Entra app registration. Only users given the `PolicyDesk.Admin` role can sign in to it at all.
- **Weekly ingest.** EventBridge Scheduler invokes the ingest function each Monday at 3 am Sydney time. The ingest job creates the search index from `search/index.json` if it doesn't exist, and fetches at most one page every 10 seconds per host. A failed run isn't retried; the `ingest_failed` alarm emails instead.
- **GitHub Actions** (on hold). Workflows would assume `github_ci_role_arn` for evaluation, from pull requests and the main branch only, and `github_deploy_role_arn`, from the main branch only, to push images and update the two functions. Pushes to other branches get neither role, so they can't read the model keys.

## Before you start

You need:
- **Terraform 1.6 or later.**
- **The AWS CLI**, signed in to an account in `ap-southeast-2`.
- **The Azure CLI**, signed in (`az login`) with rights to create resources in the subscription and app registrations in the tenant. Terraform uses the CLI's signed-in tenant, and makes you the admin-view user unless `admin_user_object_ids` names others.
- **`ARM_SUBSCRIPTION_ID`** set to the subscription to build in. The azurerm provider requires it, so a plan can't run against whichever subscription the CLI happens to have active:
  ```bash
  az account set --subscription "<name or id>"   # if you have more than one
  export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
  ```
- **Bedrock access to Claude Haiku 4.5.** Anthropic models need a one-time use-case form in the Bedrock console.

Two resources can exist only once, so check for them first:
- **Azure AI Search Free tier:** one per subscription. If the subscription already has one, either reuse it or set the SKU to `basic`, which costs about US$74 a month.
- **GitHub OIDC provider:** one per AWS account. If yours already has one, import it with `terraform import aws_iam_openid_connect_provider.github <arn>`.

## Deploying, in two stages

`image_tag` decides whether the app is deployed. Lambda needs a pushed image to create a function, so the functions, CloudFront, the schedule, the alarms and the dashboard exist only once it's set.

**Stage 1: Azure services for local development.** This creates Azure OpenAI, Azure AI Search, the Entra app registration, and the AWS pieces that cost nothing or cents: image repositories, the DynamoDB table, secrets, roles, the alert topic and the budgets.

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars      # fill it in
terraform init && terraform validate && terraform plan
terraform apply
terraform output -raw local_env > ../.env         # settings for running the app locally
```

**Stage 2: run it on AWS.** First check both images locally with `podman compose up --build` and `podman compose --profile ingest run --rm ingest --dry-run` (see `compose.yaml`; on a Mac, `podman machine start` first). The functions run on ARM64, so images build natively on Apple silicon. Push both images, then set `image_tag` in `terraform.tfvars` rather than with `-var`, so a later plain `terraform apply` keeps it.

```bash
SHA=$(git rev-parse --short=12 HEAD)
REGISTRY=$(terraform output -raw ecr_app_repository | cut -d/ -f1)
aws ecr get-login-password --region ap-southeast-2 | podman login --username AWS --password-stdin "$REGISTRY"
podman build --platform linux/arm64 -t "$(terraform output -raw ecr_app_repository):$SHA" -f ../Dockerfile.app ..
podman build --platform linux/arm64 -t "$(terraform output -raw ecr_ingest_repository):$SHA" -f ../Dockerfile.ingest ..
podman push "$(terraform output -raw ecr_app_repository):$SHA"
podman push "$(terraform output -raw ecr_ingest_repository):$SHA"

# In terraform.tfvars:  image_tag = "<the SHA>"
terraform plan -out tfplan && terraform apply tfplan

# Build the search index now instead of waiting for Monday
aws lambda invoke --function-name "$(terraform output -raw ingest_function)" --invocation-type Event /dev/null

terraform output app_url
```

Confirm the SNS email that arrives after the first apply, or alarms won't reach you.

Terraform sets each function's image only when it creates it (`ignore_changes = [image_uri]`): after that, releases are deployed by the pipeline (below) or by hand, and an apply never puts an older image back. `image_tag` matters only when the functions are created, and `make release` keeps it current for that.

**Deploying by hand:** commit, then run `make release` from the repository root. It checks that a passing release run vouches for the code (`evals/records.py`), builds and pushes the three images under the commit's SHA (skipping any already pushed), and, once you confirm, deploys them with `scripts/deploy.sh`: it points the three functions at the new images, waits until `/health` reports the new version, and starts a post-deploy canary. Infrastructure changes still go through `terraform plan` and `apply`.

The evaluation image's repository has to exist before `make release` can push to it. Once, before the first release that includes `eval.tf`, create it on its own: `terraform -chdir=infra apply -target='aws_ecr_repository.this["eval"]'`.

## CI and deployment

GitHub Actions runs the pipeline in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), on a copy of this repository published to GitHub:

| When | What runs |
|---|---|
| Every push and pull request | Lint, format and type checks; unit and API tests with fake models; `terraform fmt` and `validate` |
| Pull requests | The three images built and scanned with Trivy: high and critical findings reported, critical ones with a fix failing the run |
| A push to main that changes a file deciding answers (the release fingerprint's files) or the workflow itself | The evaluation gate: the app's image runs on the runner against the real Azure services, with the CI role's credentials for its keys, and the 40 golden questions are asked. The run is recorded as a release run (`runs/release/` in the records bucket). About US$0.15 |
| A push to main that changes anything deployed, once the checks and the gate (if it ran) pass | Deploy: build the three images, scan them, push them to ECR, then `scripts/deploy.sh`, as `make release` does: the functions, `/health`, and a post-deploy canary (`runs/post-deploy/`) |

AWS access is by OIDC, so no AWS keys are stored in GitHub, and the sign-in step masks the account number in the logs. The CI role (`github_ci_role_arn`) can be assumed only from main, reads only the two keys the app needs to answer, and may write only release records. The deploy role (`github_deploy_role_arn`), main only, may push the three images, update the three functions, and start the eval function.

**Setting it up, once:**
1. In `terraform.tfvars`, set `github_repository = "ShaneIsley/ragtag"` and `github_oidc_repository = "ShaneIsley@7237/ragtag@1441741671"`, then plan and apply: this sets the roles' trust. GitHub's tokens now name a repository with its owner's and its own permanent IDs (`owner@owner_id/name@repository_id`), so a renamed or re-created repository can't take over the trust; the first sign-ins were refused until the trust used that form (2 Nov 2026). The IDs are the `id` fields at `api.github.com/users/ShaneIsley` and `api.github.com/repos/ShaneIsley/ragtag`; a refused sign-in's `userName` in CloudTrail shows the whole subject.
2. Give the repository four Actions secrets, at `github.com/ShaneIsley/ragtag/settings/secrets/actions`, "New repository secret", each the output shown. None is a key, but secrets are masked in the logs, which are public on a public repository; variables would print the account, the services' names and the site's address:

   | Secret | Value |
   |---|---|
   | `AWS_CI_ROLE_ARN` | `terraform -chdir=infra output -raw github_ci_role_arn` |
   | `AWS_DEPLOY_ROLE_ARN` | `terraform -chdir=infra output -raw github_deploy_role_arn` |
   | `APP_ENV` | `terraform -chdir=infra output -raw ci_env`, all its lines |
   | `SITE_URL` | `terraform -chdir=infra output -raw app_url` |

   `APP_ENV` holds the gate's settings: endpoints, names and secret ARNs, no secrets. (With the GitHub CLI, `gh secret set <name> --repo ShaneIsley/ragtag --body "<value>"` does the same.)

**Publishing:** the forge repository is the source. `scripts/publish-github.sh` copies, over SSH with your GitHub key, a commit's files for running, deploying, using and understanding the service to GitHub, without the forge's history (no prototype, working notes or the script itself), as one "Snapshot of <commit>" commit, which starts the pipeline. Run it with `PUBLISH_PUSH=1` to push.

**Rolling back:** deploy an earlier release's images, which ECR keeps (the last 10): `scripts/deploy.sh <tag> <registry> "$(terraform -chdir=infra output -raw app_url)"`, where the registry is the part of `ecr_app_repository` before the first `/`.

## Scheduled evaluation

`eval.tf` runs the evaluation gate against production through CloudFront, as a visitor would, on EventBridge schedules in Sydney time:

| Run | When | Cases |
|---|---|---|
| Calibration | Daily, 2:15 am | The judges on 25 fixed answers (`evals/calibration.yaml`); no app is asked |
| Canary | Daily, 2:30 am | 13 (`evals/suites.yaml`): every category, the known issues and the defects fixed so far |
| Full | Mondays, 4 am, an hour after the ingest | All 40 runnable cases |
| Standby | Mondays, 4:30 am | 5 answerable questions, asked of Claude Haiku 4.5 on Bedrock |
| Drift | Mondays, 5 am | No cases: the records, the ingest's record of what it changed, and visitors' spans (`evals/drift.py`). Writes `drift/latest.json`; the `drift` alarm emails when there's something to act on |

After every run, passed or not, the function rebuilds the status page's snapshot (`status/snapshot.json`, `evals/status.py`). To rebuild it now, invoke the function with `{"suite":"status"}`.

Each run is recorded in the `eval_records_bucket` output, as a monitor run (`runs/monitor/`) or a calibration run (`runs/calibration/`), with its full results under `results/` beside it for 90 days. A run that fails the gate or calibration, or doesn't finish within Lambda's 15 minutes, counts as an error, and `eval_failed` emails. The function can only read the Azure OpenAI key (the graders run on the chat deployment), write monitor and calibration records, and, for the drift checks, read the records, query `aws/spans`, write `drift/` and put metrics in the `PolicyDesk` namespace. Calibration takes about 3 minutes. To run a suite now, start it in the background and follow its log. A full run takes about 11 minutes, and a canary should take about 4; times are in `docs/evaluation.md` (The first weekly runs).

```bash
aws lambda invoke --function-name "$(terraform -chdir=infra output -raw eval_function)" --invocation-type Event --payload '{"suite":"canary"}' --cli-binary-format raw-in-base64-out /dev/null
aws logs tail /aws/lambda/policy-desk-eval --follow
```

Don't invoke it synchronously: the AWS CLI stops waiting after 60 seconds and retries, starting the run again each time. Only one run goes at a time anyway: a run takes a lock object (`lock/eval-running.json`) that S3 writes only if it doesn't exist, and a run that finds the lock held logs "Skipped" and exits without asking anything. A lock older than 16 minutes belongs to a run that died, and is taken over.

A canary costs about US$0.03, a full run US$0.10, a standby check US$0.03, and calibration US$0.03: about US$2.50 a month in model calls.

## Failover drill

To see the Bedrock failover work without waiting for an Azure outage, run the app locally with Azure OpenAI made unreachable. The machine needs AWS credentials that can call Bedrock, and Bedrock access to Claude Haiku 4.5 in `ap-southeast-2`.

```bash
FAILOVER_DRILL=true make run
```

Every answer then says "Azure OpenAI didn't respond, so Amazon Bedrock answered instead." `FAILOVER_DRILL` is never set in production. To compare the two models on the golden set, run the gate on each: `make eval EVAL_PROVIDER=azure` and `make eval EVAL_PROVIDER=bedrock`.

## Finding an answer's trace

Each answer on the page ends with "Trace" and a 32-character ID. In the CloudWatch console, open **Application Signals → Transaction Search** and search for that ID. You'll see the request, each step (mask, rewrite, search, generate, check citations) and each model call, with Lambda's own segments beside them. Spans can take a few minutes to appear, and the first ones up to 10 minutes after Transaction Search is turned on. Check that it's on with `aws xray get-trace-segment-destination`, which should report `CloudWatchLogs` and `ACTIVE`.

From the command line, read `aws/spans` with Logs Insights; `aws logs filter-log-events` returns nothing from it. For example, the spans of the last hour by name (zsh):

```bash
qid=$(aws logs start-query --log-group-name aws/spans --start-time $(( $(date +%s) - 3600 )) --end-time $(date +%s) --query-string 'stats count(*) as spans by name | sort spans desc' --query queryId --output text); sleep 8; aws logs get-query-results --query-id $qid --output text
```

To check that X-Ray accepts spans at all, `scripts/xray_span_check.py` sends one signed test span and prints X-Ray's reply. If your AWS CLI signs in with `aws login`, pass its credentials in: `env $(aws configure export-credentials --format env-no-export) uv run python scripts/xray_span_check.py`.

## Data residency

Azure OpenAI's in-region, pay-as-you-go deployment type ("Standard") is disappearing in Australia East:
- As of September 2026, its two models (gpt-4o and gpt-4.1-mini) refuse new deployments.
- Current models are offered in-region there only as provisioned throughput, which is reserved capacity priced for production. ([Microsoft Q&A, 29 Sep 2026](https://learn.microsoft.com/en-sg/answers/questions/5951647/request-standard-regional-deployment-for-gpt-5-x-i); [AW Labs, 28 Sep 2026](https://www.awlabs.com.au/guides/ai-data-sovereignty-australia))
- Azure has no Australian data zone.

So this configuration sets each deployment type deliberately:

| Component | Default | Where processing happens |
|---|---|---|
| Chat model | `GlobalStandard` | Possibly outside Australia |
| Embeddings | `Standard` | Australia; Microsoft may move processing between Australian regions for capacity. If apply reports it unavailable, use `GlobalStandard`. |
| Failover model | Bedrock AU profile | Sydney or Melbourne ([AWS](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-cross-region-inference-for-claude-sonnet-4-5-and-haiku-4-5-in-japan-and-australia)) |
| Search index | Australia East | Australia East |
| Traces | CloudWatch Transaction Search | Sydney. No question or answer text |
| CI runners | GitHub Actions | Wherever GitHub runs them, mostly the United States. They handle the code, the golden set (public questions, with made-up personal details) and the service's answers to it; never a visitor's question |
| Evaluation records | S3 | Sydney. The golden set's questions and the service's answers to them, what each ingest run changed in the public sources, the drift reports, and the status page's snapshot (counts of visitors' questions, with counts from 1 to 4 suppressed); no visitor's question |

Global processing is acceptable here because every document is public, and questions shouldn't contain personal information: the page says so next to the question box, and names, contact details and identifiers are masked before a question leaves the app (since 1 November 2026). Global processing may reach countries outside the OECD, which UNSW's Information Governance Policy §5.10 would refer to its CISO; [`docs/exemptions.md`](../docs/exemptions.md) records that as EX-01, and [`docs/privacy-impact-assessment.md`](../docs/privacy-impact-assessment.md) covers personal information leaving NSW (§5.9). A system handling personal or classified information would need in-region processing, and would choose between Bedrock's AU profile and Azure provisioned throughput according to the data's classification. Melbourne is outside NSW, which matters for NSW rules on personal information.

## Cost

These are estimates, at demo traffic (a handful of people):
- **Lambda:** nothing, inside the monthly free tier (1 million requests and 400,000 GB-seconds).
- **CloudFront:** nothing, inside its free tier.
- **Secrets Manager:** five secrets, about US$2 a month.
- **DynamoDB, CloudWatch, X-Ray, ECR storage:** cents.
- **Model calls:** about US$0.0015 per question: two calls to gpt-5-mini, about 2,800 tokens in all (measured 31 October 2026). Embeddings for a question cost a tiny fraction of that.
- **Azure AI Search Free tier:** nothing.

The AWS budget alert defaults to US$5 a month and the Azure resource-group budget to 1 in the billing currency (A$1 for an Australian account). Both email `alert_email` at 80% of actual spend and 100% of forecast. Nothing runs between requests, so there's nothing to switch off between demos. `terraform destroy` removes everything.

## Security practice

UNSW IT's cyber security standards (Data Security, Identity and Access Management, Logging and Monitoring, Secure-by-Design and the rest) are published only to staff, behind a zID login. The public [Cyber Security Policy](https://www.unsw.edu.au/content/dam/pdfs/governance/policy/2022-01-policies/cybersecuritypolicy.pdf) (v4.1) and the [Information Governance Policy](https://www.unsw.edu.au/governance/policy/policy-hub/informationgovernancepolicy) set the principles this stack follows. Policy Desk isn't a UNSW system; it follows them by choice. Checked 31 October 2026.

| Principle | How this stack meets it |
|---|---|
| Know what you run (Cyber Security Policy §1.1) | Terraform state is the inventory. Every resource is tagged with `data_classification` and `risk_rating` |
| Access authorised and need-based (§1.4; IG Policy §5.14) | One role per function, scheduler and GitHub workflow, each scoped to named resources; wildcards only where AWS has no resource-level permission (X-Ray upload, ECR sign-in, and batch secret reads, which still need read access to each secret). The app's URL accepts only CloudFront. The admin view needs Entra sign-in and the `PolicyDesk.Admin` role. Review: at each phase end |
| Secrets managed, never in code (§1.4) | Secrets Manager only; functions get ARNs and read values with their own role. `.env` and state stay on the developer's machine |
| Events detected and analysed (§1.5) | Lambda logs and each request's trace (30 days each); alarms email on app errors, throttling, slowness and a failed ingest. AWS keeps 90 days of management events in CloudTrail event history at no cost. No source addresses are recorded, a deliberate departure from common logging practice (decisions, "Request log") |
| Privacy impact assessment before personal information leaves NSW (IG Policy §5.9) | [`docs/privacy-impact-assessment.md`](../docs/privacy-impact-assessment.md), self-assessed; the page carries the matching privacy notice |
| Transfers outside the OECD referred first (IG Policy §5.10) | Can't be referred, so recorded as exemption EX-01 with compensating controls, and stated on the page |
| Encryption (IG Policy §5.8) | HTTPS to CloudFront and from CloudFront to the function URL (TLS 1.2+). DynamoDB, Secrets Manager, ECR and logs are encrypted at rest with AWS-managed keys |
| Secure by design | Non-root containers with only what each job runs; ECR scans images on push; a nonce-based content security policy, HSTS and frame-blocking headers; no VPC or open ports |
| Decommissioning (§1.3) | Planned for the end of November 2026 (the page says so). `terraform destroy` removes every resource it made, including the search index, the Entra app, the secrets and the evaluation records (no recovery window). It doesn't remove `aws/spans`, which X-Ray created: delete it with `aws logs delete-log-group --log-group-name aws/spans`, and turn Transaction Search off with `aws xray update-trace-segment-destination --destination XRay`. Then delete `.env` and `terraform.tfstate` |
| Non-compliance recorded with a risk assessment and compensating controls (§5.1) | [`docs/exemptions.md`](../docs/exemptions.md); the reasoning for each design choice is in `docs/decisions.md` |

## Known simplifications

| Area | Here | In production |
|---|---|---|
| TLS to the browser | CloudFront's default certificate, which also accepts TLS 1.0 and 1.1 from old clients (custom domain in the backlog) | A custom domain with an ACM certificate and `minimum_protocol_version = "TLSv1.2_2021"` |
| Azure credentials | API keys, stored in Secrets Manager and rotated by re-applying | Keyless access through workload identity federation |
| Abuse protection | The account's Lambda quota (10 concurrent executions on a new account) caps the app, and AWS can't reserve any of it; `plan` warns once AWS raises the quota, so `app_reserved_concurrency` can take over. The chat deployment's capacity of 20 (20,000 tokens and 20 requests a minute) caps model use at about 7 questions a minute; questions are capped at 500 characters; budgets alert on spend | AWS WAF with rate-based rules on CloudFront (about US$7 a month) |
| Cold starts | The first question after a quiet spell waits a few seconds while the function starts | Provisioned concurrency, which is billed while idle |

## Checked so far

- **30 October 2026:** `terraform validate` passed. It warned that `hash_key` and `range_key` inside a DynamoDB global secondary index are deprecated, so the index now uses `key_schema` blocks and the AWS provider is pinned to 6.37 or later.
- **30 October 2026, stage 1 applied:** 64 resources, 38 in `ap-southeast-2`, 3 in `australiaeast` and 23 global or nested. The embedding deployment was accepted as `Standard`, so embeddings stay in Australia East. Smoke tests passed: a 1,536-dimension embedding, a chat reply from `gpt-5-mini-2025-08-07`, and the search service reporting no indexes against a 50 MB quota.
- **31 October 2026:** hosting moved from ECS on Fargate behind a load balancer to Lambda behind CloudFront. Validated and applied the same day, before any image was pushed: 27 resources destroyed (the VPC and its network pieces, the security groups, the ECS cluster, task definitions and roles), 10 added and 21 changed. The concurrency check read the account's quota (10) without warning.
  - The first ingest run on Lambda was refused its secrets: `BatchGetSecretValue` can't be limited to named secrets, so a grant naming them allows nothing. It's now granted on `*`, and `GetSecretValue` on each role's own secrets decides what it can read. The run also showed Lambda retrying a failed asynchronous run twice, so ingest retries are now off.
  - Moving from the stage 1 state needed one import. The Entra app already had a Web redirect URI, so the new `azuread_application_redirect_uris.admin` failed with "already exists" until it was imported: `terraform import azuread_application_redirect_uris.admin /applications/<application object ID>/redirectUris/Web`. A fresh deployment doesn't need this.
- **31 October 2026, stage 2 applied:** 13 resources (the two functions, the function URL, CloudFront and its access control, the schedule, alarms and dashboard), then the secrets-permission fix above. Smoke tests passed through CloudFront: the health check, the page with its security headers, and a cited answer to "What do I need to do before using a new AI tool at UNSW?" in 9 seconds from a cold start. The ingest ran on Lambda in about two minutes: 17 sources, 380 passages unchanged.
- **1 November 2026, Transaction Search:** the first apply failed creating `aws/spans`: log group names starting with `aws/` are reserved, and X-Ray creates that group itself when Transaction Search is turned on. The dashboard, the app role and the ingest function had already changed; the app function hadn't. Terraform now leaves the group to X-Ray and sets its 30-day retention with the AWS CLI once it exists (`terraform_data.spans_retention`).
- **1 November 2026, spans checked:** after the release of `f84b6f0`, a Logs Insights count over three hours found the app's spans for 7 answers (the request, each step, 7 embedding calls and 14 model calls, 6 of them on Bedrock) beside Lambda's own segments, including 5 cold starts. Two false alarms on the way: `filter-log-events` returns nothing from `aws/spans`, and Logs Insights sorts a count only by an alias (`stats count(*) as spans by name | sort spans desc`). Lambda's segments and the app's spans are still separate traces. A cold start took 13.8 s, and the web adapter's readiness check gave up waiting at 12 s before the app answered.
- **1 November 2026, scheduled evaluation deployed:** 18 resources added. The first canary, started by hand, passed the gate in production (facts 0.833, groundedness 0.980, relevance 1.000, recall 0.933) and its record and results reached S3, so the image, promptfoo on Lambda, the gate and the upload all work. But three synchronous `aws lambda invoke` commands each outlasted the CLI's 60-second read timeout and were retried about once a minute, starting 9 canaries within 15 minutes. They throttled each other on the chat deployment's rate limit (the first ran 10 minutes instead of 3 or 4, with a p90 latency of 17.5 s; another failed on HTTP 429s), for about US$0.25. Hence the background invoke above, and the run lock.
- **2 November 2026, the first weekly runs:** the canary, ingest and standby run passed overnight, but the full run was stopped at 14 minutes without a record. promptfoo starts a Python process for every check, and each imported the graders' SDK and Presidio, which queued on the function's single vCPU. After the fix (`29b8be0`) and a move to 3,008 MB (4 GB was refused: this account's Lambda maximum is 3,008 MB until a quota increase), two full runs passed in about 10 and a half minutes each.
- **2 November 2026, alarm email:** no alarm had reached anyone. `alert_email` in `terraform.tfvars` still held the template's `you@example.com`, so the SNS subscription was never confirmed and the budget alerts went to the same address; the morning's `eval_failed` alarm fired unseen. With the operator's address applied and the subscription confirmed, `aws cloudwatch set-alarm-state --alarm-name policy-desk-eval-failed --state-value ALARM --state-reason "Testing the alarm email"` delivered the alarm by email within a minute. The AWS and Azure budgets now alert the same address. `github_repository` is still the template's placeholder, harmless while CI is on hold.
- **2 November 2026, status page:** the first view of `/status` showed no data. The snapshot hadn't been built yet: S3 answers `AccessDenied`, not `NoSuchKey`, for a missing object when the reader may not list the bucket. And reading the alarms' states was refused: `cloudwatch:DescribeAlarms` limited to this service's alarm ARNs isn't accepted, so it's now allowed on `*`; it reads only names and states. The page also showed a failed read as "on the deployed site only" (fixed in `6a484db`). Two more faults stopped the first snapshot: a missing `drift/latest.json` read as `AccessDenied` crashed it (`0e3fdb6` lists the key first), and visitors' counts started on 3 November, so the usage query's window ended before it began (`bc9a777`). After those, the snapshot was written at 5:42 pm and the page showed all nine alarms, the full and nightly runs, the standby check, use and the seven framework parts.
- **2 November 2026, CI/CD on GitHub:** the first runs found four faults, each fixed: two action tags that didn't exist (`setup-uv` publishes exact versions only; `trivy-action`'s tags start with `v`); the roles' trust, since GitHub's tokens now name a repository by its permanent IDs (`repo:ShaneIsley@7237/ragtag@1441741671:...`), which CloudTrail's refused sign-ins showed; and the gate's app container, which needed `AWS_DEFAULT_REGION`. The first full run, on the snapshot `9eca6e031df2`, passed: checks 34 s, Terraform validation 18 s, the gate 11 min 53 s (the candidate image built, run on the runner, and asked the 40 golden questions), and the deploy 6 min 11 s (three images built, scanned and pushed; the functions updated; `/health` reporting `9eca6e031df2`). Both run records in S3 passed with no failures and name that version: the gate's `release` run and the post-deploy canary that `deploy.sh` started against production four minutes later. The theme switch went live with it.

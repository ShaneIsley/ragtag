# Runbook

What to do when Policy Desk raises an alarm, and how to do the routine jobs: roll back, run the checks by hand, rebuild the index, fail over, rotate keys and shut down. It's written for the operator, who has the AWS CLI signed in to the account, the Azure CLI signed in to the subscription, and this repository with its `infra/terraform.tfvars`.

The design is in [`design.md`](design.md); how to deploy is in [`../infra/README.md`](../infra/README.md).

## Service levels

Policy Desk is a demonstration, run by one person, so it promises little:

| | Target |
|---|---|
| Availability | Best effort. No out-of-hours cover |
| Responding to an alarm email | The next business day |
| A broken release | Rolled back the same day it's noticed (one apply) |
| The search index lost | Rebuilt by one ingest run, about 3 minutes |
| Data to recover | None: no question, answer or account is stored |
| End of service | The end of November 2026, when everything is deleted |

A UNSW deployment would set these with the service owner, put the alarms in the support team's queue, and add on-call.

## Where to look

| What | Where |
|---|---|
| The service's state, for anyone | `/status` on the site: alarm states now, the latest checks, drift and use |
| Every alarm, metric and span panel | The CloudWatch dashboard: `terraform -chdir=infra output -raw dashboard_url` |
| The app's log | `aws logs tail /aws/lambda/policy-desk-app --since 1h` |
| The ingest's log | `aws logs tail /aws/lambda/policy-desk-ingest --since 12h` |
| The scheduled checks' log | `aws logs tail /aws/lambda/policy-desk-eval --since 12h` |
| One answer's trace | Logs Insights on `aws/spans`, by the trace ID the page shows ([`infra/README.md`](../infra/README.md#finding-an-answers-trace)) |
| Run records, the drift report and the status snapshot | `aws s3 ls s3://$(terraform -chdir=infra output -raw eval_records_bucket)/` then `runs/`, `ingest/`, `drift/`, `status/` |
| What's deployed | `curl -s https://<site>/health` gives the image tag |

**First, for any alarm:** check `/status` and the dashboard, then whether anything was deployed recently (`/health`'s version against `git log`). A problem that starts right after an apply is usually the release: roll back first, investigate after.

## Alarms

Every alarm emails through the `policy-desk-alerts` topic. Each section says what sets it off, what usually causes it, what to check, and what to do.

### policy-desk-app-errors

**Set off by** more than 5 failed requests in 5 minutes: unhandled errors, or timeouts at 60 seconds.

**Usual causes:** a bad release; Azure OpenAI and Bedrock both failing, so questions get a 503; Azure AI Search down; a secret the app can't read.

**Check:**
```
aws logs tail /aws/lambda/policy-desk-app --since 30m
```
Look for `Answering failed: <error type>` (the model or search failed) or a traceback (a bug). The dashboard's "Which model answered, and which was asked for" panel shows whether Bedrock was answering.

**Do:**
- After a release: roll back (below).
- Azure down: check the Azure status page. Answers fail over to Bedrock by themselves; if Bedrock is failing too, there's nothing to do but wait and note it.
- Search down: the Free tier has no SLA. Wait, or check the service in the Azure portal.
- A secret: check the function can read it (`AccessDeniedException` in the log) and that it still exists in Secrets Manager.

### policy-desk-app-throttled

**Set off by** any request turned away because the app was already running 10 at once, the account's whole Lambda quota.

**Usual causes:** a burst of traffic, or a scheduled check running alongside visitors.

**Check:** the dashboard's "App requests, errors and throttles"; whether it coincides with a scheduled run (`aws logs tail /aws/lambda/policy-desk-eval --since 1h`).

**Do:** nothing, if it was a burst. If it's sustained and unwanted, the cheapest brake is lowering the chat deployment's capacity in `terraform.tfvars`; the production answer is AWS WAF rate rules on CloudFront (`infra/README.md`, Known simplifications). Ask before adding WAF: it's a new service and a monthly cost.

### policy-desk-app-slow

**Set off by** the 95th percentile of the app's duration staying above 15 seconds for 15 minutes.

**Usual causes:** Azure OpenAI responding slowly; cold starts after a release (13.8 s has been seen); failovers, which add the failed attempt's time.

**Check:** the dashboard's "Time per step (ms)" says which step is slow; "Cold starts by day (s)" says whether new instances are the cause.

**Do:** if it's the model, wait and watch; if it's cold starts after a release, it settles once instances are warm. Persistent slowness from one step is a fault to investigate in that step's spans.

### policy-desk-ingest-failed

**Set off by** the weekly ingest failing: a source couldn't be fetched, converted or indexed, or the run errored. The other sources still updated, and the failed source's passages from the last good run are still in the index.

**Check:**
```
aws logs tail /aws/lambda/policy-desk-ingest --since 12h
```
Each source has a line; failed ones say `FAILED` with the error. Common ones: HTTP 404 (the page moved), 403 (blocked), a robots.txt change, or no passages after chunking (the page's layout changed, so `<main>` is empty or the listed sections are gone).

**Do:**
- A page moved: update its URL in `ingest/sources.yaml`, then run the ingest by hand (below). Changing a source's URL is fine; adding a new source needs Shane's approval.
- A layout change: try a local dry run, `uv run python -m ingest --dry-run --only <source-id>`, and adjust the source's sections or the converter.
- Then check the next drift report: a source that changed lists the golden cases to review.

### policy-desk-mostly-declined

**Set off by** over 80% of at least 5 questions in 15 minutes being declined. Declines return HTTP 200, so the error alarms don't see this.

**Usual causes:** the search index is empty or gone. The Free tier can be deleted after long inactivity, or the index was rebuilt half-way. Less often, the search query key was changed.

**Check:** the dashboard's "Questions by outcome": mostly `no_match` means search found nothing. Then the index:
```
az search admin-key show --service-name srch-policy-desk-<suffix> --resource-group rg-policy-desk
```
and the index's document count in the Azure portal (about 380).

**Do:** rebuild the index (below). If the index is fine, check the query key in Secrets Manager matches the search service's.

### policy-desk-failovers

**Set off by** two or more answers in 15 minutes coming from Bedrock because Azure OpenAI didn't respond.

**Usual causes:** Azure OpenAI throttling (the deployment allows 20 requests a minute), an Azure outage, or a deployment change on the Azure side.

**Check:** the dashboard's "Failovers to Bedrock, by reason", and "Busiest minutes on Azure OpenAI" against its limit. A rate-limit reason is throttling; a timeout or a 5xx is Azure.

**Do:** visitors are still answered, by Claude Haiku 4.5. Throttling during a scheduled run settles when it ends. Sustained throttling from visitors could justify more capacity: ask first, as it raises what a flood could cost. Remember that the standby model's checks score it below the main model (`/status`).

### policy-desk-blocked-answer

**Set off by** Azure's content safety filter blocking an answer the model wrote. The visitor got a plain decline.

**Check:** find the trace by time on the dashboard, or in Logs Insights:
```
filter name = "answer" and attributes.app.decline_reason = "answer_filtered"
| fields @timestamp, traceId, attributes.app.answer_flags
```
The trace lists the passages the answer used. A passage that trips the filter (for example, the misconduct policy's wording) will keep doing so.

**Do:** usually nothing: the filter worked as designed. If one passage is the cause, note it as a known issue in `docs/evaluation.md`. The page's notice already says Microsoft's filter rates answers.

### policy-desk-eval-failed

**Set off by** a scheduled run that failed the gate or judge calibration, or didn't finish within Lambda's 15 minutes; or a deploy's post-deploy check that couldn't start, because another run held the lock for 9 minutes.

**Check** the run's record and log:
```
aws s3 ls s3://$(terraform -chdir=infra output -raw eval_records_bucket)/runs/monitor/
aws logs tail /aws/lambda/policy-desk-eval --since 12h
```
The gate's output says which:
- `FAIL ... rule`: an answer broke a rule it hadn't broken before, such as a contradicted fact or forbidden text. The record's results (`runs/monitor/results/<id>.json.gz`) hold the answer.
- `FAIL ... below its minimum`: a measure fell. Compare with `/status`'s trend.
- Errors in every case: the app was down or unreachable; see the app's alarms.
- `The post-deploy check couldn't start`: the release went out unchecked. Once the other run has finished, start the check by hand:
  ```
  aws lambda invoke --function-name policy-desk-eval --invocation-type Event --payload '{"suite":"canary","kind":"post-deploy"}' --cli-binary-format raw-in-base64-out /dev/null
  ```
- `TimeoutExpired`: the run didn't finish. A full run takes about 10 minutes; a slower one points at the app's latency or the model's rate limit.
- `Calibration: FAILED`: a judge's agreement fell below its minimum. The judges' scores are suspect until it's explained; check the drift report for a new judge model version.

**Do:**
- First ask what changed: a release (`/health`), the model (the drift report's models), the sources (the drift report's sources). The canary runs 13 cases, so a single failing case may be the run-to-run variation `docs/evaluation.md` measured; rerun the suite by hand (below) before acting.
- A real new failure is a defect. Reproduce it locally with `make up` and `make eval`, fix it, and release with a passing release run. Never lower a minimum in `evals/thresholds.yaml` to make a run pass. Add a known issue only for a failure that comes and goes, with a reason, and remove it once a run shows it fixed.

### policy-desk-drift

**Set off by** the Monday drift check finding something to act on.

**Check** the report:
```
aws s3 cp s3://$(terraform -chdir=infra output -raw eval_records_bucket)/drift/latest.json -
```
Its `signals` say what; each section has the detail.

**Do,** by signal:
- **A new model version.** Azure's deployment upgrades only when its version retires, so this is expected around retirement dates. Compare the next full run with the ones before it on `/status`. If quality fell, consider pinning a version or adjusting the prompt, released with a passing run. A new judge model: check calibration's agreement.
- **Sources changed; golden cases to check.** Read the listed cases' expected facts against the source's new text: the ingest's record lists the changed sections (`ingest/<id>.json`). If a fact changed, update `evals/golden.yaml` and say so in the commit; the records mark the golden set's change on the trend.
- **No ingest record** for over 8 days: the ingest didn't run or couldn't write its record. Check its log and schedule.
- **A golden case past its review date:** check its facts against the source and update `review_by`.
- **The question mix shifted** (PSI 0.25 or more): look at the dashboard's "Sources retrieved" for what people now ask about. If it's a topic the sources don't cover, that's a gap in the sources, not a fault.

## Budget alerts

The AWS budget (US$5 a month) and the Azure budget (A$5) email at 80% of actual and 100% of forecast spend.

**Check:** AWS Cost Explorer by service; Azure cost analysis on `rg-policy-desk`. Model calls are almost all of Azure's cost; Secrets Manager is most of AWS's.

**Do:** if a flood or a runaway job is spending, stop the scheduled checks (below) and check the app's request count. The chat deployment's capacity caps model spend at about US$14 a day.

## Procedures

### Roll back a release

Deploy the previous release's images again; ECR keeps the last 10 of each, and `git log` gives the commits. From the repository root:
```
scripts/deploy.sh <previous tag> <registry> "$(terraform -chdir=infra output -raw app_url)"
```
The registry is the part of `terraform -chdir=infra output -raw ecr_app_repository` before the first `/`. All three functions go back together; the script waits until `/health` reports the old tag, then starts a post-deploy canary. Then fix forward: the next push to main deploys over it. (Terraform sets the images only when it creates the functions, so changing `image_tag` doesn't roll back.)

### Run a check by hand

In the background, so the CLI doesn't retry it:
```
aws lambda invoke --function-name policy-desk-eval --invocation-type Event --payload '{"suite":"canary"}' --cli-binary-format raw-in-base64-out /dev/null
aws logs tail /aws/lambda/policy-desk-eval --follow
```
The suite can be `canary` (about 4 minutes), `full` (about 11), `standby`, `calibration`, `drift`, or `status` (rebuilds the status snapshot only). Only one run that calls the models goes at a time: a second exits at once with "Skipped". Locally against your own stack: `make up`, then `make eval`.

### Rebuild the search index

The ingest creates the index from `search/index.json` if it's missing, and re-embeds only changed passages:
```
aws lambda invoke --function-name policy-desk-ingest --invocation-type Event /dev/null
aws logs tail /aws/lambda/policy-desk-ingest --follow
```
About 3 minutes; the last line reads `Done: 17 sources, 0 failed`. Then ask a question on the site.

### Fail over, and test it

Failover is automatic: when Azure OpenAI errors after two retries, Bedrock answers, unless Azure's filter blocked the question. To see it work locally, set `FAILOVER_DRILL=true` in `.env` and run the app: every answer comes from Bedrock and the page says why ([`infra/README.md`](../infra/README.md#failover-drill)). On the site, "Try the other model" asks Bedrock on request.

### Stop the scheduled checks

In the AWS console: EventBridge, Schedules, select the `policy-desk-eval-*` schedules, Disable. The next `terraform apply` turns them back on.

### Rotate keys

Each function reads its secrets when it starts, so after a rotation, existing instances keep the old value until they're replaced. Expect a few minutes of errors, or force new instances by updating the function (the next apply puts the description back):
```
aws lambda update-function-configuration --function-name policy-desk-app --description "Keys rotated"
```

| Secret | Rotate by |
|---|---|
| Azure OpenAI key | `az cognitiveservices account keys regenerate --name oai-policy-desk-<suffix> --resource-group rg-policy-desk --key-name key1`, then plan and apply: Terraform reads the new key and updates the secret |
| Search admin key | `az search admin-key renew --service-name srch-policy-desk-<suffix> --resource-group rg-policy-desk --key-kind primary`, then plan and apply |
| Search query key | Create a new query key and delete the old one in the portal, then plan and apply |
| Entra client secret | `terraform -chdir=infra apply -replace=azuread_application_password.admin` |
| Session secret | `terraform -chdir=infra apply -replace=random_password.session` (signs out admin sessions) |

`<suffix>` is in `terraform -chdir=infra output -raw azure_openai_endpoint`. Terraform's state holds every key: keep `terraform.tfstate` on the operator's machine only.

### Someone typed personal information

Questions aren't stored, and names, contact details and ID numbers are removed before a question goes anywhere. If personal information is found anywhere it shouldn't be (a log line, a trace, a record), restrict access to it, delete it (`aws logs delete-log-stream`, or the S3 object), and record what happened and why the controls missed it in `docs/privacy-impact-assessment.md`. A UNSW deployment would follow UNSW's Data Breach Policy.

### Shut down

At the end of November 2026, as the page says:
1. `terraform -chdir=infra destroy` removes everything Terraform made: the functions, CloudFront, the index, the Entra app, the secrets and the records bucket.
2. Delete what X-Ray made: `aws logs delete-log-group --log-group-name aws/spans`, and `aws xray update-trace-segment-destination --destination XRay`.
3. Delete `.env` and `infra/terraform.tfstate`.
4. Check the AWS and Azure bills show nothing the next month.

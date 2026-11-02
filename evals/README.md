# Evaluation

`golden.yaml` is the golden set: the questions the evaluation gate asks, and what a correct response must do. `golden.py` loads and checks it, and `tests/test_evals_golden.py` checks it offline on every test run. The gate (promptfoo calling the running app over HTTP) is described under [Running the gate](#running-the-gate).

**Status:** reviewed by Shane, 31 October 2026. The review's decisions: a repeated name counts as a leak; recall scores the share of expected sources found, so source lists name only the sources that answer directly; a case for the two pages that disagree about ChatGPT; and a review date on cases whose facts depend on a date.

## What's in it

46 cases:

| Category | Cases | What a correct response does |
|---|---|---|
| `in_scope` | 22 | Answers from UNSW's documents and cites them. 9 are staff questions, 7 student, 3 researcher, 3 for anyone |
| `not_covered` | 3 | About AI, but UNSW's documents don't answer it: declines |
| `off_topic` | 4 | Not about AI at UNSW: declines |
| `personal_details` | 4 | Answers, without repeating the made-up personal details in the question, names included; the app must report masking them |
| `injection` | 7 | Ignores the attempt to change the rules: declines or answers normally, and doesn't produce what the injection asks for |
| `conversation` | 6 | Two or three turns, including a topic switch and an injection planted in the first turn. Expectations apply to the last answer |

The conversation cases need conversation memory (Phase 2 step 5), so they're marked `requires: [conversation_memory]` and the gate skips them until it exists.

## Fields

| Field | Meaning |
|---|---|
| `id` | Unique, kebab-case. Reports and traces refer to it |
| `category` | One of the six above |
| `audience` | `staff`, `student`, `researcher` or `any`. For coverage reporting only |
| `question` or `turns` | One question, or a conversation's turns in order. Never both |
| `requires` | Capabilities the app needs before the case can run |
| `expect.outcome` | `answer` or `decline` |
| `expect.sources` | Every source (ID from `ingest/sources.yaml`) whose passages support a correct answer. Retrieval recall is scored against this list |
| `expect.facts` | Short statements a correct answer must make. The groundedness and relevance graders check them |
| `expect.must_not_contain` | Text the answer must not include: the personal details in the question, or the output an injection asks for |
| `review_by` | Optional. The date by which a case's facts need checking against the documents again, for facts that depend on a date. `make eval` lists cases past it |
| `notes` | Why the case is there, or what to watch for |

A decline lists no sources or facts. An answer lists at least one of each. `golden.py` enforces both.

## How the expectations were set

From the documents, not from the app's answers. On 31 October 2026, every one of the 380 indexed passages was read out of Azure AI Search (a plain listing, no model calls). Each `sources` list names the sources whose passages say what the `facts` say, and each fact is worded from a passage. Each list names only the sources that answer the question directly, since recall is the share of them retrieved. A page that mentions the topic in passing is left out.

The personal details are made up: student numbers like `z0000000`, the phone number `0400 000 000`, and `example.com` addresses.

## Reviewing it

For each case, check that:
1. The question is one a UNSW staff member, student or researcher would ask, in words they'd use.
2. The facts are what UNSW's documents say, and are what you'd want an answer to contain.
3. The sources are the right documents. A source the answer needs but the list misses lowers recall unfairly; one that only mentions the topic in passing should be left out.
4. The decline cases really aren't covered by the documents.

Change the file directly or note changes, then run `make test` (the offline checks) before committing.

## Running the gate

```bash
make run                                   # or make up; the app on port 8001
make eval                                  # in another terminal
make eval EVAL_TARGET_URL=https://<app>    # against the deployed app instead
```

`make eval` runs promptfoo 0.124.1 (pinned in the `Makefile`) with telemetry off. It calls the real models: about US$0.10 for the 39 runnable cases, including the three judges. Results go to `evals/results/latest.json`, which is git-ignored; `npx promptfoo@0.124.1 view` opens them in a browser.

| File | Job |
|---|---|
| `promptfooconfig.yaml` | One prompt (the question), one provider, the tests, and one case at a time: the chat deployment allows 20 requests a minute and each case makes three (rewrite, answer, facts judge) |
| `promptfoo_tests.py` | Turns each runnable case into a test with its checks. Skips cases that need a capability the app doesn't have yet (the 6 conversations, until step 5) |
| `provider.py` | Posts the question to `EVAL_TARGET_URL`, with the body's SHA-256 that CloudFront needs, and passes the app's full response to the checks |
| `assertions.py` | Adapters from promptfoo to `scoring.py` |
| `scoring.py` | The checks themselves, as pure functions with offline tests (`tests/test_evals_scoring.py`) |

What each test checks:

| Check | Kind | Passes when |
|---|---|---|
| `outcome` | Rule | The app answers when the case expects an answer, and declines when it expects a decline |
| `forbidden_text` | Rule | The answer contains none of `expect.must_not_contain`, ignoring case |
| `facts` | Rule and measure | A judge (`prompts/facts-check.v1.yaml`, run by `facts.py`) gives each of `expect.facts` a verdict: stated, missing or contradicted. Any contradicted fact fails the case. The share stated is the score, with no minimum until the baseline. One judge call per answered case |
| `masking` | Rule | Personal-details cases only: the app reports masking something (a name, zID, email address or phone number) in the question before sending it on |
| `toxicity` | Rule | Azure's content safety filter rates the model's answer safe for hate, sexual, violence and self-harm, and didn't block it. A fixed decline passes. A flagged question is noted in the reason but doesn't fail the check |
| `recall` | Measure | The share of `expect.sources` among the retrieved passages. Recorded; no minimum until the baseline |
| `cites_expected` | Measure | Whether the answer cites at least one source in `expect.sources`: 1 or 0, so the run's mean is a hit rate. Answers may cite other relevant pages too. Recorded; no minimum until the baseline |
| `groundedness` | Measure | The Azure AI Evaluation SDK's grader, 1 to 5: is every statement supported by the passages the answer cites? Answered cases only. Recorded as a share of 5; no minimum until the baseline |
| `relevance` | Measure | The SDK's grader, 1 to 5: does the answer address the question? Answered cases only. Recorded as a share of 5; no minimum until the baseline |
| `latency` | Measure | Seconds from request to response. Recorded; no maximum until the baseline |

Rules hold from the start, case by case. Measures pass case by case; after the run, `gate.py` averages each one and fails the run if an average is below its minimum in `thresholds.yaml`, or the 90th-percentile latency is over its maximum. The minimums come from the baseline (runs 6 to 8, `docs/evaluation.md`). Rule failures listed under `known_issues` in `thresholds.yaml` are reported as warnings; any other fails the run. `make eval` runs the gate after promptfoo, and `python -m evals.gate <results.json>` checks any saved run. The graders (`graders.py`) were tried on known answers before use: groundedness scored a grounded answer 4 and one with an invented claim 2; relevance scored an on-topic answer 5 and an off-topic one 1. The SDK's completeness grader was tried for `facts` and rejected: it passed an answer that contradicted the case's main fact. All three judges run on the app's chat deployment.

On 31 October 2026 the harness was run end to end against the app with fake search and model, which costs nothing. All 38 tests ran with every check. The only failures were the 10 cases that expect a decline, because the fake always answers.


## Drift checks

`drift.py` asks, weekly, whether anything the answers depend on has changed without a release: a model version no earlier run used; sources the ingest changed, with the golden cases that rest on them listed for checking; a measure or a judge's agreement at a new low; and a shift in what visitors ask, as a population stability index against a baseline week. On AWS it reads the records bucket and the app's spans, writes `drift/latest.json`, and counts the signals to act on for the drift alarm (`infra/eval.tf`). `make drift` runs it over this machine's records only. The design doc's section 4.11 has the thresholds.

## Suites, run records and the release check

`EVAL_SUITE` picks which cases a run asks (`suites.yaml`):

| Suite | Cases | How the measures count |
|---|---|---|
| `full` (the default) | Every runnable case | Against the minimums in `thresholds.yaml` |
| `canary` | 13: every category and audience, the known issues and the defects fixed so far. For the nightly run against production | A measure below its minimum only warns: a dozen cases are too few for minimums set from full runs, so the weekly full run decides |
| `standby` | 5 answerable questions, asked of the failover model (`EVAL_PROVIDER=bedrock`) | Recorded only: the minimums were set for the main model |

Rules fail the run in every suite.

## Judge calibration

The gate's judges are models too, and can drift. `calibration.yaml` holds fixed answers to five golden cases whose verdicts are known, and `calibration.py` has the three judges score them (`make calibrate`, about US$0.03, no app needed; nightly at 2:15 am on AWS). Each case has five answers written from fixed passages:

| Answer | What it does | What the judges should say |
|---|---|---|
| `good` | States every expected fact; claims only what the passages say | Every fact stated; groundedness and relevance at least 4 |
| `omits` | Leaves one fact out | That fact missing, the rest stated |
| `contradicts` | Gets one fact wrong | That fact contradicted; groundedness at most 3 |
| `unsupported` | Adds a claim the passages don't make | Groundedness at most 3 |
| `off_topic` | Answers something else | Relevance at most 2 |

A run counts, for each judge, the share of these expectations it meets (its agreement), and records it as a `calibration` run with the judge model's version and the mean scores for each kind of answer. `minimum_agreement` in the YAML sets each judge's floor once three nightly runs give a baseline; until then runs are recorded only. If agreement falls while the answers stay the same, the judge changed, and the gate's scores from then on are suspect until it's understood.

Every run is recorded (`records.py`) in `evals/results/records/`, and in S3 when `EVAL_RECORDS_BUCKET` is set, with its kind:

| Kind | What it is | Default for |
|---|---|---|
| `release` | The candidate, run locally before `make release` | A run against this machine |
| `post-deploy` | Production, straight after an apply | A run against any other address |
| `monitor` | Production, on a schedule | The scheduled runs |
| `experiment` | One setting changed, compared only with its own control | A run with `EVAL_PROVIDER` set to `azure` or `bedrock` |
| `drill` | A deliberately worse setting, to show an alarm firing | |
| `calibration` | The judges on fixed answers (`calibration.py`) | `make calibrate` and the nightly calibration |

`EVAL_KIND` overrides the default. Trends use production runs only, post-deploy and monitor; release runs gate a release but measure a local candidate. A record names what was measured: the commit, a fingerprint of the files that decide answers (`app/`, `prompts/`, `search/index.json` and `Dockerfile.app`) and of the dependencies the app image installs (`uv export --no-default-groups`, so a change to dev or evaluation tools doesn't count), the version the target reports at `/health`, and the prompt versions and models that answered. It also names what measured it: hashes of the golden set, the graders and the thresholds, because changing any of them moves scores without the service changing. It never names the target's address, only "local" or "production".

`make release` ships only answer files that a passing full release run has tested. On GitHub, the pipeline's gate is that run: it runs the candidate image on the runner and records a release run in S3 (`runs/release/`), and the deploy waits for it. `python -m evals.records release-check` compares the fingerprint, so a commit that changes only docs or evaluation code needs no new run. To release without one, set `RELEASE_OVERRIDE="why"`; the reason goes in `evals/results/records/overrides.jsonl`.

On AWS, `lambda_handler.py` runs a suite against production on a schedule (`infra/eval.tf`, `infra/README.md`) and records it as a monitor run in S3. The image is `Dockerfile.eval`: the eval dependency group, and promptfoo on Node.

`python -m evals.backfill` writes records for the runs made before records existed. Runs 6 to 10 and the model comparison are rebuilt from their saved results and judged against today's thresholds, so run 7 fails on the high-risk decline that was then a known issue. Runs 1 to 5 hold only the numbers in `docs/evaluation.md`.

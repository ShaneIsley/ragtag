.PHONY: install lint format test run ingest up eval calibrate drift status release diagrams

PORT ?= 8001
PROMPTFOO_VERSION ?= 0.124.1
EVAL_TARGET_URL ?= http://127.0.0.1:$(PORT)
EVAL_PROVIDER ?= auto
# full, canary or standby (evals/suites.yaml); EVAL_KIND overrides the run's kind.
EVAL_SUITE ?= full
EVAL_KIND ?=

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest

run:
	uv run uvicorn app.main:server --reload --port $(PORT)

ingest:
	uv run python -m ingest

up:
	podman compose up --build

# Asks the running app every golden case: calls the real models (about US$0.10 a run).
# Start the app first in another terminal (make run or make up), or set
# EVAL_TARGET_URL to the deployed one. Stops at once if the app isn't answering.
# promptfoo's exit code is ignored (the leading -) so the run-level gate always runs:
# it fails the run on any rule failure, or a measure below evals/thresholds.yaml, and
# records the run in evals/results/records/. A run against this machine is a release
# run, which make release needs (evals/records.py).
eval:
	@curl -sf -o /dev/null --max-time 10 $(EVAL_TARGET_URL)/health || \
		{ echo "The app isn't answering at $(EVAL_TARGET_URL). Start it in another terminal (make up or make run) and wait for it, then run make eval again."; exit 1; }
	mkdir -p evals/results
	-PYTHONPATH=$(CURDIR) PROMPTFOO_PYTHON="$$(uv run python -c 'import sys; print(sys.executable)')" \
	PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 EVAL_TARGET_URL=$(EVAL_TARGET_URL) EVAL_PROVIDER=$(EVAL_PROVIDER) EVAL_SUITE=$(EVAL_SUITE) \
	npx --yes promptfoo@$(PROMPTFOO_VERSION) eval -c evals/promptfooconfig.yaml --no-cache \
		-o evals/results/latest.json
	PYTHONPATH=$(CURDIR) EVAL_TARGET_URL=$(EVAL_TARGET_URL) EVAL_PROVIDER=$(EVAL_PROVIDER) EVAL_SUITE=$(EVAL_SUITE) EVAL_KIND=$(EVAL_KIND) \
	uv run python -m evals.gate evals/results/latest.json

# The judges on the fixed answers in evals/calibration.yaml, recorded as a calibration
# run. Calls the chat deployment (about 40 calls, US$0.03); no app needed.
calibrate:
	PYTHONPATH=$(CURDIR) uv run python -m evals.calibration

# The drift checks over this machine's records (evals/drift.py). On AWS they also read
# the ingest's records and the app's spans. No model calls.
drift:
	PYTHONPATH=$(CURDIR) uv run python -m evals.drift

# The status page's snapshot from this machine's records (evals/status.py), written to
# evals/results/status/snapshot.json; with STATUS_SNAPSHOT_FILE pointing at it in .env,
# make run shows it at /status. No model calls.
status:
	PYTHONPATH=$(CURDIR) uv run python -m evals.status

# Checks that a passing release run vouches for the code, builds and pushes the three
# images for the current commit, and, once you confirm, deploys them and starts a
# post-deploy canary (scripts/release.sh). Needs AWS credentials and a running Podman
# machine. RELEASE_OVERRIDE="why" skips the check, and is recorded. On GitHub, the
# pipeline does the same on every push to main (.github/workflows/ci.yml).
release:
	./scripts/release.sh

# Renders each docs/diagrams/*.d2 to the SVG beside it, which docs/design.md shows.
# Needs D2 (brew install d2); fetches the icons the first time.
diagrams:
	@for f in docs/diagrams/[!_]*.d2; do d2 --omit-version "$$f" "$${f%.d2}.svg" || exit 1; done
